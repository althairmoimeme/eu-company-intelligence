"""
Crawleur de contenu de sites web allemands — enrichissement Equans.

Pour chaque entreprise DE avec un site web :
  1. Crawl homepage + pages clés (Leistungen, Über uns, Referenzen, Branchen)
  2. Extrait description d'activité, mots-clés Equans, effectifs, CA mentionné
  3. Met à jour companies.activity_description + employees + revenue_eur (estimé)

Mots-clés ciblés : ceux fournis pour les cibles Equans Allemagne.
"""
import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, EquansScore

logger = logging.getLogger(__name__)

# ── Mots-clés Equans DE (pour scoring de pertinence) ────────────────────────

EQUANS_KW_STRONG = [
    "elektroinstallation", "elektrotechnik", "energie- und gebäudetechnik",
    "gebäudetechnik", "technische gebäudeausrüstung", "tga",
    "schaltanlagenbau", "schaltschrankbau", "niederspannung", "mittelspannung",
    "gebäudeautomation", "automatisierungstechnik", "msr",
    "mess-, steuer- und regeltechnik", "prozessleittechnik", "prozessautomatisierung",
    "scada", "leittechnik",
    "hlk", "heizung", "lüftung", "klima", "klimatechnik", "kältetechnik",
    "industriekälte", "wärmepumpe", "kälteanlage",
    "reinraum", "reinraumtechnik",
    "brandschutz", "brandmeldetechnik", "sicherheitstechnik",
    "zutrittskontrolle", "videoüberwachung",
    "technisches facility management", "technisches gebäudemanagement",
    "instandhaltung", "wartung", "multi-technik",
    "photovoltaik", "energieeffizienz", "dekarbonisierung",
    "rechenzentrum", "data center", "kritische infrastruktur",
    "usv", "notstrom",
    "anlagenbau", "industriemontage", "industrieservice",
    "sps-technik", "steuerungstechnik",
]

EQUANS_KW_CONTEXT = [
    "pharma", "halbleiter", "mikroelektronik",
    "industrie", "industriell", "produktion",
    "krankenhaus", "klinik", "labor",
    "flughafen", "airport",
    "energieerzeugung", "kraftwerk",
]

# Pages à crawler en priorité
TARGET_PATHS = [
    "/leistungen", "/services", "/service",
    "/ueber-uns", "/uber-uns", "/über-uns", "/about", "/about-us",
    "/unternehmen", "/company",
    "/branchen", "/referenzen", "/references",
    "/produkte", "/loesungen", "/lösungen",
    "/kompetenzen", "/expertise",
]

# Regex pour extraire des infos financières/effectifs du texte
RE_REVENUE_EUR = re.compile(
    r'(?:umsatz|jahresumsatz|gesamtumsatz|revenue)[^\d]{0,30}'
    r'([\d\.,]+)\s*(?:mio\.?|mrd\.?|millionen?|milliarden?)?\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_REVENUE_NUM = re.compile(
    r'([\d\.,]+)\s*(?:mio\.?|millionen?)\s*(?:euro|eur|€)',
    re.IGNORECASE
)
RE_EMPLOYEES = re.compile(
    r'(?:über|ca\.?|rund|mehr als|circa|ungefähr)?\s*'
    r'([\d\.,]+)\s*'
    r'(?:mitarbeiter|beschäftigte|angestellte|mitarbeitende|kollegen|employees)',
    re.IGNORECASE
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}


# ── Extraction ────────────────────────────────────────────────────────────────

def _extract_text(html: str) -> str:
    """Extrait le texte brut d'une page HTML, sans nav/footer/scripts."""
    soup = BeautifulSoup(html, "lxml")
    # Retire les éléments non-contenus
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _extract_revenue(text: str) -> Optional[float]:
    """Cherche une mention de CA dans le texte (en millions €)."""
    # Cherche "Umsatz X Mio €" ou "X Millionen Euro"
    for pattern in [RE_REVENUE_EUR, RE_REVENUE_NUM]:
        for m in pattern.finditer(text):
            try:
                val_str = m.group(1).replace(".", "").replace(",", ".")
                val = float(val_str)
                # Détermine l'unité
                context = m.group(0).lower()
                if "mrd" in context or "milliarden" in context:
                    return val * 1_000_000_000
                elif "mio" in context or "million" in context:
                    return val * 1_000_000
                elif val > 1000:
                    return val * 1000  # probablement en milliers
                elif val > 1:
                    return val * 1_000_000  # probablement en millions
            except (ValueError, AttributeError):
                continue
    return None


def _extract_employees(text: str) -> Optional[int]:
    """Cherche une mention d'effectif dans le texte."""
    for m in RE_EMPLOYEES.finditer(text):
        try:
            val_str = m.group(1).replace(".", "").replace(",", "")
            val = int(val_str)
            if 5 <= val <= 50_000:  # filtre les absurdités
                return val
        except (ValueError, AttributeError):
            continue
    return None


def _score_relevance(text: str) -> tuple[int, list[str]]:
    """Compte les mots-clés Equans trouvés. Retourne (score, mots_trouvés)."""
    text_lower = text.lower()
    found = []
    for kw in EQUANS_KW_STRONG:
        if kw in text_lower:
            found.append(kw)
    return len(found), found


def _build_description(found_keywords: list[str], full_text: str) -> Optional[str]:
    """Construit une description d'activité synthétique depuis les mots-clés détectés."""
    if not found_keywords:
        return None

    # Essaie d'extraire une phrase contenant les mots-clés principaux
    sentences = re.split(r'[.!?]', full_text)
    scored_sentences = []
    for s in sentences:
        s = s.strip()
        if 20 < len(s) < 300:
            score = sum(1 for kw in found_keywords if kw.lower() in s.lower())
            if score > 0:
                scored_sentences.append((score, s))

    if scored_sentences:
        best = sorted(scored_sentences, key=lambda x: -x[0])[:2]
        desc = ". ".join(s for _, s in best)
        return desc[:500]

    # Fallback : liste des mots-clés
    return "Activités : " + ", ".join(found_keywords[:8])


def _estimate_revenue_from_employees(employees: int, nace_code: str) -> Optional[float]:
    """
    Estime le CA depuis l'effectif avec des benchmarks sectoriels allemands.
    Ratio revenus/employé pour les métiers Equans en Allemagne (source: Destatis/ZVEI/BTGA).
    """
    if not employees or employees <= 0:
        return None

    # Ratio CA/employé en € selon le NACE
    ratios = {
        "43.21": 120_000,   # Elektroinstallation
        "43.22": 110_000,   # Heizung/Klima/Sanitär
        "43.29": 115_000,   # TGA/Gebäudetechnik
        "33.20": 130_000,   # Anlagenbau
        "71.12": 150_000,   # Ingenieurbüro
        "81.10": 90_000,    # FM
        "28.29": 140_000,   # Automatisierung
        "80.20": 100_000,   # Sicherheitstechnik
    }
    ratio = ratios.get(nace_code, 115_000)
    return float(employees * ratio)


# ── Crawler ───────────────────────────────────────────────────────────────────

async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
) -> Optional[str]:
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text
    except Exception:
        pass
    return None


async def _discover_pages(
    client: httpx.AsyncClient,
    base_url: str,
    homepage_html: str,
) -> list[str]:
    """Découvre les pages pertinentes à partir de la homepage."""
    soup = BeautifulSoup(homepage_html, "lxml")
    domain = urlparse(base_url).netloc
    found_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        full_url = urljoin(base_url, a["href"])
        # Reste sur le même domaine
        if urlparse(full_url).netloc != domain:
            continue
        # Cherche les pages cibles
        for target in TARGET_PATHS:
            if target.lower() in href:
                found_urls.add(urljoin(base_url, a["href"]))
                break

    return list(found_urls)[:6]  # max 6 pages supplémentaires


async def enrich_de_website_content(
    db_path: str,
    limit: int = 200,
    min_score: int = 30,
    concurrency: int = 5,
    delay: float = 1.5,
    overwrite: bool = False,
) -> dict:
    """
    Crawl les sites des entreprises DE pour extraire :
      - description d'activité (mots-clés Equans)
      - effectifs mentionnés
      - CA mentionné ou estimé depuis effectifs

    Args:
        db_path: Chemin DB SQLite.
        limit: Nombre max d'entreprises à traiter.
        min_score: Score Equans minimum pour cibler les enrichissements.
        concurrency: Requêtes parallèles (doux = 5).
        delay: Délai entre chaque entreprise (secondes).
        overwrite: Si True, re-crawl même si description existe déjà.

    Returns:
        Stats dict.
    """
    factory = get_session_factory(db_path)

    # Récupère les entreprises DE avec site web
    async with factory() as session:
        q = (
            select(Company)
            .join(EquansScore, Company.id == EquansScore.company_id)
            .where(Company.country == "DE")
            .where(Company.website.isnot(None))
            .where(EquansScore.total_score >= min_score)
        )
        if not overwrite:
            q = q.where(Company.activity_description.is_(None))
        q = q.order_by(EquansScore.total_score.desc()).limit(limit)
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    logger.info(f"[DE-Web] {total} entreprises DE à crawler")

    enriched = 0
    with_revenue = 0
    with_employees = 0
    errors = 0

    sem = asyncio.Semaphore(concurrency)

    async def process_company(company: Company) -> dict:
        nonlocal enriched, with_revenue, with_employees, errors

        async with sem:
            async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
                try:
                    # 1. Homepage
                    homepage_html = await _fetch_page(client, company.website)
                    if not homepage_html:
                        return {}

                    all_text = _extract_text(homepage_html)

                    # 2. Pages clés découvertes
                    extra_pages = await _discover_pages(client, company.website, homepage_html)
                    for page_url in extra_pages[:4]:
                        await asyncio.sleep(0.5)
                        page_html = await _fetch_page(client, page_url)
                        if page_html:
                            all_text += " " + _extract_text(page_html)

                    # 3. Extraction
                    kw_score, found_kws = _score_relevance(all_text)
                    revenue = _extract_revenue(all_text)
                    employees = _extract_employees(all_text)
                    description = _build_description(found_kws, all_text)

                    # Estimation CA depuis effectifs si pas trouvé directement
                    if not revenue and employees:
                        revenue = _estimate_revenue_from_employees(
                            employees, company.nace_code or ""
                        )
                        if revenue:
                            logger.debug(
                                f"[DE-Web] {company.name}: "
                                f"CA estimé {revenue/1e6:.1f}M€ ({employees} empl.)"
                            )

                    return {
                        "description": description,
                        "revenue": revenue,
                        "employees": employees,
                        "kw_score": kw_score,
                        "found_kws": found_kws,
                    }

                except Exception as e:
                    logger.debug(f"[DE-Web] Erreur {company.name}: {e}")
                    return {}

    # Lance tous les crawls
    tasks = [process_company(co) for co in companies]
    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    # Sauvegarde
    for company, result in zip(companies, results_raw):
        if isinstance(result, Exception) or not result:
            errors += 1
            continue

        description = result.get("description")
        revenue = result.get("revenue")
        employees = result.get("employees")

        if not any([description, revenue, employees]):
            continue

        async with factory() as session:
            db_co = await session.get(Company, company.id)
            if not db_co:
                continue

            updated = False
            if description and not db_co.activity_description:
                db_co.activity_description = description
                updated = True

            if revenue and not db_co.revenue_eur:
                db_co.revenue_eur = revenue
                db_co.revenue_estimated = True
                db_co.revenue_year = 2025
                with_revenue += 1
                updated = True

            if employees and not db_co.employees:
                db_co.employees = employees
                with_employees += 1
                updated = True

            if updated:
                await session.commit()
                enriched += 1
                logger.info(
                    f"[DE-Web] ✓ {db_co.name[:50]} — "
                    f"kw:{result.get('kw_score',0)} "
                    f"CA:{revenue/1e6:.1f}M€ " if revenue else
                    f"[DE-Web] ✓ {db_co.name[:50]} — kw:{result.get('kw_score',0)}"
                )

        await asyncio.sleep(delay)

    logger.info(
        f"[DE-Web] Terminé: {enriched} enrichis, "
        f"{with_revenue} CA trouvés/estimés, {with_employees} effectifs, "
        f"{errors} erreurs"
    )
    return {
        "total": total,
        "enriched": enriched,
        "with_revenue": with_revenue,
        "with_employees": with_employees,
        "errors": errors,
    }
