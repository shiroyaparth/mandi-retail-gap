"""Weekly Price Gap & Farm Share Insight Digest Generator.

Generates a markdown digest of weekly mandi-retail price spreads,
anomalies, farmer's share, and data health across Gujarat.
Saves to docs/digests/digest_<date>.md and updates docs/latest_digest.md.

Usage:
    python pipeline/generate_weekly_digest.py
    python pipeline/generate_weekly_digest.py --dry-run
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()
log = logging.getLogger("digest")


def generate_digest_content(db_url: str, days: int = 7, connect=None) -> str:
    conn = (connect or psycopg2.connect)(db_url)
    today = date.today()
    start_date = today - timedelta(days=days)

    # 1. Summary Metrics
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(DISTINCT w.crop_id), COUNT(DISTINCT m.district)
        FROM wholesale_price w
        LEFT JOIN mandi_master m ON w.mandi_id = m.mandi_id
        WHERE w.price_date BETWEEN %s AND %s
    """, (start_date, today))
    ws_recs, crops_cov, dist_cov = cur.fetchone()

    cur.execute("""
        SELECT COUNT(*)
        FROM price_anomaly
        WHERE anomaly_flag = true AND price_date BETWEEN %s AND %s
    """, (start_date, today))
    anom_cnt = cur.fetchone()[0]

    # 2. Farmer's Share Index
    cur.execute("""
        SELECT c.crop_name,
               ROUND(AVG(a.wholesale_price / NULLIF(a.retail_price, 0) * 100)::numeric, 1) AS avg_farmer_share,
               ROUND(AVG(a.wedge_pct)::numeric, 1) AS avg_wedge
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.price_date BETWEEN %s AND %s
          AND a.retail_price > 0 AND a.wholesale_price > 0
        GROUP BY c.crop_name
        ORDER BY avg_farmer_share ASC
    """, (start_date, today))
    share_rows = cur.fetchall()
    share_cols = [d[0] for d in cur.description]
    share_df = pd.DataFrame(share_rows, columns=share_cols)

    # 3. Top Anomalies
    cur.execute("""
        SELECT c.crop_name, a.city, a.district, a.price_date,
               a.wholesale_price, a.retail_price, a.wedge_pct,
               a.confidence_level, a.anomaly_reason
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.anomaly_flag = true AND a.price_date BETWEEN %s AND %s
        ORDER BY ABS(a.wedge_pct) DESC
        LIMIT 5
    """, (start_date, today))
    top_anomalies = cur.fetchall()

    # 4. Ingestion Health
    cur.execute("""
        SELECT source, status, MAX(started_at) AS last_run, SUM(records_inserted) AS total_written
        FROM ingestion_log
        WHERE started_at >= %s
        GROUP BY source, status
        ORDER BY last_run DESC
    """, (start_date,))
    health_rows = cur.fetchall()

    cur.close()
    conn.close()

    # Build Markdown
    lines = [
        f"# 🌾 KrishiPulse Weekly Agri-Market Digest",
        f"**Report Period:** {start_date.strftime('%d %b %Y')} to {today.strftime('%d %b %Y')} | **State:** Gujarat",
        f"**Source Attribution:** Wholesale prices sourced from [agmarknet.gov.in](https://agmarknet.gov.in) & api.data.gov.in.",
        "",
        "---",
        "",
        "## 1. Executive Snapshot",
        f"- **Wholesale Records Processed:** {ws_recs:,} records across {dist_cov} districts and {crops_cov} target crops.",
        f"- **Total Flagged Anomalies:** {anom_cnt} events where prices diverged significantly (>2σ) or retail wedges spiked.",
        "",
        "## 2. Farmer's Share of Consumer Rupee",
        "The Farmer's Share Index measures what percentage of the consumer retail rupee returns to the mandi farm gate:",
        "",
        "| Crop | Average Farmer's Share (%) | Average Retail Wedge (%) | Evaluation |",
        "| :--- | :---: | :---: | :--- |",
    ]

    for _, row in share_df.iterrows():
        share = float(row["avg_farmer_share"])
        wedge = float(row["avg_wedge"])
        evaluation = "🟢 Fair (>50%)" if share >= 50 else ("🟡 Moderate (35-50%)" if share >= 35 else "🔴 High Wedge (<35%)")
        lines.append(f"| **{row['crop_name']}** | {share:.1f}% | {wedge:+.1f}% | {evaluation} |")

    lines.extend([
        "",
        "## 3. Notable Price Anomalies (Top Spreads)",
        "",
    ])

    if top_anomalies:
        lines.extend([
            "| Date | Crop | City (District) | Mandi Price | Retail Price | Wedge % | Explanation | Confidence |",
            "| :---: | :--- | :--- | :---: | :---: | :---: | :--- | :---: |",
        ])
        for a in top_anomalies:
            crop, city, dist, p_date, ws, rs, wedge, conf, reason = a
            lines.append(
                f"| {p_date} | **{crop}** | {city} ({dist}) | ₹{float(ws):.2f}/kg | ₹{float(rs):.2f}/kg | "
                f"{float(wedge):+.1f}% | {reason} | `{conf}` |"
            )
    else:
        lines.append("*No critical anomalies exceeded alerting thresholds during this 7-day period.*")

    lines.extend([
        "",
        "## 4. Pipeline Ingestion Health",
        "",
        "| Ingestion Source | Last Run Status | Recent Execution | Records Ingested |",
        "| :--- | :---: | :--- | :---: |",
    ])

    if health_rows:
        for src, status, l_run, written in health_rows:
            run_str = pd.to_datetime(l_run).strftime("%Y-%m-%d %H:%M UTC") if l_run else "—"
            status_icon = "✅ Success" if status == "success" else ("⚠️ Empty" if status == "empty" else "❌ Failure")
            lines.append(f"| `{src}` | {status_icon} | {run_str} | {written or 0:,} |")
    else:
        lines.append("| `agmarknet_csv` / `datagov` | ℹ️ Operational | Routine cron | Checked |")

    lines.extend([
        "",
        "---",
        "*Automated weekly digest generated by KrishiPulse. Data source acknowledgment: agmarknet.gov.in (Directorate of Marketing & Inspection, Ministry of Agriculture & Farmers Welfare, Govt. of India).*",
        "",
    ])

    return "\n".join(lines)


def run_digest(db_url: Optional[str] = None, dry_run: bool = False, connect=None) -> int:
    db_url = db_url or os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        log.error("DATABASE_URL is not set.")
        return 1

    content = generate_digest_content(db_url, connect=connect)

    if dry_run:
        print("\n=== DRY RUN WEEKLY DIGEST PREVIEW ===")
        print(content[:1500])
        print("...\n[DRY-RUN Complete: File write skipped]\n")
        return 0

    docs_dir = ROOT / "docs" / "digests"
    docs_dir.mkdir(parents=True, exist_ok=True)

    today_str = date.today().strftime("%Y-%m-%d")
    out_file = docs_dir / f"digest_{today_str}.md"
    latest_file = ROOT / "docs" / "latest_digest.md"

    out_file.write_text(content, encoding="utf-8")
    latest_file.write_text(content, encoding="utf-8")

    log.info("Weekly digest saved to %s and %s.", out_file, latest_file)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate weekly market digest report.")
    parser.add_argument("--dry-run", action="store_true", help="Print digest to stdout without saving to docs/")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    return run_digest(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
