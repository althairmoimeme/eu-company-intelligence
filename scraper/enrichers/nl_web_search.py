"""
Enrichisseur CA via DuckDuckGo pour sociétés NL (omzet miljoenen).

Stratégie :
  1. Recherche "{Nom société} omzet miljoenen" sur DuckDuckGo HTML
  2. Extrait mentions de CA depuis snippets (€ / EUR / mln / miljoen / miljard)
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
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# Regex CA néerlandais
RE_OMZET = re.compile(
    r'(?:omzet|opbrengsten?|jaaromzet|netto.omzet|revenue|turnover)'
    r'[^\d€]{0,50}([\d,\.]+)\s*'
    r'(?:mln\.?|miljoen?|mrd\.?|miljard\.?|million|bn)?\.?\s*(?:euro|eur|€)?',
    re.IGNORECASE
)
RE_EUR_MILLION = re.compile(
    r'([\d,\.]+)\s*(?:mln\.?|miljoe?n?)\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EUR_BILLION = re.compile(
    r'([\d,\.]+)\s*(?:mrd\.?|miljard)\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EUR_PLAIN = re.compile(
    r'€\s*([\d,\.]+)\s*(?:mln\.?|miljoen?|mrd\.?|miljard)',
    re.IGNORECASE
)


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


def _extract_revenue_eur(text: str) -> Optional[float]:
    """Extrait un CA en EUR depuis du texte (snippets DDG néerlandais)."""
    text_lower = text.lower()

    for m in RE_EUR_BILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v:
            return v * 1_000_000_000

    for m in RE_EUR_MILLION.finditer(text_lower):
        v = _parse_float(m.group(1))
        if v and v >= 0.1:
            return v * 1_000_000

    for m in RE_EUR_PLAIN.finditer(text_lower):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if "mrd" in full or "miljard" in full:
            return v * 1_000_000_000
        return v * 1_000_000

    for m in RE_OMZET.finditer(text_lower):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if "mrd" in full or "miljard" in full:
            return v * 1_000_000_000
        if "mln" in full or "miljoe" in full:
            return v * 1_000_000
        if v > 1_000:
            return v * 1_000
        if v >= 1:
            return v * 1_000_000

    return None


_ddg_banned: bool = False  # Set True when DDG blocks us; skip DDG entirely


async def _search_bing(
    client: httpx.AsyncClient,
    query: str,
) -> list[str]:
    """Recherche Bing HTML, retourne les snippets texte."""
    try:
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": "10", "mkt": "nl-NL", "cc": "NL"},
            headers={**HEADERS, "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"},
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
        logger.debug(f"[NL-WebSearch] Bing error: {e}")
        return []


async def _search_revenue_ddg(
    client: httpx.AsyncClient,
    company_name: str,
    city: str = "",
) -> Optional[float]:
    global _ddg_banned
    queries = [
        f'"{company_name}" omzet miljoenen',
        f'{company_name} jaaromzet euro',
    ]
    if city:
        queries.insert(0, f'"{company_name}" {city} omzet')

    # Try DDG first (unless banned)
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
                        logger.info("[NL-WebSearch] DDG banned, switching to Bing")
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
                        logger.debug(f"[NL-WebSearch] {company_name[:40]}: €{revenue/1e6:.1f}M")
                        return revenue

            except (httpx.ConnectTimeout, httpx.ConnectError):
                _ddg_banned = True
                logger.info("[NL-WebSearch] DDG ConnectError, switching to Bing")
                break
            except Exception as e:
                logger.debug(f"[NL-WebSearch] Error '{company_name}': {e}")

    # Fallback: Bing HTML
    for query in queries[:2]:
        snippets = await _search_bing(client, query)
        for combined in snippets:
            revenue = _extract_revenue_eur(combined)
            if revenue and 500_000 <= revenue <= 5_000_000_000:
                logger.debug(f"[NL-WebSearch][Bing] {company_name[:40]}: €{revenue/1e6:.1f}M")
                return revenue
        if snippets:
            break

    return None


_status: dict = {
    "running": False, "processed": 0, "enriched": 0, "total": 0, "error": None,
}


def get_nl_web_search_status() -> dict:
    return _status.copy()


async def enrich_nl_web_search(
    db_path: str,
    limit: int = 200,
    min_score: int = 25,
    delay: float = 5.0,
) -> dict:
    """Enrichit les sociétés NL sans CA via DuckDuckGo (omzet)."""
    global _status
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "error": None}
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(Company)
            .join(EquansScore, Company.id == EquansScore.company_id)
            .where(Company.country == "NL")
            .where(EquansScore.total_score >= min_score)
            .where(Company.revenue_eur.is_(None))
            .order_by(EquansScore.total_score.desc())
            .limit(limit)
        )
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    _status["total"] = total
    logger.info(f"[NL-WebSearch] {total} sociétés NL à enrichir")

    found = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, company in enumerate(companies):
            _status["processed"] = i + 1
            try:
                revenue = await _search_revenue_ddg(client, company.name, company.city or "")
                await asyncio.sleep(delay)

                if revenue:
                    async with factory() as session:
                        db_co = await session.get(Company, company.id)
                        if db_co:
                            db_co.revenue_eur = revenue
                            db_co.revenue_estimated = True
                            db_co.revenue_year = 2024
                            await session.commit()
                    found += 1
                    _status["enriched"] = found
                    logger.info(f"[NL-WebSearch] ✓ {company.name[:50]} — CA: {revenue/1e6:.1f}M€")

            except Exception as e:
                logger.warning(f"[NL-WebSearch] Erreur {company.name}: {e}")

    _status["running"] = False
    return {"total": total, "found": found}
