"""SQL-level and pipeline tests for KrishiPulse Anomaly Engine (Stage 4)."""
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import pipeline.run_anomaly_detection as rad

DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def anomaly_db():
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

    # Seed crops
    for name in ["Onion", "Potato", "Tomato", "Garlic", "Green Chilli"]:
        cur.execute("INSERT INTO crop_master (crop_name) VALUES (%s)", (name,))

    # Seed mandis in different districts
    cur.execute("INSERT INTO mandi_master (mandi_name, district, state) VALUES ('Ahmedabad APMC', 'Ahmedabad', 'Gujarat')")
    cur.execute("INSERT INTO mandi_master (mandi_name, district, state) VALUES ('Gondal APMC', 'Rajkot', 'Gujarat')")
    cur.execute("INSERT INTO mandi_master (mandi_name, district, state) VALUES ('Surat APMC', 'Surat', 'Gujarat')")

    yield conn
    conn.close()


@needs_db
def test_city_district_spatial_join(anomaly_db):
    """Verify that Rajkot retail ONLY pairs with Rajkot wholesale, and Ahmedabad with Ahmedabad."""
    cur = anomaly_db.cursor()
    today = date(2026, 10, 10)

    # Insert wholesale prices
    # Crop 1 = Onion. Mandi 1 = Ahmedabad (dist: Ahmedabad), Mandi 2 = Gondal (dist: Rajkot)
    cur.execute("""
        INSERT INTO wholesale_price (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit)
        VALUES 
            (1, 1, 'Red', '2026-10-10', 18, 22, 20, 'kg'),
            (1, 2, 'Red', '2026-10-10', 14, 18, 16, 'kg')
    """)

    # Insert retail prices for Ahmedabad and Rajkot
    cur.execute("""
        INSERT INTO retail_price (crop_id, city, platform, price_date, price, unit, data_type)
        VALUES 
            (1, 'Ahmedabad', 'Blinkit', '2026-10-10', 35, 'kg', 'scraped'),
            (1, 'Rajkot', 'Blinkit', '2026-10-10', 30, 'kg', 'scraped')
    """)

    # Run SQL anomaly engine
    total, flagged = rad.run_sql_anomaly_detection(DB_URL)
    assert total == 2  # Exactly 2 pairs, NOT 4 (no Cartesian cross-product)!

    cur.execute("SELECT district, city, wholesale_price, retail_price FROM price_anomaly ORDER BY city")
    rows = cur.fetchall()
    # Ahmedabad city joined with Ahmedabad district
    assert rows[0][0] == "Ahmedabad" and rows[0][1] == "Ahmedabad"
    assert float(rows[0][2]) == 20.0 and float(rows[0][3]) == 35.0

    # Rajkot city joined with Rajkot district (Gondal)
    assert rows[1][0] == "Rajkot" and rows[1][1] == "Rajkot"
    assert float(rows[1][2]) == 16.0 and float(rows[1][3]) == 30.0


@needs_db
def test_modeled_retail_never_flags_anomalies(anomaly_db):
    """Verify Rule (f): Modeled retail estimates are observation-only and NEVER set anomaly_flag = true."""
    cur = anomaly_db.cursor()
    # Create a 10-day history with a huge retail price spike on day 10
    base_date = date(2026, 9, 20)
    for i in range(10):
        d = base_date + timedelta(days=i)
        ws_p = 20.0
        # Modeled price has a 500% spike
        rs_p = 25.0 if i < 9 else 150.0
        cur.execute("INSERT INTO wholesale_price (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit) "
                    "VALUES (1, 1, 'Red', %s, %s, %s, %s, 'kg')", (d, ws_p-2, ws_p+2, ws_p))
        cur.execute("INSERT INTO retail_price (crop_id, city, platform, price_date, price, unit, data_type) "
                    "VALUES (1, 'Ahmedabad', 'Blinkit', %s, %s, 'kg', 'modeled')", (d, rs_p))

    total, flagged = rad.run_sql_anomaly_detection(DB_URL)
    assert total == 10
    assert flagged == 0  # 0 flagged because all rows are modeled!

    cur.execute("SELECT anomaly_flag, retail_data_type, anomaly_reason FROM price_anomaly WHERE price_date = '2026-09-29'")
    row = cur.fetchone()
    assert row[0] is False
    assert row[1] == "modeled"
    assert "Modeled retail estimate" in row[2]


@needs_db
def test_low_history_not_silently_dropped(anomaly_db):
    """Verify Rule (d): Days with low history (<5 days) are not silently dropped, but flagged with low confidence."""
    cur = anomaly_db.cursor()
    # Only 2 days of data
    for i in range(2):
        d = date(2026, 10, 1) + timedelta(days=i)
        cur.execute("INSERT INTO wholesale_price (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit) "
                    "VALUES (1, 1, 'Red', %s, 20, 24, 22, 'kg')", (d,))
        cur.execute("INSERT INTO retail_price (crop_id, city, platform, price_date, price, unit, data_type) "
                    "VALUES (1, 'Ahmedabad', 'Blinkit', %s, 35, 'kg', 'scraped')", (d,))

    total, _ = rad.run_sql_anomaly_detection(DB_URL)
    assert total == 2  # Both days present, not dropped!

    cur.execute("SELECT confidence_level FROM price_anomaly")
    levels = [r[0] for r in cur.fetchall()]
    assert all(lvl in ("low", "insufficient_history") for lvl in levels)


@needs_db
def test_robust_mad_runner(anomaly_db):
    """Verify robust MAD anomaly scoring runs and populates price_anomaly."""
    cur = anomaly_db.cursor()
    base_date = date(2026, 9, 20)
    for i in range(8):
        d = base_date + timedelta(days=i)
        ws_p = 20.0
        rs_p = 30.0 if i < 7 else 80.0  # Spikes on last day
        cur.execute("INSERT INTO wholesale_price (crop_id, mandi_id, variety, price_date, min_price, max_price, modal_price, unit) "
                    "VALUES (1, 1, 'Red', %s, %s, %s, %s, 'kg')", (d, ws_p-2, ws_p+2, ws_p))
        cur.execute("INSERT INTO retail_price (crop_id, city, platform, price_date, price, unit, data_type) "
                    "VALUES (1, 'Ahmedabad', 'Blinkit', %s, %s, 'kg', 'scraped')", (d, rs_p))

    total, flagged = rad.run_robust_mad_anomaly_detection(DB_URL, min_history=5)
    assert total == 8
    assert flagged >= 1  # Last day's spike detected by MAD
