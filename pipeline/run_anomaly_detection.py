"""Anomaly detection runner for KrishiPulse.

Executes rolling baseline computation, city->district spatial join,
retail data quality guard (modeled retail never triggers anomalies),
and configurable scoring methods (Z-score and robust Median/MAD).

Usage:
    python pipeline/run_anomaly_detection.py
    python pipeline/run_anomaly_detection.py --method mad
    python pipeline/run_anomaly_detection.py --min-history 3
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()
log = logging.getLogger("run_anomaly_detection")

SQL_PATH = ROOT / "pipeline" / "anomaly_detection.sql"


def run_sql_anomaly_detection(db_url: str, connect=None):
    """Executes the standard SQL-based anomaly detection engine."""
    with open(SQL_PATH, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = (connect or psycopg2.connect)(db_url)
    cur = conn.cursor()

    cur.execute(sql)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM price_anomaly")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM price_anomaly WHERE anomaly_flag = true")
    flagged = cur.fetchone()[0]

    cur.execute("""
        SELECT confidence_level, COUNT(*) 
        FROM price_anomaly 
        GROUP BY confidence_level
    """)
    conf_stats = dict(cur.fetchall())

    cur.execute("""
        SELECT retail_data_type, COUNT(*), SUM(CASE WHEN anomaly_flag THEN 1 ELSE 0 END)
        FROM price_anomaly
        GROUP BY retail_data_type
    """)
    type_stats = cur.fetchall()

    cur.close()
    conn.close()

    log.info("Anomaly detection (SQL z-score) complete. Total rows: %d, Flagged anomalies: %d", total, flagged)
    log.info("Confidence breakdown: %s", conf_stats)
    for dtype, count, anom_cnt in type_stats:
        log.info("  %s retail: %d pairs, %d flagged", dtype, count, anom_cnt or 0)

    return total, flagged


def run_robust_mad_anomaly_detection(db_url: str, min_history: int = 5, connect=None):
    """Computes robust anomaly detection using rolling Median and MAD (Median Absolute Deviation)."""
    conn = (connect or psycopg2.connect)(db_url)
    
    # 1. First run the base SQL engine to generate geographic pairings and daily records
    with open(SQL_PATH, "r", encoding="utf-8") as f:
        sql = f.read()
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()

    # 2. Fetch the anomaly records for in-memory robust MAD scoring
    query = """
        SELECT anomaly_id, crop_id, district, city, price_date,
               wholesale_price, retail_price, retail_data_type
        FROM price_anomaly
        ORDER BY crop_id, district, price_date
    """
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        cur.close()
        conn.close()
        return 0, 0

    df["price_date"] = pd.to_datetime(df["price_date"])
    df["wholesale_price"] = df["wholesale_price"].astype(float)
    df["retail_price"] = df["retail_price"].astype(float)
    
    # Function to calculate rolling MAD
    def calc_mad_zscore(group, price_col):
        records = []
        for i, row in group.iterrows():
            current_date = row["price_date"]
            start_date = current_date - pd.Timedelta(days=30)
            end_date = current_date - pd.Timedelta(days=1)
            
            history = group[(group["price_date"] >= start_date) & (group["price_date"] <= end_date)][price_col]
            n = len(history)
            
            if n < min_history or history.empty:
                med = float(row[price_col])
                mod_z = 0.0
                conf = "insufficient_history" if n == 0 else "low"
            else:
                med = float(history.median())
                abs_dev = (history - med).abs()
                mad = float(abs_dev.median())
                if mad == 0:
                    mean_ad = float(abs_dev.mean())
                    if mean_ad > 0:
                        mad = 1.2533 * mean_ad
                    elif med > 0:
                        mad = 0.01 * med  # 1% scale floor to prevent division by zero
                    else:
                        mad = 0.01

                mod_z = round(0.6745 * (float(row[price_col]) - med) / mad, 2)
                conf = "high" if n >= 5 else "low"
                    
            records.append((row["anomaly_id"], med, mod_z, conf))
        return records

    wholesale_mad = []
    retail_mad = []
    for _, grp in df.groupby(["crop_id", "district"]):
        wholesale_mad.extend(calc_mad_zscore(grp, "wholesale_price"))
    for _, grp in df.groupby(["crop_id", "city"]):
        retail_mad.extend(calc_mad_zscore(grp, "retail_price"))

    ws_dict = {r[0]: (r[1], r[2], r[3]) for r in wholesale_mad}
    rs_dict = {r[0]: (r[1], r[2], r[3]) for r in retail_mad}

    # Update price_anomaly with robust scores
    update_sql = """
        UPDATE price_anomaly SET
            wholesale_baseline = %s,
            wholesale_zscore = %s,
            retail_baseline = %s,
            retail_zscore = %s,
            confidence_level = %s,
            anomaly_flag = %s,
            anomaly_reason = %s
        WHERE anomaly_id = %s
    """
    
    updates = []
    flagged = 0
    for _, row in df.iterrows():
        aid = row["anomaly_id"]
        ws_base, ws_z, ws_conf = ws_dict.get(aid, (row["wholesale_price"], 0.0, "low"))
        rs_base, rs_z, rs_conf = rs_dict.get(aid, (row["retail_price"], 0.0, "low"))
        
        conf = "high" if (ws_conf == "high" and rs_conf == "high") else ("low" if "low" in (ws_conf, rs_conf) else "insufficient_history")
        
        is_modeled = row["retail_data_type"] == "modeled"
        flag = False
        if not is_modeled and conf != "insufficient_history":
            if abs(ws_z) > 2.5 or abs(rs_z) > 2.5:
                flag = True
                flagged += 1

        reason = "Robust MAD model"
        if is_modeled:
            reason = "Modeled retail estimate: observation-only (anomaly flag suppressed)"
        elif flag:
            reason = f"Robust MAD spike detected (ws_mod_z={ws_z}, rs_mod_z={rs_z})"

        updates.append((ws_base, ws_z, rs_base, rs_z, conf, flag, reason, aid))

    cur.executemany(update_sql, updates)
    conn.commit()
    cur.close()
    conn.close()

    log.info("Robust MAD Anomaly Detection complete. Total: %d, Flagged: %d", len(df), flagged)
    return len(df), flagged


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run KrishiPulse anomaly detection engine.")
    parser.add_argument("--method", choices=["zscore", "mad"], default="zscore",
                        help="Anomaly scoring method: 'zscore' (standard parametric) or 'mad' (robust median/MAD)")
    parser.add_argument("--min-history", type=int, default=5,
                        help="Minimum history days for baseline confidence (default: 5)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL not found — check .env file")
        return 1

    if args.method == "mad":
        run_robust_mad_anomaly_detection(db_url, min_history=args.min_history)
    else:
        run_sql_anomaly_detection(db_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())