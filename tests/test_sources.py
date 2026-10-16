"""Tests for multi-source wholesale ingestion (pipeline/sources/ and pipeline/run_ingestion.py).

Includes:
- Unit parsing and column detection
- Date format flexibility
- Grade aggregation
- File movement to processed/
- Real PostgreSQL DB tests for idempotent upsert and ingestion_log recording
"""
import io
import os
import pathlib
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

from pipeline.sources.base import NormalizedWholesaleRecord
from pipeline.sources.csv_import import (
    CROP_CANONICAL_MAP,
    CsvImportAdapter,
    clean_col_name,
    determine_unit_factor,
    match_columns,
    parse_date_value,
)
from pipeline.sources.agmarknet_api import AgmarknetApiAdapter
from pipeline.sources.datagov_api import DataGovApiAdapter
import pipeline.import_agmarknet_file as iaf
import pipeline.run_ingestion as ri

DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


# ------------------------------------------------------------------ Column & Unit matching
def test_clean_col_name():
    assert clean_col_name("  Modal Price (Rs./Quintal) ") == "modal price (rs./quintal)"
    assert clean_col_name("State_Name") == "state name"


def test_match_columns_agmarknet_standard():
    cols = ["State", "District", "Market", "Commodity", "Variety", "Grade",
            "Arrival Date", "Min Price (Rs./Quintal)", "Max Price (Rs./Quintal)", "Modal Price (Rs./Quintal)"]
    matched = match_columns(cols)
    assert matched["state"] == "State"
    assert matched["market"] == "Market"
    assert matched["commodity"] == "Commodity"
    assert matched["arrival_date"] == "Arrival Date"
    assert matched["modal_price"] == "Modal Price (Rs./Quintal)"


def test_match_columns_lowercase():
    cols = ["state_name", "district_name", "market_name", "cmdt_name", "variety_name",
            "grade_name", "price_date", "min_price", "max_price", "modal_price", "unit_name_price"]
    matched = match_columns(cols)
    assert matched["market"] == "market_name"
    assert matched["commodity"] == "cmdt_name"
    assert matched["arrival_date"] == "price_date"
    assert matched["unit"] == "unit_name_price"


def test_determine_unit_factor():
    # Header says quintal
    assert determine_unit_factor("Modal Price (Rs./Quintal)", None, 1800, "Onion") == 0.01
    # Unit column says kg
    assert determine_unit_factor("Modal Price", "kg", 25, "Potato") == 1.0
    # Unit column says quintal
    assert determine_unit_factor("Modal Price", "quintal", 2500, "Potato") == 0.01
    # Unit column says tonne
    assert determine_unit_factor("Modal Price", "ton", 25000, "Potato") == 0.001
    # No unit specified, but price is 1500 for Potato -> auto-detected quintal
    assert determine_unit_factor("Modal Price", None, 1500, "Potato") == 0.01
    # No unit specified, price is 20 for Potato -> auto-detected kg
    assert determine_unit_factor("Modal Price", None, 20, "Potato") == 1.0


# ------------------------------------------------------------------ Date parsing
@pytest.mark.parametrize("input_val,expected", [
    ("16/10/2026", date(2026, 10, 16)),
    ("2026-10-16", date(2026, 10, 16)),
    ("16-10-2026", date(2026, 10, 16)),
    ("16-Oct-2026", date(2026, 10, 16)),
    ("16-OCT-2026", date(2026, 10, 16)),
    ("2026/10/16", date(2026, 10, 16)),
])
def test_parse_date_value_formats(input_val, expected):
    assert parse_date_value(input_val) == expected


def test_parse_date_value_invalid():
    assert parse_date_value("not-a-date") is None
    assert parse_date_value("") is None
    assert parse_date_value(None) is None


# ------------------------------------------------------------------ Agmarknet API stub
def test_agmarknet_api_stub_raises():
    adapter = AgmarknetApiAdapter()
    with pytest.raises(NotImplementedError, match="interactive CAPTCHA"):
        adapter.fetch()


# ------------------------------------------------------------------ CSV parsing & aggregation
def test_csv_adapter_parses_synthetic_data(tmp_path):
    csv_content = """State,District,Market,Commodity,Variety,Grade,Arrival Date,Min Price (Rs./Quintal),Max Price (Rs./Quintal),Modal Price (Rs./Quintal)
Gujarat,Rajkot,Gondal APMC,Onion,Red,FAQ,10/10/2026,1200,1800,1500
Gujarat,Rajkot,Gondal APMC,Onion,Red,Local,10/10/2026,1000,1600,1300
Gujarat,Ahmedabad,Ahmedabad APMC,Potato,Desi,FAQ,10/10/2026,1500,2200,1800
Maharashtra,Pune,Pune APMC,Onion,Red,FAQ,10/10/2026,1000,2000,1500
Gujarat,Surat,Surat APMC,UnknownCrop,Desi,FAQ,10/10/2026,1000,2000,1500
"""
    f = tmp_path / "synthetic_report.csv"
    f.write_text(csv_content, encoding="utf-8")

    adapter = CsvImportAdapter(inbox_dir=str(tmp_path))
    df = adapter.read_tabular_file(f)
    records, stats = adapter.parse_dataframe(df, f.name)

    assert stats["total"] == 5
    assert stats["valid"] == 3        # 2 Gujarat Onion (Gondal) + 1 Gujarat Potato (Ahmedabad)
    assert stats["wrong_state"] == 1  # Pune, Maharashtra
    assert stats["unmapped_crop"] == 1 # UnknownCrop

    # Aggregation across FAQ and Local grades for Gondal Onion
    agg = adapter.deduplicate_and_aggregate(records)
    assert len(agg) == 2  # 1 for Gondal Onion, 1 for Ahmedabad Potato

    gondal = next(r for r in agg if r.market == "Gondal APMC")
    assert gondal.modal_price == 14.0  # (1500 + 1300)/2 / 100 = 14.0 Rs/kg
    assert gondal.min_price == 11.0    # (1200 + 1000)/2 / 100 = 11.0 Rs/kg
    assert gondal.max_price == 17.0    # (1800 + 1600)/2 / 100 = 17.0 Rs/kg


def test_file_moved_to_processed(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "report.csv"
    f.write_text("State,District,Market,Commodity,Variety,Grade,Arrival Date,Min Price,Max Price,Modal Price\nGujarat,Rajkot,Gondal,Onion,V,G,10/10/2026,10,20,15\n")

    adapter = CsvImportAdapter(inbox_dir=str(inbox))
    dest = adapter.move_to_processed(f)
    assert dest.exists()
    assert dest.parent == inbox / "processed"
    assert not f.exists()


# ------------------------------------------------------------------ Database tests (real PG)
@pytest.fixture
def fresh_db():
    import psycopg2
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.execute((ROOT / "tests" / "schema.sql").read_text())
    cur.execute((ROOT / "migrations" / "001_constraints.sql").read_text())
    cur.execute((ROOT / "migrations" / "002_multisource.sql").read_text())
    crops = ["Onion", "Potato", "Tomato", "Garlic", "Green Chilli"]
    for name in crops:
        cur.execute("INSERT INTO crop_master (crop_name) VALUES (%s)", (name,))
        cur.execute("INSERT INTO crop_alias_map (raw_string, crop_id) "
                    "SELECT %s, crop_id FROM crop_master WHERE crop_name=%s", (name, name))
    yield conn
    conn.close()


@needs_db
def test_csv_import_idempotent_no_duplicates(fresh_db, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    csv_data = """State,District,Market,Commodity,Variety,Grade,Arrival Date,Min Price (Rs./Quintal),Max Price (Rs./Quintal),Modal Price (Rs./Quintal)
Gujarat,Rajkot,Gondal APMC,Onion,Red,FAQ,10/10/2026,1200,1800,1500
Gujarat,Ahmedabad,Ahmedabad APMC,Potato,Desi,FAQ,10/10/2026,1500,2200,1800
"""
    f = inbox / "sample1.csv"
    f.write_text(csv_data, encoding="utf-8")

    # Run 1: imports 2 records
    res = iaf.run_import(db_url=DB_URL, inbox_dir=str(inbox))
    assert res == 0

    cur = fresh_db.cursor()
    cur.execute("SELECT COUNT(*) FROM wholesale_price")
    assert cur.fetchone()[0] == 2

    cur.execute("SELECT source FROM wholesale_price LIMIT 1")
    assert cur.fetchone()[0] == "agmarknet_csv"

    # Verify ingestion_log
    cur.execute("SELECT status, records_inserted FROM ingestion_log WHERE source = 'agmarknet_csv'")
    log_row = cur.fetchone()
    assert log_row[0] == "success"
    assert log_row[1] == 2

    # Put identical file again into inbox
    f2 = inbox / "sample1_copy.csv"
    f2.write_text(csv_data, encoding="utf-8")

    # Run 2: upsert should create NO duplicate rows
    res2 = iaf.run_import(db_url=DB_URL, inbox_dir=str(inbox))
    assert res2 == 0

    cur.execute("SELECT COUNT(*) FROM wholesale_price")
    assert cur.fetchone()[0] == 2  # Still 2!


@needs_db
def test_run_ingestion_csv_primary_precedence(fresh_db, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    csv_data = """State,District,Market,Commodity,Variety,Grade,Arrival Date,Min Price (Rs./Quintal),Max Price (Rs./Quintal),Modal Price (Rs./Quintal)
Gujarat,Rajkot,Gondal APMC,Onion,Red,FAQ,10/10/2026,1200,1800,1500
"""
    (inbox / "inbox_test.csv").write_text(csv_data, encoding="utf-8")

    # Ensure datagov is not called if csv_import delivers
    def fail_datagov(*a, **k):
        raise RuntimeError("datagov should not have been called!")
    monkeypatch.setattr("pipeline.sources.datagov_api.DataGovApiAdapter.fetch", fail_datagov)

    exit_code = ri.run_pipeline(
        db_url=DB_URL,
        enabled_sources=["csv_import", "datagov"],
        inbox_dir=str(inbox)
    )
    assert exit_code == 0

    cur = fresh_db.cursor()
    cur.execute("SELECT COUNT(*) FROM wholesale_price WHERE source = 'agmarknet_csv'")
    assert cur.fetchone()[0] == 1


@needs_db
def test_run_ingestion_falls_back_when_inbox_empty(fresh_db, tmp_path, monkeypatch):
    empty_inbox = tmp_path / "empty_inbox"
    empty_inbox.mkdir()

    # Mock datagov returning one record
    mock_rec = NormalizedWholesaleRecord(
        crop="Tomato",
        state="Gujarat",
        district="Surat",
        market="Surat APMC",
        variety="Local",
        grade="FAQ",
        price_date=date(2026, 10, 10),
        min_price=15.0,
        max_price=25.0,
        modal_price=20.0,
        source="datagov"
    )
    monkeypatch.setattr("pipeline.sources.datagov_api.DataGovApiAdapter.fetch", lambda *a, **k: [mock_rec])
    monkeypatch.setenv("DATA_GOV_API_KEY", "FAKE_KEY")

    exit_code = ri.run_pipeline(
        db_url=DB_URL,
        enabled_sources=["csv_import", "datagov"],
        inbox_dir=str(empty_inbox)
    )
    assert exit_code == 0

    cur = fresh_db.cursor()
    cur.execute("SELECT COUNT(*) FROM wholesale_price WHERE source = 'datagov'")
    assert cur.fetchone()[0] == 1

    cur.execute("SELECT status FROM ingestion_log WHERE source = 'agmarknet_csv'")
    assert cur.fetchone()[0] == "empty"
