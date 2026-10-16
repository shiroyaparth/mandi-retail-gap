"""Tests for retail price ingestion and data quality (Stage 3)."""
import os
import pathlib
import sys
from datetime import date

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ingestion.load_retail_manual as lrm
import ingestion.load_retail_official as lro

DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def retail_db():
    import psycopg2
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.execute((ROOT / "tests" / "schema.sql").read_text())
    cur.execute((ROOT / "migrations" / "001_constraints.sql").read_text())
    cur.execute((ROOT / "migrations" / "002_multisource.sql").read_text())
    cur.execute((ROOT / "migrations" / "003_retail_official.sql").read_text())
    cur.execute((ROOT / "migrations" / "004_anomaly_engine.sql").read_text())
    for name in ["Onion", "Potato", "Tomato", "Garlic", "Green Chilli"]:
        cur.execute("INSERT INTO crop_master (crop_name) VALUES (%s)", (name,))
        cur.execute("INSERT INTO crop_alias_map (raw_string, crop_id) "
                    "SELECT %s, crop_id FROM crop_master WHERE crop_name=%s", (name, name))
    yield conn
    conn.close()


def test_dry_run_retail_local_file(tmp_path):
    csv_file = tmp_path / "retail_sample.csv"
    csv_file.write_text("entry_date,city,platform,crop_name,price_per_kg,unit,notes\n"
                        "2026-10-10,Ahmedabad,Blinkit,Onion,35,kg,observed\n")
    reader = lrm.read_local_csv(str(csv_file))
    code = lrm.run_retail_import(reader, dry_run=True)
    assert code == 0


@needs_db
def test_retail_official_import_and_constraint(retail_db, tmp_path):
    csv_file = tmp_path / "official_dca.csv"
    csv_file.write_text("Date,Center,Commodity,Retail Price\n"
                        "10/10/2026,Ahmedabad,Onion,34.00\n"
                        "10/10/2026,Rajkot,Potato,28.50\n")

    code = lro.run_official_import(str(csv_file), db_url=DB_URL)
    assert code == 0

    cur = retail_db.cursor()
    cur.execute("SELECT city, data_type, price, confidence_note FROM retail_price ORDER BY city")
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "Ahmedabad"
    assert rows[0][1] == "official"
    assert float(rows[0][2]) == 34.0
    assert "Official Dept. of Consumer Affairs" in rows[0][3]

    # Test constraint rejects invalid data_type
    with pytest.raises(Exception):
        cur.execute("INSERT INTO retail_price (crop_id, city, platform, price_date, price, unit, data_type) "
                    "VALUES (1, 'Surat', 'Test', '2026-10-10', 40, 'kg', 'invalid_type')")
