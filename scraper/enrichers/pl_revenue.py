"""Enrichissement CA des entreprises polonaises via rejestr.io (gratuit, 500 req/jour).

Pour activer :
1. Inscription gratuite sur https://rejestr.io/rejestracja
2. Copier la clé API depuis le dashboard
3. Ajouter dans .env : REJESTR_IO_API_KEY=ta_clé

Rythme : 500 req/jour → 26 000 sociétés enrichies en ~54 jours (tâche planifiable).
Données disponibles : CA (przychody), résultat net, effectif, date création, dirigeants.
"""
import asyncio
import logging
import os
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, Director

logger = logging.getLogger(__name__)

REJESTR_API = "https://api.rejestr.io/v2"
DAILY_LIMIT = 500          # Free tier
REQUESTS_PER_SECOND = 0.5  # 1 req/2s pour rester safe
PLN_TO_EUR = 0.23          # Taux de change PLN/EUR (approximatif)


def _pln_to_eur(pln: float) -> float:
    """Convertit PLN → EUR (taux approximatif, à affiner)."""
    return round(pln * PLN_TO_EUR, 0)


def _parse_company_data(data: dict) -> tuple[dict, list[dict]] | None:
    """Parse la réponse rejestr.io → (updates, directors)."""
    if not data:
        return None

    updates = {}

    # CA (przychody ze sprzedazy = chiffre d'affaires)
    financials = data.get("financials") or data.get("finances") or {}
    if financials:
        # rejestr.io retourne les années en clés
        if isinstance(financials, dict):
            try:
                latest_year = max(financials.keys(), key=lambda y: int(str(y)[:4]))
                fin = financials[latest_year]
                revenue_pln = (
                    fin.get("revenue") or
                    fin.get("przychody") or
                    fin.get("przychodySzSprzedazy") or
                    fin.get("przychodyNetto")
                )
                if revenue_pln and float(revenue_pln) > 0:
                    updates["revenue_eur"] = _pln_to_eur(float(revenue_pln))
                    try:
                        updates["revenue_year"] = int(str(latest_year)[:4])
                    except Exception:
                        pass
            except Exception:
                pass

    # Effectif
    emp = data.get("employees") or data.get("pracownicy")
    if emp:
        try:
            updates["employees"] = int(emp)
        except Exception:
            pass

    # Dirigeants
    directors = []
    for person in (data.get("management") or data.get("zarzad") or []):
        name = (person.get("name") or person.get("imieNazwisko") or "").strip()
        if not name:
            first = person.get("firstName") or person.get("imie") or ""
            last = person.get("lastName") or person.get("nazwisko") or ""
            name = f"{first} {last}".strip()
        if not name:
            continue

        birth_year = None
        for key in ("birthYear", "rokUrodzenia", "birth_year"):
            if person.get(key):
                try:
                    birth_year = int(str(person[key])[:4])
                    break
                except Exception:
                    pass

        directors.append({
            "name": name[:200],
            "role": person.get("role") or person.get("funkcja") or "Zarząd",
            "birth_year": birth_year,
        })

    return updates, directors


async def enrich_pl_revenues(
    db_path: str,
    api_key: str,
    limit: int = DAILY_LIMIT,
    skip_existing: bool = True,
    priority: str = "fi_signal",  # "fi_signal" | "revenue" | "random"
) -> dict:
    """Enrichit les entreprises PL avec CA via rejestr.io.

    Args:
        db_path: chemin DB SQLite
        api_key: clé API rejestr.io (gratuit sur rejestr.io/rejestracja)
        limit: nombre max de requêtes (défaut = quota journalier gratuit)
        skip_existing: sauter les entreprises déjà enrichies
        priority: ordre de priorité — fi_signal (profils FI d'abord) ou revenue

    Returns:
        {"enriched": N, "no_data": N, "errors": N, "quota_used": N}
    """
    factory = get_session_factory(db_path)
    enriched = 0
    no_data = 0
    errors = 0

    async with factory() as session:
        # Sélectionner les entreprises PL à enrichir
        query = (
            select(Company.id, Company.registration_number, Company.name)
            .where(Company.country == "PL")
        )
        if skip_existing:
            query = query.where(Company.revenue_eur.is_(None))

        # Tri selon la priorité
        if priority == "fi_signal":
            # Prioriser les entreprises avec profil FI (high/moderate en premier)
            from ..db.models import FounderIntelligence
            query = (
                select(Company.id, Company.registration_number, Company.name)
                .join(FounderIntelligence, FounderIntelligence.company_id == Company.id, isouter=True)
                .where(Company.country == "PL")
            )
            if skip_existing:
                query = query.where(Company.revenue_eur.is_(None))
            query = query.order_by(
                FounderIntelligence.seller_signal_strength.desc().nullslast(),
                Company.id
            )

        result = await session.execute(query.limit(limit))
        companies = result.fetchall()

    logger.info(f"[PL-REV] {len(companies)} sociétés PL à enrichir (limit={limit})")

    async with httpx.AsyncClient(
        timeout=15,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as client:
        for company_id, krs, name in companies:
            try:
                resp = await client.get(
                    f"{REJESTR_API}/company",
                    params={"krs": krs.zfill(10)},
                )

                if resp.status_code == 429:
                    logger.warning("[PL-REV] Rate limit atteint — pause 60s")
                    await asyncio.sleep(60)
                    continue

                if resp.status_code == 402:
                    logger.warning("[PL-REV] Quota journalier épuisé")
                    break

                if resp.status_code != 200:
                    no_data += 1
                    await asyncio.sleep(1 / REQUESTS_PER_SECOND)
                    continue

                data = resp.json()
                parsed = _parse_company_data(data)
                if not parsed:
                    no_data += 1
                    await asyncio.sleep(1 / REQUESTS_PER_SECOND)
                    continue

                updates, directors = parsed

                if updates or directors:
                    async with factory() as session:
                        async with session.begin():
                            if updates:
                                await session.execute(
                                    update(Company)
                                    .where(Company.id == company_id)
                                    .values(**updates)
                                )
                            if directors:
                                await session.execute(
                                    Director.__table__.delete()
                                    .where(Director.company_id == company_id)
                                )
                                await session.execute(
                                    Director.__table__.insert().values([
                                        {"company_id": company_id, **d}
                                        for d in directors
                                    ])
                                )
                    enriched += 1
                    if enriched % 50 == 0:
                        logger.info(f"[PL-REV] {enriched}/{len(companies)} enrichies")
                else:
                    no_data += 1

            except Exception as e:
                logger.warning(f"[PL-REV] Erreur {krs}: {e}")
                errors += 1

            await asyncio.sleep(1 / REQUESTS_PER_SECOND)

    result = {
        "enriched": enriched,
        "no_data": no_data,
        "errors": errors,
        "quota_used": enriched + no_data,
    }
    logger.info(f"[PL-REV] Terminé — {result}")
    return result
