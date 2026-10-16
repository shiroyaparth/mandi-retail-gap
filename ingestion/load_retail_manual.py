import argparse
import csv
import io
import os
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
import requests

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')
RETAIL_SHEET_URL = os.getenv('RETAIL_SHEET_URL')


def fetch_sheet(url=None):
    target_url = url or RETAIL_SHEET_URL
    if not target_url:
        raise ValueError("No RETAIL_SHEET_URL configured")
    response = requests.get(target_url, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch Google Sheet: {response.status_code}")
    return csv.DictReader(io.StringIO(response.text))


def read_local_csv(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def run_retail_import(reader, db_url=None, dry_run=False, connect=None):
    db_url = db_url or DATABASE_URL
    if not dry_run and not db_url:
        print("DATABASE_URL not found — check .env file")
        return 1

    conn = None
    if not dry_run:
        conn = (connect or psycopg2.connect)(db_url)
        cur = conn.cursor()

    total_processed = 0
    total_inserted = 0
    total_skipped = 0

    for row in reader:
        total_processed += 1

        crop_name = row.get('crop_name', '').strip()
        if not dry_run:
            cur.execute(
                "SELECT crop_id FROM crop_master WHERE crop_name = %s",
                (crop_name,)
            )
            result = cur.fetchone()
            if not result:
                print(f"Unmapped crop: {crop_name} — skipping")
                total_skipped += 1
                continue
            crop_id = result[0]
        else:
            crop_id = 1

        date_str = (row.get('entry_date') or row.get('price_date', '')).strip()
        try:
            price_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            print(f"Bad date format: {date_str} — skipping. Use YYYY-MM-DD.")
            total_skipped += 1
            continue

        price_str = (row.get('price_per_kg') or row.get('price', '')).strip()
        try:
            price = float(price_str)
        except ValueError:
            print(f"Bad price for {crop_name} on {date_str} — skipping")
            total_skipped += 1
            continue

        if price < 1 or price > 1000:
            print(f"Suspicious price for {crop_name}: ₹{price}/kg — skipping")
            total_skipped += 1
            continue

        city = row.get('city', '').strip()
        platform = row.get('platform', '').strip() or 'Market'
        unit = row.get('unit', '').strip() or 'kg'
        data_type = (row.get('data_type') or 'scraped').strip().lower()
        if data_type not in ('scraped', 'official', 'modeled'):
            data_type = 'scraped'
        confidence_note = (row.get('notes') or row.get('confidence_note') or '').strip() or None

        if not dry_run:
            cur.execute(
                """
                INSERT INTO retail_price
                    (crop_id, city, platform, price_date, price, unit, data_type, confidence_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (crop_id, city, platform, price_date) DO NOTHING
                """,
                (crop_id, city, platform, price_date, price, unit, data_type, confidence_note)
            )
            total_inserted += cur.rowcount
        else:
            total_inserted += 1

    if not dry_run and conn:
        conn.commit()
        cur.close()
        conn.close()

    print(f"Total processed: {total_processed}")
    print(f"Total inserted: {total_inserted}")
    print(f"Total skipped: {total_skipped}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load retail prices from Google Sheet or CSV file.")
    parser.add_argument("--file", type=str, default=None, help="Path to local CSV file instead of Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing to database")
    args = parser.parse_args(argv)

    if args.file:
        reader = read_local_csv(args.file)
    else:
        if not RETAIL_SHEET_URL:
            print("RETAIL_SHEET_URL not found — check .env file or use --file")
            return 1
        reader = fetch_sheet()

    return run_retail_import(reader, dry_run=args.dry_run)


if __name__ == "__main__":
    exit(main())