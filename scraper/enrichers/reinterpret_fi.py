"""Re-interprétation rapide des profils Founder Intelligence existants.

N'effectue PAS de nouvelles requêtes réseau. Relit les données depuis la DB
(directors, financial_history, company) et re-applique l'interpréteur avec
les heuristiques les plus récentes.

Utile pour améliorer massivement les profils "unknown" sans relancer le batch complet.
"""
import asyncio
import json
import logging
from datetime import datetime, date

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from ..db.session import get_session_factory
from ..db.models import Company, Director, FounderIntelligence, FinancialHistory

logger = logging.getLogger(__name__)


async def reinterpret_all_fi(db_path: str, limit: int = 0, concurrency: int = 20) -> int:
    """Re-interprète tous les profils FI existants en utilisant uniquement les données DB.

    Args:
        db_path: chemin vers la base SQLite
        limit: max profils à traiter (0 = tous)
        concurrency: nombre de tâches parallèles

    Returns:
        nombre de profils mis à jour
    """
    from api.lib.founder_interpreter import interpret_founder
    from api.lib.financial_signals import compute_financial_signals

    factory = get_session_factory(db_path)

    # Charger tous les profils FI existants
    async with factory() as session:
        q = select(FounderIntelligence)
        if limit:
            q = q.limit(limit)
        result = await session.execute(q)
        fi_rows = result.scalars().all()

    logger.info(f"[REINTERPRET] {len(fi_rows)} profils à ré-interpréter")

    sem = asyncio.Semaphore(concurrency)
    updated = 0

    async def _process_one(fi: FounderIntelligence):
        nonlocal updated
        async with sem:
            try:
                company_id = fi.company_id
                async with factory() as session:
                    # Charger l'entreprise + directeurs
                    co_result = await session.execute(
                        select(Company)
                        .options(selectinload(Company.directors))
                        .where(Company.id == company_id)
                    )
                    company = co_result.scalar_one_or_none()
                    if not company:
                        return

                    # Charger l'historique financier
                    fh_result = await session.execute(
                        select(FinancialHistory)
                        .where(FinancialHistory.company_id == company_id)
                        .order_by(FinancialHistory.year)
                    )
                    fh_rows = fh_result.scalars().all()

                # Construire les signaux financiers
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

                # Construire les dicts
                company_dict = {
                    "id": company.id,
                    "name": company.name,
                    "country": company.country,
                    "creation_date": company.creation_date,
                    "revenue_eur": company.revenue_eur,
                    "sector": company.sector,
                    "website": company.website,
                    "registration_number": company.registration_number,
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
                    for d in company.directors
                ]

                # Récupérer web_data depuis le snapshot existant
                web_data = {}
                if fi.sources_snapshot:
                    try:
                        snap = json.loads(fi.sources_snapshot)
                        if fi.linkedin_url:
                            web_data["linkedin_url"] = fi.linkedin_url
                        if fi.professional_email:
                            web_data["email"] = fi.professional_email
                        if fi.phone:
                            web_data["phone"] = fi.phone
                        titles = snap.get("article_titles", [])
                        if titles:
                            web_data["article_titles"] = titles
                            web_data["article_count"] = len(titles)
                    except Exception:
                        pass

                # Score M&A simplifié (âge dirigeant + ancienneté société)
                ma_score = 0
                if company.creation_date:
                    try:
                        age = 2026 - int(company.creation_date[:4])
                        if age >= 30:
                            ma_score += 15
                    except Exception:
                        pass

                # Re-interpréter
                new_profile = interpret_founder(
                    company=company_dict,
                    directors=directors_list,
                    financial_signals=financial_signals,
                    ma_score=ma_score,
                    web_data=web_data,
                )

                # Mettre à jour si signal OU textes d'approche ont changé
                signal_changed = (
                    fi.operator_type != new_profile.operator_type
                    or fi.founder_status != new_profile.founder_status
                    or fi.seller_signal_strength != new_profile.seller_signal_strength
                )
                approach_changed = (
                    fi.recommended_approach_angle != new_profile.recommended_approach_angle
                    or fi.avoid_in_outreach != new_profile.avoid_in_outreach
                )

                if signal_changed or approach_changed:
                    async with factory() as session:
                        async with session.begin():
                            fi_obj = await session.get(FounderIntelligence, fi.id)
                            if fi_obj:
                                fi_obj.operator_type = new_profile.operator_type
                                fi_obj.founder_status = new_profile.founder_status
                                fi_obj.seller_signal_strength = new_profile.seller_signal_strength
                                fi_obj.seller_signal_reason = new_profile.seller_signal_reason
                                fi_obj.main_why_now_hypothesis = new_profile.main_why_now_hypothesis
                                fi_obj.recommended_approach_angle = new_profile.recommended_approach_angle
                                fi_obj.avoid_in_outreach = new_profile.avoid_in_outreach
                                fi_obj.approach_hooks = json.dumps(new_profile.approach_hooks)
                                fi_obj.confidence_score = new_profile.confidence_score
                                fi_obj.relationship_to_company = new_profile.relationship_to_company
                                fi_obj.successor_signal = new_profile.successor_signal
                                fi_obj.last_enriched_at = datetime.utcnow()
                    updated += 1

            except Exception as e:
                logger.warning(f"[REINTERPRET] Error on company_id={fi.company_id}: {e}")

    tasks = [_process_one(fi) for fi in fi_rows]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(f"[REINTERPRET] Terminé — {updated}/{len(fi_rows)} profils mis à jour")
    return updated
