"""Test Agmarknet 2.0 from YOUR machine (read-only: no database, no files written).

Setup (once, inside your .venv):
    pip install agmarknet

Run:
    python tools\\test_agmarknet.py              # Onion, Gujarat, last 3 days
    python tools\\test_agmarknet.py Garlic 7     # another crop, last 7 days

Then copy EVERYTHING it prints and paste it back to Claude.
Data source: https://agmarknet.gov.in (must be acknowledged when the data is shown).
"""
import sys
from datetime import date, timedelta

crop = sys.argv[1] if len(sys.argv) > 1 else "Onion"
days = int(sys.argv[2]) if len(sys.argv) > 2 else 3

try:
    from agmarknet import Agmarknet
except ImportError:
    sys.exit("Package missing. Run:  pip install agmarknet")

end = date.today()
start = end - timedelta(days=days)
print(f"Requesting {crop} / Gujarat / {start} -> {end} ...")

api = Agmarknet(timeout=60, max_retries=2, request_delay=1.0)
try:
    df = api.report(commodity=crop, state="Gujarat", from_date=start, to_date=end,
                    data_type="price")
except Exception as exc:                                    # show the real reason
    print(f"\nFAILED: {type(exc).__name__}: {str(exc)[:400]}")
    for attr in ("status_code", "url", "response_text"):
        if getattr(exc, attr, None):
            print(f"  {attr}: {str(getattr(exc, attr))[:300]}")
    sys.exit(1)

print(f"\nOK: {len(df)} rows, {len(df.columns)} columns")
print("Columns:", list(df.columns))
if df.empty:
    print("Empty result (no arrivals in this window, or filters matched nothing).")
else:
    print("\nFirst 5 rows:")
    print(df.head(5).to_string())
    for col in ("unit_name_price", "arrival_date", "state_name"):
        if col in df.columns:
            print(f"\nDistinct {col}:", sorted(df[col].astype(str).unique())[:8])
    if "arrival_date" in df.columns:
        print("Distinct dates:", df["arrival_date"].nunique())
    if "market_name" in df.columns:
        print("Markets:", df["market_name"].nunique())