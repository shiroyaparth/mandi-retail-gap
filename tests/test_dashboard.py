"""Tests for dashboard functions and error caching prevention (Stage 5)."""
import os
import pathlib
import sys

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

DB_URL = os.getenv("TEST_DATABASE_URL")
if DB_URL:
    os.environ["DATABASE_URL"] = DB_URL

import dashboard.app as dash

needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def test_formatting_functions():
    assert dash.currency_fmt(25.5) == "₹25.50"
    assert dash.currency_fmt(None) == "—"
    assert dash.number_fmt(2.345) == "2.35"
    assert dash.number_fmt(None) == "—"
    assert dash.pct_fmt(15.2) == "+15.20%"
    assert dash.pct_fmt(-5.1) == "-5.10%"


def test_normalize_district():
    assert dash.normalize_district("Vadodara(Baroda)") == "Vadodara"
    assert dash.normalize_district("Kachchh") == "Kutch"
    assert dash.normalize_district("Rajkot") == "Rajkot"


def test_run_query_does_not_cache_exceptions(monkeypatch):
    """Verify that if an error occurs, run_query returns an empty DataFrame and does not poison cache."""
    call_count = 0

    def fail_connect():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Transient connection refused")

    monkeypatch.setattr(dash, "get_connection", fail_connect)

    # First call fails
    df1 = dash.run_query("SELECT 1")
    assert df1.empty
    assert call_count == 1

    # Second call must re-attempt (not cached as success!)
    df2 = dash.run_query("SELECT 1")
    assert df2.empty
    assert call_count == 2  # Re-attempted, error was NOT cached!


@needs_db
def test_data_health_query(monkeypatch):
    monkeypatch.setattr(dash, "get_database_url", lambda: DB_URL)
    health = dash.get_data_health()
    assert "wholesale_count" in health
    assert "retail_count" in health
    assert "anomaly_count" in health
    assert "recent_logs" in health
