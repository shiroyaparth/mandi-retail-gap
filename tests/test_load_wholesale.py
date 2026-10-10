"""Tests for pipeline/load_wholesale_data.py

MOCKED  : everything using FakeSession (no network is touched).
DB tests: need a real PostgreSQL. Set TEST_DATABASE_URL to an EMPTY scratch database, e.g.
          postgresql://postgres@localhost:5432/kp_test   (they are skipped otherwise)
LIVE    : tests/test_live_api.py style checks are NOT included - run
          `python pipeline/load_wholesale_data.py --dry-run` on your own machine for that.
"""
import json
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import load_wholesale_data as lw  # noqa: E402

KEY = "SECRET-KEY-123"
TODAY = date.today()


def ddmmyyyy(d):
    return d.strftime("%d/%m/%Y")


def rec(**over):
    base = {"state": "Gujarat", "district": "Rajkot", "market": "Gondal APMC", "commodity": "Onion",
            "variety": "Onion", "grade": "FAQ", "arrival_date": ddmmyyyy(TODAY - timedelta(days=10)),
            "min_price": 1000, "max_price": 2000, "modal_price": 1500}
    base.update(over)
    return base


class FakeResp:
    def __init__(self, status=200, payload=None, text_json=True):
        self.status_code, self._payload, self._json_ok = status, payload, text_json
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if not self._json_ok:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns / raises the queued items one per .get() call."""
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params or {}, _timeout=timeout))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(records, total=None):
    return FakeResp(200, {"status": "ok", "total": total if total is not None else len(records),
                          "records": records})


def no_sleep(_):
    pass


# ------------------------------------------------------------- fetching (mocked)
def test_success_single_page():
    s = FakeSession(page([rec(), rec(market="B")]))
    assert len(lw.fetch_all_records("Onion", KEY, s, no_sleep)) == 2
    assert s.calls[0]["filters[state.keyword]"] == "Gujarat"
    assert s.calls[0]["filters[commodity.keyword]"] == "Onion"
    assert s.calls[0]["_timeout"] == (lw.CONNECT_TIMEOUT, lw.READ_TIMEOUT)


def test_pagination_three_pages():
    s = FakeSession(page([rec()] * 100, 240), page([rec()] * 100, 240), page([rec()] * 40, 240))
    assert len(lw.fetch_all_records("Onion", KEY, s, no_sleep)) == 240
    assert [c["offset"] for c in s.calls] == [0, 100, 200]


def test_pagination_stops_on_empty_page_even_if_total_lies():
    s = FakeSession(page([rec()] * 100, 9999), page([], 9999))
    assert len(lw.fetch_all_records("Onion", KEY, s, no_sleep)) == 100


def test_pagination_cannot_loop_forever(monkeypatch):
    monkeypatch.setattr(lw, "MAX_PAGES", 3)
    s = FakeSession(*[page([rec()] * 100, 10**6) for _ in range(5)])
    with pytest.raises(lw.FetchError, match="pagination"):
        lw.fetch_all_records("Onion", KEY, s, no_sleep)


def test_connection_refused_retries_then_fails_and_redacts_key():
    err = requests.exceptions.ConnectionError(f"refused ... /resource/x?api-key={KEY}&limit=100")
    s = FakeSession(*[err] * lw.MAX_ATTEMPTS)
    with pytest.raises(lw.FetchError) as exc:
        lw.fetch_all_records("Onion", KEY, s, no_sleep)
    assert len(s.calls) == lw.MAX_ATTEMPTS
    assert KEY not in str(exc.value)


def test_timeout_then_success():
    s = FakeSession(requests.exceptions.ReadTimeout("slow"), page([rec()]))
    assert len(lw.fetch_all_records("Onion", KEY, s, no_sleep)) == 1


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_http_errors_retry(code):
    s = FakeSession(FakeResp(code), page([rec()]))
    assert len(lw.fetch_all_records("Onion", KEY, s, no_sleep)) == 1
    assert len(s.calls) == 2


@pytest.mark.parametrize("code", [429, 503])
def test_retryable_http_errors_eventually_fail(code):
    s = FakeSession(*[FakeResp(code)] * lw.MAX_ATTEMPTS)
    with pytest.raises(lw.FetchError):
        lw.fetch_all_records("Onion", KEY, s, no_sleep)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_auth_and_client_errors_do_not_retry(code):
    s = FakeSession(FakeResp(code))
    with pytest.raises(lw.FetchError, match=str(code)):
        lw.fetch_all_records("Onion", KEY, s, no_sleep)
    assert len(s.calls) == 1


def test_malformed_json_is_an_error_not_empty():
    with pytest.raises(lw.FetchError, match="not JSON"):
        lw.fetch_all_records("Onion", KEY, FakeSession(FakeResp(200, text_json=False)), no_sleep)


@pytest.mark.parametrize("payload", [{}, {"records": "nope"}, [], {"status": "error", "records": []}])
def test_bad_shapes_are_errors(payload):
    with pytest.raises(lw.FetchError):
        lw.fetch_all_records("Onion", KEY, FakeSession(FakeResp(200, payload)), no_sleep)


def test_genuinely_empty_response_returns_empty_list():
    assert lw.fetch_all_records("Onion", KEY, FakeSession(page([], 0)), no_sleep) == []


# ------------------------------------------------------------------ parsing
def test_parse_converts_quintal_to_kg():
    r = lw.parse_record(rec(), "Onion", TODAY)
    assert (r["min"], r["max"], r["modal"]) == (10.0, 20.0, 15.0)
    assert r["variety"] == "Onion" and r["provisional"] is False


@pytest.mark.parametrize("over,reason", [
    ({"state": "Maharashtra"}, "not_gujarat"),
    ({"commodity": "Potato"}, "wrong_commodity"),
    ({"market": ""}, "missing_market"),
    ({"arrival_date": "2026-07-30"}, "bad_date"),
    ({"arrival_date": None}, "bad_date"),
    ({"arrival_date": ddmmyyyy(TODAY + timedelta(days=30))}, "future_date"),
    ({"modal_price": "NA"}, "bad_modal_price"),
    ({"modal_price": 0}, "bad_modal_price"),
    ({"modal_price": -5}, "bad_modal_price"),
    ({"modal_price": float("nan")}, "bad_modal_price"),
    ({"min_price": None}, "bad_min_price"),
])
def test_invalid_records_rejected(over, reason):
    with pytest.raises(ValueError, match=reason):
        lw.parse_record(rec(**over), "Onion", TODAY)


def test_missing_price_field_rejected_not_defaulted_to_zero():
    r = rec()
    del r["max_price"]
    with pytest.raises(ValueError, match="bad_max_price"):
        lw.parse_record(r, "Onion", TODAY)


def test_null_variety_becomes_unknown_and_null_district_ok():
    r = lw.parse_record(rec(variety=None, district=None), "Onion", TODAY)
    assert r["variety"] == "Unknown" and r["district"] is None


def test_out_of_bounds_price_is_provisional():
    assert lw.parse_record(rec(commodity="Tomato", modal_price=230, min_price=220, max_price=240), "Tomato", TODAY)["provisional"]


def test_recent_record_is_provisional():
    assert lw.parse_record(rec(arrival_date=ddmmyyyy(TODAY)), "Onion", TODAY)["provisional"]


def test_aggregate_merges_same_key_different_grade():
    a = lw.parse_record(rec(grade="FAQ", modal_price=800), "Onion", TODAY)
    b = lw.parse_record(rec(grade="Local", modal_price=1000), "Onion", TODAY)
    rows, merged = lw.aggregate([a, b])
    assert merged == 1 and len(rows) == 1 and rows[0]["modal"] == 9.0


# --------------------------------------------------------------- DB (real PG)
DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def db():
    import psycopg2
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.execute((ROOT / "tests" / "schema.sql").read_text())
    cur.execute((ROOT / "migrations" / "001_constraints.sql").read_text())
    for name in lw.CROPS:
        cur.execute("INSERT INTO crop_master (crop_name) VALUES (%s)", (name,))
        cur.execute("INSERT INTO crop_alias_map (raw_string, crop_id) "
                    "SELECT %s, crop_id FROM crop_master WHERE crop_name=%s", (name, name))
    yield conn
    conn.close()


def count(conn, table="wholesale_price"):
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def patch_fetch(monkeypatch, per_crop):
    """per_crop: {crop: list_of_records | Exception}"""
    def fake(crop, api_key, session=None, sleep=None):
        v = per_crop.get(crop, [])
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(lw, "fetch_all_records", fake)


@pytest.fixture(autouse=True)
def _raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lw, "RAW_DIR", str(tmp_path / "raw"))


@needs_db
def test_idempotent_rerun_creates_no_duplicates(db, monkeypatch):
    data = {"Onion": [rec(), rec(market="Other APMC"), rec(variety=None, market="NullVar APMC")],
            "Potato": [rec(commodity="Potato")]}
    patch_fetch(monkeypatch, data)
    assert lw.run(DB_URL, KEY) == 0
    first = count(db)
    assert first == 4
    assert lw.run(DB_URL, KEY) == 0
    assert lw.run(DB_URL, KEY) == 0
    assert count(db) == first                      # NULL-variety row did not duplicate either


@needs_db
def test_update_changes_price_without_new_row(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec(modal_price=1500)]})
    lw.run(DB_URL, KEY)
    patch_fetch(monkeypatch, {"Onion": [rec(modal_price=1800)]})
    lw.run(DB_URL, KEY)
    cur = db.cursor()
    cur.execute("SELECT modal_price FROM wholesale_price")
    assert [float(r[0]) for r in cur.fetchall()] == [18.0]


@needs_db
def test_outage_preserves_history_and_fails_loudly(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec(), rec(market="B")], "Potato": [rec(commodity="Potato")]})
    assert lw.run(DB_URL, KEY) == 0
    before = count(db)
    err = lw.FetchError("refused")
    patch_fetch(monkeypatch, {c: err for c in lw.CROPS})
    assert lw.run(DB_URL, KEY) == 1                 # red CI, not silent success
    assert count(db) == before                       # nothing deleted


@needs_db
def test_partial_outage_still_saves_good_crops_but_returns_failure(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec()], "Potato": lw.FetchError("boom")})
    assert lw.run(DB_URL, KEY) == 1
    assert count(db) == 1


@needs_db
def test_all_empty_with_healthy_api_is_flagged(db, monkeypatch):
    patch_fetch(monkeypatch, {})
    assert lw.run(DB_URL, KEY) == 1


@needs_db
def test_filtering_rejects_wrong_state_and_wrong_crop(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec(), rec(state="Punjab", market="P"),
                                        rec(commodity="Potato", market="Q")]})
    assert lw.run(DB_URL, KEY) == 0
    assert count(db) == 1


@needs_db
def test_invalid_rows_skipped_valid_rows_kept(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec(), rec(market="Bad", modal_price="NA"),
                                        rec(market="Bad2", arrival_date="junk")]})
    assert lw.run(DB_URL, KEY) == 0
    assert count(db) == 1


@needs_db
def test_null_district_mandi_not_duplicated(db, monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec(district=None)]})
    lw.run(DB_URL, KEY)
    lw.run(DB_URL, KEY)
    assert count(db, "mandi_master") == 1


@needs_db
def test_replay_from_raw(db, monkeypatch, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "raw_wholesale_Onion_2026-07-30.json").write_text(json.dumps([rec()]))
    (raw / "raw_wholesale_Onion_2026-07-31.json").write_text("[]")      # empty newest is skipped
    assert lw.run(DB_URL, "", from_raw=True) == 1                         # other crops have no raw
    assert count(db) == 1


@needs_db
def test_retail_on_conflict_works_after_migration(db):
    cur = db.cursor()
    sql = ("INSERT INTO retail_price (crop_id,city,platform,price_date,price,unit,data_type) "
           "VALUES (1,'Ahmedabad','Blinkit','2026-07-30',40,'kg','scraped') "
           "ON CONFLICT (crop_id,city,platform,price_date) DO NOTHING")
    cur.execute(sql)
    cur.execute(sql)
    assert count(db, "retail_price") == 1


def test_dry_run_needs_no_database(monkeypatch):
    patch_fetch(monkeypatch, {"Onion": [rec()]})
    assert lw.run("", KEY, dry_run=True) == 0


def test_main_requires_config(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_GOV_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    assert lw.main([]) == 2
