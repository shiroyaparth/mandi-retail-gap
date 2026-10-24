"""
KrishiPulse — Gujarat Mandi-Retail Price Gap Dashboard
Root entrypoint for Streamlit.
"""
import pathlib
import runpy
import sys

# Ensure repository root is in sys.path
ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

target_app = ROOT / "dashboard" / "app.py"
runpy.run_path(str(target_app), run_name="__main__")
