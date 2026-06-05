"""Founder Intelligence — endpoints."""
import json
import logging
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..settings import get_settings
from scraper.db.session import get_session_factory
from scraper.db.models import Company, Director, FounderIntelligence
from scraper.enrichers.founder_enricher import enrich_founder, batch_enrich_founders
from api.lib.email_generator import generate_email

router = APIRouter(prefix="/founder", tags=["founder"])
logger = logging.getLogger(__name__)


def _get_factory():
    return get_session_factory(get_settings().DATABASE_PATH)


def _fi_to_dict(fi: FounderIntelligence) -> dict:
    hooks = []
    if fi.approach_hooks:
        try:
            hooks = json.loads(fi.approach_hooks)
        except Exception:
            hooks = []
    return {
        "id": fi.id,
        "company_id": fi.company_id,
        "full_name": fi.full_name,
        "current_role": fi.current_role,
        "estimated_age": fi.estimated_age,
        "founder_status": fi.founder_status,
        "years_in_role": fi.years_in_role,
        "children_signal": fi.children_signal,
        "children_in_business": fi.children_in_business,
        "successor_signal": fi.successor_signal,
        "operator_type": fi.operator_type,
        "public_visibility": fi.public_visibility,
        "relationship_to_company": fi.relationship_to_company,
        "main_why_now_hypothesis": fi.main_why_now_hypothesis,
        "seller_signal_strength": fi.seller_signal_strength,
        "seller_signal_reason": fi.seller_signal_reason,
        "recommended_approach_angle": fi.recommended_approach_angle,
        "avoid_in_outreach": fi.avoid_in_outreach,
        "approach_hooks": hooks,
        "professional_email": fi.professional_email,
        "phone": fi.phone,
        "linkedin_url": fi.linkedin_url,
        "confidence_score": fi.confidence_score,
        "enrichment_status": fi.enrichment_status,
        "last_enriched_at": fi.last_enriched_at.isoformat() if fi.last_enriched_at else None,
    }


@router.get("/{company_id}")
async def get_founder_intelligence(company_id: int):
    """Récupère le Founder Intelligence existant pour une entreprise."""
    factory = _get_factory()
    async with factory() as session:
        result = await session.execute(
            select(FounderIntelligence).where(FounderIntelligence.company_id == company_id)
        )
        fi = result.scalar_one_or_none()
    if not fi:
        return {"exists": False, "company_id": company_id}
    return {"exists": True, **_fi_to_dict(fi)}


@router.post("/{company_id}/enrich")
async def trigger_enrich(company_id: int, background_tasks: BackgroundTasks):
    """Déclenche l'enrichissement Founder Intelligence en arrière-plan."""
    settings = get_settings()
    factory = _get_factory()

    # Marquer comme "running"
    async with factory() as session:
        stmt = sqlite_insert(FounderIntelligence).values(
            company_id=company_id,
            enrichment_status="running",
            last_enriched_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id"],
            set_={"enrichment_status": "running"},
        )
        await session.execute(stmt)
        await session.commit()

    background_tasks.add_task(
        enrich_founder,
        company_id=company_id,
        db_path=settings.DATABASE_PATH,
        pappers_api_key=settings.PAPPERS_API_KEY,
    )
    return {"status": "started", "company_id": company_id}


@router.get("/{company_id}/status")
async def get_enrich_status(company_id: int):
    """Retourne le statut d'enrichissement (pour polling)."""
    factory = _get_factory()
    async with factory() as session:
        result = await session.execute(
            select(FounderIntelligence.enrichment_status, FounderIntelligence.last_enriched_at)
            .where(FounderIntelligence.company_id == company_id)
        )
        row = result.first()
    if not row:
        return {"status": "not_started", "company_id": company_id}
    return {
        "status": row[0],
        "last_enriched_at": row[1].isoformat() if row[1] else None,
        "company_id": company_id,
    }


@router.post("/{company_id}/generate-approach")
async def generate_approach(company_id: int):
    """Génère un angle d'approche structuré depuis le profil existant."""
    factory = _get_factory()
    async with factory() as session:
        result = await session.execute(
            select(FounderIntelligence).where(FounderIntelligence.company_id == company_id)
        )
        fi = result.scalar_one_or_none()

        co_result = await session.execute(
            select(Company).where(Company.id == company_id)
        )
        company = co_result.scalar_one_or_none()

    if not fi:
        raise HTTPException(404, "Founder Intelligence non disponible. Lancez d'abord l'enrichissement.")

    hooks = []
    if fi.approach_hooks:
        try:
            hooks = json.loads(fi.approach_hooks)
        except Exception:
            hooks = []

    # Générer un objet d'approche structuré
    signal_labels = {
        "high": "🔴 Élevé",
        "moderate": "🟠 Modéré",
        "low": "🟡 Faible",
        "unknown": "⚪ Inconnu",
    }
    operator_labels = {
        "patrimonial": "Dirigeant patrimonial",
        "builder": "Entrepreneur-builder",
        "operator": "Manager professionnel",
        "disengaged": "Dirigeant de longue date en retrait",
        "founder": "Fondateur actif",
        "unknown": "Profil indéterminé",
    }
    founder_labels = {
        "founder": "Fondateur",
        "family_successor": "Successeur familial",
        "hired_manager": "Manager nommé",
        "unknown": "Statut inconnu",
    }

    company_name = company.name if company else f"Société #{company_id}"

    approach = {
        "company": company_name,
        "director": fi.full_name,
        "profile_summary": {
            "label": operator_labels.get(fi.operator_type, "Profil indéterminé"),
            "founder_status": founder_labels.get(fi.founder_status, fi.founder_status),
            "age": f"{fi.estimated_age} ans" if fi.estimated_age else "âge inconnu",
            "tenure": f"{fi.years_in_role} ans en poste" if fi.years_in_role else "",
        },
        "seller_signal": {
            "strength": signal_labels.get(fi.seller_signal_strength, "⚪"),
            "reason": fi.seller_signal_reason,
        },
        "why_now": fi.main_why_now_hypothesis,
        "approach": {
            "angle": fi.recommended_approach_angle,
            "avoid": fi.avoid_in_outreach,
            "hooks": hooks,
        },
        "contact": {
            "email": fi.professional_email,
            "linkedin": fi.linkedin_url,
            "phone": fi.phone,
        },
        "confidence": fi.confidence_score,
        "relationship_note": fi.relationship_to_company,
    }
    return approach


@router.post("/{company_id}/email")
async def generate_outreach_email(company_id: int):
    """Génère un email d'approche personnalisé depuis le profil Founder Intelligence."""
    factory = _get_factory()
    async with factory() as session:
        result = await session.execute(
            select(FounderIntelligence).where(FounderIntelligence.company_id == company_id)
        )
        fi = result.scalar_one_or_none()

        co_result = await session.execute(
            select(Company).where(Company.id == company_id)
        )
        company = co_result.scalar_one_or_none()

    if not fi:
        raise HTTPException(404, "Founder Intelligence non disponible. Lancez d'abord l'enrichissement.")

    fi_dict = _fi_to_dict(fi)
    company_dict = {
        "name": company.name if company else None,
        "sector": company.sector if company else None,
        "country": company.country if company else None,
        "revenue_eur": company.revenue_eur if company else None,
    }

    return generate_email(fi_dict, company_dict)


@router.post("/batch-enrich")
async def batch_enrich(
    background_tasks: BackgroundTasks,
    limit: int = 50,
):
    """Enrichit en batch les N entreprises avec le meilleur potentiel (top revenue + directors)."""
    settings = get_settings()
    background_tasks.add_task(
        batch_enrich_founders,
        db_path=settings.DATABASE_PATH,
        pappers_api_key=settings.PAPPERS_API_KEY,
        limit=limit,
    )
    return {
        "status": "started",
        "message": f"Enrichissement batch Founder Intelligence démarré pour {limit} entreprises",
    }
