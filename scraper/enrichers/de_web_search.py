"""
Enrichisseur CA par recherche web (DuckDuckGo HTML) pour les sociétés DE.

Stratégie :
  1. Recherche "{Nom société} Umsatz Millionen" sur DuckDuckGo HTML
  2. Extrait les mentions de CA depuis les snippets de résultats
  3. Sauvegarde avec revenue_estimated=True (Level C — web search)

Source : snippets DuckDuckGo (données publiques, pas de clé API requise)
Limitation : extraction approximative, couvre ~30-60% des sociétés
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

# ── Status tracking ───────────────────────────────────────────────────────────
_de_web_status: dict = {"running": False, "processed": 0, "total": 0, "enriched": 0, "error": None}

def get_de_web_search_status() -> dict:
    return _de_web_status.copy()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Regex pour extraire CA depuis du texte libre
RE_REVENUE_FULL = re.compile(
    r'(?:umsatz(?:erlöse?)?|jahresumsatz|gesamtumsatz|revenue|turnover)'
    r'[^\d]{0,40}?'
    r'([\d\.,]+)\s*(?:mio\.?|mrd\.?|millionen?|milliarden?|billion)?\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_RANGE_EUR = re.compile(
    r'([\d]+)\s*[-–]\s*([\d]+)\s*(?:mio\.?|millionen?)\.?\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_MIO_EUR = re.compile(
    r'([\d\.,]+)\s*(?:mio\.?|millionen?|mrd\.?|milliarden?)\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EUR_PLAIN = re.compile(
    r'([\d\.,]+)\s*(?:mio\.?|millionen?)\s*(?:euro|eur|€)',
    re.IGNORECASE
)


def _extract_revenue_from_text(text: str) -> Optional[float]:
    """Extrait un CA depuis du texte libre (snippets, Wikipedia, etc.)."""
    text_lower = text.lower()

    # Pattern 1 : "Umsatz betrug X Mio. Euro"
    for m in RE_REVENUE_FULL.finditer(text_lower):
        try:
            val_str = m.group(1).replace(".", "").replace(",", ".")
            val = float(val_str)
            full = m.group(0)
            if "mrd" in full or "milliard" in full:
                return val * 1_000_000_000
            elif "mio" in full or "million" in full:
                return val * 1_000_000
            elif val > 1_000:
                return val * 1_000
            return val * 1_000_000
        except (ValueError, AttributeError):
            continue

    # Pattern 2 : fourchette "10 - 50 Mio. Euro" → milieu
    m = RE_RANGE_EUR.search(text_lower)
    if m:
        try:
            low = float(m.group(1))
            high = float(m.group(2))
            return ((low + high) / 2) * 1_000_000
        except (ValueError, AttributeError):
            pass

    # Pattern 3 : "X Mio. Euro" seul (sans contexte "Umsatz")
    for m in RE_MIO_EUR.finditer(text_lower):
        try:
            val_str = m.group(1).replace(".", "").replace(",", ".")
            val = float(val_str)
            full = m.group(0)
            if "mrd" in full:
                return val * 1_000_000_000
            return val * 1_000_000
        except (ValueError, AttributeError):
            continue

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
            params={"q": query, "count": "10", "mkt": "de-DE", "cc": "DE"},
            headers={**HEADERS, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"},
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
        logger.debug(f"[WebSearch] Bing error: {e}")
        return []


async def _search_revenue_ddg(
    client: httpx.AsyncClient,
    company_name: str,
    city: str = "",
) -> Optional[float]:
    """
    Recherche le CA d'une entreprise sur DuckDuckGo HTML, avec Bing en fallback.
    Retourne le CA en euros si trouvé.
    """
    global _ddg_banned
    queries = [
        f'"{company_name}" Umsatz Millionen',
        f'{company_name} Jahresumsatz',
    ]
    if city:
        queries.insert(0, f'"{company_name}" {city} Umsatz')

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
                        logger.info("[WebSearch] DDG banned, switching to Bing")
                        break
                    continue
                if r.status_code == 202:
                    logger.debug(f"[WebSearch] DDG rate limit pour: {query[:40]}")
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
                    revenue = _extract_revenue_from_text(combined)
                    if revenue and 1_000_000 <= revenue <= 5_000_000_000:
                        logger.debug(f"[WebSearch] {company_name[:40]}: {revenue/1e6:.1f}M€")
                        return revenue

            except (httpx.ConnectTimeout, httpx.ConnectError):
                _ddg_banned = True
                logger.info("[WebSearch] DDG ConnectError, switching to Bing")
                break
            except Exception as e:
                logger.debug(f"[WebSearch] Error for '{company_name}': {e}")
                continue

    # Fallback: Bing HTML
    for query in queries[:2]:
        snippets = await _search_bing(client, query)
        for combined in snippets:
            revenue = _extract_revenue_from_text(combined)
            if revenue and 1_000_000 <= revenue <= 5_000_000_000:
                logger.debug(f"[WebSearch][Bing] {company_name[:40]}: {revenue/1e6:.1f}M€")
                return revenue
        if snippets:
            break

    return None


async def enrich_de_web_search(
    db_path: str,
    limit: int = 200,
    min_score: int = 40,
    only_without_revenue: bool = True,
    delay: float = 3.0,
) -> dict:
    """
    Enrichit les CA des sociétés DE via recherche DuckDuckGo.

    Args:
        db_path: Chemin DB SQLite.
        limit: Max entreprises.
        min_score: Score Equans minimum.
        only_without_revenue: Ne traite que celles sans CA.
        delay: Délai entre requêtes (DuckDuckGo : 3s recommandé).

    Returns:
        Stats dict.
    """
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(Company)
            .join(EquansScore, Company.id == EquansScore.company_id)
            .where(Company.country == "DE")
            .where(EquansScore.total_score >= min_score)
        )
        if only_without_revenue:
            q = q.where(Company.revenue_eur.is_(None))
        q = q.order_by(EquansScore.total_score.desc()).limit(limit)
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    logger.info(f"[WebSearch] {total} entreprises DE à rechercher")

    global _de_web_status
    _de_web_status.update({"running": True, "processed": 0, "total": total, "enriched": 0, "error": None})

    found = 0
    not_found = 0
    errors = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, company in enumerate(companies):
            _de_web_status["processed"] = i + 1
            try:
                logger.info(f"[WebSearch] [{i+1}/{total}] {company.name[:50]}")
                revenue = await _search_revenue_ddg(
                    client,
                    company.name,
                    city=company.city or "",
                )
                await asyncio.sleep(delay)

                if not revenue:
                    not_found += 1
                    continue

                async with factory() as session:
                    db_co = await session.get(Company, company.id)
                    if db_co and not db_co.revenue_eur:
                        db_co.revenue_eur = revenue
                        db_co.revenue_estimated = True
                        db_co.revenue_year = 2024
                        await session.commit()
                        found += 1
                        _de_web_status["enriched"] = found
                        logger.info(
                            f"[WebSearch] ✓ {company.name[:50]} — "
                            f"CA: {revenue/1e6:.1f}M€"
                        )

            except Exception as e:
                errors += 1
                logger.warning(f"[WebSearch] Erreur {company.name}: {e}")
                continue

    _de_web_status["running"] = False
    logger.info(
        f"[WebSearch] Terminé: {found} CA trouvés, "
        f"{not_found} non trouvés, {errors} erreurs"
    )
    return {"total": total, "found": found, "not_found": not_found, "errors": errors}
