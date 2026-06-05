"""Enrichissement de l'historique financier FR via l'API DGFiP/INPI (data.economie.gouv.fr).

Source gratuite, pas de clé API requise, 6,4M entrées couvrant la majorité des entreprises
françaises ayant déposé leurs comptes. Fournit CA, résultat net, EBE, endettement par SIREN/année.

Dataset: ratios_inpi_bce
URL: https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/ratios_inpi_bce
"""
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, FinancialHistory

logger = logging.getLogger(__name__)

API_BASE = "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/ratios_inpi_bce/records"
# Champs utiles pour nos signaux M&A
FIELDS = "siren,date_cloture_exercice,chiffre_d_affaires,resultat_net,ebe,taux_d_endettement,marge_ebe"


async def _fetch_company(client: httpx.AsyncClient, siren: str, sem: asyncio.Semaphore) -> list[dict]:
    """Récupère les entrées financières pour un SIREN. Retourne liste de dicts."""
    async with sem:
        try:
            params = {
                "where": f"siren={siren}",
                "select": FIELDS,
                "order_by": "date_cloture_exercice asc",
                "limit": 10,
            }
            resp = await client.get(API_BASE, params=params, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            logger.debug(f"[DGFIP] {siren}: {e}")
            return []


def _parse_year(date_str: str) -> int | None:
    """Extrait l'année depuis '2022-12-31'."""
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, TypeError):
        return None


def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


async def _process_batch(factory, entries_by_company: list[tuple[int, list[dict]]]) -> int:
    """Upsert un batch d'entrées financières. Retourne le nombre de snapshots insérés."""
    inserted = 0
    async with factory() as session:
        for company_id, entries in entries_by_company:
            for entry in entries:
                year = _parse_year(entry.get("date_cloture_exercice"))
                if not year:
                    continue
                ca = _to_float(entry.get("chiffre_d_affaires"))
                rn = _to_float(entry.get("resultat_net"))
                ebe = _to_float(entry.get("ebe"))

                # Skip if no meaningful data
                if ca is None and rn is None and ebe is None:
                    continue

                stmt = sqlite_insert(FinancialHistory).values(
                    company_id=company_id,
                    year=year,
                    revenue_eur=ca,
                    net_income_eur=rn,
                    ebitda_eur=ebe,
                    source="dgfip",
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["company_id", "year"],
                    set_=dict(
                        revenue_eur=stmt.excluded.revenue_eur,
                        net_income_eur=stmt.excluded.net_income_eur,
                        ebitda_eur=stmt.excluded.ebitda_eur,
                        source=stmt.excluded.source,
                    ),
                )
                await session.execute(stmt)
                inserted += 1
        await session.commit()
    return inserted


async def enrich_fr_financials_dgfip(
    db_path: str,
    limit: int = 0,
    concurrency: int = 8,
    skip_existing: bool = True,
) -> tuple[int, int]:
    """
    Enrichit l'historique financier des entreprises FR via l'API DGFiP (data.economie.gouv.fr).

    Args:
        db_path: Chemin vers la base SQLite
        limit: Limiter le nb d'entreprises (0 = toutes)
        concurrency: Requêtes simultanées (défaut 8)
        skip_existing: Ignorer les entreprises déjà enrichies via dgfip

    Returns:
        (total_enriched, total_snapshots)
    """
    factory = get_session_factory(db_path)
    sem = asyncio.Semaphore(concurrency)

    async with factory() as session:
        # Entreprises FR avec SIREN
        if skip_existing:
            already_subq = (
                select(FinancialHistory.company_id)
                .where(FinancialHistory.source == "dgfip")
                .distinct()
                .scalar_subquery()
            )
            q = select(Company.id, Company.registration_number, Company.name).where(
                Company.country == "FR",
                Company.registration_number.isnot(None),
                Company.id.not_in(already_subq),
            )
        else:
            q = select(Company.id, Company.registration_number, Company.name).where(
                Company.country == "FR",
                Company.registration_number.isnot(None),
            )
        result = await session.execute(q.order_by(Company.revenue_eur.desc().nulls_last()))
        companies = result.all()

    if limit:
        companies = companies[:limit]

    logger.info(f"[DGFIP] {len(companies)} entreprises FR à enrichir")

    total_enriched = 0
    total_snapshots = 0
    CHUNK = 50  # commit par lots de 50

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for i in range(0, len(companies), CHUNK):
            chunk = companies[i: i + CHUNK]

            tasks = [_fetch_company(client, c.registration_number, sem) for c in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            batch = []
            for c, r in zip(chunk, results):
                if isinstance(r, list) and r:
                    batch.append((c.id, r))

            if batch:
                inserted = await _process_batch(factory, batch)
                total_enriched += len(batch)
                total_snapshots += inserted

            done = min(i + CHUNK, len(companies))
            if done % 500 == 0 or done == len(companies):
                logger.info(
                    f"[DGFIP] {done}/{len(companies)} traitées "
                    f"| {total_enriched} enrichies | {total_snapshots} snapshots"
                )

    logger.info(f"[DGFIP] Terminé — {total_enriched} entreprises, {total_snapshots} snapshots")
    return total_enriched, total_snapshots
