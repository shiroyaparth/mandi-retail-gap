"""KrishiPulse wholesale ingestion (Agmarknet daily prices via data.gov.in).

Safety rules
------------
* A failed request is NEVER treated as "no data".
* Nothing is ever deleted or truncated. Writes are INSERT ... ON CONFLICT DO UPDATE.
* Exit code 0 = every crop fetched OK, 1 = at least one crop failed (CI turns red),
  2 = configuration problem (missing DATABASE_URL / API key).
* API keys are redacted from every log line.

Usage
-----
    python pipeline/load_wholesale_data.py              # normal run
    python pipeline/load_wholesale_data.py --dry-run    # fetch + validate, no DB
    python pipeline/load_wholesale_data.py --from-raw   # replay newest saved raw files into DB
"""
import argparse
import glob
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("wholesale")

CROPS = ["Onion", "Potato", "Tomato", "Garlic", "Green Chilli"]
STATE = "Gujarat"

# Configurable endpoint (the host has changed behaviour before; see README).
API_BASE = os.getenv("MANDI_API_URL", "https://api.data.gov.in/resource")
RESOURCE_ID = os.getenv("MANDI_RESOURCE_ID", "9ef84268-d588-465a-a308-a864a43d0070")
RAW_DIR = os.getenv("RAW_DIR", "pipeline/raw")

PAGE_LIMIT = 100
MAX_PAGES = 200            # hard stop against infinite pagination
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_ATTEMPTS = 4
RETRY_STATUS = {429, 500, 502, 503, 504}
HEADERS = {"User-Agent": "Mozilla/5.0 (KrishiPulse ingestion)"}

# Rs/kg plausibility bounds per crop (unchanged from the original script).
CROP_PRICE_BOUNDS = {
    "Onion": (2, 100), "Potato": (3, 80), "Tomato": (5, 150),
    "Garlic": (20, 500), "Green Chilli": (10, 300),
}
PROVISIONAL_DAYS = 3


class FetchError(Exception):
    """A crop could not be fetched reliably. Caller must NOT treat this as 'empty'."""


def redact(text, secret):
    return text.replace(secret, "***") if secret else text


# ----------------------------------------------------------------- fetching
def _get_page(session, url, params, api_key, crop, sleep):
    last = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, params=params, headers=HEADERS,
                               timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last = f"{type(exc).__name__}: {redact(str(exc), api_key)[:200]}"
            log.warning("%s: attempt %d/%d failed (%s)", crop, attempt, MAX_ATTEMPTS, last)
        else:
            code = resp.status_code
            if code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    raise FetchError(f"{crop}: HTTP 200 but body is not JSON "
                                     f"(content-type={resp.headers.get('Content-Type')})")
                if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                    raise FetchError(f"{crop}: unexpected response shape (no 'records' list)")
                if payload.get("status") not in (None, "ok"):
                    raise FetchError(f"{crop}: API status={payload.get('status')!r} "
                                     f"message={str(payload.get('message'))[:120]!r}")
                return payload
            if code in (401, 403):
                raise FetchError(f"{crop}: HTTP {code} - key rejected or this network is blocked "
                                 f"(not retrying)")
            if code in RETRY_STATUS:
                last = f"HTTP {code}"
                log.warning("%s: attempt %d/%d got HTTP %d", crop, attempt, MAX_ATTEMPTS, code)
            else:
                raise FetchError(f"{crop}: HTTP {code} (not retryable)")
        if attempt < MAX_ATTEMPTS:
            sleep(2 ** attempt)
    raise FetchError(f"{crop}: gave up after {MAX_ATTEMPTS} attempts ({last})")


def fetch_all_records(crop, api_key, session=None, sleep=time.sleep):
    """Return every record for `crop` in Gujarat. Raises FetchError on any failure."""
    session = session or requests.Session()
    url = f"{API_BASE.rstrip('/')}/{RESOURCE_ID}"
    records, offset, total = [], 0, None
    for _page in range(MAX_PAGES):
        params = {"api-key": api_key, "format": "json", "limit": PAGE_LIMIT, "offset": offset,
                  "filters[state.keyword]": STATE, "filters[commodity.keyword]": crop}
        payload = _get_page(session, url, params, api_key, crop, sleep)
        batch = payload["records"]
        if total is None:
            try:
                total = int(payload.get("total", 0))
            except (TypeError, ValueError):
                total = 0
        records.extend(batch)
        log.info("%s: fetched %d of %s records", crop, len(records), total)
        if not batch or len(batch) < PAGE_LIMIT or (total and len(records) >= total):
            return records
        offset += PAGE_LIMIT
        sleep(0.5)
    raise FetchError(f"{crop}: pagination exceeded {MAX_PAGES} pages")


def load_raw_records(crop, raw_dir=None):
    """Newest non-empty saved raw file for a crop (for --from-raw replay)."""
    raw_dir = raw_dir or RAW_DIR
    files = sorted(glob.glob(os.path.join(raw_dir, f"raw_wholesale_{crop.replace(' ', '_')}_*.json")))
    for path in reversed(files):
        with open(path) as fh:
            data = json.load(fh)
        if data:
            log.info("%s: replaying %s (%d records)", crop, path, len(data))
            return data
    raise FetchError(f"{crop}: no non-empty raw file in {raw_dir}")


# ------------------------------------------------------------------ parsing
def parse_record(rec, crop, today=None):
    """Validate one API record. Returns a clean dict or raises ValueError(reason)."""
    today = today or date.today()
    if str(rec.get("state", "")).strip() != STATE:
        raise ValueError("not_gujarat")
    if str(rec.get("commodity", "")).strip() != crop:
        raise ValueError("wrong_commodity")
    market = (rec.get("market") or "").strip()
    if not market:
        raise ValueError("missing_market")
    try:
        price_date = datetime.strptime(str(rec.get("arrival_date")), "%d/%m/%Y").date()
    except (ValueError, TypeError):
        raise ValueError("bad_date")
    if price_date > today + timedelta(days=1):
        raise ValueError("future_date")
    prices = {}
    for field in ("min_price", "max_price", "modal_price"):
        try:
            value = float(rec[field])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"bad_{field}")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"bad_{field}")
        prices[field] = round(value / 100, 4)          # Rs/quintal -> Rs/kg
    low, high = CROP_PRICE_BOUNDS.get(crop, (1, 500))
    provisional = not (low <= prices["modal_price"] <= high)
    provisional = provisional or (today - price_date).days <= PROVISIONAL_DAYS
    return {
        "crop": crop, "market": market, "district": (rec.get("district") or "").strip() or None,
        # NULL variety would defeat the UNIQUE key (NULLs never compare equal) -> 'Unknown'
        "variety": (rec.get("variety") or "").strip() or "Unknown",
        "price_date": price_date, "min": prices["min_price"], "max": prices["max_price"],
        "modal": prices["modal_price"], "provisional": provisional,
    }


def aggregate(rows):
    """Collapse rows that share the DB unique key (e.g. same variety, different grade)."""
    groups = {}
    for r in rows:
        key = (r["crop"], r["market"], r["district"], r["variety"], r["price_date"])
        groups.setdefault(key, []).append(r)
    out = []
    for group in groups.values():
        first = dict(group[0])
        if len(group) > 1:
            for k in ("min", "max", "modal"):
                first[k] = round(sum(g[k] for g in group) / len(group), 4)
            first["provisional"] = any(g["provisional"] for g in group)
        out.append(first)
    return out, len(rows) - len(out)


# ----------------------------------------------------------------- database
UPSERT_SQL = """
INSERT INTO wholesale_price
    (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit, is_provisional)
VALUES (%s, %s, %s, %s, %s, %s, %s, 'kg', %s)
ON CONFLICT (crop_id, mandi_id, variety, price_date) DO UPDATE SET
    min_price = EXCLUDED.min_price, max_price = EXCLUDED.max_price,
    modal_price = EXCLUDED.modal_price, is_provisional = EXCLUDED.is_provisional
"""


def get_or_create_mandi(cur, cache, name, district):
    key = (name, district)
    if key in cache:
        return cache[key]
    # IS NOT DISTINCT FROM so a NULL district still matches (plain '=' never matches NULL).
    cur.execute("SELECT mandi_id FROM mandi_master WHERE mandi_name = %s "
                "AND district IS NOT DISTINCT FROM %s", (name, district))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO mandi_master (mandi_name, district) VALUES (%s, %s) "
                    "RETURNING mandi_id", (name, district))
        row = cur.fetchone()
    cache[key] = row[0]
    return row[0]


def write_rows(cur, rows, crop_id, mandi_cache):
    for r in rows:
        mandi_id = get_or_create_mandi(cur, mandi_cache, r["market"], r["district"])
        cur.execute(UPSERT_SQL, (crop_id, mandi_id, r["variety"], r["price_date"],
                                 r["min"], r["max"], r["modal"], r["provisional"]))
    return len(rows)


def save_raw(crop, records, raw_dir=None):
    raw_dir = raw_dir or RAW_DIR
    os.makedirs(raw_dir, exist_ok=True)
    name = f"raw_wholesale_{crop.replace(' ', '_')}_{date.today():%Y-%m-%d}.json"
    with open(os.path.join(raw_dir, name), "w") as fh:
        json.dump(records, fh, indent=2)


# --------------------------------------------------------------------- main
def run(db_url, api_key, dry_run=False, from_raw=False, connect=None):
    stats = {"fetched": 0, "valid": 0, "rejected": 0, "merged": 0, "written": 0}
    failed, empty = [], []
    conn = cur = None
    alias = {}
    if not dry_run:
        import psycopg2
        conn = (connect or psycopg2.connect)(db_url, connect_timeout=15)
        cur = conn.cursor()
        # Housekeeping only (UPDATE, never DELETE): old rows stop being provisional.
        cur.execute("UPDATE wholesale_price SET is_provisional = false WHERE is_provisional = true "
                    "AND price_date < CURRENT_DATE - INTERVAL '%d days'" % PROVISIONAL_DAYS)
        conn.commit()
        cur.execute("SELECT raw_string, crop_id FROM crop_alias_map")
        alias = dict(cur.fetchall())
    mandi_cache = {}
    try:
        for crop in CROPS:
            try:
                records = load_raw_records(crop) if from_raw else fetch_all_records(crop, api_key)
            except FetchError as exc:
                log.error("%s", redact(str(exc), api_key))
                failed.append(crop)
                continue
            if not records:
                log.warning("%s: API reachable but returned 0 records (no arrivals today?)", crop)
                empty.append(crop)
                continue
            stats["fetched"] += len(records)
            if not from_raw:
                save_raw(crop, records)
            rows, reasons = [], {}
            for rec in records:
                try:
                    rows.append(parse_record(rec, crop))
                except ValueError as why:
                    reasons[str(why)] = reasons.get(str(why), 0) + 1
            stats["rejected"] += len(records) - len(rows)
            if reasons:
                log.warning("%s: rejected %d record(s): %s", crop, sum(reasons.values()), reasons)
            rows, merged = aggregate(rows)
            stats["valid"] += len(rows)
            stats["merged"] += merged
            if dry_run:
                continue
            try:
                crop_id = alias.get(crop)
                if crop_id is None:
                    raise RuntimeError(f"crop {crop!r} missing from crop_alias_map "
                                       f"(run load_master_data.py)")
                stats["written"] += write_rows(cur, rows, crop_id, mandi_cache)
                conn.commit()
            except Exception as exc:                  # DB problem: roll back this crop only
                conn.rollback()
                log.error("%s: database write failed: %s", crop, redact(str(exc), db_url))
                failed.append(crop)
    finally:
        if conn is not None:
            cur.close()
            conn.close()
    log.info("SUMMARY %s | failed=%s empty=%s", stats, failed, empty)
    if failed:
        return 1
    if stats["fetched"] == 0:
        log.error("No records for ANY crop although requests succeeded - treating as failure.")
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch and validate only; no database")
    ap.add_argument("--from-raw", action="store_true", help="replay newest saved raw JSON files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    db_url = os.getenv("DATABASE_URL", "").strip()
    api_key = os.getenv("DATA_GOV_API_KEY", "").strip()
    if not args.dry_run and not db_url:
        log.error("DATABASE_URL is not set")
        return 2
    if not args.from_raw and not api_key:
        log.error("DATA_GOV_API_KEY is not set")
        return 2
    return run(db_url, api_key, dry_run=args.dry_run, from_raw=args.from_raw)


if __name__ == "__main__":
    sys.exit(main())
