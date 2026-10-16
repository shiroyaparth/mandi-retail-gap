"""Multi-source ingestion adapters for KrishiPulse wholesale mandi prices.
"""
from .base import BaseSourceAdapter, NormalizedWholesaleRecord
from .csv_import import CsvImportAdapter
from .datagov_api import DataGovApiAdapter
from .agmarknet_api import AgmarknetApiAdapter

__all__ = [
    "BaseSourceAdapter",
    "NormalizedWholesaleRecord",
    "CsvImportAdapter",
    "DataGovApiAdapter",
    "AgmarknetApiAdapter",
]
