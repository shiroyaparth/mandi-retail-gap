"""CSV and Excel import adapter for Agmarknet manual downloads."""
import csv
import logging
import os
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dateutil import parser as date_parser

from .base import BaseSourceAdapter, NormalizedWholesaleRecord

log = logging.getLogger(__name__)

# Valid price bounds in Rs/kg
VALID_BOUNDS_PER_KG = {
    "Onion": (2.0, 200.0),
    "Potato": (2.0, 150.0),
    "Tomato": (2.0, 250.0),
    "Garlic": (10.0, 600.0),
    "Green Chilli": (5.0, 400.0),
}

CROP_CANONICAL_MAP = {
    "onion": "Onion",
    "pyaz": "Onion",
    "dungri": "Onion",
    "potato": "Potato",
    "batata": "Potato",
    "aloo": "Potato",
    "alu": "Potato",
    "tomato": "Tomato",
    "tameta": "Tomato",
    "tamatar": "Tomato",
    "garlic": "Garlic",
    "lahsun": "Garlic",
    "lasan": "Garlic",
    "green chilli": "Green Chilli",
    "green chili": "Green Chilli",
    "chilli green": "Green Chilli",
    "chili green": "Green Chilli",
    "green chilly": "Green Chilli",
    "hari mirch": "Green Chilli",
    "lili marchi": "Green Chilli",
    "chilli (green)": "Green Chilli",
}

COLUMN_SYNONYMS = {
    "state": ["state", "state_name", "state name"],
    "district": ["district", "district_name", "district name"],
    "market": ["market", "market_name", "market name", "mandi", "mandi_name", "apmc", "center", "centre"],
    "commodity": ["commodity", "cmdt_name", "cmdt", "crop", "crop_name", "commodity_name", "commodity name"],
    "variety": ["variety", "variety_name", "variety name"],
    "grade": ["grade", "grade_name", "grade name"],
    "arrival_date": ["arrival_date", "arrival date", "price_date", "price date", "date", "reported_date", "entry_date"],
    "min_price": ["min_price", "min price", "min", "min price (rs./quintal)", "min price(rs./quintal)",
                  "min_price_rs_quintal", "min price (rs/quintal)", "min price (rs/kg)", "min price (rs./kg)"],
    "max_price": ["max_price", "max price", "max", "max price (rs./quintal)", "max price(rs./quintal)",
                  "max_price_rs_quintal", "max price (rs/quintal)", "max price (rs/kg)", "max price (rs./kg)"],
    "modal_price": ["modal_price", "modal price", "modal", "model_price", "model price",
                    "modal price (rs./quintal)", "modal price(rs./quintal)", "modal_price_rs_quintal",
                    "modal price (rs/quintal)", "modal price (rs/kg)", "modal price (rs./kg)"],
    "unit": ["unit", "unit_name_price", "unit of price", "price_unit", "unit name"],
}


def clean_col_name(c: str) -> str:
    """Normalize column header: lowercase, stripped, special characters normalized."""
    s = str(c).strip().lower()
    s = re.sub(r"[\s_]+", " ", s)
    return s


def match_columns(columns: List[str]) -> Dict[str, str]:
    """Map canonical field name -> actual column name in the file."""
    mapping = {}
    col_lookup = {clean_col_name(c): c for c in columns}
    for field_name, synonyms in COLUMN_SYNONYMS.items():
        for syn in synonyms:
            clean_syn = clean_col_name(syn)
            if clean_syn in col_lookup:
                mapping[field_name] = col_lookup[clean_syn]
                break
    return mapping


def parse_date_value(val: Any) -> Optional[date]:
    """Parse a date value from string, datetime, or timestamp."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    if isinstance(val, date):
        return val

    s = str(val).strip()
    if not s:
        return None

    # Try common formats explicitly first for speed & accuracy
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    try:
        dt = date_parser.parse(s, dayfirst=True)
        return dt.date()
    except (ValueError, TypeError):
        return None


def determine_unit_factor(col_header: str, row_unit: Optional[str], raw_price: float, crop: str) -> float:
    """Determine multiplication factor to convert raw price to Rs/kg.

    Agmarknet wholesale standard is Rs./Quintal (1 Quintal = 100 kg), so factor is 0.01.
    If already Rs/kg, factor is 1.0.
    """
    col_str = str(col_header).lower()
    unit_str = str(row_unit or "").strip().lower()

    # 1. Check row unit explicitly
    if unit_str:
        if "tonne" in unit_str or "ton" in unit_str or "mt" in unit_str:
            return 0.001
        if "quintal" in unit_str or "qtl" in unit_str or unit_str == "q":
            return 0.01
        if "kg" in unit_str or "kilogram" in unit_str:
            return 1.0

    # 2. Check header text
    if "quintal" in col_str or "qtl" in col_str:
        return 0.01
    if "tonne" in col_str or "ton" in col_str:
        return 0.001
    if "/kg" in col_str or "(rs/kg)" in col_str or "(rs./kg)" in col_str:
        return 1.0

    # 3. Value-based auto-detection against crop bounds
    bounds = VALID_BOUNDS_PER_KG.get(crop, (1.0, 500.0))
    low, high = bounds
    if low <= raw_price <= high:
        return 1.0
    if raw_price > high and (low <= raw_price / 100 <= high):
        return 0.01
    if raw_price > high * 10 and (low <= raw_price / 1000 <= high):
        return 0.001

    # Default wholesale portal assumption: Rs./Quintal -> Rs/kg
    return 0.01


class CsvImportAdapter(BaseSourceAdapter):
    name = "agmarknet_csv"

    def __init__(self, inbox_dir: str = "data_inbox"):
        self.inbox_dir = Path(inbox_dir)
        self.processed_dir = self.inbox_dir / "processed"

    def find_inbox_files(self) -> List[Path]:
        if not self.inbox_dir.exists():
            return []
        patterns = ["*.csv", "*.xlsx", "*.xls"]
        files = []
        for p in patterns:
            files.extend(self.inbox_dir.glob(p))
        return sorted([f for f in files if f.is_file()])

    def read_tabular_file(self, filepath: Path) -> pd.DataFrame:
        """Reads CSV or Excel, auto-detecting header row if metadata rows precede it."""
        suffix = filepath.suffix.lower()
        if suffix == ".csv":
            # Detect header row: check first 5 rows for typical header names
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                sample_lines = [f.readline() for _ in range(10)]

            header_idx = 0
            for idx, line in enumerate(sample_lines):
                lower = line.lower()
                if "commodity" in lower or "market" in lower or "cmdt" in lower or "arrival" in lower:
                    header_idx = idx
                    break

            return pd.read_csv(filepath, skiprows=header_idx, dtype=str)
        elif suffix in (".xlsx", ".xls"):
            return pd.read_excel(filepath, dtype=str)
        else:
            raise ValueError(f"Unsupported file format: {suffix}")

    def parse_dataframe(self, df: pd.DataFrame, source_file_name: str) -> Tuple[List[NormalizedWholesaleRecord], Dict[str, int]]:
        col_map = match_columns(list(df.columns))
        required = ["market", "commodity", "modal_price", "arrival_date"]
        missing = [r for r in required if r not in col_map]
        if missing:
            raise ValueError(f"File {source_file_name} missing essential columns: {missing}. Found: {list(df.columns)}")

        records = []
        stats = {"total": len(df), "valid": 0, "rejected": 0, "wrong_state": 0, "unmapped_crop": 0}
        today = date.today()

        for _, row in df.iterrows():
            # State validation
            state_val = str(row.get(col_map.get("state", ""), "")).strip() if "state" in col_map else ""
            if state_val and state_val.lower() not in ("gujarat", "gujrat"):
                stats["wrong_state"] += 1
                stats["rejected"] += 1
                continue

            # Commodity validation
            raw_crop = str(row.get(col_map["commodity"], "")).strip()
            canonical_crop = CROP_CANONICAL_MAP.get(raw_crop.lower())
            if not canonical_crop:
                stats["unmapped_crop"] += 1
                stats["rejected"] += 1
                continue

            # Market & District
            market = str(row.get(col_map["market"], "")).strip()
            if not market:
                stats["rejected"] += 1
                continue
            district = str(row.get(col_map.get("district", ""), "")).strip() or None

            # Variety & Grade
            variety = str(row.get(col_map.get("variety", ""), "")).strip() or "Unknown"
            grade = str(row.get(col_map.get("grade", ""), "")).strip() or "FAQ"

            # Date
            raw_date = row.get(col_map["arrival_date"])
            parsed_date = parse_date_value(raw_date)
            if not parsed_date or parsed_date > today + timedelta(days=1):
                stats["rejected"] += 1
                continue

            # Price parsing
            try:
                raw_modal = float(str(row.get(col_map["modal_price"], "")).replace(",", "").strip())
                if raw_modal <= 0 or pd.isna(raw_modal):
                    stats["rejected"] += 1
                    continue
            except (ValueError, TypeError):
                stats["rejected"] += 1
                continue

            raw_unit = str(row.get(col_map.get("unit", ""), "")).strip() if "unit" in col_map else None
            factor = determine_unit_factor(col_map["modal_price"], raw_unit, raw_modal, canonical_crop)

            modal_kg = round(raw_modal * factor, 4)

            # Min and Max price (fallback to modal if absent or invalid)
            def parse_opt_price(col_key: str, default_val: float) -> float:
                if col_key in col_map:
                    try:
                        p = float(str(row.get(col_map[col_key], "")).replace(",", "").strip())
                        if p > 0 and not pd.isna(p):
                            return round(p * factor, 4)
                    except (ValueError, TypeError):
                        pass
                return default_val

            min_kg = parse_opt_price("min_price", modal_kg)
            max_kg = parse_opt_price("max_price", modal_kg)

            if min_kg > max_kg:
                min_kg, max_kg = max_kg, min_kg
            if modal_kg < min_kg or modal_kg > max_kg:
                min_kg = min(min_kg, modal_kg)
                max_kg = max(max_kg, modal_kg)

            # Bound check & provisional flag
            low, high = VALID_BOUNDS_PER_KG[canonical_crop]
            is_provisional = False
            if modal_kg < low or modal_kg > high:
                is_provisional = True
            if (today - parsed_date).days <= 3:
                is_provisional = True

            records.append(NormalizedWholesaleRecord(
                crop=canonical_crop,
                state="Gujarat",
                district=district,
                market=market,
                variety=variety,
                grade=grade,
                price_date=parsed_date,
                min_price=min_kg,
                max_price=max_kg,
                modal_price=modal_kg,
                source=self.name,
                is_provisional=is_provisional,
            ))
            stats["valid"] += 1

        return records, stats

    def deduplicate_and_aggregate(self, records: List[NormalizedWholesaleRecord]) -> List[NormalizedWholesaleRecord]:
        """Aggregate records sharing (crop, state, district, market, variety, price_date) across grades."""
        groups: Dict[Tuple, List[NormalizedWholesaleRecord]] = {}
        for r in records:
            key = (r.crop, r.state, r.district, r.market, r.variety, r.price_date)
            groups.setdefault(key, []).append(r)

        aggregated = []
        for key, group in groups.items():
            if len(group) == 1:
                aggregated.append(group[0])
            else:
                first = group[0]
                avg_min = round(sum(g.min_price for g in group) / len(group), 4)
                avg_max = round(sum(g.max_price for g in group) / len(group), 4)
                avg_modal = round(sum(g.modal_price for g in group) / len(group), 4)
                is_prov = any(g.is_provisional for g in group)
                all_grades = ", ".join(sorted(set(g.grade for g in group if g.grade)))
                aggregated.append(NormalizedWholesaleRecord(
                    crop=first.crop,
                    state=first.state,
                    district=first.district,
                    market=first.market,
                    variety=first.variety,
                    grade=all_grades or first.grade,
                    price_date=first.price_date,
                    min_price=avg_min,
                    max_price=avg_max,
                    modal_price=avg_modal,
                    source=first.source,
                    is_provisional=is_prov,
                ))
        return aggregated

    def move_to_processed(self, filepath: Path) -> Path:
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        dest = self.processed_dir / filepath.name
        if dest.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = self.processed_dir / f"{filepath.stem}_{ts}{filepath.suffix}"
        shutil.move(str(filepath), str(dest))
        return dest

    def fetch(self, **kwargs) -> List[NormalizedWholesaleRecord]:
        """Scans inbox_dir for files, parses them, aggregates, and returns normalized records."""
        specific_file = kwargs.get("file_path")
        files = [Path(specific_file)] if specific_file else self.find_inbox_files()
        if not files:
            log.info("No files found in inbox directory: %s", self.inbox_dir)
            return []

        all_records = []
        processed_files = []

        for f in files:
            log.info("Reading Agmarknet file: %s", f.name)
            try:
                df = self.read_tabular_file(f)
                records, stats = self.parse_dataframe(df, f.name)
                log.info("Parsed %s: %s records extracted, %s valid, %s rejected",
                         f.name, stats["total"], stats["valid"], stats["rejected"])
                all_records.extend(records)
                processed_files.append(f)
            except Exception as exc:
                log.error("Failed to parse file %s: %s (leaving file untouched)", f.name, exc)
                # Fail gracefully per file; never touch history

        aggregated = self.deduplicate_and_aggregate(all_records)
        return aggregated
