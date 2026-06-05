"""GB SIC enricher — fetches SIC codes from Companies House API.

Targets GB companies with CH-format registration numbers (≤8 chars) that lack
activity_description. After this enricher runs, the NACE inferrer can convert
SIC codes to NACE codes (SIC 43210 → NACE 43.21).

Companies House API: GET /company/{company_number}
Rate limit: 600 req/min → safe at 0.12s delay.
"""
import asyncio
import logging
import os

import httpx
from sqlalchemy import select, update

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

CH_BASE = "https://api.company-information.service.gov.uk"

_status: dict = {
    "running": False,
    "processed": 0,
    "enriched": 0,
    "total": 0,
    "error": None,
}


def get_gb_sic_status() -> dict:
    return _status.copy()


async def enrich_gb_sic(
    db_path: str,
    api_key: str,
    limit: int = 2000,
    delay: float = 0.6,
) -> dict:
    """Fetch SIC codes from Companies House for GB companies missing activity_description."""
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}

    factory = get_session_factory(db_path)

    from sqlalchemy import func, case as sa_case
    async with factory() as session:
        # Prioritize numeric-only CH numbers (standard Eng/Welsh companies)
        # They have much higher SIC coverage than NI/SC/OC prefixed numbers
        numeric_priority = sa_case(
            (Company.registration_number.regexp_match(r'^[0-9]+$'), 0),
            else_=1
        )
        q = (
            select(Company)
            .where(
                Company.country == "GB",
                Company.registration_number.isnot(None),
                # CH-format numbers: up to 8 chars (digits or NI/SC/OC prefix)
                func.length(Company.registration_number) <= 8,
            )
            .where(
                # No SIC codes yet
                (Company.activity_description.is_(None))
                | (Company.activity_description == "")
            )
            .order_by(numeric_priority, Company.id)
        )
        if limit:
            q = q.limit(limit)
        companies = (await session.execute(q)).scalars().all()

    _status["total"] = len(companies)
    logger.info(f"[GB-SIC] {len(companies)} companies to enrich with SIC codes")

    auth = httpx.BasicAuth(api_key, "")
    enriched = 0
    not_found = 0

    async with httpx.AsyncClient(
        auth=auth,
        base_url=CH_BASE,
        timeout=15,
        follow_redirects=True,
    ) as client:
        for i, company in enumerate(companies):
            _status["processed"] = i + 1
            try:
                resp = await client.get(f"/company/{company.registration_number}")
                await asyncio.sleep(delay)

                if resp.status_code == 404:
                    not_found += 1
                    continue
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    wait = max(retry_after, 30)
                    logger.warning(f"[GB-SIC] Rate limited — sleeping {wait}s")
                    await asyncio.sleep(wait)
                    # retry once
                    resp = await client.get(f"/company/{company.registration_number}")
                    if resp.status_code != 200:
                        not_found += 1
                        continue
                if resp.status_code != 200:
                    not_found += 1
                    continue

                data = resp.json()
                sic_codes = data.get("sic_codes", [])
                creation_date = None
                if not company.creation_date:
                    raw_date = data.get("date_of_creation")
                    if raw_date and len(raw_date) == 10:
                        creation_date = raw_date

                # Build update dict — update creation_date even if no SIC
                update_vals: dict = {}
                if sic_codes:
                    update_vals["activity_description"] = ", ".join(sic_codes)
                if creation_date:
                    update_vals["creation_date"] = creation_date

                if not update_vals:
                    not_found += 1
                    continue

                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            update(Company)
                            .where(Company.id == company.id)
                            .values(**update_vals)
                        )
                enriched += 1
                _status["enriched"] = enriched

                if i % 200 == 0:
                    logger.info(
                        f"[GB-SIC] {i}/{len(companies)} — enriched={enriched}, not_found={not_found}"
                    )

            except Exception as e:
                logger.error(f"[GB-SIC] Error {company.registration_number}: {e}")
                await asyncio.sleep(1)

    _status["running"] = False
    logger.info(f"[GB-SIC] Done — {enriched} enriched, {not_found} not found")
    return {"enriched": enriched, "not_found": not_found, "total": len(companies)}
