"""Multi-source ingestion runner for KrishiPulse wholesale mandi prices.

Tries enabled sources in priority order:
  1. csv_import (PRIMARY: reads manual Agmarknet reports from data_inbox/)
  2. datagov (AUTO-SOURCE: api.data.gov.in)
  3. agmarknet_api (STUB: documented non-CAPTCHA guard)

Records per-source execution outcome into ingestion_log.
Exits with code 0 if at least one source delivered data, or code 1 if NO source delivered fresh data.

Usage:
    python pipeline/run_ingestion.py
    python pipeline/run_ingestion.py --source csv_import
    python pipeline/run_ingestion.py --dry-run
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "pipeline") not in sys.path:
    sys.path.insert(0, str(ROOT / "pipeline"))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
from pipeline.sources.base import BaseSourceAdapter, NormalizedWholesaleRecord
from pipeline.sources.csv_import import CsvImportAdapter
from pipeline.sources.datagov_api import DataGovApiAdapter
from pipeline.sources.agmarknet_api import AgmarknetApiAdapter
from pipeline.import_agmarknet_file import import_records, LOG_INGESTION_SQL

log = logging.getLogger("run_ingestion")


def log_audit_record(
    conn,
    source_name: str,
    status: str,
    fetched: int = 0,
    inserted: int = 0,
    updated: int = 0,
    rejected: int = 0,
    error: Optional[str] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None
):
    """Writes an audit row to ingestion_log table."""
    try:
        cur = conn.cursor()
        cur.execute(LOG_INGESTION_SQL, (
            source_name,
            status,
            fetched,
            inserted,
            updated,
            rejected,
            error[:1000] if error else None,
            started_at or datetime.now(timezone.utc),
            completed_at or datetime.now(timezone.utc)
        ))
        conn.commit()
        cur.close()
    except Exception as exc:
        log.warning("Could not write to ingestion_log: %s", exc)
        if conn:
            conn.rollback()


def run_pipeline(
    db_url: str,
    enabled_sources: Optional[List[str]] = None,
    dry_run: bool = False,
    from_raw: bool = False,
    inbox_dir: str = "data_inbox",
    connect=None
) -> int:
    """Executes multi-source ingestion in priority order. Returns 0 if data was ingested, 1 otherwise."""
    sources_order = enabled_sources or ["csv_import", "datagov"]
    api_key = os.getenv("DATA_GOV_API_KEY", "").strip()

    conn = None
    if not dry_run and db_url:
        try:
            conn = (connect or psycopg2.connect)(db_url, connect_timeout=15)
        except Exception as exc:
            log.error("Failed to connect to database: %s", exc)
            return 2

    any_source_delivered = False
    source_results = {}

    for source_name in sources_order:
        started_at = datetime.now(timezone.utc)
        log.info("--- Attempting source: %s ---", source_name)

        if source_name == "csv_import":
            adapter = CsvImportAdapter(inbox_dir=inbox_dir)
            files = adapter.find_inbox_files()
            if not files:
                log.info("csv_import: No files in inbox %s (skipping)", inbox_dir)
                source_results[source_name] = {"status": "empty", "records": 0}
                if conn:
                    log_audit_record(conn, adapter.name, "empty", started_at=started_at, completed_at=datetime.now(timezone.utc))
                continue

            try:
                records = adapter.fetch()
                if not records:
                    log.warning("csv_import: Files found in inbox but 0 valid records parsed")
                    source_results[source_name] = {"status": "empty", "records": 0}
                    if conn:
                        log_audit_record(conn, adapter.name, "empty", started_at=started_at, completed_at=datetime.now(timezone.utc))
                    continue

                if dry_run:
                    log.info("[DRY-RUN] csv_import parsed %d records", len(records))
                    any_source_delivered = True
                    source_results[source_name] = {"status": "success", "records": len(records)}
                    break

                stats = import_records(conn, records)
                conn.commit()

                for f in files:
                    try:
                        dest = adapter.move_to_processed(f)
                        log.info("Moved processed file: %s -> %s", f.name, dest)
                    except Exception as me:
                        log.warning("Could not move %s: %s", f.name, me)

                any_source_delivered = True
                source_results[source_name] = {"status": "success", "records": stats["written"]}
                if conn:
                    log_audit_record(conn, adapter.name, "success",
                                     fetched=len(records), inserted=stats["written"],
                                     started_at=started_at, completed_at=datetime.now(timezone.utc))
                log.info("csv_import succeeded: wrote %d records. Primary source fulfilled run.", stats["written"])
                break  # Primary source delivered fresh data!
            except Exception as exc:
                if conn:
                    conn.rollback()
                log.error("csv_import failed: %s (falling back to next source)", exc)
                source_results[source_name] = {"status": "failure", "error": str(exc)}
                if conn:
                    log_audit_record(conn, adapter.name, "failure", error=str(exc),
                                     started_at=started_at, completed_at=datetime.now(timezone.utc))

        elif source_name == "datagov":
            if not api_key and not from_raw:
                log.warning("datagov: DATA_GOV_API_KEY not configured and --from-raw not set")
                source_results[source_name] = {"status": "skipped", "error": "No API key"}
                if conn:
                    log_audit_record(conn, "datagov", "skipped", error="No API key configured",
                                     started_at=started_at, completed_at=datetime.now(timezone.utc))
                continue

            adapter = DataGovApiAdapter(api_key=api_key, from_raw=from_raw)
            try:
                records = adapter.fetch()
                if not records:
                    log.warning("datagov: API responded but returned 0 records")
                    source_results[source_name] = {"status": "empty", "records": 0}
                    if conn:
                        log_audit_record(conn, adapter.name, "empty", started_at=started_at, completed_at=datetime.now(timezone.utc))
                    continue

                if dry_run:
                    log.info("[DRY-RUN] datagov fetched %d records", len(records))
                    any_source_delivered = True
                    source_results[source_name] = {"status": "success", "records": len(records)}
                    break

                stats = import_records(conn, records)
                conn.commit()

                any_source_delivered = True
                source_results[source_name] = {"status": "success", "records": stats["written"]}
                if conn:
                    log_audit_record(conn, adapter.name, "success",
                                     fetched=len(records), inserted=stats["written"],
                                     started_at=started_at, completed_at=datetime.now(timezone.utc))
                log.info("datagov succeeded: wrote %d records", stats["written"])
                break
            except Exception as exc:
                if conn:
                    conn.rollback()
                log.error("datagov API failed: %s", exc)
                source_results[source_name] = {"status": "failure", "error": str(exc)}
                if conn:
                    log_audit_record(conn, adapter.name, "failure", error=str(exc),
                                     started_at=started_at, completed_at=datetime.now(timezone.utc))

        elif source_name == "agmarknet_api":
            adapter = AgmarknetApiAdapter()
            try:
                adapter.fetch()
            except NotImplementedError as nie:
                log.info("agmarknet_api: %s (skipping)", nie)
                source_results[source_name] = {"status": "skipped", "error": str(nie)}

    if conn is not None:
        conn.close()

    log.info("=== Ingestion Summary ===")
    for src, res in source_results.items():
        log.info("  %s: %s", src, res)

    if any_source_delivered:
        log.info("Ingestion completed successfully.")
        return 0
    else:
        log.error("ALL enabled sources failed to deliver fresh data. Data remains STALE.")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Multi-source wholesale price ingestion runner.")
    parser.add_argument("--source", type=str, choices=["csv_import", "datagov", "all"], default="all",
                        help="Specific source to run or 'all' to run in priority order")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate only; no database changes")
    parser.add_argument("--from-raw", action="store_true", help="Replay from raw JSON files for datagov adapter")
    parser.add_argument("--inbox", type=str, default="data_inbox", help="Path to CSV/Excel inbox directory")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    db_url = os.getenv("DATABASE_URL", "").strip()

    if not args.dry_run and not db_url:
        log.error("DATABASE_URL is not set.")
        return 2

    enabled = ["csv_import", "datagov"] if args.source == "all" else [args.source]
    return run_pipeline(
        db_url=db_url,
        enabled_sources=enabled,
        dry_run=args.dry_run,
        from_raw=args.from_raw,
        inbox_dir=args.inbox
    )


if __name__ == "__main__":
    sys.exit(main())
