"""
Scraper multi-sources pour entreprises allemandes — ciblage Equans M&A.

Sources gratuites :
  1. WLW.de        — Wer liefert was, annuaire B2B industriel allemand
  2. Europages.com  — Annuaire B2B européen, bonne couverture DE
  3. Gelbeseiten.de — Pages jaunes allemandes
  4. Kompass.com/de — Annuaire industriel avec données structurées

Codes WZ/NACE ciblés (Equans core business) :
  33.20 — Installation de machines et équipements
  43.21 — Elektroinstallation
  43.22 — CVC / HLK / Sanitär
  43.29 — Sonstige Bauinstallation (TGA, Gebäudeautomation)
  80.20 — Sicherheitsdienste mit Überwachungs-/Alarmsystemen
  71.12 — Ingenieurbüros
  28.29 — Automatisierungstechnik
  81.10 — Facility Management
"""
import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

# ── Codes WZ/NACE × mots-clés de recherche ──────────────────────────────────
# Fournis par l'utilisateur + mappés aux codes NACE Equans

EQUANS_SEARCHES: list[tuple[str, str, list[str]]] = [
    # (nace_code, sector_label, [keywords])
    ("43.21", "Elektroinstallation", [
        "Elektroinstallation",
        "Elektrotechnik",
        "Energie- und Gebäudetechnik",
        "Schaltanlagenbau",
        "Schaltschrankbau",
        "Niederspannung",
        "Mittelspannung",
        "Starkstrominstallation",
    ]),
    ("43.22", "Heizung/Klima/Sanitär (HLK)", [
        "HLK",
        "Heizung Lüftung Klima",
        "Klimatechnik",
        "Kältetechnik",
        "Industriekälte",
        "Wärmepumpe",
        "Kälteanlage",
        "SHK",       # Sanitär-Heizung-Klima
        "HLS",       # Heizung-Lüftung-Sanitär
        "Lüftungstechnik",
    ]),
    ("43.29", "Gebäudetechnik / TGA", [
        "Gebäudetechnik",
        "Technische Gebäudeausrüstung",
        "TGA",
        "Gebäudeautomation",
        "MSR",
        "Mess- Steuer- und Regeltechnik",
        "Reinraumtechnik",
    ]),
    ("33.20", "Anlagenbau / Industriemontage", [
        "Anlagenbau",
        "Industriemontage",
        "Prozessleittechnik",
        "Prozessautomatisierung",
        "Leittechnik",
        "SCADA",
    ]),
    ("28.29", "Automatisierungstechnik", [
        "Automatisierungstechnik",
        "MSR-Technik",
        "Prozessautomation",
        "SPS-Technik",
        "Steuerungstechnik",
    ]),
    ("80.20", "Sicherheitstechnik / Brandschutz", [
        "Sicherheitstechnik",
        "Brandschutz",
        "Brandmeldetechnik",
        "Zutrittskontrolle",
        "Videoüberwachung",
        "USV",
        "Notstrom",
    ]),
    ("81.10", "Facility Management", [
        "Technisches Facility Management",
        "Technisches Gebäudemanagement",
        "Instandhaltung",
        "Multi-Technik",
        "Wartung und Service",
    ]),
    ("71.12", "Ingenieurbüro / Energietechnik", [
        "Ingenieurbüro Elektrotechnik",
        "TGA-Planung",
        "Energieeffizienz",
        "Photovoltaik",
        "Dekarbonisierung",
        "Rechenzentrum",
        "kritische Infrastruktur",
    ]),
    # Secteurs critiques (Reinraum, Pharma, Halbleiter, Data Center)
    ("43.29", "Reinraum / Pharma / Halbleiter", [
        "Reinraum",
        "Pharma",
        "Halbleiter",
        "Mikroelektronik",
        "Data Center",
        "Rechenzentrum",
    ]),
]

# ── Headers HTTP réalistes ────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_CONSECUTIVE_FAILURES = 4

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_reg_number(name: str, city: str, source: str) -> str:
    """Génère un registration_number synthétique stable pour les sociétés sans HRB."""
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9]", "", s)
    digest = hashlib.sha1((norm(name) + norm(city)).encode()).hexdigest()[:10]
    return f"DE_DIR_{source.upper()}_{digest}"


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


def _extract_city_from_address(address: str) -> str | None:
    """Extrait la ville depuis une adresse allemande (code postal 5 chiffres)."""
    if not address:
        return None
    m = re.search(r"\b(\d{5})\s+([A-ZÄÖÜa-zäöüß][^\n,]{2,40})", address)
    if m:
        return m.group(2).strip()
    # Fallback: dernier segment après virgule
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return parts[-1] if parts else None


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
                wait = int(resp.headers.get("Retry-After", 45))
                logger.warning(f"[{source}] Rate limit — pause {wait}s")
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (403, 404):
                return None
            if resp.status_code >= 500:
                await asyncio.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.TimeoutException:
            await asyncio.sleep(3 * (attempt + 1))
        except Exception as e:
            logger.debug(f"[{source}] Fetch error {url}: {e}")
            if attempt == retries - 1:
                return None
            await asyncio.sleep(2 * (attempt + 1))
    return None


def _to_company_row(raw: dict, source_tag: str) -> dict | None:
    name = (raw.get("name") or "").strip()
    # Nettoyage des noms trop courts ou manifestement invalides
    if not name or len(name) < 3 or name.lower() in ("gmbh", "ag", "kg"):
        return None
    city = (raw.get("city") or "").strip() or None
    reg_num = _make_reg_number(name, city or "", source_tag)
    return {
        "name": name[:200],
        "country": "DE",
        "registration_number": reg_num,
        "city": city,
        "address": (raw.get("address") or "")[:500] or None,
        "website": raw.get("website") or None,
        "phone": raw.get("phone") or None,
        "nace_code": raw.get("nace_code") or None,
        "sector": raw.get("sector") or None,
        "activity_description": (raw.get("activity_description") or "")[:500] or None,
        "employees": _parse_employees(raw.get("employees")),
        "source_url": raw.get("source_url") or None,
        "scraped_at": datetime.utcnow(),
    }


# ── WLW.de ────────────────────────────────────────────────────────────────────

def _parse_wlw_page(html: str, nace_code: str, sector: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    # WLW structure (2024/2025) : div.company-tile avec h2 + span.city
    cards = soup.select("div.company-tile")

    for card in cards:
        # Nom : dans le h2
        name_el = card.select_one("h2")
        if not name_el:
            # Fallback : premier lien texte long
            name_el = card.select_one("a[class*='font-display']")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or len(name) < 3:
            continue

        # Ville : span.city
        city_el = card.select_one("span.city")
        city = city_el.get_text(strip=True) if city_el else None

        # Description : cherche les spans de texte
        desc_el = (
            card.select_one("p.description") or
            card.select_one("div[class*='description']") or
            card.select_one("div[class*='teaser']")
        )
        description = desc_el.get_text(strip=True)[:500] if desc_el else None

        # Employés
        emp_el = card.select_one("span[class*='employee'], div[class*='employee']")
        employees = None
        if emp_el:
            employees = _parse_employees(emp_el.get_text(strip=True))

        results.append({
            "name": name,
            "city": city,
            "activity_description": description,
            "employees": employees,
            "nace_code": nace_code,
            "sector": sector,
            "source_url": base_url,
        })
    return results


async def scrape_wlw(
    client: httpx.AsyncClient,
    keyword: str,
    nace_code: str,
    sector: str,
    max_pages: int = 8,
    delay: float = 2.5,
) -> list[dict]:
    results = []
    consecutive_failures = 0
    for page in range(1, max_pages + 1):
        url = f"https://www.wlw.de/de/suche?q={quote_plus(keyword)}&page={page}"
        html = await _fetch_html(client, url, source="WLW")
        if not html:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        consecutive_failures = 0
        rows = _parse_wlw_page(html, nace_code, sector, url)
        if not rows:
            break  # Plus de résultats
        results.extend(rows)
        logger.info(f"[WLW] '{keyword}' p{page} → {len(rows)} entrées")
        await asyncio.sleep(delay)
    return results


# ── Europages.com ─────────────────────────────────────────────────────────────

def _parse_europages_page(html: str, nace_code: str, sector: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Essaie d'abord les données structurées JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Organization", "LocalBusiness", "Corporation"):
                    name = (item.get("name") or "").strip()
                    if not name:
                        continue
                    addr = item.get("address") or {}
                    city = addr.get("addressLocality") if isinstance(addr, dict) else None
                    results.append({
                        "name": name,
                        "city": city,
                        "website": item.get("url"),
                        "phone": item.get("telephone"),
                        "activity_description": (item.get("description") or "")[:500] or None,
                        "nace_code": nace_code,
                        "sector": sector,
                        "source_url": base_url,
                    })
        except Exception:
            pass
    if results:
        return results

    # Fallback CSS
    cards = (
        soup.select("div.company-card") or
        soup.select("article.company-item") or
        soup.select("li.result-item")
    )
    for card in cards:
        name_el = card.select_one("h2, h3, .company-name, a.company-card__name")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        addr_el = card.select_one(".address, .company-card__address, .location")
        city = _extract_city_from_address(addr_el.get_text(strip=True)) if addr_el else None
        desc_el = card.select_one(".description, .activity, .company-description")
        desc = desc_el.get_text(strip=True)[:500] if desc_el else None
        results.append({
            "name": name, "city": city,
            "activity_description": desc,
            "nace_code": nace_code, "sector": sector, "source_url": base_url,
        })
    return results


async def scrape_europages(
    client: httpx.AsyncClient,
    keyword: str,
    nace_code: str,
    sector: str,
    max_pages: int = 6,
    delay: float = 3.0,
) -> list[dict]:
    results = []
    consecutive_failures = 0
    kw_slug = keyword.lower().replace(" ", "-").replace("/", "-")

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"https://www.europages.com/companies/{quote_plus(keyword)}/country/DE.html"
        else:
            url = f"https://www.europages.com/companies/{quote_plus(keyword)}/country/DE/page/{page}.html"

        html = await _fetch_html(client, url, source="Europages")
        if not html:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        consecutive_failures = 0
        rows = _parse_europages_page(html, nace_code, sector, url)
        if not rows:
            break
        results.extend(rows)
        logger.info(f"[Europages] '{keyword}' p{page} → {len(rows)} entrées")
        await asyncio.sleep(delay)
    return results


# ── Gelbeseiten.de ────────────────────────────────────────────────────────────

def _parse_gelbeseiten_page(html: str, nace_code: str, sector: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    items = (
        soup.select("article[data-wle-section='treffer']") or
        soup.select("li.mod-Treffer") or
        soup.select("div.result-entry")
    )
    for item in items:
        name_el = (
            item.select_one("p.mod-Treffer--name") or
            item.select_one("h2.mod-Treffer--name") or
            item.select_one("a.mod-Treffer--name") or
            item.select_one("[itemprop='name']")
        )
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        addr_el = (
            item.select_one("p[data-role='address']") or
            item.select_one(".mod-Treffer--adresse") or
            item.select_one("[itemprop='address']")
        )
        address = addr_el.get_text(strip=True) if addr_el else None
        city = _extract_city_from_address(address)

        phone_el = item.select_one("a[data-role='phone'], span.mod-Treffer--telefon")
        phone = phone_el.get_text(strip=True) if phone_el else None

        website_el = item.select_one("a[data-role='website']")
        website = website_el.get("href") if website_el else None

        results.append({
            "name": name, "city": city, "address": address,
            "phone": phone, "website": website,
            "nace_code": nace_code, "sector": sector, "source_url": base_url,
        })
    return results


async def scrape_gelbeseiten(
    client: httpx.AsyncClient,
    keyword: str,
    nace_code: str,
    sector: str,
    max_results: int = 300,
    delay: float = 2.5,
) -> list[dict]:
    results = []
    consecutive_failures = 0
    kw_slug = re.sub(r"[^a-z0-9äöüß]", "-", keyword.lower()).strip("-")
    offset = 0
    page_size = 50

    while offset < max_results:
        url = (
            f"https://www.gelbeseiten.de/branchenbuch/{quote_plus(keyword)}/bundesweit/"
            f"?treffer={page_size}&von={offset}"
        )
        html = await _fetch_html(client, url, source="Gelbeseiten")
        if not html:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        consecutive_failures = 0
        rows = _parse_gelbeseiten_page(html, nace_code, sector, url)
        if not rows:
            break
        results.extend(rows)
        logger.info(f"[Gelbeseiten] '{keyword}' off={offset} → {len(rows)} entrées")
        offset += page_size
        await asyncio.sleep(delay)
    return results


# ── Kompass.com ───────────────────────────────────────────────────────────────

def _parse_kompass_page(html: str, nace_code: str, sector: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Kompass utilise Next.js — cherche __NEXT_DATA__
    next_data_el = soup.find("script", id="__NEXT_DATA__")
    if next_data_el:
        try:
            data = json.loads(next_data_el.string or "")
            companies = (
                data.get("props", {}).get("pageProps", {}).get("companies") or
                data.get("props", {}).get("pageProps", {}).get("results") or
                data.get("props", {}).get("pageProps", {}).get("companyList") or
                []
            )
            for co in companies:
                name = (co.get("name") or co.get("companyName") or "").strip()
                if not name:
                    continue
                city = co.get("city") or co.get("town") or co.get("locality")
                website = co.get("website") or co.get("websiteUrl")
                phone = co.get("phone") or co.get("phoneNumber")
                desc = (
                    co.get("description") or
                    co.get("activity") or
                    co.get("activityDescription") or
                    co.get("mainActivity")
                )
                results.append({
                    "name": name, "city": city, "website": website, "phone": phone,
                    "activity_description": (desc or "")[:500] or None,
                    "employees": co.get("employees") or co.get("employeesRange"),
                    "nace_code": nace_code, "sector": sector, "source_url": base_url,
                })
            if results:
                return results
        except Exception:
            pass

    # Fallback CSS
    for card in soup.select("div.company-card, li.company-result, article.result-item, div[class*='CompanyCard']"):
        name_el = card.select_one("h2, h3, .company-name, a.company-link")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        city_el = card.select_one(".city, .location, .company-location, [class*='city']")
        city = city_el.get_text(strip=True) if city_el else None
        desc_el = card.select_one(".description, .activity, [class*='description']")
        desc = desc_el.get_text(strip=True)[:500] if desc_el else None
        results.append({
            "name": name, "city": city,
            "activity_description": desc,
            "nace_code": nace_code, "sector": sector, "source_url": base_url,
        })
    return results


async def scrape_kompass(
    client: httpx.AsyncClient,
    keyword: str,
    nace_code: str,
    sector: str,
    max_pages: int = 5,
    delay: float = 3.5,
) -> list[dict]:
    results = []
    consecutive_failures = 0
    for page in range(1, max_pages + 1):
        url = (
            f"https://de.kompass.com/searchCompany"
            f"?text={quote_plus(keyword)}&country=DE&page={page}"
        )
        html = await _fetch_html(client, url, source="Kompass")
        if not html:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        consecutive_failures = 0
        rows = _parse_kompass_page(html, nace_code, sector, url)
        if not rows:
            break
        results.extend(rows)
        logger.info(f"[Kompass] '{keyword}' p{page} → {len(rows)} entrées")
        await asyncio.sleep(delay)
    return results


# ── DB upsert ─────────────────────────────────────────────────────────────────

async def _upsert_companies(factory, rows: list[dict]) -> tuple[int, int]:
    """Upsert avec COALESCE et déduplication par website URL."""
    from sqlalchemy import text as sa_text

    inserted = 0
    skipped = 0

    # Déduplique les rows entrantes par website (même batch)
    seen_websites: set[str] = set()
    deduped_rows = []
    for row in rows:
        website = (row.get("website") or "").rstrip("/").lower()
        if website and website in seen_websites:
            skipped += 1
            continue
        if website:
            seen_websites.add(website)
        deduped_rows.append(row)

    async with factory() as session:
        # Récupère les websites déjà en DB pour DE (évite les doublons cross-batch)
        existing_websites: set[str] = set()
        if deduped_rows:
            websites_in_batch = [
                (r.get("website") or "").rstrip("/").lower()
                for r in deduped_rows
                if r.get("website")
            ]
            if websites_in_batch:
                result = await session.execute(
                    sa_text("SELECT LOWER(RTRIM(website, '/')) FROM companies WHERE country='DE' AND website IS NOT NULL")
                )
                existing_websites = {row[0] for row in result.fetchall() if row[0]}

        for row in deduped_rows:
            website = (row.get("website") or "").rstrip("/").lower()
            # Si le site web existe déjà dans la DB → skip
            if website and website in existing_websites:
                skipped += 1
                logger.debug(f"[DB] Skip duplicate website: {website[:60]}")
                continue

            try:
                stmt = sqlite_insert(Company).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["country", "registration_number"],
                    set_={
                        col: getattr(stmt.excluded, col)
                        for col in (
                            "city", "website", "phone", "address",
                            "activity_description", "employees",
                            "nace_code", "sector",
                        )
                    },
                )
                await session.execute(stmt)
                if website:
                    existing_websites.add(website)
                inserted += 1
            except Exception as e:
                logger.debug(f"[DB] Upsert failed for '{row.get('name')}': {e}")
                skipped += 1
        await session.commit()

    return inserted, skipped


# ── Orchestrateur principal ────────────────────────────────────────────────────

_import_status: dict = {
    "running": False,
    "total_inserted": 0,
    "total_skipped": 0,
    "current_source": "",
    "current_keyword": "",
    "keywords_done": 0,
    "keywords_total": 0,
    "error": None,
}


def get_import_status() -> dict:
    return _import_status.copy()


async def import_de_directories(
    db_path: str,
    sources: list[str] | None = None,
    nace_filter: list[str] | None = None,
    max_pages_per_keyword: int = 5,
    delay_factor: float = 1.0,
    limit: int = 0,
) -> dict:
    """
    Orchestre le scraping multi-sources DE pour les cibles Equans.

    Args:
        db_path: Chemin vers la DB SQLite.
        sources: Sous-ensemble de ["wlw", "europages", "gelbeseiten", "kompass"].
                 None = toutes les sources.
        nace_filter: Liste de codes NACE à cibler (None = tous).
        max_pages_per_keyword: Pages max par mot-clé par source (défaut 5).
        delay_factor: Multiplicateur sur les délais (1.0 = normal, 2.0 = plus lent).
        limit: Nombre max d'entreprises à insérer (0 = illimité).

    Returns:
        Stats dict.
    """
    global _import_status

    all_sources = ["wlw", "europages", "gelbeseiten", "kompass"]
    enabled_sources = sources or all_sources

    # Construction de la liste keyword × source
    search_plan = []
    for nace_code, sector, keywords in EQUANS_SEARCHES:
        if nace_filter and nace_code not in nace_filter:
            continue
        for kw in keywords:
            search_plan.append((nace_code, sector, kw))

    _import_status = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
        "current_source": "",
        "current_keyword": "",
        "keywords_done": 0,
        "keywords_total": len(search_plan) * len(enabled_sources),
        "error": None,
    }

    factory = get_session_factory(db_path)

    scraper_map = {
        "wlw": scrape_wlw,
        "europages": scrape_europages,
        "gelbeseiten": scrape_gelbeseiten,
        "kompass": scrape_kompass,
    }

    total_inserted = 0
    total_skipped = 0

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            for source in enabled_sources:
                scrape_fn = scraper_map.get(source)
                if not scrape_fn:
                    continue

                source_failures = 0
                logger.info(f"[DE-Dir] Source: {source.upper()}")

                for nace_code, sector, keyword in search_plan:
                    if limit and total_inserted >= limit:
                        logger.info(f"[DE-Dir] Limite {limit} atteinte")
                        break

                    _import_status["current_source"] = source
                    _import_status["current_keyword"] = keyword

                    try:
                        kwargs = dict(
                            client=client,
                            keyword=keyword,
                            nace_code=nace_code,
                            sector=sector,
                        )
                        if source == "gelbeseiten":
                            kwargs["max_results"] = max_pages_per_keyword * 50
                            kwargs["delay"] = 2.5 * delay_factor
                        else:
                            kwargs["max_pages"] = max_pages_per_keyword
                            kwargs["delay"] = {
                                "wlw": 2.5,
                                "europages": 3.0,
                                "kompass": 3.5,
                            }.get(source, 2.5) * delay_factor

                        raw_rows = await scrape_fn(**kwargs)

                        company_rows = [
                            r for raw in raw_rows
                            if (r := _to_company_row(raw, source)) is not None
                        ]
                        if company_rows:
                            ins, skip = await _upsert_companies(factory, company_rows)
                            total_inserted += ins
                            total_skipped += skip
                            _import_status["total_inserted"] = total_inserted
                            _import_status["total_skipped"] = total_skipped
                            logger.info(
                                f"[DE-Dir] {source} '{keyword}' → "
                                f"{len(raw_rows)} bruts, {ins} insérés, {skip} ignorés"
                            )
                        else:
                            logger.debug(f"[DE-Dir] {source} '{keyword}' → 0 résultats")

                        source_failures = 0

                    except Exception as e:
                        source_failures += 1
                        logger.warning(f"[DE-Dir] {source} '{keyword}' erreur: {e}")
                        if source_failures >= MAX_CONSECUTIVE_FAILURES:
                            logger.error(f"[DE-Dir] {source} — trop d'erreurs, source ignorée")
                            break

                    _import_status["keywords_done"] += 1

                logger.info(f"[DE-Dir] {source.upper()} terminé")

    except Exception as exc:
        _import_status["error"] = str(exc)
        logger.error(f"[DE-Dir] Erreur globale: {exc}", exc_info=True)
    finally:
        _import_status["running"] = False
        _import_status["total_inserted"] = total_inserted
        _import_status["total_skipped"] = total_skipped

    logger.info(
        f"[DE-Dir] Terminé — {total_inserted} insérés, {total_skipped} ignorés"
    )
    return {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "sources_run": enabled_sources,
        "keywords_processed": _import_status["keywords_done"],
    }
