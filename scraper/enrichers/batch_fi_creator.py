"""Création en masse de profils Founder Intelligence sans appel réseau.

Pour les entreprises qui ont des dirigeants en DB mais pas encore de profil FI.
Applique les heuristiques de l'interpréteur sur les données DB uniquement.

Très rapide : ~500–1000 profils/minute (pas de DDG, pas de scraping).
"""
import asyncio
import json
import logging
from datetime import date, datetime

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from ..db.session import get_session_factory
from ..db.models import Company, Director, FounderIntelligence, FinancialHistory

logger = logging.getLogger(__name__)


async def create_missing_fi_profiles(
    db_path: str,
    limit: int = 0,
    concurrency: int = 30,
    priority: str = "revenue",  # "revenue" | "signal" | "country"
    country: str = "",          # filtre optionnel sur un pays
    company_ids: list[int] | None = None,  # filtre sur IDs spécifiques (optionnel)
) -> dict:
    """Crée les profils FI manquants pour les entreprises avec dirigeants en DB.

    Args:
        db_path: chemin vers la DB SQLite
        limit: nombre max de profils à créer (0 = tous)
        concurrency: tâches parallèles (élevé OK, pas de réseau)
        priority: ordre de traitement — revenue | country
        country: filtre sur pays (ex: "GB", "NO") — vide = tous
        company_ids: si fourni, restreint le traitement à ces IDs

    Returns:
        dict {"processed": N, "created": N, "skipped": N, "errors": N}
    """
    from api.lib.founder_interpreter import interpret_founder
    from api.lib.financial_signals import compute_financial_signals

    factory = get_session_factory(db_path)

    # ── 1. Trouver les entreprises avec dirigeants mais sans FI ──────────────
    async with factory() as session:
        already_fi = select(FounderIntelligence.company_id)

        q = (
            select(Company.id)
            .join(Director, Director.company_id == Company.id)
            .where(Company.id.notin_(already_fi))
            .group_by(Company.id)
        )
        if company_ids:
            q = q.where(Company.id.in_(company_ids))
        if country:
            q = q.where(Company.country == country)

        if priority == "revenue":
            q = q.order_by(Company.revenue_eur.desc().nulls_last())
        else:
            q = q.order_by(Company.id.asc())

        if limit:
            q = q.limit(limit)

        rows = (await session.execute(q)).all()
        company_ids = [r[0] for r in rows]

    total = len(company_ids)
    logger.info(f"[BATCH_FI] {total} entreprises à traiter (concurrency={concurrency})")

    sem = asyncio.Semaphore(concurrency)
    created = 0
    skipped = 0
    errors = 0

    async def _process_one(company_id: int):
        nonlocal created, skipped, errors
        async with sem:
            try:
                async with factory() as session:
                    # Charger entreprise + dirigeants
                    co = (await session.execute(
                        select(Company)
                        .options(selectinload(Company.directors))
                        .where(Company.id == company_id)
                    )).scalar_one_or_none()
                    if not co:
                        skipped += 1
                        return

                    # Vérifier double-check qu'il n'y a pas déjà un FI
                    existing = (await session.execute(
                        select(FounderIntelligence.id)
                        .where(FounderIntelligence.company_id == company_id)
                    )).scalar_one_or_none()
                    if existing:
                        skipped += 1
                        return

                    # Charger historique financier
                    fh_rows = (await session.execute(
                        select(FinancialHistory)
                        .where(FinancialHistory.company_id == company_id)
                        .order_by(FinancialHistory.year)
                    )).scalars().all()

                # ── Construire les dicts ──────────────────────────────────────
                company_dict = {
                    "id": co.id,
                    "name": co.name,
                    "country": co.country,
                    "creation_date": co.creation_date,
                    "revenue_eur": co.revenue_eur,
                    "sector": co.sector,
                    "website": co.website,
                    "registration_number": co.registration_number,
                }

                directors_list = [
                    {
                        "name": d.name,
                        "role": d.role,
                        "birth_year": d.birth_year,
                        "appointed_at": d.appointed_at.isoformat() if d.appointed_at else None,
                        "tenure_years": (
                            (date.today() - d.appointed_at).days // 365
                            if d.appointed_at else None
                        ),
                        "nationality": d.nationality,
                    }
                    for d in co.directors
                ]

                # ── Signaux financiers ────────────────────────────────────────
                financial_signals = []
                if fh_rows:
                    snaps = [
                        {
                            "year": r.year,
                            "revenue_eur": r.revenue_eur,
                            "operating_income_eur": r.operating_income_eur,
                            "net_income_eur": r.net_income_eur,
                        }
                        for r in fh_rows
                    ]
                    try:
                        _, financial_signals, _, _ = compute_financial_signals(snaps)
                    except Exception:
                        pass

                # ── Score M&A simplifié ───────────────────────────────────────
                ma_score = 0
                if co.creation_date:
                    try:
                        age = 2026 - int(co.creation_date[:4])
                        if age >= 30:
                            ma_score += 15
                        if age >= 50:
                            ma_score += 10
                    except Exception:
                        pass
                if co.revenue_eur:
                    if co.revenue_eur >= 10_000_000:
                        ma_score += 10
                    if co.revenue_eur >= 50_000_000:
                        ma_score += 10

                # ── Interprétation ────────────────────────────────────────────
                profile = interpret_founder(
                    company=company_dict,
                    directors=directors_list,
                    financial_signals=financial_signals,
                    ma_score=ma_score,
                    web_data={},
                )

                # ── Choisir le dirigeant principal ────────────────────────────
                def _score_dir(d):
                    s = 0
                    if d.get("birth_year"):
                        s += (2026 - d["birth_year"])
                    if d.get("tenure_years"):
                        s += d["tenure_years"] * 2
                    role = (d.get("role") or "").lower()
                    if any(k in role for k in ["président", "pdg", "gérant", "dg", "ceo", "fondateur", "director"]):
                        s += 50
                    return s

                main_dir = max(directors_list, key=_score_dir) if directors_list else {}

                # ── Estimer l'âge ─────────────────────────────────────────────
                estimated_age = None
                if main_dir.get("birth_year"):
                    estimated_age = 2026 - main_dir["birth_year"]

                # ── Années dans le rôle ───────────────────────────────────────
                years_in_role = main_dir.get("tenure_years")

                # ── Sauvegarder ───────────────────────────────────────────────
                async with factory() as session:
                    async with session.begin():
                        fi = FounderIntelligence(
                            company_id=company_id,
                            enrichment_status="db_only",
                            full_name=main_dir.get("name"),
                            current_role=main_dir.get("role"),
                            estimated_age=estimated_age,
                            years_in_role=years_in_role,
                            founder_status=profile.founder_status,
                            operator_type=profile.operator_type,
                            seller_signal_strength=profile.seller_signal_strength,
                            seller_signal_reason=profile.seller_signal_reason,
                            main_why_now_hypothesis=profile.main_why_now_hypothesis,
                            recommended_approach_angle=profile.recommended_approach_angle,
                            avoid_in_outreach=profile.avoid_in_outreach,
                            approach_hooks=json.dumps(profile.approach_hooks) if profile.approach_hooks else None,
                            confidence_score=profile.confidence_score,
                            relationship_to_company=profile.relationship_to_company,
                            successor_signal=profile.successor_signal,
                            sources_snapshot=json.dumps({"sources": ["db"]}),
                            last_enriched_at=datetime.utcnow(),
                        )
                        session.add(fi)

                created += 1
                if created % 500 == 0:
                    logger.info(f"[BATCH_FI] Progression : {created}/{total} créés")

            except Exception as e:
                errors += 1
                logger.warning(f"[BATCH_FI] Erreur company_id={company_id}: {e}")

    await asyncio.gather(*[_process_one(cid) for cid in company_ids], return_exceptions=True)

    result = {"processed": total, "created": created, "skipped": skipped, "errors": errors}
    logger.info(f"[BATCH_FI] Terminé — {result}")
    return result
