"""Norway scraper via Brønnøysund Register Centre.
Both endpoints are free, no authentication required.
Entity register: https://data.brreg.no/enhetsregisteret/api/enheter
Accounts register: https://data.brreg.no/regnskapsregisteret/regnskap
"""
import asyncio
import logging
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label
from ..enrichers.currency import to_eur
from ..utils.http_client import safe_get

logger = logging.getLogger(__name__)

ENTITY_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap"
ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller"
PAGE_SIZE = 100


class NorwayScraper(BaseScraper):
    name = "brreg_no"
    country = "NO"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        min_employees = self.config.get("MIN_EMPLOYEES_PROXY", 200)
        checkpoint = self.load_checkpoint() if resume else {}
        start_page = checkpoint.get("page", 0)

        async with httpx.AsyncClient(timeout=30) as client:
            page = start_page
            while True:
                params = {
                    "fraAntallAnsatte": min_employees,
                    "konkurs": "false",
                    "underAvvikling": "false",
                    "size": PAGE_SIZE,
                    "page": page,
                }
                data = await safe_get(client, ENTITY_URL, params=params)
                if not data:
                    break

                embedded = data.get("_embedded", {})
                items = embedded.get("enheter", [])
                if not items:
                    break

                for raw in items:
                    record = await self._enrich(client, raw)
                    if record:
                        yield record

                self.save_checkpoint({"page": page + 1})
                logger.info(f"[NO] Page {page} — {len(items)} companies")

                page_info = data.get("page", {})
                if page >= page_info.get("totalPages", 0) - 1:
                    break
                if len(items) < PAGE_SIZE:
                    break

                page += 1
                await asyncio.sleep(0.2)

    async def _enrich(self, client: httpx.AsyncClient, raw: dict) -> CompanyRecord | None:
        try:
            orgnr = raw.get("organisasjonsnummer", "")
            nace_obj = raw.get("naeringskode1", {}) or {}
            nace_raw = nace_obj.get("kode")
            nace = normalize_code(nace_raw)

            # Try to get financial data — use path param, not query param
            revenue_eur = None
            revenue_year = None
            accounts = await safe_get(client, f"{ACCOUNTS_URL}/{orgnr}")
            await asyncio.sleep(0.15)

            if accounts:
                # API returns either a list or a single object
                acct_list = accounts if isinstance(accounts, list) else [accounts]
                if acct_list:
                    latest = acct_list[0]
                    currency = latest.get("valuta", "NOK")
                    # Revenue: sumDriftsinntekter (total operating revenue)
                    rev_raw = (
                        latest.get("resultatregnskapResultat", {})
                              .get("driftsresultat", {})
                              .get("driftsinntekter", {})
                              .get("sumDriftsinntekter")
                    )
                    if rev_raw and float(rev_raw) > 0:
                        revenue_eur = await to_eur(float(rev_raw), currency)
                        revenue_year = latest.get("regnskapsperiode", {}).get("fraDato", "")[:4]
                        if revenue_year:
                            revenue_year = int(revenue_year)

            # Fetch roles/directors
            directors = []
            roles_data = await safe_get(client, ROLES_URL.format(orgnr=orgnr))
            await asyncio.sleep(0.15)

            if roles_data:
                for role_group in roles_data.get("rollegrupper", []):
                    role_label = role_group.get("type", {}).get("beskrivelse", "")
                    for role in role_group.get("roller", []):
                        person = role.get("person", {}) or {}
                        navn = person.get("navn", {}) or {}
                        full_name = f"{navn.get('fornavn', '')} {navn.get('etternavn', '')}".strip()
                        if not full_name:
                            continue
                        birth_date = person.get("fodselsdato", "")
                        birth_year = int(birth_date[:4]) if birth_date and len(birth_date) >= 4 else None
                        directors.append(DirectorRecord(
                            name=full_name,
                            role=role_label,
                            birth_year=birth_year,
                        ))

            addr = raw.get("forretningsadresse", {}) or {}
            addr_lines = addr.get("adresse", []) or []
            address = ", ".join(addr_lines) if addr_lines else None

            min_revenue = self.config.get("MIN_REVENUE_EUR", 75_000_000)
            # If we have revenue and it's below threshold, skip
            if revenue_eur is not None and revenue_eur < min_revenue:
                return None

            return CompanyRecord(
                name=raw.get("navn", ""),
                country="NO",
                registration_number=orgnr,
                revenue_eur=revenue_eur,
                revenue_year=revenue_year,
                revenue_estimated=(revenue_eur is None),
                employees=raw.get("antallAnsatte"),
                sector=code_to_sector_label(nace) or nace_obj.get("beskrivelse"),
                nace_code=nace,
                activity_description=" ".join(raw.get("vedtektsfestetFormaal") or []) or nace_obj.get("beskrivelse"),
                creation_date=raw.get("stiftelsesdato"),
                address=address,
                city=addr.get("poststed"),
                postal_code=addr.get("postnummer"),
                source_url=f"https://www.brreg.no/foretak/oppslag/?orgnr={orgnr}",
                directors=directors,
            )
        except Exception as e:
            logger.error(f"[NO] Error enriching {raw.get('organisasjonsnummer')}: {e}")
            return None
