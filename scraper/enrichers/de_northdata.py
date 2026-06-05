"""Enrichisseur DE via Northdata.de API.

Northdata agrège Handelsregister + Bundesanzeiger + GLEIF pour les sociétés allemandes.
Retourne : Unternehmensgegenstand, Geschäftsführer, CA estimé, code WZ (= NACE).

Inscription gratuite : https://www.northdata.de/api
Free tier : 100 requêtes/jour (suffisant pour enrichir ~100 cibles Equans/jour).

Clé à ajouter dans .env :
    NORTHDATA_API_KEY=your_key_here
"""
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..db.session import get_session_factory
from ..db.models import Company, Director, EquansScore

logger = logging.getLogger(__name__)

BASE_URL = "https://www.northdata.de/api/v1"
CURRENT_YEAR = 2026


async def _fetch_company(
    client: httpx.AsyncClient,
    name: str,
    city: str | None,
    api_key: str,
) -> dict | None:
    """Fetch company data from Northdata by name + city."""
    params = {
        "name": name,
        "api_key": api_key,
        "financials": "true",
        "history": "false",
        "publications": "false",
    }
    if city:
        params["address"] = city

    try:
        resp = await client.get(f"{BASE_URL}/company", params=params, timeout=15)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            logger.warning("[Northdata] Rate limit atteint — pause 60s")
            await asyncio.sleep(60)
            return None
        if resp.status_code == 401:
            logger.error("[Northdata] Clé API invalide ou quota épuisé")
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        logger.debug(f"[Northdata] HTTP {e.response.status_code} pour '{name}'")
        return None
    except Exception as e:
        logger.debug(f"[Northdata] Erreur pour '{name}': {e}")
        return None


def _extract_revenue(data: dict) -> float | None:
    """Extrait le CA le plus récent depuis les données financières Northdata."""
    financials = data.get("financials") or {}
    revenues = financials.get("revenues") or []
    if not revenues:
        return None
    # Prend le plus récent
    try:
        latest = max(revenues, key=lambda r: r.get("year", 0))
        val = latest.get("value")
        currency = latest.get("currency", "EUR")
        if val and currency == "EUR":
            return float(val)
        if val and currency == "USD":
            return float(val) * 0.92  # approximation
    except Exception:
        pass
    return None


def _extract_employees(data: dict) -> int | None:
    try:
        emp = data.get("employees") or {}
        return emp.get("value")
    except Exception:
        return None


def _extract_description(data: dict) -> str | None:
    """Extrait le Unternehmensgegenstand (objet social)."""
    # Northdata renvoie le champ 'description' ou 'purpose'
    return (
        data.get("purpose")
        or data.get("description")
        or data.get("subject")  # champ alternatif
        or None
    )


def _extract_wz_code(data: dict) -> str | None:
    """Extrait le code WZ 2008 (= NACE pour l'Allemagne)."""
    industries = data.get("industries") or []
    if not industries:
        return None
    # Prend le code principal
    main = industries[0]
    code = main.get("code") or main.get("id") or ""
    # Convertit format WZ "4321" → "43.21"
    code = str(code).strip().replace(".", "").replace(" ", "")
    if len(code) >= 4 and code[:4].isdigit():
        return f"{code[:2]}.{code[2:4]}"
    return None


def _extract_directors(data: dict) -> list[dict]:
    """Extrait les Geschäftsführer / Vorstand."""
    directors = []
    for role_key in ["managingDirectors", "directors", "officers", "persons"]:
        persons = data.get(role_key) or []
        for p in persons[:5]:
            name = (
                p.get("name")
                or f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
            )
            if not name:
                continue
            role = p.get("role") or p.get("position") or "Geschäftsführer"
            birth_year = None
            birth = p.get("birthDate") or p.get("birthYear") or ""
            if birth:
                try:
                    birth_year = int(str(birth)[:4])
                except (ValueError, TypeError):
                    pass
            directors.append({
                "name": name,
                "role": role,
                "birth_year": birth_year,
            })
        if directors:
            break
    return directors


async def enrich_de_northdata(
    db_path: str,
    api_key: str,
    limit: int = 100,
    only_equans_targets: bool = True,
    min_equans_score: int = 0,
    skip_existing: bool = True,
) -> dict:
    """Enrichit les entreprises DE via Northdata.

    Args:
        db_path: Chemin vers la DB SQLite.
        api_key: Clé API Northdata.
        limit: Nombre max d'entreprises à traiter (défaut 100 = quota free/jour).
        only_equans_targets: Si True, priorise les cibles Equans scorées.
        min_equans_score: Score Equans minimum (0 = toutes les DE).
        skip_existing: Skip les entreprises qui ont déjà une description.

    Returns:
        dict avec stats.
    """
    factory = get_session_factory(db_path)

    async with factory() as session:
        if only_equans_targets and min_equans_score > 0:
            # Cibler d'abord les meilleures cibles Equans DE
            q = (
                select(Company)
                .join(EquansScore, Company.id == EquansScore.company_id)
                .where(Company.country == "DE")
                .where(EquansScore.total_score >= min_equans_score)
            )
        else:
            q = select(Company).where(Company.country == "DE")

        if skip_existing:
            q = q.where(Company.activity_description.is_(None))

        q = q.order_by(Company.revenue_eur.desc().nulls_last()).limit(limit)
        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    logger.info(f"[Northdata] {total} entreprises DE à enrichir")

    enriched = 0
    not_found = 0
    errors = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (research-bot)"},
        timeout=20,
    ) as client:
        for co in companies:
            data = await _fetch_company(client, co.name, co.city, api_key)
            await asyncio.sleep(1.2)  # ~100 req/jour → ~1 req/15min en free tier

            if not data:
                not_found += 1
                logger.debug(f"[Northdata] Non trouvé: {co.name}")
                continue

            # Extraire les données
            revenue = _extract_revenue(data)
            employees = _extract_employees(data)
            description = _extract_description(data)
            wz_code = _extract_wz_code(data)
            directors_data = _extract_directors(data)

            if not any([revenue, description, wz_code, directors_data]):
                not_found += 1
                continue

            # Sauvegarder
            async with factory() as session:
                db_co = await session.get(Company, co.id)
                if not db_co:
                    continue

                if revenue and not db_co.revenue_eur:
                    db_co.revenue_eur = revenue
                    db_co.revenue_year = CURRENT_YEAR - 1
                    db_co.revenue_estimated = True

                if employees and not db_co.employees:
                    db_co.employees = employees

                if description and not db_co.activity_description:
                    db_co.activity_description = description[:500]

                if wz_code and not db_co.nace_code:
                    db_co.nace_code = wz_code

                # Dirigeants
                if directors_data:
                    existing = (await session.execute(
                        select(Director).where(Director.company_id == co.id)
                    )).scalars().all()
                    if not existing:
                        for d in directors_data:
                            session.add(Director(
                                company_id=co.id,
                                name=d["name"],
                                role=d.get("role"),
                                birth_year=d.get("birth_year"),
                            ))

                await session.commit()
                enriched += 1
                logger.info(f"[Northdata] ✓ {co.name} — CA:{revenue} WZ:{wz_code} dirs:{len(directors_data)}")

    logger.info(f"[Northdata] Terminé: {enriched} enrichis, {not_found} non trouvés, {errors} erreurs")
    return {
        "processed": total,
        "enriched": enriched,
        "not_found": not_found,
        "errors": errors,
    }
