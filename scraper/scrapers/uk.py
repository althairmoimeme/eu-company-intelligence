"""UK scraper via Companies House API.
Free API key: https://developer.company-information.service.gov.uk/
Limit: 600 req / 5 min = 2 req/s.
Note: Revenue is NOT available via the CH API. We pull company identity + officers.
"""
import asyncio
import logging
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import sic_to_sector_label
from ..utils.http_client import safe_get

logger = logging.getLogger(__name__)

BASE_URL = "https://api.company-information.service.gov.uk"
PAGE_SIZE = 100

# SIC codes for large-company sectors (focus on B2B, exclude micro sectors)
TARGET_SIC_CODES = [
    "01110", "01120",  # Agriculture
    "06100",  # Oil
    "10110", "10200", "10910",  # Food manufacturing
    "13100", "14110", "14120",  # Textiles
    "20110", "20120", "20130", "20150", "20200", "20300",  # Chemicals
    "22110", "22210", "22220",  # Rubber/plastic
    "24100", "24200", "24340", "24420", "24510",  # Metals
    "25110", "25120", "25910",  # Fabricated metals
    "26110", "26200", "26300", "26400", "26511", "26600",  # Electronics
    "27110", "27200", "27400", "27510", "27520",  # Electrical equipment
    "28110", "28120", "28130", "28140", "28150", "28160", "28210", "28220",  # Machinery
    "29100", "29201", "29202",  # Motor vehicles
    "30110", "30120", "30200", "30300", "30400",  # Other transport
    "35110", "35120", "35130", "35140", "35210", "35220", "35230",  # Energy
    "36000",  # Water
    "38110", "38120", "38210", "38220", "38310", "38320",  # Waste
    "41100", "41201", "41202",  # Construction
    "42110", "42120", "42130", "42210", "42220",  # Civil engineering
    "45111", "45112", "45190", "45200", "45310", "45320",  # Motor trade
    "46110", "46120", "46130", "46140", "46150", "46160", "46170",  # Wholesale
    "46180", "46190", "46210", "46220", "46230", "46240", "46310",
    "46320", "46330", "46340", "46350", "46360", "46370", "46380",
    "46390", "46410", "46420", "46430", "46440", "46450", "46460",
    "46470", "46480", "46490", "46510", "46520", "46610", "46620",
    "46630", "46640", "46650", "46660", "46690", "46710", "46720",
    "46730", "46740", "46750", "46760", "46770", "46900",
    "49100", "49200", "49310", "49320", "49390", "49410", "49420",  # Transport
    "50100", "50200", "50300", "50400",  # Water transport
    "51100", "51210", "51220",  # Air transport
    "52100", "52211", "52212", "52213", "52219", "52220", "52230",  # Storage
    "52241", "52242", "52243", "52244", "52290",
    "58110", "58120", "58130", "58141", "58142", "58190", "58210", "58290",  # Publishing
    "59111", "59112", "59113", "59114", "59120", "59130", "59140",  # Media
    "61100", "61200", "61300", "61900",  # Telecoms
    "62011", "62012", "62020", "62030", "62090",  # IT
    "63110", "63120", "63910", "63990",  # Information services
    "64110", "64191", "64192", "64205", "64209",  # Banking
    "64301", "64302", "64303", "64304", "64305", "64306",  # Investment
    "64910", "64920", "64991", "64992", "64999",
    "65110", "65120", "65201", "65202", "65210", "65300",  # Insurance
    "66110", "66120", "66190", "66210", "66220", "66290", "66300",  # Finance support
    "68100", "68201", "68202", "68209", "68310", "68320",  # Real estate
    "69101", "69102", "69109", "69201", "69202", "69203", "69209",  # Professional
    "70100", "70221", "70229",  # Management consulting
    "71111", "71112", "71120",  # Architecture/engineering
    "72110", "72190", "72200",  # R&D
    "73110", "73120", "73200",  # Advertising
    "74100", "74201", "74202", "74203", "74204", "74209", "74300", "74901", "74902", "74909",
    "77110", "77120", "77210", "77220", "77291", "77299", "77310",  # Rental
    "77320", "77330", "77341", "77342", "77351", "77352", "77390", "77400",
    "78101", "78109", "78200", "78300",  # Employment
    "79110", "79120", "79901", "79909",  # Travel
    "80100", "80200", "80300",  # Security
    "81100", "81210", "81221", "81222", "81223", "81229", "81291", "81292", "81299", "81300",
    "82110", "82190", "82200", "82301", "82302", "82911", "82912", "82920", "82990",
    "86101", "86102", "86210", "86220", "86230", "86900",  # Healthcare
    "87100", "87200", "87300", "87900",
]


class UKScraper(BaseScraper):
    name = "companies_house_uk"
    country = "GB"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        api_key = self.config.get("COMPANIES_HOUSE_API_KEY")
        if not api_key:
            logger.warning("No COMPANIES_HOUSE_API_KEY set — skipping UK scraper")
            return

        auth = httpx.BasicAuth(api_key, "")
        checkpoint = self.load_checkpoint() if resume else {}
        start_index = checkpoint.get("start_index", 0)

        async with httpx.AsyncClient(auth=auth, timeout=30) as client:
            index = start_index
            while True:
                params = {
                    "company_status": "active",
                    "company_type": "plc,ltd",
                    "size": PAGE_SIZE,
                    "start_index": index,
                }
                data = await safe_get(client, f"{BASE_URL}/advanced-search/companies", params=params)
                if not data:
                    break

                items = data.get("items", [])
                if not items:
                    break

                for raw in items:
                    record = await self._parse_with_officers(client, raw)
                    if record:
                        yield record

                self.save_checkpoint({"start_index": index + PAGE_SIZE})
                logger.info(f"[GB] index {index} — {len(items)} companies")

                if len(items) < PAGE_SIZE:
                    break

                index += PAGE_SIZE
                await asyncio.sleep(0.6)  # respect 2 req/s limit

    async def _parse_with_officers(self, client: httpx.AsyncClient, raw: dict) -> CompanyRecord | None:
        try:
            number = raw.get("company_number", "")
            sic_codes = raw.get("sic_codes", [])
            sic = sic_codes[0] if sic_codes else None

            # Fetch officers
            directors = []
            officers_data = await safe_get(client, f"{BASE_URL}/company/{number}/officers",
                                            params={"items_per_page": 10})
            await asyncio.sleep(0.5)

            if officers_data:
                for officer in officers_data.get("items", []):
                    if officer.get("resigned_on"):
                        continue
                    role = officer.get("officer_role", "")
                    if role not in ("director", "corporate-director", "nominee-director",
                                    "managing-officer"):
                        continue
                    dob = officer.get("date_of_birth", {}) or {}
                    birth_year = dob.get("year")
                    directors.append(DirectorRecord(
                        name=officer.get("name", ""),
                        role=role.replace("-", " ").title(),
                        birth_year=int(birth_year) if birth_year else None,
                    ))

            address = raw.get("registered_office_address", {}) or {}
            city = address.get("locality") or address.get("region")
            addr_parts = [address.get("address_line_1"), address.get("address_line_2")]
            addr = ", ".join(p for p in addr_parts if p)

            return CompanyRecord(
                name=raw.get("company_name", ""),
                country="GB",
                registration_number=number,
                revenue_eur=None,           # not available via CH API
                revenue_estimated=False,
                sector=sic_to_sector_label(sic),
                nace_code=None,
                activity_description=", ".join(raw.get("sic_codes", [])),
                creation_date=raw.get("date_of_creation"),
                address=addr or None,
                city=city,
                postal_code=address.get("postal_code"),
                source_url=f"https://find-and-update.company-information.service.gov.uk/company/{number}",
                directors=directors,
            )
        except Exception as e:
            logger.error(f"[GB] Parse error for {raw.get('company_number')}: {e}")
            return None
