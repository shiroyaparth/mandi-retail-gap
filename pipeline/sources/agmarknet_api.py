"""Agmarknet 2.0 API adapter stub.

Verified on 16 Oct 2026:
Direct calls to Agmarknet 2.0 (POST /v1/daily-price-arrival/report) return
HTTP 400 {"detail": "Captcha key and captcha value are required.", "code": "TOKEN_OR_CAPTCHA_REQUIRED"}.

Per project rules and site terms of service:
- We do NOT bypass, solve, or automate CAPTCHAs.
- No keyless, non-CAPTCHA API endpoint currently exists for Agmarknet 2.0 prices.
- For Agmarknet data, use CsvImportAdapter with reports downloaded manually from agmarknet.gov.in.
"""
from typing import List
from .base import BaseSourceAdapter, NormalizedWholesaleRecord


class AgmarknetApiAdapter(BaseSourceAdapter):
    name = "agmarknet_api"

    def fetch(self, **kwargs) -> List[NormalizedWholesaleRecord]:
        raise NotImplementedError(
            "Agmarknet 2.0 API requires interactive CAPTCHA verification "
            "(HTTP 400 TOKEN_OR_CAPTCHA_REQUIRED). Bypassing or automating CAPTCHAs "
            "is prohibited. Please download reports directly from agmarknet.gov.in "
            "into data_inbox/ and use the 'agmarknet_csv' import adapter."
        )
