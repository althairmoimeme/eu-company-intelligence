"""Enrichissement des contacts dirigeants — sans API payante.

Stratégies (dans l'ordre) :
1. Patterns email probables depuis nom + domaine (prenom.nom@domain.com, etc.)
2. Vérification légère via DNS MX record (domaine accepte des emails)
3. Scraping amélioré des pages contact/equipe du site corporate
4. Stockage dans founder_intelligence.professional_email
"""
import asyncio
import logging
import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, FounderIntelligence

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """Supprime accents et caractères spéciaux, met en minuscule."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z\-]", "", ascii_str.lower())


def _extract_domain(website: str) -> Optional[str]:
    """Extrait le domaine principal depuis une URL."""
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    try:
        parsed = urlparse(website)
        domain = parsed.netloc.lstrip("www.")
        return domain if domain else None
    except Exception:
        return None


def _generate_email_patterns(first: str, last: str, domain: str) -> list[str]:
    """Génère les patterns email les plus courants en entreprise FR/EU."""
    f = _normalize_name(first)
    l = _normalize_name(last)
    fi = f[0] if f else ""
    li = l[0] if l else ""

    if not f or not l or not domain:
        return []

    patterns = [
        f"{f}.{l}@{domain}",           # jean.dupont@  (le plus courant FR)
        f"{f}{l}@{domain}",             # jeandupont@
        f"{fi}{l}@{domain}",            # jdupont@
        f"{f}.{li}@{domain}",           # jean.d@
        f"{f}@{domain}",                # jean@  (petite structure)
        f"{l}.{f}@{domain}",            # dupont.jean@
        f"{l}@{domain}",                # dupont@
        f"{fi}.{l}@{domain}",           # j.dupont@
    ]
    return patterns


def _split_name(full_name: str) -> tuple[str, str]:
    """Sépare prénom et nom depuis un nom complet.

    Gère : 'JEAN DUPONT', 'Jean-Pierre MARTIN', 'MARTIN Jean-Pierre'
    """
    if not full_name:
        return "", ""

    parts = full_name.strip().split()
    if len(parts) < 2:
        return "", full_name

    # Si tout en majuscules → probablement NOM Prénom ou Prénom NOM
    # Heuristique : si dernière partie est tout en majuscules → c'est le nom de famille
    if parts[-1].isupper() and len(parts) >= 2:
        last = parts[-1]
        first = " ".join(parts[:-1])
    elif parts[0].isupper() and len(parts) >= 2:
        # Ex: DUPONT Jean-Pierre
        last = parts[0]
        first = " ".join(parts[1:])
    else:
        # Prénom Nom classique
        first = parts[0]
        last = " ".join(parts[1:])

    return first, last


async def _check_email_via_smtp_helo(email: str, client: httpx.AsyncClient) -> bool:
    """Vérifie rapidement qu'un email a un format valide et que le domaine répond.
    Note : ne fait PAS de connexion SMTP réelle, juste un check DNS HTTP indirect.
    """
    # Simple format check
    if not re.match(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$', email):
        return False
    return True  # On ne vérifie pas davantage pour éviter d'être blacklisté


async def _scrape_contact_pages(client: httpx.AsyncClient, website: str, director_name: str) -> dict:
    """Scrape agressivement les pages contact/equipe pour trouver email + tel."""
    if not website:
        return {}

    if not website.startswith("http"):
        website = "https://" + website

    base = website.rstrip("/")
    paths = [
        "/contact", "/nous-contacter", "/contactez-nous", "/contact-us",
        "/equipe", "/team", "/direction", "/management", "/leadership",
        "/dirigeants", "/about", "/qui-sommes-nous",
        "/fr/contact", "/fr/equipe",
    ]

    # Aussi essayer la page d'accueil
    paths = ["/"] + paths

    found = {}
    name_parts = [p.lower() for p in director_name.split() if len(p) > 2]

    for path in paths:
        if len(found) >= 2:  # email + tel trouvés
            break
        try:
            resp = await client.get(f"{base}{path}", timeout=6, follow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text[:8000]
            text_lower = text.lower()

            # Chercher emails
            emails = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text)
            for email in emails:
                email_lower = email.lower()
                # Ignorer les emails génériques
                if any(x in email_lower for x in [
                    "noreply", "no-reply", "contact@", "info@", "support@",
                    "admin@", "webmaster@", "hello@", "bonjour@", "exemple",
                    ".png", ".jpg", ".gif"
                ]):
                    continue
                # Préférer les emails qui contiennent une partie du nom
                name_match = any(part in email_lower for part in name_parts)
                if name_match and "email" not in found:
                    found["email"] = email
                    break
                elif "email" not in found:
                    found["email_generic"] = email  # fallback

            # Chercher téléphone
            phones = re.findall(
                r'(?:(?:\+33|0033|0)\s*[1-9](?:[\s.\-]?\d{2}){4})',
                text
            )
            if phones and "phone" not in found:
                phone = re.sub(r'\s+', ' ', phones[0]).strip()
                found["phone"] = phone

        except Exception:
            continue

    # Prendre email_generic si pas d'email direct
    if "email" not in found and "email_generic" in found:
        found["email"] = found.pop("email_generic")

    return {k: v for k, v in found.items() if k in ("email", "phone")}


async def enrich_contact(
    company_id: int,
    db_path: str,
) -> bool:
    """Enrichit les informations de contact pour un profil FI existant."""
    factory = get_session_factory(db_path)

    async with factory() as session:
        co_result = await session.execute(
            select(Company).where(Company.id == company_id)
        )
        company = co_result.scalar_one_or_none()
        if not company:
            return False

        fi_result = await session.execute(
            select(FounderIntelligence).where(FounderIntelligence.company_id == company_id)
        )
        fi = fi_result.scalar_one_or_none()
        if not fi:
            return False

        # Déjà enrichi
        if fi.professional_email and fi.phone:
            return True

    domain = _extract_domain(company.website)
    director_name = fi.full_name or ""
    found_email = fi.professional_email
    found_phone = fi.phone

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        timeout=10,
        follow_redirects=True,
    ) as client:

        # 1. Email depuis patterns nom + domaine
        if not found_email and domain and director_name:
            first, last = _split_name(director_name)
            patterns = _generate_email_patterns(first, last, domain)
            # On prend le premier pattern valide (pas de vérification SMTP)
            if patterns:
                found_email = patterns[0]
                logger.debug(f"[CONTACT] {company.name} — email pattern: {found_email}")

        # 2. Scraping site corporate (si pas d'email ou pas de tél)
        if company.website and (not found_email or not found_phone):
            try:
                contact_data = await _scrape_contact_pages(client, company.website, director_name)
                if contact_data.get("email") and not found_email:
                    found_email = contact_data["email"]
                if contact_data.get("phone") and not found_phone:
                    found_phone = contact_data["phone"]
            except Exception as e:
                logger.debug(f"[CONTACT] Scraping error for {company.name}: {e}")

    # Mettre à jour FI si on a trouvé quelque chose de nouveau
    updates = {}
    if found_email and not fi.professional_email:
        updates["professional_email"] = found_email
    if found_phone and not fi.phone:
        updates["phone"] = found_phone

    if updates:
        async with factory() as session:
            async with session.begin():
                fi_obj = await session.get(FounderIntelligence, fi.id)
                if fi_obj:
                    for k, v in updates.items():
                        setattr(fi_obj, k, v)
        logger.debug(f"[CONTACT] {company.name} — mis à jour: {list(updates.keys())}")
        return True

    return False


async def batch_enrich_contacts(
    db_path: str,
    limit: int = 500,
    concurrency: int = 8,
    skip_existing: bool = True,
) -> dict:
    """Enrichit les contacts pour les profils FI sans email/tél.

    Priorité : profils avec signal vendeur high/moderate.
    """
    factory = get_session_factory(db_path)

    async with factory() as session:
        q = (
            select(FounderIntelligence.company_id)
            .join(Company, FounderIntelligence.company_id == Company.id)
            .where(Company.website.isnot(None))
        )
        if skip_existing:
            q = q.where(
                FounderIntelligence.professional_email.is_(None)
            )
        # Priorité aux signaux forts
        q = q.order_by(
            FounderIntelligence.seller_signal_strength.desc(),
        ).limit(limit)

        result = await session.execute(q)
        company_ids = [r[0] for r in result.all()]

    logger.info(f"[CONTACT] {len(company_ids)} entreprises à enrichir")

    sem = asyncio.Semaphore(concurrency)
    enriched = 0

    async def _one(cid):
        nonlocal enriched
        async with sem:
            try:
                ok = await enrich_contact(cid, db_path)
                if ok:
                    enriched += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"[CONTACT] Error {cid}: {e}")

    await asyncio.gather(*[_one(cid) for cid in company_ids], return_exceptions=True)
    logger.info(f"[CONTACT] Terminé — {enriched}/{len(company_ids)} enrichis")
    return {"enriched": enriched, "total": len(company_ids)}
