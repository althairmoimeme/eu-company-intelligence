"""UK Revenue enricher via Yahoo Finance (for LSE-listed companies)
and Companies House iXBRL accounts (for private companies).

Strategy:
1. Search Yahoo Finance by company name → get LSE ticker
2. If found, fetch annual revenue (totalRevenue)
3. If not listed, try Companies House iXBRL filing
4. Update DB with revenue data
"""
import asyncio
import logging
import re
import httpx
import yfinance as yf
from sqlalchemy import select, update
from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

YF_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
CH_DOC_API = "https://document-api.company-information.service.gov.uk/document"
CH_FILING_API = "https://api.company-information.service.gov.uk"

# Regex pour extraire le chiffre d'affaires depuis iXBRL Companies House.
# Format réel observé: <ix:nonFraction name="d:TurnoverRevenue" ...>26,389,077</ix:nonFraction>
# Le préfixe varie: d: / core: / uk-gaap: / ifrs-full: / bus: / (vide)
XBRL_REVENUE_TAGS = [
    # Format ix:nonFraction avec n'importe quel préfixe namespace
    r'<ix:nonFraction[^>]+name="[^:"]*:?(?:TurnoverRevenue|Turnover|Revenue|TotalRevenue|NetRevenue)[^"]*"[^>]*>\s*([0-9,\-\.]+)\s*</ix:nonFraction>',
    # Fallback: attribut name= seul (sans tag ix:)
    r'name="[^:"]*:?(?:TurnoverRevenue|Turnover|Revenue)[^"]*"[^>]*>\s*([0-9,\-\.]+)',
]

# GBP to EUR rate (approximate)
GBP_TO_EUR = 1.17


async def enrich_uk_revenues(db_path: str, ch_api_key: str,
                               limit: int = None, resume: bool = True,
                               concurrency: int = 8):
    """Main enrichment function. Updates UK companies with revenue data.

    Priorise les entreprises avec profil FI (high/moderate signal d'abord).
    Traitement concurrent (concurrency=8 par défaut pour respecter rate limits CH).
    """
    from scraper.db.models import FounderIntelligence
    factory = get_session_factory(db_path)

    async with factory() as session:
        from sqlalchemy import exists as sa_exists, case as sa_case
        from scraper.db.models import EquansScore
        # Use EXISTS subquery to avoid SQLite "too many SQL variables" limit
        fi_subq = (
            select(FounderIntelligence.company_id)
            .where(
                FounderIntelligence.seller_signal_strength.in_(["high", "moderate"]),
                FounderIntelligence.company_id == Company.id,
            )
            .correlate(Company)
        )
        priority_case = sa_case(
            (sa_exists(fi_subq), 0),
            (Company.name.ilike("%PLC%"), 1),
            (Company.name.ilike("%GROUP%"), 2),
            else_=3
        )
        q = (
            select(Company)
            .outerjoin(EquansScore, EquansScore.company_id == Company.id)
            .where(
                Company.country == "GB",
                Company.revenue_eur.is_(None),
                # Only target CH-format (8-digit) numbers for iXBRL lookup
                # + Equans-relevant sectors (sector_score >= 20)
                Company.registration_number.regexp_match(r'^[A-Z0-9]{6,8}$'),
            )
            .order_by(
                EquansScore.total_score.desc().nulls_last(),
                priority_case,
                Company.employees.desc().nulls_last(),
            )
        )

        if limit:
            q = q.limit(limit)

        result = await session.execute(q)
        companies = result.scalars().all()

    logger.info(f"[UK-ENRICH] {len(companies)} UK companies to enrich (concurrency={concurrency})")

    enriched = 0
    not_found = 0
    sem = asyncio.Semaphore(concurrency)

    async def _process_one(company):
        nonlocal enriched, not_found
        async with sem:
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
                timeout=30,
                follow_redirects=True,
            ) as client:
                try:
                    revenue = await _get_revenue_yahoo(client, company.name)

                    if revenue is None:
                        revenue = await _get_revenue_xbrl(
                            client, company.registration_number, ch_api_key
                        )

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
                        logger.info(f"[UK-ENRICH] ✓ {company.name}: €{revenue/1e6:.1f}M")
                    else:
                        not_found += 1

                except Exception as e:
                    logger.error(f"[UK-ENRICH] Error for {company.name}: {e}")
                    not_found += 1

                await asyncio.sleep(0.3)  # rate limit CH API

    # Batch processing avec logs de progression
    tasks = [_process_one(c) for c in companies]
    for i, batch in enumerate(
        [tasks[j:j+50] for j in range(0, len(tasks), 50)]
    ):
        await asyncio.gather(*batch, return_exceptions=True)
        logger.info(f"[UK-ENRICH] Progress: {min((i+1)*50, len(companies))}/{len(companies)} "
                    f"(enriched={enriched}, not_found={not_found})")

    logger.info(f"[UK-ENRICH] Done — {enriched} enriched, {not_found} not found")
    return {"enriched": enriched, "not_found": not_found, "total": len(companies)}


async def _get_revenue_yahoo(client: httpx.AsyncClient, company_name: str) -> float | None:
    """Search Yahoo Finance for a UK company and get its revenue."""
    try:
        # Search for the company
        resp = await client.get(
            YF_SEARCH_URL,
            params={"q": company_name, "country": "GB", "lang": "en-GB",
                    "type": "equity", "newsCount": 0},
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        quotes = data.get("quotes", [])

        # Find a UK-listed company (ticker ends in .L)
        ticker = None
        for q in quotes[:5]:
            sym = q.get("symbol", "")
            name_match = _name_similarity(company_name, q.get("longname") or q.get("shortname") or "")
            if sym.endswith(".L") and name_match > 0.6:
                ticker = sym
                break

        if not ticker:
            return None

        # Get financial data via yfinance (run in thread to avoid blocking)
        loop = asyncio.get_event_loop()
        revenue = await loop.run_in_executor(None, _fetch_yfinance_revenue, ticker)
        return revenue

    except Exception as e:
        logger.debug(f"[UK-ENRICH] Yahoo Finance error for {company_name}: {e}")
        return None


def _fetch_yfinance_revenue(ticker: str) -> float | None:
    """Fetch revenue from Yahoo Finance (synchronous)."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        revenue_gbp = info.get("totalRevenue")
        if revenue_gbp and float(revenue_gbp) > 0:
            return float(revenue_gbp) * GBP_TO_EUR
        return None
    except Exception:
        return None


async def _get_revenue_xbrl(client: httpx.AsyncClient, company_number: str,
                              ch_api_key: str) -> float | None:
    """Try to get revenue from Companies House iXBRL filing."""
    try:
        auth = (ch_api_key, "")

        # Get latest accounts filing
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

            # Check if iXBRL is available
            meta_resp = await client.get(doc_url, auth=auth)
            await asyncio.sleep(0.2)
            if meta_resp.status_code != 200:
                continue

            meta = meta_resp.json()
            resources = meta.get("resources", {})

            # Only process iXBRL documents
            content_type = None
            for ct in ["application/xhtml+xml", "text/html"]:
                if ct in resources:
                    content_type = ct
                    break

            if not content_type:
                continue

            # Download iXBRL document (link already contains /content)
            content_url = meta.get("links", {}).get("document")
            doc_resp = await client.get(
                content_url,
                headers={"Accept": content_type},
                auth=auth,
            )
            await asyncio.sleep(0.3)

            if doc_resp.status_code != 200:
                continue

            # Parse XBRL for revenue
            html_content = doc_resp.text
            revenue = _parse_xbrl_revenue(html_content)
            if revenue:
                return revenue * GBP_TO_EUR

    except Exception as e:
        logger.debug(f"[UK-ENRICH] XBRL error for {company_number}: {e}")

    return None


def _parse_xbrl_revenue(html: str) -> float | None:
    """Extrait le CA depuis un document iXBRL Companies House.

    Format réel observé :
      <ix:nonFraction name="d:TurnoverRevenue" ...>26,389,077</ix:nonFraction>
    Les valeurs sont en £ entières (pas en milliers).
    On prend la valeur la plus grande parmi les matches (exercice le plus récent).
    """
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
                # Détection unité : si val < 50_000 c'est probablement en £000s
                if val < 50_000:
                    val *= 1_000
                candidates.append(val)
            except ValueError:
                continue

    if not candidates:
        return None

    # Prend la valeur médiane parmi les candidates (évite outliers)
    # ou la plus grande si on a peu de valeurs
    candidates.sort(reverse=True)
    best = candidates[0]

    # Minimum £500K pour être significatif
    return best if best >= 500_000 else None


def _name_similarity(name1: str, name2: str) -> float:
    """Simple name similarity score."""
    if not name1 or not name2:
        return 0.0

    n1 = re.sub(r'\b(PLC|LTD|LIMITED|GROUP|HOLDINGS?|INC|CORP)\b', '',
                name1.upper()).strip()
    n2 = re.sub(r'\b(PLC|LTD|LIMITED|GROUP|HOLDINGS?|INC|CORP)\b', '',
                name2.upper()).strip()

    if n1 == n2:
        return 1.0

    # Word overlap
    words1 = set(n1.split())
    words2 = set(n2.split())
    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)
