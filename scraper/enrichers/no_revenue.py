"""Norway Revenue enricher via Brønnøysund accounts register.

Fixes the NULL revenue issue: the original scraper used wrong URL format
(query param instead of path param).

Fetches revenue for all NO companies in DB that have NULL revenue.
"""
import asyncio
import logging
import httpx
from sqlalchemy import select, update, delete
from ..db.session import get_session_factory
from ..db.models import Company
from ..enrichers.currency import to_eur

logger = logging.getLogger(__name__)

ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap"


async def enrich_no_revenues(db_path: str, limit: int = None):
    """Fetch revenue for Norwegian companies via accounts register."""
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = select(Company).where(
            Company.country == "NO",
            Company.revenue_eur.is_(None),
            Company.registration_number.isnot(None),
        )
        if limit:
            q = q.limit(limit)
        result = await session.execute(q)
        companies = result.scalars().all()

    logger.info(f"[NO-ENRICH] {len(companies)} Norwegian companies to enrich")

    enriched = 0
    below_threshold = 0
    not_found = 0

    MIN_REVENUE_EUR = 1_000_000  # Garder toutes sociétés >1M€ (Equans cible 5-100M€)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for i, company in enumerate(companies):
            try:
                orgnr = company.registration_number
                resp = await client.get(f"{ACCOUNTS_URL}/{orgnr}")
                await asyncio.sleep(0.15)

                if resp.status_code != 200:
                    not_found += 1
                    continue

                data = resp.json()
                acct_list = data if isinstance(data, list) else [data]
                if not acct_list:
                    not_found += 1
                    continue

                latest = acct_list[0]
                currency = latest.get("valuta", "NOK")
                rev_raw = (
                    latest.get("resultatregnskapResultat", {})
                          .get("driftsresultat", {})
                          .get("driftsinntekter", {})
                          .get("sumDriftsinntekter")
                )

                if not rev_raw or float(rev_raw) <= 0:
                    not_found += 1
                    continue

                revenue_eur = await to_eur(float(rev_raw), currency)
                revenue_year_str = latest.get("regnskapsperiode", {}).get("fraDato", "")[:4]
                revenue_year = int(revenue_year_str) if revenue_year_str else None

                if revenue_eur >= MIN_REVENUE_EUR:
                    async with factory() as session:
                        async with session.begin():
                            await session.execute(
                                update(Company)
                                .where(Company.id == company.id)
                                .values(
                                    revenue_eur=revenue_eur,
                                    revenue_year=revenue_year,
                                    revenue_estimated=False,
                                )
                            )
                    enriched += 1
                    logger.info(
                        f"[NO-ENRICH] ✓ {company.name}: "
                        f"{revenue_eur/1e6:.0f}M EUR ({currency})"
                    )
                else:
                    below_threshold += 1
                    # Conserver mais ne pas enrichir (trop petit pour Equans)
                    logger.debug(f"[NO-ENRICH] Trop petit: {company.name} ({revenue_eur/1e6:.1f}M€)")

                if i % 100 == 0:
                    logger.info(
                        f"[NO-ENRICH] Progress {i}/{len(companies)} "
                        f"(enriched={enriched}, below={below_threshold}, not_found={not_found})"
                    )

            except Exception as e:
                logger.error(f"[NO-ENRICH] Error for {company.name}: {e}")
                await asyncio.sleep(0.5)

    logger.info(
        f"[NO-ENRICH] Done — {enriched} enriched, "
        f"{below_threshold} removed (below threshold), {not_found} not found"
    )
    return {"enriched": enriched, "below_threshold": below_threshold,
            "not_found": not_found, "total": len(companies)}
