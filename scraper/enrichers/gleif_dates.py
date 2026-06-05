"""
Enrichisseur de dates de création via GLEIF pour les sociétés sans creation_date.

API GLEIF : GET /lei-records/{lei}
  Returns entity.creationDate → ISO 8601

Met à jour Company.creation_date (format "YYYY-MM-DD").
Déclenche un re-score pour le longevity_score.
"""
import asyncio
import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, EquansScore

logger = logging.getLogger(__name__)

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"
HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "EUCompanyScraper/1.0",
}

_status: dict = {
    "running": False,
    "processed": 0,
    "enriched": 0,
    "total": 0,
    "error": None,
}


def get_gleif_dates_status() -> dict:
    return _status.copy()


def _extract_year(date_str: str) -> str | None:
    """Extrait YYYY-MM-DD d'une date ISO 8601."""
    if not date_str:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    return m.group(1) if m else None


async def enrich_gleif_dates(
    db_path: str,
    countries: list[str] | None = None,
    limit: int = 1000,
    delay: float = 0.2,
) -> dict:
    """
    Récupère les dates de création GLEIF pour les sociétés sans creation_date.
    Seules les sociétés avec registration_number correspondant à un LEI (17 chars) sont traitées.
    """
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}

    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(Company)
            .where(
                Company.creation_date.is_(None),
                # LEI : exactement 20 caractères alphanumériques
                Company.registration_number.regexp_match(r'^[A-Z0-9]{18,20}$'),
            )
        )
        if countries:
            q = q.where(Company.country.in_(countries))
        # Prioritise companies closest to the 60-point threshold
        q = q.outerjoin(EquansScore, EquansScore.company_id == Company.id)
        q = q.order_by(EquansScore.total_score.desc().nulls_last(), Company.id)
        if limit:
            q = q.limit(limit)
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    _status["total"] = total
    logger.info(f"[GLEIF-dates] {total} sociétés à enrichir")

    enriched = 0

    async with httpx.AsyncClient(timeout=20) as client:
        for i, company in enumerate(companies):
            _status["processed"] = i + 1
            try:
                r = await client.get(
                    GLEIF_URL.format(lei=company.registration_number),
                    headers=HEADERS,
                    timeout=15,
                )
                if r.status_code != 200:
                    continue

                data = r.json()
                attr = data.get("data", {}).get("attributes", {})
                ent = attr.get("entity", {})
                raw_date = ent.get("creationDate", "") or ""
                creation_date = _extract_year(raw_date)

                if creation_date:
                    async with factory() as session:
                        db_obj = await session.get(Company, company.id)
                        if db_obj and not db_obj.creation_date:
                            db_obj.creation_date = creation_date
                            await session.commit()
                            enriched += 1
                            _status["enriched"] = enriched
                            if enriched % 50 == 0:
                                logger.info(f"[GLEIF-dates] {enriched}/{total} enrichies")

            except Exception as e:
                logger.debug(f"[GLEIF-dates] {company.name[:40]}: {e}")

            await asyncio.sleep(delay)

    _status["running"] = False
    logger.info(f"[GLEIF-dates] Terminé — {enriched}/{total} dates récupérées")
    return {"enriched": enriched, "total": total}
