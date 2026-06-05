"""
Enricher de CA pour les sociétés GB avec code LEI (pas de numéro CH direct).

Stratégie :
1. Pour chaque société GB avec LEI et sans CA :
   - Appel GLEIF /lei-records/{lei} → entity.registeredAs = numéro Companies House
2. Appel CH filing history → iXBRL document → extraction CA (Umsatz/Revenue)
3. Mise à jour DB

Cible : ~35 entreprises GB à 55pts (sector=30, integration=15, longevity=10)
        → avec CA ≥ 10M€ elles passent à 61pts+
"""
import asyncio
import logging
import re

import httpx
from sqlalchemy import select, update

from ..db.session import get_session_factory
from ..db.models import Company, EquansScore

logger = logging.getLogger(__name__)

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"
CH_FILING_API = "https://api.company-information.service.gov.uk"
CH_DOC_API = "https://document-api.company-information.service.gov.uk/document"

GBP_TO_EUR = 1.17

XBRL_REVENUE_TAGS = [
    r'<ix:nonFraction[^>]+name="[^:"]*:?(?:TurnoverRevenue|Turnover|Revenue|TotalRevenue|NetRevenue)[^"]*"[^>]*>\s*([0-9,\-\.]+)\s*</ix:nonFraction>',
    r'name="[^:"]*:?(?:TurnoverRevenue|Turnover|Revenue)[^"]*"[^>]*>\s*([0-9,\-\.]+)',
]


async def _get_ch_number_from_gleif(client: httpx.AsyncClient, lei: str) -> str | None:
    """Fetch Companies House registration number from GLEIF."""
    try:
        r = await client.get(
            GLEIF_URL.format(lei=lei),
            headers={"Accept": "application/vnd.api+json", "User-Agent": "EUCompanyScraper/1.0"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        attr = data.get("data", {}).get("attributes", {})
        entity = attr.get("entity", {})
        registered_as = entity.get("registeredAs", "")
        if registered_as and re.match(r'^[A-Z0-9]{6,8}$', str(registered_as).strip().upper()):
            return str(registered_as).strip().upper()
        # Also check legalJurisdiction to confirm it's a UK entity
        return None
    except Exception as e:
        logger.debug(f"[GB-LEI] GLEIF error for {lei}: {e}")
        return None


def _parse_xbrl_revenue(html: str) -> float | None:
    """Extract revenue from iXBRL document."""
    candidates = []
    for pattern in XBRL_REVENUE_TAGS:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for m in matches:
            try:
                raw = m.strip().replace(",", "").replace(" ", "")
                if not raw or raw == "-":
                    continue
                val = float(raw)
                if val <= 0:
                    continue
                if val < 50_000:
                    val *= 1_000
                candidates.append(val)
            except ValueError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0]
    return best if best >= 500_000 else None


async def _get_revenue_xbrl(client: httpx.AsyncClient, company_number: str,
                              ch_api_key: str) -> float | None:
    """Try to get revenue from Companies House iXBRL filing."""
    try:
        auth = (ch_api_key, "")
        resp = await client.get(
            f"{CH_FILING_API}/company/{company_number}/filing-history",
            params={"category": "accounts", "items_per_page": 3},
            auth=auth,
        )
        await asyncio.sleep(0.3)
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None

        for item in items:
            doc_url = item.get("links", {}).get("document_metadata")
            if not doc_url:
                continue
            meta_resp = await client.get(doc_url, auth=auth)
            await asyncio.sleep(0.2)
            if meta_resp.status_code != 200:
                continue
            meta = meta_resp.json()
            resources = meta.get("resources", {})
            content_type = None
            for ct in ["application/xhtml+xml", "text/html"]:
                if ct in resources:
                    content_type = ct
                    break
            if not content_type:
                continue
            content_url = meta.get("links", {}).get("document")
            if not content_url:
                continue
            doc_resp = await client.get(
                content_url,
                headers={"Accept": content_type},
                auth=auth,
            )
            await asyncio.sleep(0.3)
            if doc_resp.status_code != 200:
                continue
            revenue = _parse_xbrl_revenue(doc_resp.text)
            if revenue:
                return revenue * GBP_TO_EUR
    except Exception as e:
        logger.debug(f"[GB-LEI] XBRL error for {company_number}: {e}")
    return None


async def enrich_gb_lei_revenues(
    db_path: str,
    ch_api_key: str,
    min_score: int = 50,
    limit: int = 100,
    concurrency: int = 4,
) -> dict:
    """
    Enrichit les sociétés GB identifiées par LEI (pas de numéro CH direct).
    Étapes : GLEIF → CH number → CH iXBRL → revenue.
    """
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(Company)
            .join(EquansScore, EquansScore.company_id == Company.id)
            .where(
                Company.country == "GB",
                Company.revenue_eur.is_(None),
                # LEI format : 18-20 chars alphanumeric
                Company.registration_number.regexp_match(r'^[0-9A-Z]{18,20}$'),
                EquansScore.total_score >= min_score,
            )
            .order_by(EquansScore.total_score.desc())
            .limit(limit)
        )
        result = await session.execute(q)
        companies = result.scalars().all()

    total = len(companies)
    logger.info(f"[GB-LEI] {total} sociétés GB (LEI) à enrichir (concurrency={concurrency})")

    enriched = 0
    ch_found = 0
    not_found = 0
    sem = asyncio.Semaphore(concurrency)

    async def _process_one(company):
        nonlocal enriched, ch_found, not_found
        async with sem:
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
                timeout=30,
                follow_redirects=True,
            ) as client:
                try:
                    lei = company.registration_number
                    ch_number = await _get_ch_number_from_gleif(client, lei)
                    await asyncio.sleep(0.2)

                    if not ch_number:
                        not_found += 1
                        return

                    ch_found += 1
                    logger.debug(f"[GB-LEI] {company.name}: LEI={lei} → CH={ch_number}")

                    revenue = await _get_revenue_xbrl(client, ch_number, ch_api_key)

                    if revenue and revenue >= 500_000:
                        async with factory() as session:
                            async with session.begin():
                                await session.execute(
                                    update(Company)
                                    .where(Company.id == company.id)
                                    .values(
                                        revenue_eur=revenue,
                                        revenue_year=2024,
                                        revenue_estimated=False,
                                    )
                                )
                        enriched += 1
                        logger.info(f"[GB-LEI] ✓ {company.name}: £→€{revenue/1e6:.1f}M (CH: {ch_number})")
                    else:
                        not_found += 1
                        logger.debug(f"[GB-LEI] No revenue in CH for {company.name} (CH: {ch_number})")

                except Exception as e:
                    logger.error(f"[GB-LEI] Error for {company.name}: {e}")
                    not_found += 1

    tasks = [_process_one(c) for c in companies]
    for i, batch in enumerate([tasks[j:j+20] for j in range(0, len(tasks), 20)]):
        await asyncio.gather(*batch, return_exceptions=True)
        logger.info(
            f"[GB-LEI] Progress: {min((i+1)*20, total)}/{total} "
            f"(ch_found={ch_found}, enriched={enriched}, not_found={not_found})"
        )

    logger.info(f"[GB-LEI] Done — {enriched} enriched from {ch_found} CH numbers found")
    return {"enriched": enriched, "ch_found": ch_found, "not_found": not_found, "total": total}
