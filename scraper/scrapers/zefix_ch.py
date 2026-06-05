"""
Scraper Zefix (registre du commerce suisse) pour ciblage Equans CH.

API publique, sans authentification :
  POST https://www.zefix.ch/ZefixREST/api/v1/firm/search.json
  Body JSON: { name, maxEntries, languageKey, searchType, offset }

Données disponibles : nom, UID, commune, forme juridique, statut.
NACE inférée depuis le mot-clé de recherche.
"""
import asyncio
import hashlib
import logging
import unicodedata
import re
from datetime import datetime

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import text as sa_text

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

ZEFIX_URL = "https://www.zefix.ch/ZefixREST/api/v1/firm/search.json"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; EUCompanyScraper/1.0)",
}

# Mots-clés Equans ciblés → (nace_code, sector_label)
EQUANS_KEYWORDS_CH: list[tuple[str, str, str]] = [
    ("43.21", "Electrical Installation", "elektroinstallation"),
    ("43.21", "Electrical Installation", "elektrotechnik"),
    ("43.21", "Electrical Installation", "elektriker"),
    ("43.22", "HVAC", "klimatechnik"),
    ("43.22", "HVAC", "haustechnik"),
    ("43.22", "HVAC", "lueftungstechnik"),
    ("43.22", "HVAC", "sanitaertechnik"),
    ("43.29", "Building Automation", "gebaeudeautomation"),
    ("43.29", "Building Automation", "brandschutz"),
    ("43.29", "Building Automation", "sicherheitstechnik"),
    ("33.20", "Industrial Maintenance", "anlagenbau"),
    ("33.20", "Industrial Maintenance", "industrieservice"),
    ("81.10", "Facility Management", "facility"),
    ("71.12", "Engineering", "gebaeudeinformatik"),
]

# Statut global
_zefix_status: dict = {
    "running": False,
    "total_inserted": 0,
    "total_skipped": 0,
    "current_keyword": "",
    "keywords_done": 0,
    "keywords_total": len(EQUANS_KEYWORDS_CH),
    "error": None,
}


def get_zefix_status() -> dict:
    return _zefix_status.copy()


def _make_reg_number(uid: str) -> str:
    """Normalise l'UID Zefix comme registration_number."""
    return uid.replace("-", "").replace(".", "")  # CHE366403063


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


async def _fetch_zefix(
    client: httpx.AsyncClient,
    keyword: str,
    max_entries: int = 500,
) -> list[dict]:
    """Appelle l'API Zefix et retourne la liste brute."""
    body = {
        "name": keyword,
        "maxEntries": max_entries,
        "languageKey": "de",
        "searchType": 0,
    }
    try:
        r = await client.post(ZEFIX_URL, json=body, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("list", [])
    except Exception as e:
        logger.warning(f"[Zefix] '{keyword}' erreur: {e}")
        return []


async def _upsert_companies(
    factory,
    rows: list[dict],
) -> tuple[int, int]:
    """Upsert en DB avec déduplication par registration_number."""
    inserted = 0
    skipped = 0

    # Déduplication dans le batch
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
        # Charger les registration_numbers existants pour CH
        result = await session.execute(
            sa_text("SELECT registration_number FROM companies WHERE country='CH'")
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
                logger.debug(f"[Zefix] DB error for '{row.get('name')}': {e}")
                skipped += 1

        await session.commit()

    return inserted, skipped


async def scrape_zefix(
    db_path: str,
    max_entries_per_kw: int = 500,
    delay: float = 1.0,
) -> dict:
    """
    Scrape le registre du commerce suisse (Zefix) pour les keywords Equans.
    Insère les entreprises actives avec nace_code inféré du keyword.
    """
    global _zefix_status

    _zefix_status = {
        "running": True,
        "total_inserted": 0,
        "total_skipped": 0,
        "current_keyword": "",
        "keywords_done": 0,
        "keywords_total": len(EQUANS_KEYWORDS_CH),
        "error": None,
    }

    factory = get_session_factory(db_path)
    total_inserted = 0
    total_skipped = 0

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for nace_code, sector, keyword in EQUANS_KEYWORDS_CH:
                _zefix_status["current_keyword"] = keyword

                raw = await _fetch_zefix(client, keyword, max_entries=max_entries_per_kw)
                # Filtrer les entreprises actives seulement
                active = [r for r in raw if r.get("status") == "EXISTIEREND"]

                rows = []
                for item in active:
                    name = (item.get("name") or "").strip()
                    if not name or len(name) < 2:
                        continue
                    uid = item.get("uid") or ""
                    if not uid:
                        # Fallback: hash du nom + commune
                        uid = "CH_NOID_" + hashlib.sha1(_norm(name).encode()).hexdigest()[:10]
                    rows.append({
                        "name": name[:200],
                        "country": "CH",
                        "registration_number": _make_reg_number(uid),
                        "city": (item.get("legalSeat") or "").strip() or None,
                        "nace_code": nace_code,
                        "sector": sector,
                        "source_url": item.get("cantonalExcerptWeb") or None,
                        "scraped_at": datetime.utcnow(),
                    })

                if rows:
                    ins, skip = await _upsert_companies(factory, rows)
                    total_inserted += ins
                    total_skipped += skip
                    _zefix_status["total_inserted"] = total_inserted
                    _zefix_status["total_skipped"] = total_skipped
                    logger.info(
                        f"[Zefix] '{keyword}' → {len(active)} actives, "
                        f"{ins} insérées, {skip} ignorées"
                    )
                else:
                    logger.debug(f"[Zefix] '{keyword}' → 0 résultats")

                _zefix_status["keywords_done"] += 1
                await asyncio.sleep(delay)

    except Exception as exc:
        _zefix_status["error"] = str(exc)
        logger.error(f"[Zefix] Erreur globale: {exc}", exc_info=True)
    finally:
        _zefix_status["running"] = False
        _zefix_status["total_inserted"] = total_inserted
        _zefix_status["total_skipped"] = total_skipped

    logger.info(f"[Zefix] Terminé — {total_inserted} insérées, {total_skipped} ignorées")
    return {"inserted": total_inserted, "skipped": total_skipped}
