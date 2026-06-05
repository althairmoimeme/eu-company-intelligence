"""
Scraper GLEIF ciblé par mots-clés pour les pays faibles en couverture (AT, BE, NL...).

API GLEIF : https://api.gleif.org/api/v1/lei-records
  Filtres : filter[entity.legalAddress.country], filter[entity.status]=ACTIVE, filter[fulltext]
  Pagination : page[number], page[size] (max 200)

Données : LEI, nom légal, ville, pays.
NACE inférée depuis le mot-clé de recherche.
"""
import asyncio
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import text as sa_text

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "EUCompanyScraper/1.0 (contact: scraper@example.com)",
}

# Configuration par pays : liste de (nace_code, sector, keyword)
COUNTRY_KEYWORDS: dict[str, list[tuple[str, str, str]]] = {
    "AT": [
        ("43.21", "Electrical Installation", "Elektro"),
        ("43.21", "Electrical Installation", "Elektrotechnik"),
        ("43.22", "HVAC", "Heizung"),
        ("43.22", "HVAC", "Klima"),
        ("43.22", "HVAC", "Lüftung"),
        ("43.22", "HVAC", "Sanitär"),
        ("43.22", "HVAC", "Haustechnik"),
        ("43.29", "Building Automation", "Gebäudeautomation"),
        ("43.29", "Building Automation", "Brandschutz"),
        ("43.29", "Building Automation", "Sicherheitstechnik"),
        ("33.20", "Industrial Maintenance", "Anlagenbau"),
        ("33.20", "Industrial Maintenance", "Industrieservice"),
        ("81.10", "Facility Management", "Facility"),
        ("71.12", "Engineering", "Gebäudetechnik"),
    ],
    "BE": [
        ("43.21", "Electrical Installation", "Electr"),
        ("43.21", "Electrical Installation", "Electric"),
        ("43.21", "Electrical Installation", "Elektro"),
        ("43.21", "Electrical Installation", "Installateur"),
        ("43.22", "HVAC", "Chauffage"),
        ("43.22", "HVAC", "Klimaat"),
        ("43.22", "HVAC", "HVAC"),
        ("43.22", "HVAC", "Sanitaire"),
        ("43.22", "HVAC", "Warmte"),
        ("43.22", "HVAC", "Ventilation"),
        ("43.29", "Building Automation", "Automatisation"),
        ("43.29", "Building Automation", "Automatisering"),
        ("43.29", "Building Automation", "Incendie"),
        ("43.29", "Building Automation", "Beveiliging"),
        ("33.20", "Industrial Maintenance", "Industriel"),
        ("33.20", "Industrial Maintenance", "Industrieel"),
        ("81.10", "Facility Management", "Facility"),
        ("71.12", "Engineering", "Techniek"),
        ("71.12", "Engineering", "Technique"),
        ("71.12", "Engineering", "Génie"),
    ],
    "IT": [
        ("43.21", "Electrical Installation", "Elettric"),
        ("43.21", "Electrical Installation", "Impianti"),
        ("43.21", "Electrical Installation", "Impiantistica"),
        ("43.21", "Electrical Installation", "Elettrico"),
        ("43.21", "Electrical Installation", "Infrastrutture"),
        ("43.22", "HVAC", "Clima"),
        ("43.22", "HVAC", "Termoidraulica"),
        ("43.22", "HVAC", "Riscaldamento"),
        ("43.22", "HVAC", "Impianti Termici"),
        ("43.22", "HVAC", "Condizionamento"),
        ("43.22", "HVAC", "Frigorifera"),
        ("43.22", "HVAC", "Termica"),
        ("43.29", "Building Automation", "Automazione"),
        ("43.29", "Building Automation", "Antincendio"),
        ("43.29", "Building Automation", "Sicurezza"),
        ("43.29", "Building Automation", "Building Automation"),
        ("33.20", "Industrial Maintenance", "Industriale"),
        ("33.20", "Industrial Maintenance", "Manutenzione"),
        ("33.20", "Industrial Maintenance", "Meccatronica"),
        ("81.10", "Facility Management", "Facility"),
        ("81.10", "Facility Management", "Gestione Impianti"),
        ("71.12", "Engineering", "Tecnica"),
        ("71.12", "Engineering", "Installazioni"),
        ("71.12", "Engineering", "Ingegneria"),
    ],
    "NL": [
        ("43.21", "Electrical Installation", "Elektro"),
        ("43.21", "Electrical Installation", "Electro"),
        ("43.21", "Electrical Installation", "Elektrotechniek"),
        ("43.22", "HVAC", "Klimaat"),
        ("43.22", "HVAC", "Installatie"),
        ("43.22", "HVAC", "Sanitair"),
        ("43.22", "HVAC", "Werktuigbouw"),
        ("43.22", "HVAC", "Luchtbehandeling"),
        ("43.29", "Building Automation", "Beveiliging"),
        ("43.29", "Building Automation", "Brandbeveiliging"),
        ("43.29", "Building Automation", "Gebouwautomatisering"),
        ("33.20", "Industrial Maintenance", "Technisch"),
        ("33.20", "Industrial Maintenance", "Industrieel"),
        ("33.20", "Industrial Maintenance", "Onderhoud"),
        ("81.10", "Facility Management", "Facility"),
        ("81.10", "Facility Management", "Facilitair"),
        ("71.12", "Engineering", "Techniek"),
    ],
    "PL": [
        ("43.21", "Electrical Installation", "Elektro"),
        ("43.21", "Electrical Installation", "Elektrotechnik"),
        ("43.21", "Electrical Installation", "Instal"),
        ("43.22", "HVAC", "Ogrzewanie"),
        ("43.22", "HVAC", "Klimatyzacja"),
        ("43.22", "HVAC", "Wentylacja"),
        ("43.22", "HVAC", "Hydraulika"),
        ("43.29", "Building Automation", "Automatyka"),
        ("43.29", "Building Automation", "Alarm"),
        ("43.29", "Building Automation", "Pozarowy"),
        ("33.20", "Industrial Maintenance", "Przemyslowy"),
        ("33.20", "Industrial Maintenance", "Montaz"),
        ("81.10", "Facility Management", "Facility"),
        ("71.12", "Engineering", "Inzynieria"),
    ],
    "DE": [
        ("43.21", "Electrical Installation", "Elektrotechnik"),
        ("43.21", "Electrical Installation", "Elektroinstallation"),
        ("43.22", "HVAC", "Heizungsbau"),
        ("43.22", "HVAC", "Klimatechnik"),
        ("43.22", "HVAC", "Lüftungstechnik"),
        ("43.22", "HVAC", "Sanitärtechnik"),
        ("43.22", "HVAC", "Haustechnik"),
        ("43.29", "Building Automation", "Gebäudeautomation"),
        ("43.29", "Building Automation", "Brandschutztechnik"),
        ("43.29", "Building Automation", "Sicherheitstechnik"),
        ("33.20", "Industrial Maintenance", "Anlagenbau"),
        ("33.20", "Industrial Maintenance", "Anlagentechnik"),
        ("81.10", "Facility Management", "Facility"),
        ("71.12", "Engineering", "Ingenieure"),
    ],
    "GB": [
        ("43.21", "Electrical Installation", "Electrical"),
        ("43.21", "Electrical Installation", "Electrician"),
        ("43.22", "HVAC", "Heating"),
        ("43.22", "HVAC", "Plumbing"),
        ("43.22", "HVAC", "Mechanical"),
        ("43.29", "Building Automation", "Fire Protection"),
        ("43.29", "Building Automation", "Security"),
        ("33.20", "Industrial Maintenance", "Industrial"),
        ("81.10", "Facility Management", "Facility"),
        ("71.12", "Engineering", "Engineering"),
    ],
}

# Statut global
_status: dict = {
    "running": False,
    "total_inserted": 0,
    "total_skipped": 0,
    "current_country": "",
    "current_keyword": "",
    "keywords_done": 0,
    "keywords_total": 0,
    "error": None,
}


def get_gleif_targeted_status() -> dict:
    return _status.copy()


async def _fetch_gleif_page(
    client: httpx.AsyncClient,
    country: str,
    keyword: str,
    page: int = 1,
    page_size: int = 200,
) -> tuple[list[dict], int]:
    """Récupère une page de résultats GLEIF. Retourne (items, total)."""
    params = {
        "filter[entity.legalAddress.country]": country,
        "filter[entity.status]": "ACTIVE",
        "filter[fulltext]": keyword,
        "page[number]": page,
        "page[size]": page_size,
    }
    try:
        r = await client.get(GLEIF_URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        total = d.get("meta", {}).get("pagination", {}).get("total", 0)
        items = d.get("data", [])
        return items, total
    except Exception as e:
        logger.warning(f"[GLEIF-targeted] {country}+{keyword} p{page}: {e}")
        return [], 0


def _extract_company(item: dict, country: str, nace_code: str, sector: str) -> dict | None:
    """Extrait les champs pertinents d'un item GLEIF."""
    try:
        attr = item.get("attributes", {})
        lei = item.get("id", "") or attr.get("lei", "")
        if not lei:
            return None
        ent = attr.get("entity", {})
        name = (ent.get("legalName") or {}).get("name", "").strip()
        if not name or len(name) < 2:
            return None
        city = (ent.get("legalAddress") or {}).get("city", "").strip() or None
        # Tente d'extraire la date de création depuis GLEIF
        creation_date = None
        raw_date = ent.get("creationDate") or ""
        if raw_date:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", raw_date)
            if m:
                creation_date = m.group(1)
        row = {
            "name": name[:200],
            "country": country,
            "registration_number": lei,
            "city": city,
            "nace_code": nace_code,
            "sector": sector,
            "source_url": f"https://www.gleif.org/en/lei/{lei}",
            "scraped_at": datetime.utcnow(),
        }
        if creation_date:
            row["creation_date"] = creation_date
        return row
    except Exception:
        return None


async def _upsert_companies(factory, rows: list[dict], country: str) -> tuple[int, int]:
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
            sa_text(f"SELECT registration_number FROM companies WHERE country='{country}'")
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
                        for col in ("city", "nace_code", "sector", "source_url", "creation_date")
                    },
                )
                await session.execute(stmt)
                existing.add(row["registration_number"])
                inserted += 1
            except Exception as e:
                logger.debug(f"[GLEIF-targeted] DB error '{row.get('name')}': {e}")
                skipped += 1

        await session.commit()

    return inserted, skipped


async def scrape_gleif_targeted(
    db_path: str,
    countries: list[str] | None = None,
    max_pages_per_kw: int = 5,
    delay: float = 0.5,
) -> dict:
    """
    Scrape GLEIF par mots-clés pour les pays ciblés (AT, BE...).
    Insère les entreprises actives avec NACE inféré du mot-clé.
    """
    global _status

    target_countries = countries or list(COUNTRY_KEYWORDS.keys())
    total_kw = sum(len(COUNTRY_KEYWORDS[c]) for c in target_countries if c in COUNTRY_KEYWORDS)

    _status = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
        "current_country": "",
        "current_keyword": "",
        "keywords_done": 0,
        "keywords_total": total_kw,
        "error": None,
    }

    factory = get_session_factory(db_path)
    total_inserted = 0
    total_skipped = 0

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for country in target_countries:
                keywords = COUNTRY_KEYWORDS.get(country, [])
                if not keywords:
                    logger.warning(f"[GLEIF-targeted] Pas de keywords pour {country}")
                    continue

                _status["current_country"] = country
                logger.info(f"[GLEIF-targeted] Démarrage {country} — {len(keywords)} keywords")

                for nace_code, sector, keyword in keywords:
                    _status["current_keyword"] = keyword
                    kw_inserted = 0

                    # Première page pour connaître le total
                    items, total = await _fetch_gleif_page(
                        client, country, keyword, page=1, page_size=200
                    )
                    last_page = min(max_pages_per_kw, (total + 199) // 200)

                    rows = [
                        r for item in items
                        if (r := _extract_company(item, country, nace_code, sector))
                    ]
                    if rows:
                        ins, skip = await _upsert_companies(factory, rows, country)
                        total_inserted += ins
                        total_skipped += skip
                        kw_inserted += ins
                        _status["total_inserted"] = total_inserted
                        _status["total_skipped"] = total_skipped

                    logger.info(
                        f"[GLEIF-targeted] {country}+{keyword} "
                        f"total={total} p1={len(items)} ins={ins} "
                        f"pages_restantes={last_page-1}"
                    )

                    # Pages suivantes
                    for page_num in range(2, last_page + 1):
                        await asyncio.sleep(delay)
                        items, _ = await _fetch_gleif_page(
                            client, country, keyword, page=page_num, page_size=200
                        )
                        if not items:
                            break
                        rows = [
                            r for item in items
                            if (r := _extract_company(item, country, nace_code, sector))
                        ]
                        if rows:
                            ins, skip = await _upsert_companies(factory, rows, country)
                            total_inserted += ins
                            total_skipped += skip
                            kw_inserted += ins
                            _status["total_inserted"] = total_inserted
                            _status["total_skipped"] = total_skipped

                    logger.info(
                        f"[GLEIF-targeted] {country}+{keyword} total inséré: {kw_inserted}"
                    )
                    _status["keywords_done"] += 1
                    await asyncio.sleep(delay)

    except Exception as exc:
        _status["error"] = str(exc)
        logger.error(f"[GLEIF-targeted] Erreur globale: {exc}", exc_info=True)
    finally:
        _status["running"] = False
        _status["total_inserted"] = total_inserted
        _status["total_skipped"] = total_skipped

    logger.info(f"[GLEIF-targeted] Terminé — {total_inserted} insérées, {total_skipped} ignorées")
    return {"inserted": total_inserted, "skipped": total_skipped}
