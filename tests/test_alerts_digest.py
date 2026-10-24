"""Tests for alerts and weekly digest generation (Stage 6)."""
import os
import pathlib
import sys
from datetime import date, timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

import pipeline.alerts as alerts
import pipeline.generate_weekly_digest as digest

DB_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def test_format_telegram_alert():
    sample_anomalies = [
        {
            "crop_name": "Onion",
            "city": "Ahmedabad",
            "district": "Ahmedabad",
            "price_date": date(2026, 10, 10),
            "wholesale_price": 18.0,
            "retail_price": 40.0,
            "wedge_pct": 122.2,
            "wholesale_zscore": 2.4,
            "retail_zscore": 2.1,
            "confidence_level": "high",
            "retail_data_type": "scraped",
            "anomaly_reason": "Synchronized surge: wholesale and retail spike >2σ",
        }
    ]
    msg = alerts.format_telegram_alert(sample_anomalies)
    assert "KrishiPulse Price Anomaly Alert" in msg
    assert "Onion" in msg
    assert "₹18.00/kg" in msg
    assert "₹40.00/kg" in msg
    assert "+122.2%" in msg
    assert "agmarknet.gov.in" in msg


def test_telegram_skipped_without_token():
    assert alerts.send_telegram("hello", bot_token="", chat_id="") is False


@needs_db
def test_digest_and_alerts_dry_run(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)

    # Test alerts dry run
    code_alerts = alerts.run_alerts(db_url=DB_URL, dry_run=True)
    assert code_alerts == 0

    # Test digest dry run
    code_digest = digest.run_digest(db_url=DB_URL, dry_run=True)
    assert code_digest == 0
