"""
Enrichisseur CA par recherche web (DuckDuckGo) pour les sociétés GB.

Stratégie :
  1. Recherche "{Nom société} annual turnover revenue million" sur DuckDuckGo HTML
  2. Extrait les mentions de CA depuis les snippets (£ millions, turnover, revenue)
  3. Convertit GBP → EUR (taux fixe)
  4. Sauvegarde avec revenue_estimated=True (Level C — web search)

Source : snippets DuckDuckGo (données publiques, pas de clé API requise)
"""
import asyncio
import logging
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, EquansScore

logger = logging.getLogger(__name__)

GBP_TO_EUR = 1.17

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Regex pour extraire CA en GBP depuis du texte libre
RE_GBP_MILLION = re.compile(
    r'£\s*([\d,\.]+)\s*(?:m\b|mn\b|million|mln)',
    re.IGNORECASE
)
RE_GBP_BILLION = re.compile(
    r'£\s*([\d,\.]+)\s*(?:bn\b|billion)',
    re.IGNORECASE
)
RE_TURNOVER_GBP = re.compile(
    r'(?:turnover|revenue|sales)[^\d£]{0,40}£\s*([\d,\.]+)\s*(?:m\b|mn\b|million|mln|bn\b|billion)?',
    re.IGNORECASE
)
RE_GBP_K = re.compile(
    r'£\s*([\d,\.]+)\s*(?:k\b|thousand)',
    re.IGNORECASE
)
RE_EUR_MILLION = re.compile(
    r'(?:revenue|turnover|sales)[^\d€]{0,40}€\s*([\d,\.]+)\s*(?:m\b|mn\b|million)',
    re.IGNORECASE
)
RE_PLAIN_MILLION = re.compile(
    r'(?:turnover|revenue|annual sales)[^\d£€]{0,30}([\d,\.]+)\s*million',
    re.IGNORECASE
)


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return None


def _extract_revenue_gbp(text: str) -> Optional[float]:
    """Extrait un CA en £ depuis du texte libre (snippets DDG)."""
    text_lower = text.lower()

    # Pattern 1 : £X billion
    for m in RE_GBP_BILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v:
            return v * 1_000_000_000

    # Pattern 2 : £X million / £Xm
    for m in RE_GBP_MILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v and v >= 0.1:
            return v * 1_000_000

    # Pattern 3 : "turnover £X million/m/bn"
    for m in RE_TURNOVER_GBP.finditer(text_lower):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if "bn" in full or "billion" in full:
            return v * 1_000_000_000
        if "m" in full or "million" in full:
            return v * 1_000_000
        if v > 100:
            return v * 1_000  # probablement en £K
        return v * 1_000_000

    # Pattern 4 : revenue/turnover €X million (déjà en EUR)
    for m in RE_EUR_MILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v and v >= 0.1:
            return v * 1_000_000 / GBP_TO_EUR  # convert back to GBP for consistency

    # Pattern 5 : "annual sales X million"
    for m in RE_PLAIN_MILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v and v >= 0.5:
            return v * 1_000_000

    return None


_ddg_banned: bool = False  # Set True when DDG blocks us; skip DDG entirely


async def _search_bing(
    client: httpx.AsyncClient,
    query: str,
    lang: str = "en-GB",
) -> list[str]:
    """Recherche Bing HTML, retourne les snippets texte."""
    try:
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": "10", "mkt": "en-GB", "cc": "GB"},
            headers={**HEADERS, "Accept-Language": "en-GB,en;q=0.9"},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        snippets = []
        for result in soup.select("li.b_algo"):
            title_el = result.select_one("h2")
            snippet_el = result.select_one(".b_caption p") or result.select_one("p")
            title = title_el.get_text(" ") if title_el else ""
            snippet = snippet_el.get_text(" ") if snippet_el else ""
            if title or snippet:
                snippets.append(f"{title} {snippet}")
        return snippets
    except Exception as e:
        logger.debug(f"[GB-WebSearch] Bing error: {e}")
        return []


async def _search_revenue_ddg(
    client: httpx.AsyncClient,
    company_name: str,
    city: str = "",
) -> Optional[float]:
    """Recherche le CA d'une entreprise GB sur DuckDuckGo, avec Bing en fallback."""
    global _ddg_banned
    queries = [
        f'"{company_name}" annual turnover revenue',
        f'{company_name} turnover million',
    ]
    if city:
        queries.insert(0, f'"{company_name}" {city} turnover')

    # Try DDG first (unless we already know it's banned)
    if not _ddg_banned:
        for query in queries:
            try:
                r = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query, "b": ""},
                    headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                if r.status_code not in (200, 202):
                    if r.status_code == 403:
                        _ddg_banned = True
                        logger.info("[GB-WebSearch] DDG banned, switching to Bing")
                        break
                    continue
                if r.status_code == 202:
                    logger.debug(f"[GB-WebSearch] DDG rate limit: {query[:40]}")
                    await asyncio.sleep(10)
                    continue

                soup = BeautifulSoup(r.text, "lxml")
                for result in soup.select(".result"):
                    snippet_el = result.select_one(".result__snippet")
                    title_el = result.select_one(".result__title")
                    if not snippet_el:
                        continue

                    combined = ""
                    if title_el:
                        combined = title_el.get_text(separator=" ") + " "
                    combined += snippet_el.get_text(separator=" ")

                    revenue_gbp = _extract_revenue_gbp(combined)
                    if revenue_gbp and 1_000_000 <= revenue_gbp <= 10_000_000_000:
                        logger.debug(
                            f"[GB-WebSearch] {company_name[:40]}: "
                            f"£{revenue_gbp/1e6:.1f}M"
                        )
                        return revenue_gbp

            except (httpx.ConnectTimeout, httpx.ConnectError):
                _ddg_banned = True
                logger.info("[GB-WebSearch] DDG ConnectError, switching to Bing")
                break
            except Exception as e:
                logger.debug(f"[GB-WebSearch] DDG error '{company_name}': {e}")

    # Fallback: Bing HTML
    for query in queries[:2]:
        snippets = await _search_bing(client, query)
        for combined in snippets:
            revenue_gbp = _extract_revenue_gbp(combined)
            if revenue_gbp and 1_000_000 <= revenue_gbp <= 10_000_000_000:
                logger.debug(f"[GB-WebSearch][Bing] {company_name[:40]}: £{revenue_gbp/1e6:.1f}M")
                return revenue_gbp
        if snippets:
            break

    return None


# Statut global
_status: dict = {
    "running": False,
    "processed": 0,
    "enriched": 0,
    "total": 0,
    "error": None,
}


def get_gb_web_search_status() -> dict:
    return _status.copy()


async def enrich_gb_web_search(
    db_path: str,
    limit: int = 200,
    min_score: int = 30,
    delay: float = 6.0,
) -> dict:
    """
    Enrichit les sociétés GB sans CA via DuckDuckGo.
    Priorise celles avec le meilleur score Equans.
    """
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}

    factory = get_session_factory(db_path)

    async with factory() as session:
        from sqlalchemy import exists as sa_exists, case as sa_case
        # Use EXISTS subquery to avoid SQLite "too many SQL variables" limit
        scored_subq = (
            select(EquansScore.company_id)
            .where(EquansScore.total_score >= min_score, EquansScore.company_id == Company.id)
            .correlate(Company)
        )
        priority = sa_case(
            (sa_exists(scored_subq), 0),
            else_=1
        )
        q = (
            select(Company)
            .where(
                Company.country == "GB",
                Company.revenue_eur.is_(None),
            )
            .order_by(priority, Company.employees.desc().nulls_last())
        )
        if limit:
            q = q.limit(limit)

        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    _status["total"] = total
    logger.info(f"[GB-WebSearch] {total} GB companies à enrichir (limit={limit})")

    enriched = 0

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for i, company in enumerate(companies):
            _status["processed"] = i + 1
            try:
                revenue_gbp = await _search_revenue_ddg(client, company.name, company.city or "")
                if revenue_gbp:
                    revenue_eur = revenue_gbp * GBP_TO_EUR
                    async with factory() as session:
                        db_obj = await session.get(Company, company.id)
                        if db_obj:
                            db_obj.revenue_eur = revenue_eur
                            db_obj.revenue_year = 2024
                            db_obj.revenue_estimated = True
                            await session.commit()
                    enriched += 1
                    _status["enriched"] = enriched
                    logger.info(
                        f"[GB-WebSearch] ✓ {company.name[:40]}: "
                        f"£{revenue_gbp/1e6:.1f}M → €{revenue_eur/1e6:.1f}M"
                    )

            except Exception as e:
                logger.debug(f"[GB-WebSearch] {company.name}: {e}")

            await asyncio.sleep(delay)

    _status["running"] = False
    logger.info(f"[GB-WebSearch] Terminé — {enriched}/{total} enrichies")
    return {"enriched": enriched, "total": total}
