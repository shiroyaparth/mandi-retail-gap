"""Importer for official retail prices (e.g. from Dept. of Consumer Affairs / fcainfoweb.nic.in).

Imports records with data_type='official'. Modeled prices are NEVER presented as observed.

Usage:
    python ingestion/load_retail_official.py --file path/to/official_prices.csv
    python ingestion/load_retail_official.py --file path/to/official_prices.csv --dry-run
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("load_retail_official")

CITY_ALIASES = {
    "ahmedabad": "Ahmedabad",
    "ahmadabad": "Ahmedabad",
    "surat": "Surat",
    "vadodara": "Vadodara",
    "baroda": "Vadodara",
    "rajkot": "Rajkot",
    "gandhinagar": "Gandhinagar",
    "bhavnagar": "Bhavnagar",
    "jamnagar": "Jamnagar",
}

CROP_ALIASES = {
    "potato": "Potato",
    "aloo": "Potato",
    "batata": "Potato",
    "onion": "Onion",
    "pyaz": "Onion",
    "dungri": "Onion",
    "tomato": "Tomato",
    "tamatar": "Tomato",
    "tameta": "Tomato",
    "garlic": "Garlic",
    "lahsun": "Garlic",
    "green chilli": "Green Chilli",
    "chilli": "Green Chilli",
}

UPSERT_RETAIL_SQL = """
INSERT INTO retail_price
    (crop_id, city, platform, price_date, price, unit, data_type, confidence_note)
VALUES (%s, %s, %s, %s, %s, 'kg', 'official', %s)
ON CONFLICT (crop_id, city, platform, price_date) DO UPDATE SET
    price = EXCLUDED.price,
    data_type = 'official',
    confidence_note = EXCLUDED.confidence_note
"""


def parse_date(val) -> Optional[date]:
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return date_parser.parse(str(val), dayfirst=True).date()
    except Exception:
        return None


def run_official_import(file_path: str, db_url: Optional[str] = None, dry_run: bool = False, connect=None) -> int:
    path = Path(file_path)
    if not path.exists():
        log.error("File does not exist: %s", file_path)
        return 1

    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            df = pd.read_csv(path, dtype=str)
        elif suffix in (".xlsx", ".xls"):
            df = pd.read_excel(path, dtype=str)
        else:
            log.error("Unsupported file format: %s", suffix)
            return 1
    except Exception as exc:
        log.error("Failed to read file %s: %s", path.name, exc)
        return 1

    # Column mapping
    cols = {c.strip().lower(): c for c in df.columns}
    city_col = next((cols[k] for k in cols if any(x in k for x in ["city", "center", "centre"])), None)
    crop_col = next((cols[k] for k in cols if any(x in k for x in ["crop", "commodity", "item"])), None)
    date_col = next((cols[k] for k in cols if any(x in k for x in ["date", "price_date", "day"])), None)
    price_col = next((cols[k] for k in cols if any(x in k for x in ["retail_price", "retail price", "price", "rate"])), None)

    if not all([city_col, crop_col, date_col, price_col]):
        log.error("Could not find required columns in %s. Found: %s", path.name, list(df.columns))
        return 1

    valid_rows = []
    for _, row in df.iterrows():
        raw_city = str(row.get(city_col, "")).strip().lower()
        city = CITY_ALIASES.get(raw_city)
        if not city:
            continue

        raw_crop = str(row.get(crop_col, "")).strip().lower()
        crop = CROP_ALIASES.get(raw_crop)
        if not crop:
            continue

        p_date = parse_date(row.get(date_col))
        if not p_date or p_date > date.today():
            continue

        try:
            price = float(str(row.get(price_col, "")).replace(",", "").strip())
            if price <= 0 or price > 1000:
                continue
        except (ValueError, TypeError):
            continue

        valid_rows.append({
            "city": city,
            "crop": crop,
            "price_date": p_date,
            "price": price,
            "platform": "DCA",
            "confidence_note": "Official Dept. of Consumer Affairs report (fcainfoweb.nic.in)"
        })

    log.info("Extracted %d valid official retail price rows from %d total rows.", len(valid_rows), len(df))

    if dry_run:
        log.info("[DRY-RUN] No database writes performed.")
        return 0

    db_url = db_url or os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        log.error("DATABASE_URL not configured.")
        return 2

    import psycopg2
    conn = None
    try:
        conn = (connect or psycopg2.connect)(db_url)
        cur = conn.cursor()

        cur.execute("SELECT crop_name, crop_id FROM crop_master")
        crop_ids = dict(cur.fetchall())

        inserted = 0
        for r in valid_rows:
            crop_id = crop_ids.get(r["crop"])
            if not crop_id:
                cur.execute(
                    "INSERT INTO crop_master (crop_name) VALUES (%s) ON CONFLICT (crop_name) DO NOTHING RETURNING crop_id",
                    (r["crop"],)
                )
                ret = cur.fetchone()
                crop_id = ret[0] if ret else crop_ids[r["crop"]]
                crop_ids[r["crop"]] = crop_id

            cur.execute(UPSERT_RETAIL_SQL, (
                crop_id,
                r["city"],
                r["platform"],
                r["price_date"],
                r["price"],
                r["confidence_note"]
            ))
            inserted += cur.rowcount

        conn.commit()
        cur.close()
        log.info("Successfully upserted %d official retail price rows.", inserted)
        return 0
    except Exception as exc:
        if conn:
            conn.rollback()
        log.error("Failed to write to database: %s", exc)
        return 1
    finally:
        if conn:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load official retail price reports into KrishiPulse.")
    parser.add_argument("--file", type=str, required=True, help="Path to official retail CSV/Excel file")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing to database")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    return run_official_import(args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
