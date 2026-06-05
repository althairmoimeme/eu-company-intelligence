"""Scraper Bundesanzeiger — Jahresabschlüsse (bilans annuels) pour GmbH allemandes.

Source officielle gratuite. Publie les comptes des "grandes" sociétés (§267 HGB) :
- Umsatzerlöse (CA) > ~€10-40M selon la taille
- ~15 000-20 000 sociétés éligibles en Allemagne

Utilise Playwright (headless Chromium) car Wicket nécessite JavaScript.
"""
import asyncio
import logging
import re
import json
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, Director

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bundesanzeiger.de/pub/de/start"
SEARCH_DELAY = 2.0   # secondes entre pages
MAX_RETRIES = 3


def _parse_revenue_de(text: str) -> float | None:
    """Parse un montant allemand (1.234.567,89) → float euros."""
    if not text:
        return None
    text = text.strip().replace(" ", "").replace("\xa0", "")
    # Format allemand : points = séparateurs milliers, virgule = décimale
    # Ex: "45.678.901,23" → 45678901.23
    text = re.sub(r"[^\d,\.-]", "", text)
    # Enlever les points (séparateurs milliers)
    text = text.replace(".", "")
    # Remplacer virgule par point
    text = text.replace(",", ".")
    try:
        val = float(text)
        return val if val > 0 else None
    except ValueError:
        return None


def _extract_revenue_from_html(html: str) -> float | None:
    """Extrait le CA (Umsatzerlöse) depuis le HTML d'un rapport annuel."""
    patterns = [
        # Format tableau: label | montant
        r"Umsatzerlöse?\s*(?:<[^>]+>)*\s*([\d\.,]+)",
        r"Umsatz\b\s*(?:<[^>]+>)*\s*([\d\.,]+)",
        r"Gesamtleistung\s*(?:<[^>]+>)*\s*([\d\.,]+)",
        r"Umsatzerlöse?\s*(?:TEUR|EUR|€)?\s*([\d\.,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            val = _parse_revenue_de(m.group(1))
            if val and val > 100_000:  # min €100K
                return val
    return None


def _extract_company_info_from_html(html: str) -> dict:
    """Extrait nom, ville, exercice depuis le HTML du rapport."""
    info = {}

    # Nom de la société
    name_m = re.search(r"<h[12][^>]*>\s*([^<]{5,80}(?:GmbH|AG|KG|OHG|UG|eG|SE)[^<]{0,30})\s*</h[12]>", html)
    if name_m:
        info["name"] = name_m.group(1).strip()

    # Exercice fiscal
    year_m = re.search(r"Geschäftsjahr[^<]*(\d{4})", html)
    if year_m:
        info["revenue_year"] = int(year_m.group(1))

    return info


async def _scrape_single_company(page, link_url: str, row_index: int) -> dict | None:
    """Ouvre un rapport annuel et extrait le CA."""
    try:
        await page.goto(link_url, wait_until="networkidle", timeout=15000)
        await asyncio.sleep(1)

        html = await page.content()
        revenue = _extract_revenue_from_html(html)
        info = _extract_company_info_from_html(html)

        if revenue:
            return {**info, "revenue_eur": revenue}
        return None
    except PlaywrightTimeout:
        logger.warning(f"Timeout sur rapport {row_index}")
        return None
    except Exception as e:
        logger.warning(f"Erreur rapport {row_index}: {e}")
        return None


async def scrape_bundesanzeiger(
    db_path: str,
    search_query: str = "Jahresabschluss GmbH",
    limit: int = 500,
    min_revenue: float = 5_000_000,
    max_revenue: float = 300_000_000,
    headless: bool = True,
) -> dict:
    """Scrape les Jahresabschlüsse depuis Bundesanzeiger.

    Args:
        db_path: chemin DB SQLite
        search_query: requête de recherche (ex: "Jahresabschluss GmbH Bayern")
        limit: nombre max de sociétés à importer
        min_revenue: CA minimum en EUR (filtre)
        max_revenue: CA maximum en EUR (filtre)
        headless: mode headless Chromium

    Returns:
        {"imported": N, "skipped": N, "no_revenue": N}
    """
    factory = get_session_factory(db_path)
    imported = 0
    skipped = 0
    no_revenue = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            locale="de-DE",
        )
        page = await context.new_page()

        try:
            # 1. Ouvrir la page de recherche
            logger.info(f"[Bundesanzeiger] Ouverture {BASE_URL}")
            await page.goto(BASE_URL, wait_until="networkidle", timeout=20000)
            await asyncio.sleep(1)

            # 2. Entrer la requête et chercher
            logger.info(f"[Bundesanzeiger] Recherche: '{search_query}'")
            await page.fill('input[name="fulltext"]', search_query)
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(1.5)

            page_num = 0
            while imported < limit:
                page_num += 1
                html = await page.content()

                # Extraire les résultats de la page courante
                rows = re.findall(
                    r'class="row back".*?'
                    r'class="col-md-3">(.*?)</div>.*?'
                    r'href="(https://www\.bundesanzeiger[^"]+panel-rows-(\d+)-[^"]+publication~link)"',
                    html, re.DOTALL
                )

                if not rows:
                    logger.info(f"[Bundesanzeiger] Aucun résultat page {page_num}, fin")
                    break

                logger.info(f"[Bundesanzeiger] Page {page_num}: {len(rows)} résultats")

                for name_city_html, report_link, row_idx in rows:
                    if imported >= limit:
                        break

                    # Parser nom et ville
                    name_city = re.sub(r"<[^>]+>", "", name_city_html).strip()
                    parts = [p.strip() for p in name_city.split("\n") if p.strip()]
                    company_name = parts[0] if parts else ""
                    city = parts[1] if len(parts) > 1 else ""

                    if not company_name:
                        skipped += 1
                        continue

                    # Cliquer sur le rapport via AJAX Wicket
                    try:
                        # Utiliser l'URL directe du lien (Wicket AJAX)
                        await page.goto(
                            report_link,
                            wait_until="networkidle",
                            timeout=20000,
                        )
                        await asyncio.sleep(1)

                        report_html = await page.content()
                        revenue = _extract_revenue_from_html(report_html)
                        rev_year_m = re.search(r"Geschäftsjahr[^<]*(\d{4})", report_html)
                        rev_year = int(rev_year_m.group(1)) if rev_year_m else datetime.now().year - 1

                    except Exception as e:
                        logger.warning(f"[Bundesanzeiger] Erreur rapport {company_name}: {e}")
                        no_revenue += 1
                        # Revenir aux résultats
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        continue

                    if not revenue:
                        no_revenue += 1
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        continue

                    # Filtre CA
                    if revenue < min_revenue or revenue > max_revenue:
                        skipped += 1
                        await page.go_back()
                        await page.wait_for_load_state("networkidle", timeout=10000)
                        continue

                    # Insérer en DB
                    row = {
                        "name": company_name[:200],
                        "country": "DE",
                        "registration_number": f"BA-{company_name[:30]}",
                        "city": city or None,
                        "revenue_eur": revenue,
                        "revenue_year": rev_year,
                        "source_url": "https://www.bundesanzeiger.de",
                    }

                    async with factory() as session:
                        async with session.begin():
                            stmt = sqlite_insert(Company).values([row])
                            stmt = stmt.on_conflict_do_update(
                                index_elements=None,
                                set_={"revenue_eur": revenue, "revenue_year": rev_year},
                            )
                            # Conflict sur (country, registration_number)
                            stmt = sqlite_insert(Company).values([row]).on_conflict_do_nothing()
                            result = await session.execute(stmt)
                            if result.rowcount:
                                imported += 1
                                logger.info(
                                    f"[Bundesanzeiger] ✓ {company_name[:40]} — "
                                    f"CA={revenue/1e6:.1f}M€ [{city}]"
                                )
                            else:
                                skipped += 1

                    # Retourner aux résultats
                    await page.go_back()
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await asyncio.sleep(SEARCH_DELAY)

                # Pagination : chercher le bouton "Suivant"
                next_btn = await page.query_selector('a[title*="nächste"]') or \
                           await page.query_selector('.pagination .next')
                if next_btn:
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(SEARCH_DELAY)
                else:
                    # Essayer le lien de pagination Wicket (ex: navigation-1)
                    next_link = re.search(
                        r'href="(https://www\.bundesanzeiger[^"]+pager-navigation-1-pagination[^"]*)"',
                        html
                    )
                    if next_link:
                        await page.goto(next_link.group(1), wait_until="networkidle")
                        await asyncio.sleep(SEARCH_DELAY)
                    else:
                        logger.info(f"[Bundesanzeiger] Pas de page suivante, fin")
                        break

        except Exception as e:
            logger.error(f"[Bundesanzeiger] Erreur fatale: {e}", exc_info=True)
        finally:
            await browser.close()

    result = {
        "imported": imported,
        "skipped": skipped,
        "no_revenue": no_revenue,
    }
    logger.info(f"[Bundesanzeiger] Terminé: {result}")
    return result


async def scrape_bundesanzeiger_sectors(
    db_path: str,
    limit_per_query: int = 200,
    min_revenue: float = 5_000_000,
) -> dict:
    """Lance plusieurs recherches sectorielles pour maximiser la couverture."""
    # Requêtes ciblées : industries avec fort potentiel M&A
    queries = [
        "Jahresabschluss GmbH Maschinenbau",
        "Jahresabschluss GmbH Produktion",
        "Jahresabschluss GmbH Logistik",
        "Jahresabschluss GmbH Großhandel",
        "Jahresabschluss GmbH Bau",
        "Jahresabschluss GmbH Lebensmittel",
        "Jahresabschluss GmbH Energie",
        "Jahresabschluss GmbH Automotive",
        "Jahresabschluss GmbH IT",
        "Jahresabschluss GmbH Dienstleistungen",
    ]
    total = {"imported": 0, "skipped": 0, "no_revenue": 0}
    for q in queries:
        res = await scrape_bundesanzeiger(
            db_path=db_path,
            search_query=q,
            limit=limit_per_query,
            min_revenue=min_revenue,
        )
        for k in total:
            total[k] += res.get(k, 0)
        await asyncio.sleep(3)
    return total
