"""CLI and script for importing manual Agmarknet CSV / Excel reports from data_inbox/.

Reads downloaded Agmarknet reports, auto-detects column headers, date formats,
and price units, validates against bounds, upserts idempotently into wholesale_price,
and moves processed files to data_inbox/processed/.

Usage:
    python pipeline/import_agmarknet_file.py
    python pipeline/import_agmarknet_file.py --file data_inbox/sample.csv --dry-run
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.sources.base import NormalizedWholesaleRecord
from pipeline.sources.csv_import import CsvImportAdapter

log = logging.getLogger("import_agmarknet_file")

UPSERT_WHOLESALE_SQL = """
INSERT INTO wholesale_price
    (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit, is_provisional, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, 'kg', %s, %s)
ON CONFLICT (crop_id, mandi_id, variety, price_date) DO UPDATE SET
    min_price = EXCLUDED.min_price,
    max_price = EXCLUDED.max_price,
    modal_price = EXCLUDED.modal_price,
    is_provisional = EXCLUDED.is_provisional,
    source = EXCLUDED.source
"""

LOG_INGESTION_SQL = """
INSERT INTO ingestion_log
    (source, status, records_fetched, records_inserted, records_updated, records_rejected, error_message, started_at, completed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING log_id
"""


def get_or_create_mandi(cur, cache: dict, name: str, district: Optional[str]) -> int:
    key = (name, district)
    if key in cache:
        return cache[key]
    cur.execute(
        "SELECT mandi_id FROM mandi_master WHERE mandi_name = %s AND district IS NOT DISTINCT FROM %s",
        (name, district)
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO mandi_master (mandi_name, district) VALUES (%s, %s) RETURNING mandi_id",
            (name, district)
        )
        row = cur.fetchone()
    cache[key] = row[0]
    return row[0]


def import_records(conn, records: List[NormalizedWholesaleRecord]) -> Dict[str, int]:
    """Writes normalized records idempotently into wholesale_price. Returns write stats."""
    cur = conn.cursor()
    cur.execute("SELECT crop_name, crop_id FROM crop_master")
    crop_map = dict(cur.fetchall())

    cur.execute("SELECT raw_string, crop_id FROM crop_alias_map")
    alias_map = dict(cur.fetchall())
    # Merge alias map
    for k, v in alias_map.items():
        if k not in crop_map:
            crop_map[k] = v

    mandi_cache = {}
    inserted = 0

    for r in records:
        crop_id = crop_map.get(r.crop)
        if crop_id is None:
            # Attempt to insert crop if missing
            cur.execute(
                "INSERT INTO crop_master (crop_name) VALUES (%s) ON CONFLICT (crop_name) DO NOTHING RETURNING crop_id",
                (r.crop,)
            )
            ret = cur.fetchone()
            if ret:
                crop_id = ret[0]
            else:
                cur.execute("SELECT crop_id FROM crop_master WHERE crop_name = %s", (r.crop,))
                crop_id = cur.fetchone()[0]
            crop_map[r.crop] = crop_id

        mandi_id = get_or_create_mandi(cur, mandi_cache, r.market, r.district)
        cur.execute(UPSERT_WHOLESALE_SQL, (
            crop_id,
            mandi_id,
            r.variety,
            r.price_date,
            r.min_price,
            r.max_price,
            r.modal_price,
            r.is_provisional,
            r.source
        ))
        inserted += 1

    cur.close()
    return {"written": inserted}


def run_import(
    db_url: str,
    inbox_dir: str = "data_inbox",
    specific_file: Optional[str] = None,
    dry_run: bool = False,
    connect=None
) -> int:
    started_at = datetime.now(timezone.utc)
    adapter = CsvImportAdapter(inbox_dir=inbox_dir)

    files = [Path(specific_file)] if specific_file else adapter.find_inbox_files()
    if not files:
        log.info("No files to import in %s.", inbox_dir)
        return 0

    log.info("Found %d file(s) to process: %s", len(files), [f.name for f in files])

    total_valid_records = []
    processed_files = []
    total_raw_count = 0
    total_rejected = 0

    for f in files:
        try:
            df = adapter.read_tabular_file(f)
            records, stats = adapter.parse_dataframe(df, f.name)
            total_raw_count += stats["total"]
            total_rejected += stats["rejected"]
            total_valid_records.extend(records)
            processed_files.append(f)
        except Exception as exc:
            log.error("Error reading file %s: %s", f.name, exc)
            if not dry_run and db_url:
                try:
                    import psycopg2
                    conn = (connect or psycopg2.connect)(db_url)
                    cur = conn.cursor()
                    cur.execute(LOG_INGESTION_SQL, (
                        adapter.name, "failure", 0, 0, 0, 0,
                        f"Failed to read file {f.name}: {str(exc)[:500]}",
                        started_at, datetime.now(timezone.utc)
                    ))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            return 1

    aggregated = adapter.deduplicate_and_aggregate(total_valid_records)
    log.info("Extracted %d raw records -> %d valid -> %d aggregated records",
             total_raw_count, len(total_valid_records), len(aggregated))

    if dry_run:
        log.info("[DRY-RUN] Processed %d records successfully. No database changes made.", len(aggregated))
        return 0

    if not db_url:
        log.error("DATABASE_URL is not configured.")
        return 2

    import psycopg2
    conn = None
    try:
        conn = (connect or psycopg2.connect)(db_url)
        stats = import_records(conn, aggregated)

        # Log to ingestion_log
        cur = conn.cursor()
        cur.execute(LOG_INGESTION_SQL, (
            adapter.name,
            "success" if aggregated else "empty",
            len(aggregated),
            stats["written"],
            0,
            total_rejected,
            None,
            started_at,
            datetime.now(timezone.utc)
        ))
        conn.commit()

        # Safely move processed files
        for f in processed_files:
            try:
                dest = adapter.move_to_processed(f)
                log.info("Moved %s -> %s", f.name, dest)
            except Exception as move_err:
                log.warning("Could not move %s: %s", f.name, move_err)

        log.info("Successfully imported %d wholesale price records from %d file(s).",
                 stats["written"], len(processed_files))
        return 0
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        log.error("Database error during import: %s (database transaction rolled back)", exc)
        return 1
    finally:
        if conn is not None:
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import Agmarknet CSV / Excel files into KrishiPulse wholesale_price.")
    parser.add_argument("--file", type=str, default=None, help="Specific file to import (default: all in data_inbox/)")
    parser.add_argument("--inbox", type=str, default="data_inbox", help="Directory where incoming files are stored")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate only; do not write to database or move files")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    db_url = os.getenv("DATABASE_URL", "").strip()

    return run_import(
        db_url=db_url,
        inbox_dir=args.inbox,
        specific_file=args.file,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    sys.exit(main())
