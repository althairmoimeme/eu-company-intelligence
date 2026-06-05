"""
Scraper d'annuaires B2B européens — ciblage Equans M&A.

Pays couverts : IT, AT, BE, NL, CH
Source principale : Europages (domaine local par pays)

URL patterns corrects (confirmés par ingénierie inverse) :
  IT  → https://www.europages.it/it/search/{kw}/country/italia
        paginer : /page/{N}
  BE  → https://www.europages.be/fr/search/{kw}/country/belgique
        paginer : /page/{N}
  NL  → https://www.europages.nl/nl/search/{kw}/country/nederland
        paginer : /page/{N}
  AT  → https://www.europages.de/unternehmen/{kw}.html  (filtre AT in-code)
        paginer : /unternehmen/pg-{N}/{kw}.html
  CH  → https://www.europages.de/unternehmen/{kw}.html  (filtre CH in-code)
        paginer : /unternehmen/pg-{N}/{kw}.html

Données extraites du JSON-LD "ItemList" (server-side rendu) :
  name, addressLocality, addressCountry, numberOfEmployees, url (Europages)

Codes NACE ciblés (Equans core business) :
  43.21 — Electrical Installation
  43.22 — HVAC
  43.29 — Building Automation / Fire Protection
  33.20 — Industrial Maintenance
  81.10 — Facility Management
"""
import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text as sa_text

from ..db.session import get_session_factory
from ..db.models import Company
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)

# ── Configuration URL par pays ────────────────────────────────────────────────

COUNTRY_URL_CONFIG: dict[str, dict] = {
    "IT": {
        "domain": "www.europages.it",
        "url_p1": "https://www.europages.it/it/search/{kw}/country/italia",
        "url_pN": "https://www.europages.it/it/search/{kw}/country/italia/page/{N}",
        "filter_country": "IT",
    },
    "BE": {
        "domain": "www.europages.be",
        "url_p1": "https://www.europages.be/fr/search/{kw}/country/belgique",
        "url_pN": "https://www.europages.be/fr/search/{kw}/country/belgique/page/{N}",
        "filter_country": "BE",
    },
    "NL": {
        "domain": "www.europages.nl",
        "url_p1": "https://www.europages.nl/nl/search/{kw}/country/nederland",
        "url_pN": "https://www.europages.nl/nl/search/{kw}/country/nederland/page/{N}",
        "filter_country": "NL",
    },
    # AT et CH redirigent vers europages.de → on scrape DE et filtre par pays
    "AT": {
        "domain": "www.europages.de",
        "url_p1": "https://www.europages.de/unternehmen/{kw}.html",
        "url_pN": "https://www.europages.de/unternehmen/pg-{N}/{kw}.html",
        "filter_country": "AT",
    },
    "CH": {
        "domain": "www.europages.de",
        "url_p1": "https://www.europages.de/unternehmen/{kw}.html",
        "url_pN": "https://www.europages.de/unternehmen/pg-{N}/{kw}.html",
        "filter_country": "CH",
    },
}

# ── Mots-clés par pays ────────────────────────────────────────────────────────
# Format : { country_code: [(nace_code, sector_label, keyword_encoded), ...] }
# Les keywords DOIVENT être dans la langue du domaine cible (voir COUNTRY_URL_CONFIG)

EQUANS_SEARCHES: dict[str, list[tuple[str, str, str]]] = {
    "IT": [
        # Electrical installation (IT)
        ("43.21", "Electrical Installation", "impianti-elettrici"),
        ("43.21", "Electrical Installation", "installazione-elettrica"),
        ("43.21", "Electrical Installation", "impianti-elettrici-industriali"),
        # HVAC (IT)
        ("43.22", "HVAC", "climatizzazione"),
        ("43.22", "HVAC", "impianti-termici"),
        ("43.22", "HVAC", "condizionamento-aria"),
        # Building Automation (IT)
        ("43.29", "Building Automation", "automazione-industriale"),
        ("43.29", "Building Automation", "automazione-edifici"),
        # Industrial Maintenance (IT)
        ("33.20", "Industrial Maintenance", "manutenzione-industriale"),
        ("33.20", "Industrial Maintenance", "manutenzione-impianti"),
        # Facility Management (IT)
        ("81.10", "Facility Management", "facility-management"),
        # Fire / Safety (IT)
        ("43.29", "Fire Protection", "impianti-antincendio"),
    ],

    "BE": [
        # Electrical installation (FR + NL)
        ("43.21", "Electrical Installation", "installation-electrique"),
        ("43.21", "Electrical Installation", "elektrotechniek"),
        # HVAC (FR + NL)
        ("43.22", "HVAC", "technique-du-batiment"),
        ("43.22", "HVAC", "gebouwtechniek"),
        # Building Automation (FR + NL)
        ("43.29", "Building Automation", "automatisation-industrielle"),
        ("43.29", "Building Automation", "industriele-automatisering"),
        # Facility Management
        ("81.10", "Facility Management", "facility-management"),
        # Fire Protection
        ("43.29", "Fire Protection", "brandbeveiliging"),
    ],

    "NL": [
        # Electrical installation (NL)
        ("43.21", "Electrical Installation", "elektrotechniek"),
        ("43.21", "Electrical Installation", "elektroinstallatie"),
        # HVAC (NL)
        ("43.22", "HVAC", "klimaattechniek"),
        ("43.22", "HVAC", "gebouwtechniek"),
        # Building Automation (NL)
        ("43.29", "Building Automation", "industriele-automatisering"),
        ("43.29", "Building Automation", "bouwautomatisering"),
        # Facility Management
        ("81.10", "Facility Management", "facilitaire-diensten"),
        # Fire Protection
        ("43.29", "Fire Protection", "brandbeveiliging"),
    ],

    # AT et CH utilisent le domaine europages.de → mots-clés allemands
    "AT": [
        ("43.21", "Electrical Installation", "elektroinstallation"),
        ("43.21", "Electrical Installation", "elektrotechnik"),
        ("43.22", "HVAC", "klimatechnik"),
        ("43.22", "HVAC", "haustechnik"),
        ("43.29", "Building Automation", "gebaeudeautomation"),
        ("33.20", "Industrial Maintenance", "anlagenbau"),
        ("81.10", "Facility Management", "facility-management"),
        ("43.29", "Fire Protection", "brandschutz"),
    ],

    "CH": [
        ("43.21", "Electrical Installation", "elektroinstallation"),
        ("43.21", "Electrical Installation", "elektrotechnik"),
        ("43.22", "HVAC", "klimatechnik"),
        ("43.22", "HVAC", "haustechnik"),
        ("43.29", "Building Automation", "gebaeudeautomation"),
        ("33.20", "Industrial Maintenance", "anlagenbau"),
        ("81.10", "Facility Management", "facility-management"),
        ("43.29", "Fire Protection", "brandschutz"),
    ],
}

# ── Headers HTTP ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_CONSECUTIVE_FAILURES = 3

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_reg_number(name: str, city: str, country: str) -> str:
    """Génère un registration_number synthétique stable."""
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]", "", s)
    digest = hashlib.sha1((norm(name) + norm(city)).encode()).hexdigest()[:10]
    return f"EU_{country.upper()}_DIR_{digest}"


def _parse_employees(raw) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    raw = str(raw).strip()
    m = re.match(r"(\d[\d\s]*)\s*[-–]\s*(\d[\d\s]*)", raw)
    if m:
        lo = int(re.sub(r"\s", "", m.group(1)))
        hi = int(re.sub(r"\s", "", m.group(2)))
        return (lo + hi) // 2
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    retries: int = 3,
    source: str = "",
) -> str | None:
    for attempt in range(retries):
        try:
            resp = await client.get(url, timeout=25, follow_redirects=True)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"[{source}] Rate limit — pause {wait}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (403, 404, 422):
                logger.debug(f"[{source}] HTTP {resp.status_code} → {url}")
                return None
            if resp.status_code >= 500:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.TimeoutException:
            logger.debug(f"[{source}] Timeout attempt {attempt+1} → {url}")
            await asyncio.sleep(3 * (attempt + 1))
        except Exception as e:
            logger.debug(f"[{source}] Fetch error {url}: {e}")
            if attempt == retries - 1:
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


def _build_url(country: str, keyword: str, page: int) -> str:
    """Construit l'URL Europages correcte selon le pays et la page."""
    cfg = COUNTRY_URL_CONFIG[country]
    kw = keyword  # déjà encodé (tirets) dans EQUANS_SEARCHES
    if page == 1:
        return cfg["url_p1"].format(kw=kw)
    else:
        return cfg["url_pN"].format(kw=kw, N=page)


def _parse_jsonld_organizations(html: str, filter_country: str) -> list[dict]:
    """
    Extrait les Organizations depuis le JSON-LD ItemList d'Europages.
    Filtre par addressCountry == filter_country.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]

            for node in graph:
                # Structure : ItemList > itemListElement > item (Organization)
                if node.get("@type") == "ItemList":
                    for elem in node.get("itemListElement", []):
                        org = elem.get("item", {}) if isinstance(elem, dict) else {}
                        if org.get("@type") != "Organization":
                            continue
                        addr = org.get("address", {})
                        country_code = addr.get("addressCountry", "")
                        # Filtrage pays
                        if filter_country and country_code != filter_country:
                            continue
                        name = (org.get("name") or "").strip()
                        if not name or len(name) < 2:
                            continue
                        emp_raw = org.get("numberOfEmployees", {})
                        emp_val = emp_raw.get("value") if isinstance(emp_raw, dict) else emp_raw
                        results.append({
                            "name": name,
                            "city": addr.get("addressLocality"),
                            "country_code": country_code,
                            "employees_raw": emp_val,
                            "source_url": org.get("url") or "",  # URL page Europages
                        })
        except Exception as e:
            logger.debug(f"JSON-LD parse error: {e}")

    return results


def _to_company_row(raw: dict, country: str, nace_code: str, sector: str) -> dict | None:
    name = (raw.get("name") or "").strip()
    if not name or len(name) < 2:
        return None
    city = (raw.get("city") or "").strip() or None
    reg_num = _make_reg_number(name, city or "", country)
    return {
        "name": name[:200],
        "country": country.upper(),
        "registration_number": reg_num,
        "city": city,
        "nace_code": nace_code,
        "sector": sector,
        "employees": _parse_employees(raw.get("employees_raw")),
        "source_url": (raw.get("source_url") or "")[:500] or None,
        "scraped_at": datetime.utcnow(),
    }


# ── Scraper principal ─────────────────────────────────────────────────────────

async def scrape_europages_country(
    client: httpx.AsyncClient,
    keyword: str,
    nace_code: str,
    sector: str,
    country: str,
    max_pages: int = 5,
    delay: float = 3.0,
) -> list[dict]:
    """
    Scrape Europages pour un pays et un mot-clé donnés.
    Utilise les URLs correctes par pays et filtre les résultats par addressCountry.
    """
    results: list[dict] = []
    consecutive_failures = 0
    filter_cc = COUNTRY_URL_CONFIG[country]["filter_country"]

    for page in range(1, max_pages + 1):
        url = _build_url(country, keyword, page)
        html = await _fetch_html(client, url, source=f"EP/{country}")

        if not html:
            consecutive_failures += 1
            logger.debug(f"[EP/{country}] '{keyword}' p{page} → fetch failed (failure {consecutive_failures})")
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break
            continue

        consecutive_failures = 0
        rows = _parse_jsonld_organizations(html, filter_country=filter_cc)

        if not rows:
            logger.debug(f"[EP/{country}] '{keyword}' p{page} → 0 résultats après filtre {filter_cc}, arrêt")
            break

        results.extend(rows)
        logger.info(
            f"[EP/{country}] '{keyword}' p{page} → {len(rows)} entreprises {filter_cc}"
        )

        # Arrêt anticipé si moins de 5 résultats (page sparse = fin de données)
        if len(rows) < 5:
            break

        await asyncio.sleep(delay)

    return results


# ── DB upsert ─────────────────────────────────────────────────────────────────

async def _upsert_companies(factory, rows: list[dict], country: str) -> tuple[int, int]:
    """Upsert par registration_number avec déduplication par nom+ville."""
    inserted = 0
    skipped = 0

    # Déduplication dans le batch par (name, city)
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for row in rows:
        key = (row.get("name", "").lower(), (row.get("city") or "").lower())
        if key in seen_keys:
            skipped += 1
            continue
        seen_keys.add(key)
        deduped.append(row)

    if not deduped:
        return 0, skipped

    async with factory() as session:
        for row in deduped:
            try:
                stmt = sqlite_insert(Company).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["country", "registration_number"],
                    set_={
                        col: getattr(stmt.excluded, col)
                        for col in (
                            "city", "employees", "nace_code", "sector", "source_url",
                        )
                    },
                )
                await session.execute(stmt)
                inserted += 1
            except Exception as e:
                logger.debug(f"[DB] Upsert failed for '{row.get('name')}': {e}")
                skipped += 1

        await session.commit()

    return inserted, skipped


# ── Statut global ─────────────────────────────────────────────────────────────

_import_eu_status: dict = {
    "running": False,
    "total_inserted": 0,
    "total_skipped": 0,
    "current_country": "",
    "current_keyword": "",
    "keywords_done": 0,
    "keywords_total": 0,
    "error": None,
}


def get_import_eu_status() -> dict:
    return _import_eu_status.copy()


# ── Orchestrateur ─────────────────────────────────────────────────────────────

async def import_eu_directories(
    db_path: str,
    countries: list[str] | None = None,
    limit_per_kw: int = 50,
    delay: float = 3.0,
) -> dict:
    """
    Orchestre le scraping Europages pour les pays EU hors Allemagne.

    Args:
        db_path: Chemin vers la DB SQLite.
        countries: Sous-ensemble de ['IT', 'AT', 'BE', 'NL', 'CH'].
        limit_per_kw: Nombre max d'entreprises cibles par mot-clé.
                      max_pages = max(1, limit_per_kw // 5)  (5 résultats/page min)
        delay: Délai entre requêtes.
    """
    global _import_eu_status

    target_countries = [c.upper() for c in (countries or list(EQUANS_SEARCHES.keys()))]
    target_countries = [c for c in target_countries if c in EQUANS_SEARCHES]

    search_plan: list[tuple[str, str, str, str]] = []
    for country in target_countries:
        for nace_code, sector, keyword in EQUANS_SEARCHES[country]:
            search_plan.append((country, nace_code, sector, keyword))

    max_pages_per_kw = max(1, limit_per_kw // 5)

    _import_eu_status = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
        "current_country": "",
        "current_keyword": "",
        "keywords_done": 0,
        "keywords_total": len(search_plan),
        "error": None,
    }

    factory = get_session_factory(db_path)
    total_inserted = 0
    total_skipped = 0

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            for country, nace_code, sector, keyword in search_plan:
                _import_eu_status["current_country"] = country
                _import_eu_status["current_keyword"] = keyword

                try:
                    raw_rows = await scrape_europages_country(
                        client=client,
                        keyword=keyword,
                        nace_code=nace_code,
                        sector=sector,
                        country=country,
                        max_pages=max_pages_per_kw,
                        delay=delay,
                    )

                    company_rows = [
                        r for raw in raw_rows
                        if (r := _to_company_row(raw, country, nace_code, sector)) is not None
                    ]

                    if company_rows:
                        ins, skip = await _upsert_companies(factory, company_rows, country)
                        total_inserted += ins
                        total_skipped += skip
                        _import_eu_status["total_inserted"] = total_inserted
                        _import_eu_status["total_skipped"] = total_skipped
                        logger.info(
                            f"[EU-Dir] {country} '{keyword}' → "
                            f"{len(raw_rows)} filtrés, {ins} insérés, {skip} ignorés"
                        )
                    else:
                        logger.debug(f"[EU-Dir] {country} '{keyword}' → 0 résultats")

                except Exception as e:
                    logger.warning(f"[EU-Dir] {country} '{keyword}' erreur: {e}", exc_info=True)

                _import_eu_status["keywords_done"] += 1

    except Exception as exc:
        _import_eu_status["error"] = str(exc)
        logger.error(f"[EU-Dir] Erreur globale: {exc}", exc_info=True)
    finally:
        _import_eu_status["running"] = False
        _import_eu_status["total_inserted"] = total_inserted
        _import_eu_status["total_skipped"] = total_skipped

    logger.info(
        f"[EU-Dir] Terminé — {total_inserted} insérés, {total_skipped} ignorés"
    )
    return {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "countries_run": target_countries,
        "keywords_processed": _import_eu_status["keywords_done"],
    }
