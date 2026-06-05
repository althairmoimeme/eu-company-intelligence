"""Belgium scraper via OpenCorporates BE jurisdiction.
CBEAPI and OpenCorporates provide identity data only (no revenue).
Free key: https://opencorporates.com/api_accounts/new
"""
import asyncio
import logging
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label
from ..utils.http_client import safe_get

logger = logging.getLogger(__name__)

OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
PAGE_SIZE = 100


class BelgiumScraper(BaseScraper):
    name = "opencorporates_be"
    country = "BE"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        api_token = self.config.get("OPENCORPORATES_API_TOKEN")
        if not api_token:
            logger.warning("[BE] No OPENCORPORATES_API_TOKEN — skipping Belgium scraper")
            return

        checkpoint = self.load_checkpoint() if resume else {}
        start_page = checkpoint.get("page", 1)

        async with httpx.AsyncClient(timeout=30) as client:
            page = start_page
            while True:
                params = {
                    "jurisdiction_code": "be",
                    "current_status": "Active",
                    "inactive": "false",
                    "per_page": PAGE_SIZE,
                    "page": page,
                    "api_token": api_token,
                }
                data = await safe_get(client, OC_URL, params=params)
                if not data:
                    break

                companies_data = data.get("results", {}).get("companies", [])
                if not companies_data:
                    break

                for item in companies_data:
                    record = self._parse(item.get("company", {}))
                    if record:
                        yield record

                self.save_checkpoint({"page": page + 1})
                logger.info(f"[BE] Page {page} — {len(companies_data)} companies")

                if len(companies_data) < PAGE_SIZE:
                    break
                page += 1
                await asyncio.sleep(2)  # 50 req/day free tier = very conservative

    def _parse(self, raw: dict) -> CompanyRecord | None:
        try:
            number = raw.get("company_number", "")
            industry_codes = raw.get("industry_codes", []) or []
            nace_raw = industry_codes[0].get("code") if industry_codes else None
            nace = normalize_code(nace_raw)

            directors = []
            for officer in raw.get("officers", []) or []:
                o = officer.get("officer", {}) or {}
                directors.append(DirectorRecord(
                    name=o.get("name", ""),
                    role=o.get("position"),
                    birth_year=None,
                ))

            addr = raw.get("registered_address", {}) or {}

            return CompanyRecord(
                name=raw.get("name", ""),
                country="BE",
                registration_number=number,
                revenue_eur=None,
                revenue_estimated=True,
                sector=code_to_sector_label(nace),
                nace_code=nace,
                creation_date=raw.get("incorporation_date"),
                address=addr.get("street_address"),
                city=addr.get("locality"),
                postal_code=addr.get("postal_code"),
                source_url=raw.get("opencorporates_url"),
                directors=directors,
            )
        except Exception as e:
            logger.error(f"[BE] Parse error: {e}")
            return None
