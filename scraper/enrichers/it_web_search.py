"""
Enrichisseur CA par recherche web (DuckDuckGo) pour les sociétés IT.

Stratégie :
  1. Recherche "{Nom société} fatturato milioni" sur DuckDuckGo
  2. Extrait les mentions de CA depuis les snippets
  3. Sauvegarde avec revenue_estimated=True (Level C)
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# Regex CA en euros — format italien
RE_FATTURATO = re.compile(
    r'(?:fatturato|ricavi|giro d.affari|revenue|turnover)[^\d€]{0,40}([\d,\.]+)\s*'
    r'(?:mln\.?|milioni?|mrd\.?|miliardi?|million|bn)?\.?\s*(?:euro|eur|€)?',
    re.IGNORECASE
)
RE_EUR_MILLION = re.compile(
    r'([\d,\.]+)\s*(?:mln\.?|milioni?)\s*(?:di\s+)?(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EUR_BILLION = re.compile(
    r'([\d,\.]+)\s*(?:mrd\.?|miliardi?)\s*(?:di\s+)?(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EUR_PLAIN = re.compile(
    r'€\s*([\d,\.]+)\s*(?:mln\.?|milioni?|mrd\.?)',
    re.IGNORECASE
)


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def _extract_revenue_eur(text: str) -> Optional[float]:
    """Extrait un CA en EUR depuis du texte (snippets DDG italiens)."""
    text_lower = text.lower()

    # Miliardi
    for m in RE_EUR_BILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v:
            return v * 1_000_000_000

    # Milioni de/d' euro
    for m in RE_EUR_MILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v and v >= 0.1:
            return v * 1_000_000

    # €X mln
    for m in RE_EUR_PLAIN.finditer(text_lower):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if "mrd" in full or "miliard" in full:
            return v * 1_000_000_000
        return v * 1_000_000

    # Fatturato/ricavi ... X milioni
    for m in RE_FATTURATO.finditer(text_lower):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if "mrd" in full or "miliard" in full:
            return v * 1_000_000_000
        if "mln" in full or "milion" in full:
            return v * 1_000_000
        if v > 1_000:
            return v * 1_000  # probablement en milliers
        if v >= 1:
            return v * 1_000_000

    return None


_ddg_banned: bool = False  # Set True when DDG blocks us; skip DDG entirely


async def _search_bing(
    client: httpx.AsyncClient,
    query: str,
) -> list[str]:
    """Recherche Bing HTML, retourne les snippets texte."""
    bing_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    try:
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": "10", "mkt": "it-IT", "cc": "IT"},
            headers=bing_headers,
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
        logger.debug(f"[IT-WebSearch] Bing error: {e}")
        return []


async def _search_revenue_ddg(
    client: httpx.AsyncClient,
    company_name: str,
    city: str = "",
) -> Optional[float]:
    global _ddg_banned
    queries = [
        f'"{company_name}" fatturato milioni',
        f'{company_name} ricavi annui',
    ]
    if city:
        queries.insert(0, f'"{company_name}" {city} fatturato')

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
                        logger.info("[IT-WebSearch] DDG banned, switching to Bing")
                        break
                    continue
                if r.status_code == 202:
                    await asyncio.sleep(10)
                    continue

                soup = BeautifulSoup(r.text, "lxml")
                for result in soup.select(".result"):
                    snippet_el = result.select_one(".result__snippet")
                    title_el = result.select_one(".result__title")
                    if not snippet_el:
                        continue
                    combined = (title_el.get_text(" ") if title_el else "") + " " + snippet_el.get_text(" ")
                    revenue = _extract_revenue_eur(combined)
                    if revenue and 500_000 <= revenue <= 5_000_000_000:
                        logger.debug(f"[IT-WebSearch] {company_name[:40]}: €{revenue/1e6:.1f}M")
                        return revenue

            except (httpx.ConnectTimeout, httpx.ConnectError):
                _ddg_banned = True
                logger.info("[IT-WebSearch] DDG ConnectError, switching to Bing")
                break
            except Exception as e:
                logger.debug(f"[IT-WebSearch] DDG error '{company_name}': {e}")

    # Fallback: Bing HTML
    for query in queries[:2]:
        snippets = await _search_bing(client, query)
        for combined in snippets:
            revenue = _extract_revenue_eur(combined)
            if revenue and 500_000 <= revenue <= 5_000_000_000:
                logger.debug(f"[IT-WebSearch][Bing] {company_name[:40]}: €{revenue/1e6:.1f}M")
                return revenue
        if snippets:
            break  # Got results from Bing, no need for 2nd query

    return None


_status: dict = {
    "running": False,
    "processed": 0,
    "enriched": 0,
    "total": 0,
    "error": None,
}


def get_it_web_search_status() -> dict:
    return _status.copy()


async def enrich_it_web_search(
    db_path: str,
    limit: int = 200,
    min_score: int = 25,
    delay: float = 6.0,
) -> dict:
    """Enrichit les sociétés IT sans CA via DuckDuckGo (fatturato)."""
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}

    factory = get_session_factory(db_path)

    async with factory() as session:
        from sqlalchemy import exists as sa_exists
        from sqlalchemy import case as sa_case
        # Use EXISTS subquery instead of IN(list) to avoid SQLite variable limit
        scored_subq = (
            select(EquansScore.company_id)
            .where(EquansScore.total_score >= min_score, EquansScore.company_id == Company.id)
            .correlate(Company)
        )
        priority = sa_case((sa_exists(scored_subq), 0), else_=1)
        q = (
            select(Company)
            .where(Company.country == "IT", Company.revenue_eur.is_(None))
            .order_by(priority, Company.employees.desc().nulls_last())
        )
        if limit:
            q = q.limit(limit)
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    _status["total"] = total
    logger.info(f"[IT-WebSearch] {total} IT companies à enrichir")

    enriched = 0

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for i, company in enumerate(companies):
            _status["processed"] = i + 1
            try:
                revenue = await _search_revenue_ddg(client, company.name, company.city or "")
                if revenue:
                    async with factory() as session:
                        db_obj = await session.get(Company, company.id)
                        if db_obj:
                            db_obj.revenue_eur = revenue
                            db_obj.revenue_year = 2024
                            db_obj.revenue_estimated = True
                            await session.commit()
                    enriched += 1
                    _status["enriched"] = enriched
                    logger.info(f"[IT-WebSearch] ✓ {company.name[:40]}: €{revenue/1e6:.1f}M")
            except Exception as e:
                logger.debug(f"[IT-WebSearch] {company.name}: {e}")

            await asyncio.sleep(delay)

    _status["running"] = False
    logger.info(f"[IT-WebSearch] Terminé — {enriched}/{total}")
    return {"enriched": enriched, "total": total}
