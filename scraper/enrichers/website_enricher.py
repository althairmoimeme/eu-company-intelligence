"""Website enricher — trouve l'URL du site officiel d'une entreprise.

Stratégie :
1. Nettoyage du nom (suppression suffixes légaux)
2. Recherche DuckDuckGo : "{nom}" {pays} site officiel
3. Filtrage des domaines blacklistés (LinkedIn, Wikipedia, annuaires...)
4. Validation légère (HEAD request)
5. Sauvegarde dans Company.website

Priorité aux entreprises avec profil FI (high/moderate signal).
"""
import asyncio
import logging
import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, or_

from ..db.session import get_session_factory
from ..db.models import Company, FounderIntelligence

logger = logging.getLogger(__name__)

# ── Domaines / patterns à ignorer ────────────────────────────────────────────
BLACKLIST = {
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "tiktok.com", "wikipedia.org", "wikimedia.org",
    "glassdoor.com", "glassdoor.fr", "indeed.com", "indeed.fr",
    "malt.fr", "malt.com", "societe.com", "pappers.fr", "infogreffe.fr",
    "verif.com", "manageo.fr", "kompass.com", "europages.fr",
    "europages.com", "corporama.com", "societeinfo.com", "rncs.fr",
    "bodacc.fr", "journal-officiel.gouv.fr", "krs.com.pl", "companies-house.gov.uk",
    "companieshouse.gov.uk", "gov.uk", "brreg.no", "data.gouv.fr",
    "avisverifies.com", "trustpilot.com", "google.com", "bing.com",
    "yahoo.com", "reddit.com", "quora.com", "crunchbase.com",
    "bloomberg.com", "reuters.com", "lesechos.fr", "lefigaro.fr",
    "lemonde.fr", "bfmtv.com", "challenges.fr", "capital.fr",
    "annuaire-mairie.fr", "pagesjaunes.fr", "juripredis.com",
    "manageo.fr", "rnbservices.fr", "denomineo.fr", "societeinfo.com",
    "opencorporates.com", "bizprofile.net", "yellowpages.com",
    "dnb.com", "zoominfo.com", "hoovers.com",
    # Hébergeurs / sites génériques
    "free.fr", "perso.wanadoo.fr", "pagesperso-orange.fr", "wix.com", "wixsite.com",
    "weebly.com", "jimdo.com", "webnode.fr", "e-monsite.com", "le-site-de.com",
    "sites.google.com", "squarespace.com", "webflow.io", "strikingly.com",
    "tribuca.net", "myshopify.com", "prestashop.com",
    # Annuaires & comparateurs
    "societe.com", "verif.com", "infogreffe.fr", "societeinfo.com",
    "corporama.com", "manageo.fr", "rncs.fr", "bodacc.fr",
    "annuaire-gratuit.fr", "annuaire.com", "lafourchette.com",
    "tripadvisor.fr", "tripadvisor.com", "yelp.com", "yelp.fr",
    "leboncoin.fr", "pages24.fr",
}

# Suffixes légaux à supprimer du nom pour la recherche
LEGAL_SUFFIXES = re.compile(
    r"\b(SAS|SARL|SA|SNC|SCI|EURL|SCP|SC|SE|"
    r"LTD|LIMITED|PLC|LLP|"
    r"GMBH|AG|KG|GMBH\s*&\s*CO\s*KG|"
    r"AS|ASA|ANS|DA|NUF|"
    r"AB|OY|OYJ|"
    r"NV|BV|VOF|"
    r"SRL|SPA|"
    r"SP\s*Z\s*O\.?\s*O\.?|SA)\s*$",
    re.IGNORECASE,
)

COUNTRY_QUERY = {
    "FR": ("fr-fr", "{name} site officiel"),
    "GB": ("uk-en", "{name} official website"),
    "PL": ("pl-pl", "{name} oficjalna strona"),
    "NO": ("no-no", "{name} offisiell nettside"),
    "DE": ("de-de", "{name} offizielle Website"),
    "ES": ("es-es", "{name} sitio web oficial"),
    "IT": ("it-it", "{name} sito ufficiale"),
    "NL": ("nl-nl", "{name} officiële website"),
    "DK": ("dk-da", "{name} officiel hjemmeside"),
    "SE": ("se-sv", "{name} officiell webbplats"),
    "BE": ("be-fr", "{name} site officiel"),
    "CH": ("ch-fr", "{name} site officiel"),
    "AT": ("at-de", "{name} offizielle Website"),
    "PT": ("pt-pt", "{name} site oficial"),
    "RO": ("ro-ro", "{name} site oficial"),
}


def _clean_name(name: str) -> str:
    """Supprime suffixes légaux et caractères superflus."""
    name = name.strip()
    name = LEGAL_SUFFIXES.sub("", name).strip().rstrip(",.-")
    # Supprime contenu entre parenthèses
    name = re.sub(r"\(.*?\)", "", name).strip()
    return name


def _extract_root_domain(url: str) -> Optional[str]:
    """Extrait le domaine racine (sans sous-domaine) d'une URL."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.lstrip("www.")
        # Prend les 2 derniers segments (ex: vinci.com, vinci-construction.com)
        parts = netloc.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else netloc
    except Exception:
        return None


def _is_blacklisted(url: str) -> bool:
    domain = _extract_root_domain(url)
    if not domain:
        return True
    return any(bl in domain for bl in BLACKLIST)


def _looks_corporate(url: str, company_name: str) -> bool:
    """Heuristique : l'URL ressemble-t-elle au site corporate de l'entreprise ?"""
    domain = _extract_root_domain(url) or ""
    # Normalise le nom pour comparaison
    clean = re.sub(r"[^a-z0-9]", "", _clean_name(company_name).lower())
    dom_clean = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])

    # Refus si le domaine est trop court ou générique
    if len(dom_clean) < 3:
        return False

    # Refus si aucun overlap entre nom et domaine (min 3 chars communs)
    if len(clean) >= 5 and len(dom_clean) >= 3:
        # Vérifie que le domaine contient au moins 3 chars du nom (sous-chaîne)
        for n in range(3, min(8, len(clean)) + 1):
            if clean[:n] in dom_clean or dom_clean[:n] in clean:
                return True
        # Si aucun overlap → suspect mais on accepte si DDG 1er résultat (score moyen)
        return len(clean) < 5  # très court nom → acceptable
    return True


async def _validate_url(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Vérifie que l'URL répond (HEAD). Retourne l'URL finale (après redirects)."""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        r = await client.head(url, follow_redirects=True, timeout=8)
        if r.status_code < 400:
            final = str(r.url)
            # Garde seulement le domaine racine (pas de path)
            parsed = urlparse(final)
            return f"{parsed.scheme}://{parsed.netloc}/"
    except Exception:
        pass
    return None


async def _search_website(name: str, country: str) -> Optional[str]:
    """Cherche le site web d'une entreprise via DuckDuckGo."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.error("ddgs / duckduckgo_search non installé")
            return None

    region, query_tpl = COUNTRY_QUERY.get(country, ("wt-wt", "{name} official website"))
    clean = _clean_name(name)
    query = query_tpl.format(name=clean)

    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, region=region, max_results=5, backend="html")
        if not results:
            return None
        for r in results:
            href = r.get("href", "")
            if not href:
                continue
            if _is_blacklisted(href):
                continue
            if not _looks_corporate(href, name):
                logger.debug(f"Rejeté (pas corporate): {href} pour '{name}'")
                continue
            return href
    except Exception as e:
        logger.debug(f"DDG search error for '{name}': {e}")
    return None


async def enrich_websites(
    db_path: str,
    limit: int = 200,
    concurrency: int = 3,
    only_with_fi: bool = True,
    company_ids: list[int] | None = None,  # filtre sur IDs spécifiques (optionnel)
) -> dict:
    """Enrichit Company.website pour les entreprises sans site connu.

    Args:
        db_path: Chemin vers la base SQLite.
        limit: Nombre max d'entreprises à traiter.
        concurrency: Requêtes DDG simultanées (garder bas pour éviter ban).
        only_with_fi: Si True, traite en priorité celles avec profil FI.
        company_ids: si fourni, restreint à ces IDs (ignore only_with_fi).
    """
    factory = get_session_factory(db_path)
    async with factory() as session:
        if company_ids:
            q = (
                select(Company.id, Company.name, Company.country)
                .where(Company.id.in_(company_ids))
                .where(Company.website.is_(None))
                .limit(limit)
            )
        elif only_with_fi:
            # Priorité : profils FI sans website, high/moderate d'abord
            q = (
                select(Company.id, Company.name, Company.country)
                .join(FounderIntelligence, FounderIntelligence.company_id == Company.id)
                .where(Company.website.is_(None))
                .order_by(
                    FounderIntelligence.seller_signal_strength.in_(["high", "moderate"]).desc(),
                )
                .limit(limit)
            )
        else:
            q = (
                select(Company.id, Company.name, Company.country)
                .where(Company.website.is_(None))
                .limit(limit)
            )
        rows = (await session.execute(q)).all()

    if not rows:
        return {"processed": 0, "found": 0, "skipped": 0}

    logger.info(f"Website enrichment: {len(rows)} entreprises à traiter")

    found = 0
    skipped = 0
    sem = asyncio.Semaphore(concurrency)

    async def process_one(company_id: int, name: str, country: str):
        nonlocal found, skipped
        async with sem:
            url = await _search_website(name, country)
            if not url:
                skipped += 1
                logger.debug(f"[{country}] {name} → aucun résultat")
                return

            # Validation HTTP
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"},
                timeout=10,
            ) as client:
                validated = await _validate_url(client, url)

            if not validated:
                skipped += 1
                logger.debug(f"[{country}] {name} → URL invalide: {url}")
                return

            # Sauvegarde
            async with factory() as session:
                co = await session.get(Company, company_id)
                if co:
                    co.website = validated
                    await session.commit()
                    found += 1
                    logger.info(f"[{country}] {name} → {validated}")

            # Délai anti-ban DDG
            await asyncio.sleep(1.5)

    tasks = [process_one(r[0], r[1], r[2]) for r in rows]
    await asyncio.gather(*tasks)

    return {"processed": len(rows), "found": found, "skipped": skipped}
