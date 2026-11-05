"""KrishiPulse Alerting Service.

Sends notifications (Telegram Bot / SMTP Email) when new price anomalies
or extreme retail spread spikes occur.

Environment Variables:
  TELEGRAM_BOT_TOKEN: Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID: Target user or channel chat ID
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / ALERT_EMAIL: Optional email alerts

Usage:
    python pipeline/alerts.py --dry-run
    python pipeline/alerts.py
"""
import argparse
import logging
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()
log = logging.getLogger("alerts")


def fetch_recent_anomalies(db_url: str, days_back: int = 1, connect=None) -> List[Dict[str, Any]]:
    conn = (connect or psycopg2.connect)(db_url)
    cur = conn.cursor()
    query = """
        SELECT
            c.crop_name,
            a.district,
            a.city,
            a.price_date,
            a.wholesale_price,
            a.retail_price,
            a.wedge_pct,
            a.wholesale_zscore,
            a.retail_zscore,
            a.confidence_level,
            a.retail_data_type,
            a.anomaly_reason
        FROM price_anomaly a
        JOIN crop_master c ON c.crop_id = a.crop_id
        WHERE a.anomaly_flag = true
          AND a.price_date >= CURRENT_DATE - INTERVAL '%s days'
        ORDER BY a.price_date DESC, ABS(a.wedge_pct) DESC
    """
    cur.execute(query, (days_back,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


def format_telegram_alert(anomalies: List[Dict[str, Any]]) -> str:
    if not anomalies:
        return ""
    lines = [
        "🌾 <b>KrishiPulse Price Anomaly Alert</b>",
        f"📅 Date: {date.today().strftime('%d %b %Y')}",
        f"🚨 <b>{len(anomalies)} Anomaly Event(s) Detected</b> in Gujarat:\n",
    ]
    for a in anomalies[:8]:  # Limit message length
        crop = a["crop_name"]
        city = a["city"]
        dist = a["district"]
        ws = f"₹{float(a['wholesale_price']):.2f}"
        rs = f"₹{float(a['retail_price']):.2f}"
        wedge = f"{float(a['wedge_pct']):+.1f}%"
        reason = a.get("anomaly_reason") or "Price spike detected"
        conf = a.get("confidence_level", "high").upper()

        lines.append(
            f"• <b>{crop}</b> ({city} / {dist}):"
            f"\n   Wholesale: {ws}/kg | Retail: {rs}/kg | Wedge: <b>{wedge}</b>"
            f"\n   Reason: <i>{reason}</i> [Confidence: {conf}]"
        )

    if len(anomalies) > 8:
        lines.append(f"\n<i>...and {len(anomalies) - 8} more. View dashboard for full report.</i>")

    lines.append("\n📊 <i>Wholesale Source: agmarknet.gov.in (MoA&FW)</i>")
    return "\n".join(lines)


def send_pipeline_failure_alert(source: str, error_msg: str, dry_run: bool = False) -> bool:
    """Dispatches immediate notification when an automated ingestion source encounters an error."""
    now_str = datetime.now().strftime('%d %b %Y %H:%M')
    message = (
        "⚠️ <b>KrishiPulse Pipeline Warning: Wholesale Ingestion</b>\n\n"
        f"📅 Timestamp: {now_str}\n"
        f"📡 Source: <code>{source}</code>\n"
        f"❌ Status: <b>Degraded / Failed</b>\n"
        f"🔍 Error: <code>{error_msg[:300]}</code>\n\n"
        "🛡️ <i>Safety rule applied: Historical data preserved. Downstream retail & anomaly analysis continued.</i>\n"
        "💡 <i>Action: Check api.data.gov.in status or drop an Agmarknet report into data_inbox/.</i>"
    )
    if dry_run:
        print("\n=== DRY RUN FAILURE ALERT PREVIEW ===")
        try:
            print(message)
        except UnicodeEncodeError:
            print(message.encode("ascii", errors="replace").decode("ascii"))
        print("=====================================\n")
        return True

    tg = send_telegram(message)
    em = send_email("⚠️ KrishiPulse Wholesale Ingestion Degraded", f"<pre>{message}</pre>")
    return tg or em


def send_telegram(message: str, bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not target:
        log.warning("Telegram alerts skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": target,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            log.info("Telegram alert sent successfully.")
            return True
        else:
            log.error("Telegram API error (%d): %s", res.status_code, res.text)
            return False
    except Exception as exc:
        log.error("Failed to connect to Telegram API: %s", exc)
        return False


def send_email(subject: str, html_body: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    to_email = os.getenv("ALERT_EMAIL", "").strip()

    if not all([smtp_host, smtp_user, smtp_pass, to_email]):
        log.info("Email alerting skipped (SMTP credentials not configured).")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        log.info("Email alert dispatched to %s.", to_email)
        return True
    except Exception as exc:
        log.error("Failed to send SMTP email: %s", exc)
        return False


def run_alerts(db_url: Optional[str] = None, dry_run: bool = False, days_back: int = 1, connect=None) -> int:
    db_url = db_url or os.getenv("DATABASE_URL", "").strip()
    if not dry_run and not db_url:
        log.error("DATABASE_URL not set.")
        return 1

    anomalies = []
    if db_url:
        try:
            anomalies = fetch_recent_anomalies(db_url, days_back=days_back, connect=connect)
        except Exception as exc:
            log.error("Error fetching anomalies: %s", exc)
            return 1

    if not anomalies:
        log.info("No new anomalies flagged in the last %d day(s). No alert necessary.", days_back)
        return 0

    message = format_telegram_alert(anomalies)

    if dry_run:
        print("\n=== DRY RUN ALERT PREVIEW ===")
        try:
            print(message)
        except UnicodeEncodeError:
            print(message.encode("ascii", errors="replace").decode("ascii"))
        print("=============================\n")
        return 0

    tg_ok = send_telegram(message)
    email_ok = send_email("🚨 KrishiPulse Agri-Price Anomaly Alert", f"<pre>{message}</pre>")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send price anomaly alerts via Telegram / Email.")
    parser.add_argument("--dry-run", action="store_true", help="Print formatted alert to stdout without sending")
    parser.add_argument("--days", type=int, default=1, help="Lookback window in days (default: 1)")
    parser.add_argument("--notify-failure", type=str, default=None, help="Send pipeline failure notification with error message")
    parser.add_argument("--source", type=str, default="wholesale", help="Source name for failure notification")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    if args.notify_failure:
        send_pipeline_failure_alert(source=args.source, error_msg=args.notify_failure, dry_run=args.dry_run)
        return 0

    return run_alerts(dry_run=args.dry_run, days_back=args.days)


if __name__ == "__main__":
    sys.exit(main())
