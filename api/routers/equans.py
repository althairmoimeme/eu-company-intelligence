"""FastAPI router — Equans M&A targeting."""
# ruff: noqa: E501
import csv
import io
import json
import math
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ..settings import get_settings, Settings
from scraper.db.session import get_session_factory
from scraper.db.models import Company, EquansScore, FounderIntelligence
from ..lib.equans_scoring import score_company

router = APIRouter(prefix="/equans", tags=["equans"])
logger = logging.getLogger(__name__)

CURRENT_YEAR = 2026


def db_dep(settings: Settings = Depends(get_settings)):
    return get_session_factory(settings.DATABASE_PATH)


# ── Global in-memory state ────────────────────────────────────────────────────

_status: dict = {"running": False, "scored": 0, "total": 0, "error": None}
_enrich_status: dict = {
    "running": False,
    "step": "",
    "fi_created": 0,
    "fi_total": 0,
    "web_found": 0,
    "web_total": 0,
    "scored": 0,
    "error": None,
}


async def _run_enrich_batch(db_path: str, score_threshold: int, limit_fi: int, limit_web: int) -> None:
    """Enrichissement ciblé des top cibles Equans : FI → Website → Re-scoring."""
    global _enrich_status
    _enrich_status = {
        "running": True, "step": "init",
        "fi_created": 0, "fi_total": 0,
        "web_found": 0, "web_total": 0,
        "scored": 0, "error": None,
    }

    from scraper.db.session import get_session_factory as _gsf
    from scraper.enrichers.batch_fi_creator import create_missing_fi_profiles
    from scraper.enrichers.website_enricher import enrich_websites

    factory = _gsf(db_path)

    try:
        # ── 1. Récupérer les IDs des top cibles Equans ──────────────────────
        async with factory() as session:
            rows = (await session.execute(
                select(EquansScore.company_id)
                .where(EquansScore.total_score >= score_threshold)
                .order_by(EquansScore.total_score.desc())
                .limit(2000)
            )).scalars().all()
        target_ids = list(rows)
        logger.info(f"[Equans Enrich] {len(target_ids)} cibles (score≥{score_threshold})")

        # ── 2. Batch FI pour celles sans profil ─────────────────────────────
        _enrich_status["step"] = "founder_intelligence"
        _enrich_status["fi_total"] = len(target_ids)

        fi_result = await create_missing_fi_profiles(
            db_path=db_path,
            limit=limit_fi,
            concurrency=20,
            priority="revenue",
            company_ids=target_ids,
        )
        _enrich_status["fi_created"] = fi_result.get("created", 0)
        logger.info(f"[Equans Enrich] FI créés : {_enrich_status['fi_created']}")

        # ── 3. Website enrichment pour celles sans URL ──────────────────────
        _enrich_status["step"] = "websites"
        _enrich_status["web_total"] = len(target_ids)

        web_result = await enrich_websites(
            db_path=db_path,
            limit=limit_web,
            concurrency=3,
            company_ids=target_ids,
        )
        _enrich_status["web_found"] = web_result.get("found", 0)
        logger.info(f"[Equans Enrich] Sites trouvés : {_enrich_status['web_found']}")

        # ── 4. Re-scoring pour intégrer les nouveaux signaux FI ─────────────
        _enrich_status["step"] = "rescoring"
        await _run_scoring(db_path)
        _enrich_status["scored"] = _status.get("scored", 0)

        _enrich_status["step"] = "done"

    except Exception as exc:
        _enrich_status["error"] = str(exc)
        logger.error(f"[Equans Enrich] Erreur : {exc}", exc_info=True)
    finally:
        _enrich_status["running"] = False


async def _run_scoring(db_path: str) -> None:
    global _status
    _status = {"running": True, "scored": 0, "total": 0, "error": None}

    from scraper.db.session import get_session_factory as _gsf
    factory = _gsf(db_path)
    BATCH = 300

    try:
        async with factory() as session:
            total = (await session.execute(select(func.count(Company.id)))).scalar() or 0
        _status["total"] = total

        offset = 0
        while True:
            async with factory() as session:
                rows = (await session.execute(
                    select(Company)
                    .options(
                        selectinload(Company.directors),
                    )
                    .offset(offset)
                    .limit(BATCH)
                )).scalars().all()

                if not rows:
                    break

                # Fetch FounderIntelligence for this batch
                company_ids = [c.id for c in rows]
                fi_map: dict[int, object] = {}
                fi_rows = (await session.execute(
                    select(FounderIntelligence)
                    .where(FounderIntelligence.company_id.in_(company_ids))
                )).scalars().all()
                for fi in fi_rows:
                    fi_map[fi.company_id] = fi

                # Fetch existing scores for upsert
                existing_scores: dict[int, EquansScore] = {}
                eq_rows = (await session.execute(
                    select(EquansScore).where(EquansScore.company_id.in_(company_ids))
                )).scalars().all()
                for eq in eq_rows:
                    existing_scores[eq.company_id] = eq

                for company in rows:
                    fi = fi_map.get(company.id)
                    data = score_company(
                        nace_code=company.nace_code,
                        sector=company.sector,
                        activity_description=company.activity_description,
                        revenue_eur=company.revenue_eur,
                        creation_date=company.creation_date,
                        directors=company.directors,
                        fi=fi,
                        nace_inferred=getattr(company, "nace_inferred", None),
                        name=company.name,
                        country=company.country,
                        has_public_infra_contracts=bool(getattr(company, "has_public_infra_contracts", 0)),
                    )

                    eq = existing_scores.get(company.id)
                    if eq is None:
                        eq = EquansScore(company_id=company.id)
                        session.add(eq)

                    for k, v in data.items():
                        setattr(eq, k, v)
                    eq.scored_at = datetime.utcnow()

                    _status["scored"] += 1

                await session.commit()

            offset += BATCH
            logger.info(f"[Equans] Scored {_status['scored']}/{total}")

    except Exception as exc:
        _status["error"] = str(exc)
        logger.error(f"[Equans] Scoring failed: {exc}", exc_info=True)
    finally:
        _status["running"] = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize(company: Company, eq: EquansScore) -> dict:
    directors = [
        {
            "name": d.name,
            "role": d.role,
            "birth_year": d.birth_year,
            "age": (CURRENT_YEAR - d.birth_year) if d.birth_year else None,
        }
        for d in company.directors[:3]
    ]
    return {
        "id": company.id,
        "name": company.name,
        "country": company.country,
        "registration_number": company.registration_number,
        "city": company.city,
        "website": company.website,
        "revenue_eur": company.revenue_eur,
        "employees": company.employees,
        "sector": company.sector,
        "nace_code": company.nace_code,
        "creation_date": company.creation_date,
        "source_url": company.source_url,
        "directors": directors,
        # Equans scores
        "equans_score": eq.total_score,
        "sector_score": eq.sector_score,
        "revenue_score": eq.revenue_score,
        "integration_score": eq.integration_score,
        "critical_score": eq.critical_score,
        "founder_score": eq.founder_score,
        "longevity_score": eq.longevity_score,
        "has_engineering": eq.has_engineering,
        "has_installation": eq.has_installation,
        "has_maintenance": eq.has_maintenance,
        "has_critical_sectors": eq.has_critical_sectors,
        "is_founder_owned": eq.is_founder_owned,
        "is_european": getattr(eq, "is_european", True),
        "revenue_bracket": getattr(eq, "revenue_bracket", None),
        "thesis": eq.thesis,
        "match_reasons": json.loads(eq.match_reasons) if eq.match_reasons else [],
    }


def _build_conditions(
    country, min_score, revenue_min, revenue_max,
    has_engineering, has_installation, has_maintenance, has_critical, sector,
    only_european=False, revenue_bracket=None, include_no_revenue=False,
) -> list:
    conds = [EquansScore.total_score >= min_score]
    if country:
        conds.append(Company.country.in_(country))
    if revenue_min is not None:
        if include_no_revenue:
            conds.append(or_(Company.revenue_eur >= revenue_min, Company.revenue_eur.is_(None)))
        else:
            conds.append(Company.revenue_eur >= revenue_min)
    if revenue_max is not None:
        if include_no_revenue:
            conds.append(or_(Company.revenue_eur <= revenue_max, Company.revenue_eur.is_(None)))
        else:
            conds.append(Company.revenue_eur <= revenue_max)
    if has_engineering:
        conds.append(EquansScore.has_engineering.is_(True))
    if has_installation:
        conds.append(EquansScore.has_installation.is_(True))
    if has_maintenance:
        conds.append(EquansScore.has_maintenance.is_(True))
    if has_critical:
        conds.append(EquansScore.has_critical_sectors.is_(True))
    if sector:
        conds.append(Company.sector == sector)
    if only_european:
        conds.append(EquansScore.is_european.is_(True))
    if revenue_bracket:
        conds.append(EquansScore.revenue_bracket == revenue_bracket)
    return conds





# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/targets")
async def list_equans_targets(
    country: Optional[list[str]] = Query(default=None),
    min_score: int = Query(default=0),
    revenue_min: Optional[float] = Query(default=None),
    revenue_max: Optional[float] = Query(default=None),
    has_engineering: bool = Query(default=False),
    has_installation: bool = Query(default=False),
    has_maintenance: bool = Query(default=False),
    has_critical: bool = Query(default=False),
    sector: Optional[str] = Query(default=None),
    only_european: bool = Query(default=False),
    revenue_bracket: Optional[str] = Query(default=None),
    include_no_revenue: bool = Query(default=False),
    sort_by: str = Query(default="total_score"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200),
    factory=Depends(db_dep),
):
    sort_col_map = {
        "total_score": EquansScore.total_score,
        "revenue_eur": Company.revenue_eur,
        "sector_score": EquansScore.sector_score,
        "name": Company.name,
    }
    sort_col = sort_col_map.get(sort_by, EquansScore.total_score)
    order = sort_col.desc() if sort_dir == "desc" else sort_col.asc()

    conds = _build_conditions(
        country, min_score, revenue_min, revenue_max,
        has_engineering, has_installation, has_maintenance, has_critical, sector,
        only_european=only_european, revenue_bracket=revenue_bracket,
        include_no_revenue=include_no_revenue,
    )
    join_on = Company.id == EquansScore.company_id

    async with factory() as session:
        total = (await session.execute(
            select(func.count()).select_from(
                select(Company.id)
                .join(EquansScore, join_on)
                .where(and_(*conds))
                .subquery()
            )
        )).scalar() or 0

        rows = (await session.execute(
            select(Company, EquansScore)
            .join(EquansScore, join_on)
            .where(and_(*conds))
            .options(selectinload(Company.directors))
            .order_by(order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )).all()

    pages = max(1, math.ceil(total / page_size))
    return {
        "items": [_serialize(c, eq) for c, eq in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/stats")
async def get_equans_stats(factory=Depends(db_dep)):
    async with factory() as session:
        total_scored = (await session.execute(
            select(func.count(EquansScore.id))
        )).scalar() or 0

        high = (await session.execute(
            select(func.count(EquansScore.id))
            .where(EquansScore.total_score >= 60)
        )).scalar() or 0

        medium = (await session.execute(
            select(func.count(EquansScore.id))
            .where(and_(EquansScore.total_score >= 30, EquansScore.total_score < 60))
        )).scalar() or 0

        with_installation = (await session.execute(
            select(func.count(EquansScore.id))
            .where(EquansScore.has_installation.is_(True))
        )).scalar() or 0

        with_critical = (await session.execute(
            select(func.count(EquansScore.id))
            .where(EquansScore.has_critical_sectors.is_(True))
        )).scalar() or 0

        founder_owned = (await session.execute(
            select(func.count(EquansScore.id))
            .where(EquansScore.is_founder_owned.is_(True))
        )).scalar() or 0

    return {
        "total_scored": total_scored,
        "high_score": high,
        "medium_score": medium,
        "with_installation": with_installation,
        "with_critical": with_critical,
        "founder_owned": founder_owned,
        "scoring_status": _status,
    }


@router.post("/score")
async def run_equans_scoring(
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
):
    if _status["running"]:
        return {"status": "already_running", "message": "Scoring déjà en cours", "progress": _status}
    background_tasks.add_task(_run_scoring, settings.DATABASE_PATH)
    return {"status": "started", "message": "Scoring Equans démarré en arrière-plan"}


@router.post("/enrich-batch")
async def run_equans_enrich_batch(
    background_tasks: BackgroundTasks,
    score_threshold: int = 30,
    limit_fi: int = 500,
    limit_web: int = 200,
    settings: Settings = Depends(get_settings),
):
    """Enrichit les top cibles Equans en 3 étapes :
    1. Founder Intelligence (heuristiques, rapide)
    2. Sites web (DuckDuckGo, lent ~2-3s/entreprise)
    3. Re-scoring Equans (intègre les nouveaux signaux FI)
    """
    if _enrich_status["running"]:
        return {"status": "already_running", "progress": _enrich_status}
    background_tasks.add_task(
        _run_enrich_batch, settings.DATABASE_PATH, score_threshold, limit_fi, limit_web
    )
    return {
        "status": "started",
        "message": f"Enrichissement démarré — FI({limit_fi}) + Web({limit_web}) + re-scoring sur cibles ≥{score_threshold}",
    }


@router.get("/enrich-status")
async def get_equans_enrich_status():
    return _enrich_status


@router.get("/targets/{company_id}")
async def get_equans_target(company_id: int, factory=Depends(db_dep)):
    from fastapi import HTTPException
    async with factory() as session:
        row = (await session.execute(
            select(Company, EquansScore)
            .join(EquansScore, Company.id == EquansScore.company_id)
            .where(Company.id == company_id)
            .options(selectinload(Company.directors))
        )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Entreprise non scorée ou introuvable")
    return _serialize(*row)


@router.get("/export")
async def export_equans_csv(
    country: Optional[list[str]] = Query(default=None),
    min_score: int = Query(default=0),
    has_engineering: bool = Query(default=False),
    has_installation: bool = Query(default=False),
    has_maintenance: bool = Query(default=False),
    has_critical: bool = Query(default=False),
    factory=Depends(db_dep),
):
    conds = _build_conditions(
        country, min_score, None, None,
        has_engineering, has_installation, has_maintenance, has_critical, None,
    )
    join_on = Company.id == EquansScore.company_id

    async with factory() as session:
        rows = (await session.execute(
            select(Company, EquansScore)
            .join(EquansScore, join_on)
            .where(and_(*conds))
            .options(selectinload(Company.directors))
            .order_by(EquansScore.total_score.desc())
            .limit(10_000)
        )).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Nom", "Pays", "N° Registre", "Ville", "CA (€)", "Effectif",
        "Secteur", "NACE", "Dirigeants",
        "Score Equans", "Score Secteur", "Score CA", "Score Intégration",
        "Score Critique", "Score Fondateur", "Score Longévité",
        "Ingénierie", "Installation", "Maintenance", "Secteurs Critiques",
        "Fondateur/Famille", "Thèse", "Raisons",
    ])
    for company, eq in rows:
        directors_str = " | ".join(
            f"{d.name} ({CURRENT_YEAR - d.birth_year}a)" if d.birth_year else d.name
            for d in company.directors[:3]
        )
        reasons = json.loads(eq.match_reasons) if eq.match_reasons else []
        writer.writerow([
            company.id, company.name, company.country, company.registration_number,
            company.city, company.revenue_eur, company.employees,
            company.sector, company.nace_code, directors_str,
            eq.total_score, eq.sector_score, eq.revenue_score, eq.integration_score,
            eq.critical_score, eq.founder_score, eq.longevity_score,
            eq.has_engineering, eq.has_installation, eq.has_maintenance,
            eq.has_critical_sectors, eq.is_founder_owned, eq.thesis,
            " | ".join(reasons),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=equans_targets.csv"},
    )
