"""
Scraper Goudengids.nl (pages jaunes néerlandaises) pour ciblage Equans NL.

URL pattern : https://www.goudengids.nl/nl/zoeken/{keyword}/
              https://www.goudengids.nl/nl/zoeken/{keyword}/?page={N}

JSON-LD : ItemList avec items LocalBusiness (name, url, city).
Pas d'authentification requise.
"""
import asyncio
import hashlib
import json
import logging
import unicodedata
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import text as sa_text

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

BASE_URL = "https://www.goudengids.nl"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Mots-clés Equans NL → (nace_code, sector_label, keyword_nl)
EQUANS_KEYWORDS_NL: list[tuple[str, str, str]] = [
    ("43.21", "Electrical Installation", "elektrotechniek"),
    ("43.21", "Electrical Installation", "elektro-installatie"),
    ("43.21", "Electrical Installation", "elektricien"),
    ("43.22", "HVAC", "klimaatbeheersing"),
    ("43.22", "HVAC", "installatiebedrijf"),
    ("43.22", "HVAC", "luchtbehandeling"),
    ("43.22", "HVAC", "sanitair-techniek"),
    ("43.29", "Building Automation", "gebouwautomatisering"),
    ("43.29", "Building Automation", "brandbeveiliging"),
    ("43.29", "Building Automation", "beveiligingstechniek"),
    ("33.20", "Industrial Maintenance", "industrieel-onderhoud"),
    ("33.20", "Industrial Maintenance", "machinebouw"),
    ("81.10", "Facility Management", "facilitair-management"),
    ("71.12", "Engineering", "technisch-adviesbureau"),
]

# Statut global
_status: dict = {
    "running": False,
    "total_inserted": 0,
    "total_skipped": 0,
    "current_keyword": "",
    "keywords_done": 0,
    "keywords_total": len(EQUANS_KEYWORDS_NL),
    "error": None,
}


def get_goudengids_status() -> dict:
    return _status.copy()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _make_reg_number(name: str, city: str, source_url: str) -> str:
    """Génère un identifiant stable depuis l'URL de la fiche ou le nom+ville."""
    # Extraire l'ID Goudengids depuis l'URL : /nl/bedrijf/{city}/{ID}/{name}/
    m = re.search(r"/bedrijf/[^/]+/([A-Z0-9]+)/", source_url or "")
    if m:
        return f"NL_GG_{m.group(1)}"
    key = _norm(name) + _norm(city or "")
    return "NL_GG_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _parse_page(html: str) -> list[dict]:
    """
    Parse une page Goudengids et retourne les entreprises trouvées.
    Tente d'abord JSON-LD, puis scraping HTML des fiches.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []

    # --- Tentative JSON-LD ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, dict):
            if data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif data.get("@type") in ("LocalBusiness", "Organization"):
                items = [{"item": data}]
            else:
                # Chercher dans @graph
                for node in data.get("@graph", []):
                    if node.get("@type") == "ItemList":
                        items = node.get("itemListElement", [])
                        break

        for elem in items:
            if isinstance(elem, dict):
                org = elem.get("item", elem)
            else:
                continue
            t = org.get("@type", "")
            if t not in ("LocalBusiness", "Organization", "LocalBusiness"):
                continue
            name = (org.get("name") or "").strip()
            url = (org.get("url") or "").strip()
            addr = org.get("address") or {}
            city = (addr.get("addressLocality") or "").strip() if isinstance(addr, dict) else ""
            # Extraire la ville depuis l'URL Goudengids si pas dans JSON-LD
            if not city and url:
                m = re.search(r"/bedrijf/([^/]+)/[A-Z0-9]+/", url)
                if m:
                    city = m.group(1).replace("+", " ").replace("%20", " ").strip()
            if not name:
                continue
            results.append({"name": name, "city": city or None, "source_url": url or None})

    if results:
        return results

    # --- Fallback : scraping HTML ---
    # Chercher les liens de fiches : /nl/bedrijf/{city}/{ID}/{name}/
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"/nl/bedrijf/")):
        href = a.get("href", "")
        full_url = href if href.startswith("http") else BASE_URL + href
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        # Extraire le nom depuis le lien ou le texte
        m = re.search(r"/nl/bedrijf/[^/]+/[A-Z0-9]+/([^/]+)/?$", href)
        if m:
            raw_name = m.group(1).replace("+", " ").replace("-", " ").strip()
        else:
            raw_name = a.get_text(strip=True)
        # Extraire la ville
        m_city = re.search(r"/nl/bedrijf/([^/]+)/", href)
        city = m_city.group(1).replace("-", " ").title() if m_city else None
        if raw_name and len(raw_name) > 1:
            results.append({"name": raw_name[:200], "city": city, "source_url": full_url})

    return results


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    max_retries: int = 3,
) -> str:
    """Fetche une page avec retry en cas de timeout."""
    for attempt in range(max_retries):
        try:
            r = await client.get(url, headers=HEADERS, timeout=45, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            logger.debug(f"[Goudengids] {url} → HTTP {r.status_code}")
            return ""
        except httpx.ReadTimeout:
            wait = 5 * (attempt + 1)
            logger.warning(f"[Goudengids] Timeout sur {url}, retry {attempt+1}/{max_retries} dans {wait}s")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.warning(f"[Goudengids] Erreur {url}: {e}")
            return ""
    return ""


async def _upsert_companies(factory, rows: list[dict]) -> tuple[int, int]:
    """Upsert en DB avec déduplication par registration_number."""
    inserted = 0
    skipped = 0

    seen: set[str] = set()
    deduped = []
    for row in rows:
        key = row["registration_number"]
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        deduped.append(row)

    if not deduped:
        return 0, skipped

    async with factory() as session:
        result = await session.execute(
            sa_text("SELECT registration_number FROM companies WHERE country='NL'")
        )
        existing = {r[0] for r in result.fetchall()}

        for row in deduped:
            if row["registration_number"] in existing:
                skipped += 1
                continue
            try:
                stmt = sqlite_insert(Company).values(**row)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["country", "registration_number"],
                    set_={
                        col: getattr(stmt.excluded, col)
                        for col in ("city", "nace_code", "sector", "source_url")
                    },
                )
                await session.execute(stmt)
                existing.add(row["registration_number"])
                inserted += 1
            except Exception as e:
                logger.debug(f"[Goudengids] DB error '{row.get('name')}': {e}")
                skipped += 1

        await session.commit()

    return inserted, skipped


async def scrape_goudengids(
    db_path: str,
    max_pages_per_kw: int = 5,
    delay: float = 2.0,
) -> dict:
    """
    Scrape Goudengids.nl pour les keywords Equans NL.
    Insère les entreprises avec nace_code inféré du keyword.
    """
    global _status

    _status = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
        "current_keyword": "",
        "keywords_done": 0,
        "keywords_total": len(EQUANS_KEYWORDS_NL),
        "error": None,
    }

    factory = get_session_factory(db_path)
    total_inserted = 0
    total_skipped = 0

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            for nace_code, sector, keyword in EQUANS_KEYWORDS_NL:
                _status["current_keyword"] = keyword
                kw_inserted = 0

                for page_num in range(1, max_pages_per_kw + 1):
                    if page_num == 1:
                        url = f"{BASE_URL}/nl/zoeken/{keyword}/"
                    else:
                        url = f"{BASE_URL}/nl/zoeken/{keyword}/?page={page_num}"

                    html = await _fetch_page(client, url)
                    if not html:
                        logger.debug(f"[Goudengids] '{keyword}' p{page_num} → vide, arrêt")
                        break

                    parsed = _parse_page(html)
                    if not parsed:
                        logger.debug(f"[Goudengids] '{keyword}' p{page_num} → 0 résultats, arrêt")
                        break

                    rows = []
                    for item in parsed:
                        name = item["name"]
                        if not name or len(name) < 2:
                            continue
                        city = item.get("city")
                        src_url = item.get("source_url")
                        reg_num = _make_reg_number(name, city or "", src_url or "")
                        rows.append({
                            "name": name[:200],
                            "country": "NL",
                            "registration_number": reg_num,
                            "city": city,
                            "nace_code": nace_code,
                            "sector": sector,
                            "source_url": src_url,
                            "scraped_at": datetime.utcnow(),
                        })

                    if rows:
                        ins, skip = await _upsert_companies(factory, rows)
                        total_inserted += ins
                        total_skipped += skip
                        kw_inserted += ins
                        _status["total_inserted"] = total_inserted
                        _status["total_skipped"] = total_skipped
                        logger.info(
                            f"[Goudengids] '{keyword}' p{page_num} → "
                            f"{len(parsed)} found, {ins} insérées, {skip} ignorées"
                        )

                    await asyncio.sleep(delay)

                logger.info(f"[Goudengids] '{keyword}' total: {kw_inserted} insérées")
                _status["keywords_done"] += 1

    except Exception as exc:
        _status["error"] = str(exc)
        logger.error(f"[Goudengids] Erreur globale: {exc}", exc_info=True)
    finally:
        _status["running"] = False
        _status["total_inserted"] = total_inserted
        _status["total_skipped"] = total_skipped

    logger.info(f"[Goudengids] Terminé — {total_inserted} insérées, {total_skipped} ignorées")
    return {"inserted": total_inserted, "skipped": total_skipped}
