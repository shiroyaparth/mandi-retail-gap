"""Adapter for api.data.gov.in wholesale mandi price resource."""
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from .base import BaseSourceAdapter, NormalizedWholesaleRecord
import load_wholesale_data as lw

log = logging.getLogger(__name__)


class DataGovApiAdapter(BaseSourceAdapter):
    name = "datagov"

    def __init__(self, api_key: str, from_raw: bool = False):
        self.api_key = api_key
        self.from_raw = from_raw

    def fetch(self, **kwargs) -> List[NormalizedWholesaleRecord]:
        """Fetches wholesale records from data.gov.in (or raw cache) for all 5 crops."""
        crops = kwargs.get("crops", lw.CROPS)
        records = []
        today = date.today()

        for crop in crops:
            try:
                if self.from_raw:
                    raw_recs = lw.load_raw_records(crop)
                else:
                    raw_recs = lw.fetch_all_records(crop, self.api_key)
                    if raw_recs:
                        lw.save_raw(crop, raw_recs)
            except Exception as exc:
                log.error("data.gov.in error for %s: %s", crop, lw.redact(str(exc), self.api_key))
                raise

            if not raw_recs:
                log.warning("%s: 0 records returned from data.gov.in", crop)
                continue

            parsed_rows = []
            for rec in raw_recs:
                try:
                    p = lw.parse_record(rec, crop, today)
                    parsed_rows.append(p)
                except ValueError as err:
                    log.debug("Skipping invalid record for %s: %s", crop, err)

            aggregated, _ = lw.aggregate(parsed_rows)
            for r in aggregated:
                records.append(NormalizedWholesaleRecord(
                    crop=r["crop"],
                    state="Gujarat",
                    district=r["district"],
                    market=r["market"],
                    variety=r["variety"],
                    grade=r.get("grade", "FAQ"),
                    price_date=r["price_date"],
                    min_price=r["min"],
                    max_price=r["max"],
                    modal_price=r["modal"],
                    source=self.name,
                    is_provisional=r["provisional"]
                ))

        return records
