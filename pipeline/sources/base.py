"""Base adapter interface and common normalized record schema for wholesale sources."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class NormalizedWholesaleRecord:
    crop: str
    state: str
    district: Optional[str]
    market: str
    variety: str
    grade: str
    price_date: date
    min_price: float       # in Rs/kg
    max_price: float       # in Rs/kg
    modal_price: float     # in Rs/kg
    source: str            # e.g. 'agmarknet_csv', 'datagov', 'agmarknet_api'
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_provisional: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "crop": self.crop,
            "state": self.state,
            "district": self.district,
            "market": self.market,
            "variety": self.variety,
            "grade": self.grade,
            "price_date": self.price_date,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "modal_price": self.modal_price,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "is_provisional": self.is_provisional,
        }


class BaseSourceAdapter(ABC):
    """Abstract base class for all wholesale price source adapters."""
    name: str = "base"

    @abstractmethod
    def fetch(self, **kwargs) -> List[NormalizedWholesaleRecord]:
        """Fetch records from the source and return a list of normalized records.

        Must raise an exception or return an empty list; must never fabricate fake records.
        """
        raise NotImplementedError
