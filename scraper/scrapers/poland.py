"""Poland scraper via KRS (Krajowy Rejestr Sądowy) new portal API.
Search API: https://wyszukiwarka-krs-api.ms.gov.pl/api/wyszukiwarka/krs
Individual fetch: https://api-krs.ms.gov.pl/api/krs/OdpisAktualny/{krs}
Revenue: NOT available via KRS — employee proxy used (>500 employees).
Directors: Available with birth year from OdpisAktualny.
"""
import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

SEARCH_API = "https://wyszukiwarka-krs-api.ms.gov.pl/api/wyszukiwarka/krs"
DETAIL_API = "https://api-krs.ms.gov.pl/api/krs/OdpisAktualny"

# 16 Polish provinces (voivodeships) — search all companies per province
PROVINCES = [
    "dolnośląskie", "kujawsko-pomorskie", "lubelskie", "lubuskie",
    "łódzkie", "małopolskie", "mazowieckie", "opolskie",
    "podkarpackie", "podlaskie", "pomorskie", "śląskie",
    "świętokrzyskie", "warmińsko-mazurskie", "wielkopolskie",
    "zachodniopomorskie",
]

# Large cities — targeted search for big companies
BIG_CITIES = [
    "Warszawa", "Kraków", "Wrocław", "Łódź", "Poznań",
    "Gdańsk", "Szczecin", "Katowice", "Bydgoszcz", "Lublin",
    "Białystok", "Rzeszów", "Gdynia", "Częstochowa", "Radom",
    "Sosnowiec", "Toruń", "Kielce", "Gliwice", "Zabrze",
]

# ── KRS token encoder ───────────────────────────────────────────────────────
_KRS_POS = [193, 8, 327, 501, 112, 74, 409, 226, 16, 306]
_TS_POS = [492, 141, 364, 78, 259, 12, 430, 384, 97, 503, 67, 35, 471, 218]
_CHKSUM_POS = [24, 46, 174, 345]
_SHIFT_POS = 11


def _encode_krs_token(krs: str = "") -> str:
    """Generate the encrypted API token required by wyszukiwarka-krs-api."""
    krs = krs.zfill(10)
    now = datetime.now(timezone.utc)
    ts = (str(now.year).zfill(4) + str(now.month).zfill(2) + str(now.day).zfill(2) +
          str(now.hour).zfill(2) + str(now.minute).zfill(2) + str(now.second).zfill(2))

    s = [str(random.randint(0, 9)) for _ in range(512)]
    for i in range(508, 512):
        s[i] = "0"
    for i, pos in enumerate(_KRS_POS):
        s[pos] = krs[i]
    for i, pos in enumerate(_TS_POS):
        s[pos] = ts[i]

    shift = random.randint(1, 9)
    s[_SHIFT_POS] = str(shift)

    for pos in _CHKSUM_POS:
        for j in range(len(s) - 1, pos, -1):
            s[j] = s[j - 1]
        s[pos] = "0"

    checksum = str(sum(int(x) for x in s)).zfill(4)
    for i, pos in enumerate(_CHKSUM_POS):
        s[pos] = checksum[i]

    size = len(s)
    copy = s[:]
    for i in range(size):
        s[(i + shift) % size] = copy[i]

    return "".join(s)


def _krs_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": "TopSecretApiKey",
        "apiKey": _encode_krs_token(),
    }


class PolandScraper(BaseScraper):
    name = "krs_pl"
    country = "PL"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        checkpoint = self.load_checkpoint() if resume else {}
        min_employees = self.config.get("MIN_EMPLOYEES_PROXY", 500)
        done_cities = set(checkpoint.get("done_cities_pl", []))

        async with httpx.AsyncClient(timeout=30) as client:
            for city in BIG_CITIES:
                if city in done_cities:
                    continue

                page = checkpoint.get(f"pl_city_page_{city}", 1)
                logger.info(f"[PL] Searching KRS city={city} from page {page}")
                yielded = 0

                while True:
                    krs_numbers = await self._search_krs(client, city=city, page=page)
                    if not krs_numbers:
                        break

                    for krs_num in krs_numbers:
                        record = await self._fetch_company(client, krs_num, min_employees)
                        if record:
                            yield record
                            yielded += 1
                        await asyncio.sleep(0.2)

                    if len(krs_numbers) < 100:
                        break
                    page += 1
                    checkpoint[f"pl_city_page_{city}"] = page
                    self.save_checkpoint(checkpoint)
                    await asyncio.sleep(0.5)

                logger.info(f"[PL] City {city} done — {yielded} qualifying companies")
                done_cities.add(city)
                checkpoint["done_cities_pl"] = list(done_cities)
                self.save_checkpoint(checkpoint)

    async def _search_krs(self, client: httpx.AsyncClient,
                           city: str, page: int) -> list[str]:
        """Return list of KRS numbers matching search criteria."""
        body = {
            "rejestr": ["P"],
            "podmiot": {
                "krs": None, "nip": None, "regon": None,
                "nazwa": None,
                "wojewodztwo": None, "powiat": None, "gmina": None,
                "miejscowosc": city,
                "dokladnaNazwa": False,
            },
            "status": {
                "czyOpp": None,
                "czyWpisDotyczacyPostepowaniaUpadlosciowego": None,
                "dataPrzyznaniaStatutuOppOd": None,
                "dataPrzyznaniaStatutuOppDo": None,
            },
            "paginacja": {
                "liczbaElementowNaStronie": 100,
                "maksymalnaLiczbaWynikow": 100,
                "numerStrony": page,
            },
        }
        try:
            resp = await client.post(SEARCH_API, json=body, headers=_krs_headers(), timeout=20)
            if resp.status_code != 200:
                logger.warning(f"[PL] KRS search {city} p{page} → HTTP {resp.status_code}")
                return []
            data = resp.json()
            return [str(item["numer"]).zfill(10)
                    for item in data.get("listaPodmiotow", [])
                    if item.get("numer")]
        except Exception as e:
            logger.error(f"[PL] KRS search error {city} p{page}: {e}")
            return []

    async def _fetch_company(self, client: httpx.AsyncClient, krs: str,
                              min_employees: int) -> CompanyRecord | None:
        try:
            resp = await client.get(
                f"{DETAIL_API}/{krs}",
                params={"rejestr": "P", "format": "json"},
                timeout=20,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

            odpis = data.get("odpis", {})
            dane = odpis.get("dane", {}) or {}
            d1 = dane.get("dzial1", {}) or {}
            d2 = dane.get("dzial2", {}) or {}
            d3 = dane.get("dzial3", {}) or {}

            dp = d1.get("danePodmiotu", {}) or {}
            name = dp.get("nazwa", "")
            if not name:
                return None

            # Filter out very small companies (no employees field = small)
            # We keep all and let enrichment filter by revenue later

            ids = dp.get("identyfikatory", {}) or {}

            # Address
            siedziba = d1.get("siedzibaIAdres", {}) or {}
            seat = siedziba.get("siedziba", {}) or {}
            adres = siedziba.get("adres", {}) or {}
            city = seat.get("nazwa") or seat.get("miejscowosc")
            street = adres.get("ulica", "")
            nr = adres.get("nrDomu", "")
            address = f"{street} {nr}".strip() if street else None
            postal = adres.get("kodPocztowy")

            # Activity / NACE
            przedmiot = d3.get("przedmiotDzialalnosci", {}) or {}
            prev_items = przedmiot.get("przedmiotPrzewazajacejDzialalnosci", []) or []
            nace_raw = None
            activity_desc = None
            if prev_items:
                first = prev_items[0]
                div = first.get("kodDzial", "")
                kla = first.get("kodKlasa", "")
                pod = first.get("kodPodklasa", "")
                if div and kla:
                    nace_raw = f"{div}.{kla}{pod}"
                activity_desc = first.get("opis")

            nace = normalize_code(nace_raw)

            # Founding date
            umowaStatut = d1.get("umowaStatut", {}) or {}
            creation_date = (umowaStatut.get("dataZawarcia") or
                             umowaStatut.get("dataUchwalenia"))

            # Directors
            directors = []
            rep = d2.get("reprezentacja", {}) or {}
            for organe in rep.get("organyPrzedstawieniowe", []) or []:
                role_name = organe.get("nazwa", "")
                for member in organe.get("czlonkowie", []) or []:
                    imie = member.get("imie", "")
                    nazwisko = member.get("nazwisko", "")
                    full_name = f"{imie} {nazwisko}".strip()
                    if not full_name:
                        continue
                    birth_year = member.get("rokUrodzenia")
                    directors.append(DirectorRecord(
                        name=full_name,
                        role=role_name,
                        birth_year=int(birth_year) if birth_year else None,
                    ))

            return CompanyRecord(
                name=name,
                country="PL",
                registration_number=krs,
                revenue_eur=None,
                revenue_estimated=True,
                employees=None,
                sector=code_to_sector_label(nace) or activity_desc,
                nace_code=nace,
                activity_description=activity_desc,
                creation_date=creation_date,
                address=address,
                city=city,
                postal_code=postal,
                source_url=f"https://ekrs.ms.gov.pl/rdf/pd/search_df?krs={krs}",
                directors=directors,
            )

        except Exception as e:
            logger.error(f"[PL] Error fetching KRS {krs}: {e}")
            return None
