"""Scraper GLEIF pour l'import en masse de sociétés DE/IT/ES.

GLEIF (Global LEI Foundation) = registre légal global, gratuit, sans authentification.
~231K DE / ~228K IT / ~177K ES sociétés actives avec LEI.

Données disponibles : nom, pays, ville, code postal, N° d'enregistrement local (HRB etc.), date de création.
PAS de chiffre d'affaires ni de dirigeants — à enrichir ensuite via Yahoo Finance / Bundesanzeiger.

Utilise la pagination par curseur GLEIF (pas de limite sur le nombre total de résultats).
"""
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

GLEIF_API = "https://api.gleif.org/api/v1/lei-records"
PAGE_SIZE = 200

# Ancienneté minimum : on ne veut pas les startups récentes
MIN_AGE_YEARS = 8  # fondée avant 2018


def _parse_records(records: list, country: str, cutoff_year: int) -> tuple[list, int]:
    """Parse GLEIF records → rows à insérer + nb skipped."""
    rows = []
    skipped = 0
    for rec in records:
        attr = rec.get("attributes", {})
        entity = attr.get("entity", {})

        # Filtre ancienneté
        creation_raw = entity.get("creationDate")
        creation_date = None
        if creation_raw:
            try:
                creation_date = creation_raw[:10]  # YYYY-MM-DD
                if int(creation_date[:4]) > cutoff_year:
                    skipped += 1
                    continue
            except Exception:
                pass

        name = entity.get("legalName", {}).get("name", "").strip()
        if not name or len(name) < 3:
            skipped += 1
            continue

        addr = entity.get("legalAddress", {})
        city = addr.get("city") or None
        postal_code = addr.get("postalCode") or None
        reg_number = entity.get("registeredAs") or None
        lei = attr.get("lei", "")

        # registration_number est NOT NULL — LEI comme fallback
        final_reg = reg_number or lei or f"GLEIF-{name[:30]}"

        rows.append({
            "name": name,
            "country": country,
            "city": city,
            "postal_code": postal_code,
            "creation_date": creation_date,
            "registration_number": final_reg,
            "source_url": f"https://www.gleif.org/en/lei/{lei}" if lei else None,
        })
    return rows, skipped


async def import_gleif_country(
    db_path: str,
    country: str,
    limit: int = 0,
    min_age_years: int = MIN_AGE_YEARS,
) -> dict:
    """Importe les sociétés GLEIF pour un pays donné via pagination par curseur.

    Args:
        db_path: chemin DB SQLite
        country: code pays ISO 2 lettres (DE, IT, ES)
        limit: max sociétés à importer (0 = tout)
        min_age_years: ancienneté minimum en années

    Returns:
        {"country": ..., "imported": N, "skipped": N, "pages": N}
    """
    factory = get_session_factory(db_path)
    cutoff_year = datetime.now().year - min_age_years

    imported = 0
    skipped = 0
    pages = 0
    # Démarrer avec cursor=*
    next_url = (
        f"{GLEIF_API}"
        f"?filter%5Bentity.legalAddress.country%5D={country}"
        f"&filter%5Bentity.status%5D=ACTIVE"
        f"&page%5Bcursor%5D=%2A"
        f"&page%5Bsize%5D={PAGE_SIZE}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        retries = 0
        while next_url:
            if limit and imported >= limit:
                break

            try:
                resp = await client.get(next_url)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"[GLEIF-{country}] 429 rate-limit — attente {wait}s avant retry")
                    await asyncio.sleep(wait)
                    retries = 0
                    continue  # retry same page
                resp.raise_for_status()
                body = resp.json()
                retries = 0
            except Exception as e:
                retries += 1
                if retries > 5:
                    logger.error(f"[GLEIF-{country}] Abandon après 5 erreurs: {e}")
                    break
                logger.warning(f"[GLEIF-{country}] Fetch error page {pages+1} (retry {retries}/5): {e}")
                await asyncio.sleep(15 * retries)
                continue  # retry same page

            records = body.get("data", [])
            if not records:
                break

            rows, page_skipped = _parse_records(records, country, cutoff_year)
            skipped += page_skipped

            if rows:
                async with factory() as session:
                    async with session.begin():
                        stmt = sqlite_insert(Company).values(rows)
                        stmt = stmt.on_conflict_do_nothing()
                        result = await session.execute(stmt)
                        page_imported = result.rowcount if result.rowcount >= 0 else len(rows)
                        imported += page_imported
                        skipped += len(rows) - page_imported

            pages += 1
            next_url = body.get("links", {}).get("next")

            if pages % 50 == 0:
                logger.info(
                    f"[GLEIF-{country}] Page {pages} — "
                    f"imported={imported}, skipped={skipped}"
                )

            # Pause légère pour ne pas surcharger l'API
            await asyncio.sleep(0.1)

    result = {
        "country": country,
        "imported": imported,
        "skipped": skipped,
        "pages": pages,
    }
    logger.info(f"[GLEIF-{country}] Terminé — {result}")
    return result


async def import_gleif_all(
    db_path: str,
    countries: list[str] | None = None,
    limit_per_country: int = 0,
    min_age_years: int = MIN_AGE_YEARS,
) -> dict:
    """Importe DE + IT + ES en séquence."""
    targets = countries or ["DE", "IT", "ES"]
    results = {}
    for country in targets:
        res = await import_gleif_country(
            db_path=db_path,
            country=country,
            limit=limit_per_country,
            min_age_years=min_age_years,
        )
        results[country] = res
        await asyncio.sleep(2)
    return results
