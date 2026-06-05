"""
Enrichisseur CA via DuckDuckGo pour AT / BE / CH.

AT/CH: "{Société} Umsatz Millionen" (allemand)
BE   : "{Société} chiffre d'affaires millions" (français) ou "omzet miljoenen" (néerlandais)
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

HEADERS_DE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}

COUNTRY_QUERIES = {
    "AT": lambda name, city: [
        f'"{name}" Umsatz Millionen',
        f'{name} Jahresumsatz Euro',
    ],
    "CH": lambda name, city: [
        f'"{name}" Umsatz Millionen',
        f'{name} Jahresumsatz CHF',
        f'"{name}" chiffre affaires millions',
    ],
    "BE": lambda name, city: [
        f'"{name}" chiffre affaires millions euros',
        f'"{name}" omzet miljoenen',
        f'{name} jaarlijkse omzet',
    ],
}

# Regex multi-devise
RE_AMOUNT = re.compile(
    r'(?:umsatz|umsatzerlöse?|jahresumsatz|chiffre.d.affaires|omzet|opbrengst|revenue|turnover|ricavi)'
    r'[^\d€CHF]{0,60}([\d,\.]+)\s*'
    r'(?:mln\.?|mio\.?|millio?n?|mrd\.?|milliard\.?|mia\.?|bn)?\.?\s*(?:euro|eur|chf|€|fr\.)?',
    re.IGNORECASE
)
RE_EUR_M = re.compile(r'([\d,\.]+)\s*(?:mln\.?|mio\.?|millio?n?)\s*(?:euro|eur|chf|€|fr\.?)', re.IGNORECASE)
RE_EUR_B = re.compile(r'([\d,\.]+)\s*(?:mrd\.?|milliard\.?|mia\.?)\s*(?:euro|eur|chf|€|fr\.?)', re.IGNORECASE)

CHF_TO_EUR = 1.04


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return None


def _extract_revenue(text: str, country: str) -> Optional[float]:
    fx = CHF_TO_EUR if country == "CH" else 1.0
    t = text.lower()

    for m in RE_EUR_B.finditer(t):
        v = _parse_float(m.group(1))
        if v:
            return v * 1_000_000_000 * fx

    for m in RE_EUR_M.finditer(t):
        v = _parse_float(m.group(1))
        if v and v >= 0.1:
            return v * 1_000_000 * fx

    for m in RE_AMOUNT.finditer(t):
        v = _parse_float(m.group(1))
        if not v:
            continue
        full = m.group(0).lower()
        if any(x in full for x in ("mrd", "milliard", "mia")):
            return v * 1_000_000_000 * fx
        if any(x in full for x in ("mln", "mio", "million")):
            return v * 1_000_000 * fx
        if v > 1_000:
            return v * 1_000 * fx
        if v >= 1:
            return v * 1_000_000 * fx
    return None


_ddg_banned: bool = False


async def _search_bing_snippets(client, query: str, lang: str = "de") -> list[str]:
    """Bing HTML search fallback."""
    try:
        cc = {"de": "DE", "fr": "FR", "nl": "NL"}.get(lang, "DE")
        mkt = {"de": "de-DE", "fr": "fr-FR", "nl": "nl-NL"}.get(lang, "de-DE")
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": "10", "mkt": mkt, "cc": cc},
            headers={**HEADERS_DE, "Accept-Language": f"{lang}-{cc},{lang};q=0.9"},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        snippets = []
        for result in soup.select("li.b_algo"):
            t = result.select_one("h2")
            s = result.select_one(".b_caption p") or result.select_one("p")
            snippets.append(f"{t.get_text(' ') if t else ''} {s.get_text(' ') if s else ''}")
        return [s for s in snippets if s.strip()]
    except Exception as e:
        logger.debug(f"[DACH-WS] Bing error: {e}")
        return []


async def _search(client, name, city, country) -> Optional[float]:
    global _ddg_banned
    gen_queries = COUNTRY_QUERIES.get(country, COUNTRY_QUERIES["AT"])
    queries = gen_queries(name, city)
    lang = {"AT": "de", "CH": "de", "BE": "fr"}.get(country, "de")

    if not _ddg_banned:
        for query in queries:
            try:
                r = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query, "b": ""},
                    headers={**HEADERS_DE, "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                if r.status_code not in (200, 202):
                    if r.status_code == 403:
                        _ddg_banned = True
                        logger.info("[DACH-WS] DDG banned, switching to Bing")
                        break
                    continue
                if r.status_code == 202:
                    await asyncio.sleep(10)
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                for result in soup.select(".result"):
                    s_el = result.select_one(".result__snippet")
                    t_el = result.select_one(".result__title")
                    if not s_el:
                        continue
                    combined = (t_el.get_text(" ") if t_el else "") + " " + s_el.get_text(" ")
                    rev = _extract_revenue(combined, country)
                    if rev and 500_000 <= rev <= 5_000_000_000:
                        return rev
            except (httpx.ConnectTimeout, httpx.ConnectError):
                _ddg_banned = True
                logger.info("[DACH-WS] DDG ConnectError, switching to Bing")
                break
            except Exception as e:
                logger.debug(f"[DACH-WS] Error '{name}': {e}")

    # Fallback: Bing
    for query in queries[:2]:
        snippets = await _search_bing_snippets(client, query, lang)
        for combined in snippets:
            rev = _extract_revenue(combined, country)
            if rev and 500_000 <= rev <= 5_000_000_000:
                return rev
        if snippets:
            break
    return None


_status: dict = {"running": False, "processed": 0, "enriched": 0, "total": 0, "countries": [], "error": None}


def get_dach_web_search_status() -> dict:
    return _status.copy()


async def enrich_dach_web_search(
    db_path: str,
    countries: list[str] | None = None,
    limit: int = 300,
    min_score: int = 25,
    delay: float = 5.0,
) -> dict:
    global _status
    target = countries or ["AT", "BE", "CH"]
    _status = {"running": True, "processed": 0, "enriched": 0, "total": 0, "countries": target, "error": None}
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(Company)
            .join(EquansScore, Company.id == EquansScore.company_id)
            .where(Company.country.in_(target))
            .where(EquansScore.total_score >= min_score)
            .where(Company.revenue_eur.is_(None))
            .order_by(EquansScore.total_score.desc())
            .limit(limit)
        )
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    _status["total"] = total
    logger.info(f"[DACH-WS] {total} sociétés {'+'.join(target)} à enrichir")

    found = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, co in enumerate(companies):
            _status["processed"] = i + 1
            try:
                revenue = await _search(client, co.name, co.city or "", co.country)
                await asyncio.sleep(delay)
                if revenue:
                    async with factory() as session:
                        db_co = await session.get(Company, co.id)
                        if db_co:
                            db_co.revenue_eur = revenue
                            db_co.revenue_estimated = True
                            db_co.revenue_year = 2024
                            await session.commit()
                    found += 1
                    _status["enriched"] = found
                    logger.info(f"[DACH-WS] ✓ {co.name[:50]} ({co.country}) — CA: {revenue/1e6:.1f}M€")
            except Exception as e:
                logger.warning(f"[DACH-WS] Erreur {co.name}: {e}")

    _status["running"] = False
    return {"total": total, "found": found}
