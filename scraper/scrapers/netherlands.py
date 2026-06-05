"""Netherlands scraper via KVK (Kamer van Koophandel) API.
Free API key required: https://developers.kvk.nl/
Revenue: NOT available via KVK — employee proxy used.
Directors: Available via KVK (no birth year).
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

KVK_SEARCH_URL = "https://api.kvk.nl/api/v1/naamgevingen/zoeken"
KVK_PROFILE_URL = "https://api.kvk.nl/api/v1/basisprofielen"

# SBI codes (Dutch equivalent of NACE) for large companies
TARGET_SBI_CODES = [
    "0510", "0610", "0710",
    "1011", "1051", "1071", "1091",
    "1920", "2011", "2012", "2013", "2014",
    "2020", "2030", "2110", "2120",
    "2410", "2420", "2431",
    "2511", "2512", "2611", "2620",
    "2630", "2640", "2711", "2712",
    "2811", "2812", "2910",
    "3511", "3512", "3513",
    "4110", "4120", "4211",
    "4511", "4519",
    "4610", "4620", "4630", "4640",
    "4650", "4660", "4670", "4690",
    "4711", "4721", "4730",
    "4910", "4920", "4941", "5110",
    "5210", "5221", "5222",
    "5811", "5812",
    "6110", "6120", "6190",
    "6201", "6202", "6209",
    "6419", "6491", "6499",
    "6511", "6512",
    "6810", "6820",
    "6910", "6920", "7010",
    "7111", "7112",
    "8610", "8621", "8622",
]


class NetherlandsScraper(BaseScraper):
    name = "kvk_nl"
    country = "NL"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        api_key = self.config.get("KVK_API_KEY")
        if not api_key:
            logger.warning("[NL] No KVK_API_KEY set — skipping Netherlands scraper")
            return

        headers = {"apikey": api_key}
        checkpoint = self.load_checkpoint() if resume else {}
        min_employees = self.config.get("MIN_EMPLOYEES_PROXY", 500)
        done_sbi = set(checkpoint.get("done_sbi_nl", []))

        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            for sbi in TARGET_SBI_CODES:
                if sbi in done_sbi:
                    continue

                page = checkpoint.get(f"nl_page_{sbi}", 1)
                yielded = 0

                while True:
                    params = {
                        "type": "hoofdvestiging",
                        "sbiCode": sbi,
                        "pagina": page,
                        "aantal": 100,
                    }
                    data = await safe_get(client, KVK_SEARCH_URL, params=params)
                    if not data:
                        break

                    items = data.get("resultaten", []) or []
                    if not items:
                        break

                    for item in items:
                        record = await self._enrich(client, item, min_employees, sbi)
                        if record:
                            yield record
                            yielded += 1
                        await asyncio.sleep(0.2)

                    total = data.get("totaal", 0)
                    logger.info(f"[NL] SBI {sbi} page {page} — {yielded} qualifying / {total} total")

                    if len(items) < 100 or page * 100 >= total:
                        break
                    page += 1
                    checkpoint[f"nl_page_{sbi}"] = page
                    self.save_checkpoint(checkpoint)
                    await asyncio.sleep(0.5)

                done_sbi.add(sbi)
                checkpoint["done_sbi_nl"] = list(done_sbi)
                self.save_checkpoint(checkpoint)

    async def _enrich(self, client: httpx.AsyncClient, item: dict,
                       min_employees: int, sbi: str) -> CompanyRecord | None:
        try:
            kvk_number = item.get("kvkNummer", "")
            if not kvk_number:
                return None

            # Fetch full profile
            profile_data = await safe_get(client, f"{KVK_PROFILE_URL}/{kvk_number}")
            await asyncio.sleep(0.2)

            if not profile_data:
                # Use search result data only
                profile_data = item

            name = (profile_data.get("naam") or profile_data.get("handelsnaam") or
                    item.get("naam") or "")
            if not name:
                return None

            # Employee count
            employees = None
            for key in ["totaalWerkzamePersonen", "werkzamePersonen", "aantalWerkzamePersonen"]:
                val = profile_data.get(key)
                if val is not None:
                    try:
                        employees = int(val)
                        break
                    except Exception:
                        pass

            if employees is not None and employees < min_employees:
                return None

            # Address
            adres = {}
            for addr_type in ["bezoekadres", "correspondentieadres"]:
                adres = profile_data.get(addr_type, {}) or {}
                if adres:
                    break

            city = adres.get("plaats") or adres.get("woonplaats")
            address = adres.get("volledigAdres") or adres.get("straatnaam")
            postal = adres.get("postcode")

            # SBI / sector
            sbi_raw = None
            sbi_desc = None
            for sbi_field in ["sbiActiviteiten", "activiteiten"]:
                acts = profile_data.get(sbi_field, []) or []
                if acts:
                    first = acts[0] if isinstance(acts, list) else {}
                    sbi_raw = first.get("sbiCode") or first.get("code")
                    sbi_desc = first.get("sbiOmschrijving") or first.get("omschrijving")
                    break

            if not sbi_raw:
                sbi_raw = sbi
            nace = normalize_code(f"{sbi_raw[:2]}.{sbi_raw[2:]}" if len(str(sbi_raw)) >= 4 else sbi_raw)

            # Directors (KVK doesn't provide birth years in basic profile)
            directors = []
            for officer in profile_data.get("functionarissen", []) or []:
                fname = (officer.get("naam") or
                         f"{officer.get('voornaam','')} {officer.get('achternaam','')}".strip())
                if fname:
                    directors.append(DirectorRecord(
                        name=fname,
                        role=officer.get("functietitel") or officer.get("functie"),
                        birth_year=None,
                    ))

            creation_date = (profile_data.get("datumOprichting") or
                             profile_data.get("startdatum") or
                             item.get("datumOprichting"))

            return CompanyRecord(
                name=name,
                country="NL",
                registration_number=kvk_number,
                revenue_eur=None,
                revenue_estimated=True,
                employees=employees,
                sector=code_to_sector_label(nace) or sbi_desc,
                nace_code=nace,
                activity_description=sbi_desc,
                creation_date=str(creation_date) if creation_date else None,
                address=address,
                city=city,
                postal_code=postal,
                website=profile_data.get("websites", [None])[0] if profile_data.get("websites") else None,
                source_url=f"https://www.kvk.nl/orderstraat/bedrijf-kiezen/?kvknummer={kvk_number}",
                directors=directors,
            )

        except Exception as e:
            logger.error(f"[NL] Error enriching {item.get('kvkNummer')}: {e}")
            return None
