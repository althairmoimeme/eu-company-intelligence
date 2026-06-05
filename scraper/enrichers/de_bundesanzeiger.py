"""
Enrichisseur CA via Bundesanzeiger (Playwright headless).

Stratégie :
  1. Ouvre le Bundesanzeiger dans Chromium headless
  2. Remplit la barre de recherche avec le nom de la société
  3. Dans les résultats, clique sur le Jahresabschluss le plus récent
  4. Extrait l'Umsatzerlöse (CA) depuis le document HTML

Gratuit — documents publics obligatoires pour GmbH, AG, GmbH & Co. KG, KGaA, SE, eG.

Requires: pip install playwright && playwright install chromium
"""
import asyncio
import logging
import re
from typing import Optional

from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, EquansScore

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bundesanzeiger.de"

# Regex pour extraire Umsatzerlöse
RE_UMSATZ = re.compile(
    r'umsatzerlöse?[^\d€]{0,80}([\d\.,]+)\s*(?:t\.?\s*€|teur|tsd\.?\s*€|euro|€|eur)',
    re.IGNORECASE
)
RE_UMSATZ_PLAIN = re.compile(
    r'umsatzerlöse?\s*[\|\s:;]{0,5}\s*([\d\.,]+)',
    re.IGNORECASE
)

PUBLISHING_LEGAL_FORMS = [
    "gmbh", "ag", "gmbh & co. kg", "gmbh & co kg",
    "kgaa", "se", "eg", "ohg", "kg",
]


def _is_publishing_entity(name: str) -> bool:
    name_lower = name.lower()
    return any(form in name_lower for form in PUBLISHING_LEGAL_FORMS)


def _parse_umsatz(text: str) -> Optional[float]:
    """Extrait le CA (Umsatzerlöse) depuis le texte d'un document comptable."""
    text_lower = text.lower()
    for pattern in [RE_UMSATZ, RE_UMSATZ_PLAIN]:
        for m in pattern.finditer(text_lower):
            try:
                val_str = m.group(1).strip()
                if "," in val_str and "." in val_str:
                    val_str = val_str.replace(".", "").replace(",", ".")
                elif "," in val_str:
                    val_str = val_str.replace(",", ".")
                else:
                    val_str = val_str.replace(".", "")

                val = float(val_str)
                if val <= 0:
                    continue

                context = m.group(0).lower()
                if any(u in context for u in ["teur", "t. eur", "t.€", "tsd", "tsde"]):
                    return val * 1_000
                elif any(u in context for u in ["mio", "million", "m eur", "m€"]):
                    return val * 1_000_000
                elif val > 100_000:
                    return val
                elif val > 100:
                    return val * 1_000
                else:
                    return val * 1_000_000

            except (ValueError, AttributeError, IndexError):
                continue
    return None


def _clean_name_for_search(name: str) -> str:
    """Nettoie le nom de société pour la recherche Bundesanzeiger."""
    cleaned = re.sub(
        r'\b(gmbh|ag|kg|gmbh & co\.? kg|co\.?\s*kg|se|kgaa|mbh|ohg|e\.?k\.?)\b',
        '', name, flags=re.IGNORECASE
    ).strip().strip("&").strip(",").strip()
    return cleaned if len(cleaned) >= 3 else name


async def _search_and_extract_umsatz(page, company_name: str) -> Optional[float]:
    """
    Utilise une page Playwright pour:
    1. Rechercher le nom de la société sur Bundesanzeiger
    2. Cliquer sur le Jahresabschluss le plus récent
    3. Extraire l'Umsatzerlöse
    """
    search_name = _clean_name_for_search(company_name)
    logger.debug(f"[Bundesanzeiger] Search: '{search_name}' (from '{company_name}')")

    try:
        # 1. Navigation + recherche
        await page.goto(
            f"{BASE_URL}/pub/de/suche",
            wait_until="domcontentloaded",
            timeout=25_000
        )
        await asyncio.sleep(1.5)

        # Remplit la recherche
        await page.locator('input[name="fulltext"]').fill(search_name)
        await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        # 2. Trouve les liens Jahresabschluss visibles
        all_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll("a[href]")).map(a => ({
                text: a.innerText.trim().replace(/\\s+/g, ' '),
                href: a.href,
                visible: a.offsetWidth > 0 && a.offsetHeight > 0
            }))
        """)

        jahres_links = [
            lnk for lnk in all_links
            if lnk["visible"] and any(
                kw in lnk["text"].lower()
                for kw in ["jahresabschluss", "jahresabschluß", "konzernabschluss"]
            )
        ]

        # Tri par date (les plus récents ont généralement 2022/2023/2024 dans le texte)
        def _year_in_text(text):
            m = re.search(r'\b(20\d\d)\b', text)
            return int(m.group(1)) if m else 0

        jahres_links.sort(key=lambda x: _year_in_text(x["text"]), reverse=True)

        if not jahres_links:
            logger.debug(f"[Bundesanzeiger] Pas de Jahresabschluss pour: {company_name}")
            return None

        # 3. Clique sur le premier Jahresabschluss (le plus récent)
        for lnk in jahres_links[:2]:
            try:
                # Cherche et clique le lien par son URL dans la page
                link_el = await page.query_selector(f'a[href="{lnk["href"]}"]')
                if not link_el:
                    # Parfois le href change; cherche par texte exact
                    link_el = await page.query_selector(
                        f'a:has-text("{lnk["text"][:40]}")'
                    )

                if link_el:
                    await link_el.click()
                    await asyncio.sleep(3)
                else:
                    await page.goto(lnk["href"], wait_until="domcontentloaded", timeout=25_000)
                    await asyncio.sleep(2)

                # 4. Extrait le texte de la page du document
                doc_content = await page.inner_text("body")
                umsatz = _parse_umsatz(doc_content)

                if umsatz:
                    logger.info(
                        f"[Bundesanzeiger] ✓ {company_name[:50]} — "
                        f"CA: {umsatz/1e6:.1f}M€ ({lnk['text'][:40]})"
                    )
                    return umsatz

                # Si pas trouvé dans le texte visible, essaie le HTML source
                html_content = await page.content()
                umsatz = _parse_umsatz(html_content)
                if umsatz:
                    return umsatz

                # Retourne à la liste
                await page.go_back()
                await asyncio.sleep(1.5)

            except Exception as e:
                logger.debug(f"[Bundesanzeiger] Click error for {lnk['text'][:40]}: {e}")
                try:
                    await page.go_back()
                    await asyncio.sleep(1)
                except Exception:
                    pass
                continue

        return None

    except Exception as e:
        logger.debug(f"[Bundesanzeiger] Error for '{company_name}': {e}")
        return None


async def enrich_de_bundesanzeiger(
    db_path: str,
    limit: int = 100,
    min_score: int = 40,
    only_without_revenue: bool = True,
    only_publishing_entities: bool = True,
) -> dict:
    """
    Enrichit les entreprises DE avec les CA publiés au Bundesanzeiger.
    Utilise Playwright/Chromium headless pour contourner le Wicket JS.

    Args:
        db_path: Chemin DB SQLite.
        limit: Nombre max d'entreprises.
        min_score: Score Equans minimum.
        only_without_revenue: Ne traite que celles sans CA.
        only_publishing_entities: Filtre GmbH/AG.

    Returns:
        Stats dict.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "[Bundesanzeiger] playwright non installé. "
            "Exécuter: pip install playwright && playwright install chromium"
        )
        return {"total": 0, "found": 0, "not_found": 0, "errors": 1,
                "error": "playwright not installed"}

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
        q = q.order_by(EquansScore.total_score.desc()).limit(limit * 2)
        companies = (await session.execute(q)).scalars().all()

    if only_publishing_entities:
        companies = [c for c in companies if _is_publishing_entity(c.name)]

    companies = companies[:limit]
    total = len(companies)
    logger.info(f"[Bundesanzeiger] {total} entreprises DE à rechercher via Playwright")

    found = 0
    not_found = 0
    errors = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="de-DE",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Bloque images/polices pour accélérer (garde JS et AJAX)
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot}",
            lambda route: route.abort()
        )

        for i, company in enumerate(companies):
            try:
                logger.info(f"[Bundesanzeiger] [{i+1}/{total}] {company.name[:50]}")
                umsatz = await _search_and_extract_umsatz(page, company.name)
                await asyncio.sleep(2.5)

                if not umsatz:
                    not_found += 1
                    continue

                async with factory() as session:
                    db_co = await session.get(Company, company.id)
                    if db_co and not db_co.revenue_eur:
                        db_co.revenue_eur = umsatz
                        db_co.revenue_estimated = False  # CA officiel !
                        db_co.revenue_year = 2023
                        await session.commit()
                        found += 1

            except Exception as e:
                errors += 1
                logger.warning(f"[Bundesanzeiger] Erreur {company.name}: {e}")
                # Tente de récupérer en naviguant à la page de recherche
                try:
                    await page.goto(f"{BASE_URL}/pub/de/suche", wait_until="domcontentloaded", timeout=15_000)
                    await asyncio.sleep(1)
                except Exception:
                    pass
                continue

        await browser.close()

    logger.info(
        f"[Bundesanzeiger] Terminé: {found} CA trouvés, "
        f"{not_found} non trouvés, {errors} erreurs"
    )
    return {"total": total, "found": found, "not_found": not_found, "errors": errors}
