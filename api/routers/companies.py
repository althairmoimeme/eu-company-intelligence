"""Companies router — list, detail, export."""
import csv
import io
import math
from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ..settings import get_settings, Settings
from scraper.db.session import get_session_factory
from scraper.db.models import Company, Director, FinancialHistory, FounderIntelligence
from ..schemas.company import CompanySummary, CompanyOut, PagedResponse
from scraper.enrichers.nace import search_nace, get_nace_suggestions
from scraper.db.models import BrokerListing
from api.lib.financial_signals import compute_financial_signals

router = APIRouter(prefix="/companies", tags=["companies"])

CURRENT_YEAR = 2026


def _compute_ma(company) -> tuple[int, list[str]]:
    """Compute M&A acquisition score (0-100) and signal list for a company."""
    score = 0
    signals = []

    # ── Signal 1 : Dirigeant senior (≥65 ans) ────────────────────────────────
    ages = [CURRENT_YEAR - d.birth_year for d in company.directors if d.birth_year]
    if ages:
        max_age = max(ages)
        if max_age >= 75:
            score += 40
            signals.append("👴 Dirigeant ≥75 ans")
        elif max_age >= 65:
            score += 25
            signals.append("👴 Dirigeant ≥65 ans")

    # ── Signal 2 : Long mandat (≥20 ans en poste) ────────────────────────────
    from datetime import date as _date
    for d in company.directors:
        if d.appointed_at:
            t = (_date.today() - d.appointed_at).days // 365
            if t >= 20:
                score += 20
                signals.append(f"⏳ Mandat {t} ans")
                break

    # ── Signal 3 : Société ancienne (>40 ans) ────────────────────────────────
    if company.creation_date:
        try:
            year = int(str(company.creation_date)[:4])
            age_co = CURRENT_YEAR - year
            if age_co >= 40:
                score += 15
                signals.append(f"🏛️ Fondée en {year}")
            elif age_co >= 25:
                score += 8
        except (ValueError, TypeError):
            pass

    # ── Signal 4 : Annonce de cession (broker listing) ───────────────────────
    # Only access if already eagerly loaded (avoid lazy load in async context)
    broker_listings = company.__dict__.get("broker_listings")
    if broker_listings:
        score += 40
        signals.append("📋 En vente (broker)")

    # ── Signal 5 : Dirigeant unique ──────────────────────────────────────────
    if len(company.directors) == 1:
        score += 5
        signals.append("👤 Dirigeant unique")

    # ── Signal 6 : Analyse financière (données historiques) ──────────────────
    # Les données financial_history sont chargées via eager load si disponibles
    fin_history = company.__dict__.get("financial_history")
    if fin_history:
        snaps_raw = [
            {
                "year": fh.year,
                "revenue_eur": fh.revenue_eur,
                "operating_income_eur": fh.operating_income_eur,
                "net_income_eur": fh.net_income_eur,
                "cash_eur": fh.cash_eur,
                "debt_eur": fh.debt_eur,
                "ebitda_eur": fh.ebitda_eur,
            }
            for fh in fin_history
        ]
        fin_score, fin_sigs, _, plateau = compute_financial_signals(snaps_raw)
        score += fin_score
        signals.extend(fin_sigs)

    return min(score, 100), signals


def _get_db_dep():
    settings = get_settings()
    factory = get_session_factory(settings.DATABASE_PATH)

    async def dep():
        async with factory() as session:
            yield session

    return dep


db_dep = _get_db_dep()


def _build_filters(
    country: list[str] | None,
    sector: list[str] | None,
    revenue_min: float | None,
    revenue_max: float | None,
    employees_min: int | None,
    employees_max: int | None,
    creation_from: int | None,
    creation_to: int | None,
    director_birth_min: int | None,
    director_birth_max: int | None,
    search: str | None,
    include_no_revenue: bool,
    nace_query: str | None = None,
    nace_mode: str = "broad",
    nace_depth: str = "close",
):
    filters = []

    if country:
        filters.append(Company.country.in_(country))
    if sector:
        filters.append(Company.sector.in_(sector))
    if revenue_min is not None:
        if include_no_revenue:
            filters.append(or_(Company.revenue_eur >= revenue_min,
                                Company.revenue_eur.is_(None)))
        else:
            filters.append(Company.revenue_eur >= revenue_min)
    if revenue_max is not None:
        filters.append(or_(Company.revenue_eur <= revenue_max,
                            Company.revenue_eur.is_(None)))
    if employees_min is not None:
        filters.append(Company.employees >= employees_min)
    if employees_max is not None:
        filters.append(Company.employees <= employees_max)
    if creation_from is not None:
        filters.append(Company.creation_date >= str(creation_from))
    if creation_to is not None:
        filters.append(Company.creation_date <= str(creation_to) + "-12-31")
    if search:
        filters.append(Company.name.ilike(f"%{search}%"))

    # NACE activity code filter
    if nace_query:
        matches = search_nace(nace_query, mode=nace_mode, depth=nace_depth)
        if matches:
            nace_codes = [m.code for m in matches]
            # Match on nace_code OR activity_description (text fallback)
            nace_filter = or_(
                Company.nace_code.in_(nace_codes),
                Company.activity_description.ilike(f"%{nace_query}%"),
            )
            filters.append(nace_filter)

    return filters


@router.get("/activity-codes")
async def activity_code_suggestions(
    q: str = Query(default="", min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Autocomplete suggestions for NACE activity codes."""
    suggestions = get_nace_suggestions(q, limit=limit)
    return [{"code": s["code"], "label": s["label"], "level": s["level"]} for s in suggestions]


@router.get("", response_model=PagedResponse)
async def list_companies(
    country: list[str] = Query(default=None),
    sector: list[str] = Query(default=None),
    revenue_min: Optional[float] = Query(default=None),
    revenue_max: Optional[float] = Query(default=None),
    employees_min: Optional[int] = Query(default=None),
    employees_max: Optional[int] = Query(default=None),
    creation_from: Optional[int] = Query(default=None),
    creation_to: Optional[int] = Query(default=None),
    director_birth_min: Optional[int] = Query(default=None),
    director_birth_max: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    include_no_revenue: bool = Query(default=True),
    nace_query: Optional[str] = Query(default=None),
    nace_mode: str = Query(default="broad"),
    nace_depth: str = Query(default="close"),
    ma_score_min: Optional[int] = Query(default=None),
    fi_seller_signal: Optional[str] = Query(default=None),   # high|moderate|low
    fi_operator_type: Optional[str] = Query(default=None),   # patrimonial|builder|operator|disengaged
    fi_has_data: Optional[bool] = Query(default=None),       # true = only companies with FI
    has_infra_contracts: Optional[bool] = Query(default=None),  # true = marchés publics infra critique
    sort_by: str = Query(default="revenue_eur"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(db_dep),
):
    filters = _build_filters(
        country, sector, revenue_min, revenue_max, employees_min, employees_max,
        creation_from, creation_to, director_birth_min, director_birth_max,
        search, include_no_revenue, nace_query, nace_mode, nace_depth,
    )

    # DECP infra contracts filter
    if has_infra_contracts:
        filters.append(Company.has_public_infra_contracts >= 1)

    # Build base query
    q = select(Company)
    if filters:
        q = q.where(and_(*filters))

    # Founder Intelligence filters — require a join
    if fi_has_data or fi_seller_signal or fi_operator_type:
        q = q.join(FounderIntelligence, Company.id == FounderIntelligence.company_id, isouter=False)
        q = q.where(FounderIntelligence.enrichment_status == "done")
        if fi_seller_signal:
            q = q.where(FounderIntelligence.seller_signal_strength == fi_seller_signal)
        if fi_operator_type:
            q = q.where(FounderIntelligence.operator_type == fi_operator_type)

    # Director age filter requires a join (applied on top of existing filters)
    if director_birth_min or director_birth_max:
        q = q.join(Director, Company.id == Director.company_id, isouter=False)
        if director_birth_min:
            birth_max = CURRENT_YEAR - director_birth_min
            q = q.where(Director.birth_year <= birth_max)
        if director_birth_max:
            birth_min = CURRENT_YEAR - director_birth_max
            q = q.where(Director.birth_year >= birth_min)

    # Count (always from the same base query)
    count_q = select(func.count()).select_from(
        q.with_only_columns(Company.id).distinct().subquery()
    )

    # Sort
    sort_col = getattr(Company, sort_by, Company.revenue_eur)
    if sort_dir == "asc":
        q = q.order_by(sort_col.asc().nulls_last())
    else:
        q = q.order_by(sort_col.desc().nulls_last())

    q = q.options(selectinload(Company.directors), selectinload(Company.financial_history))

    # When filtering by ma_score_min, fetch a larger batch and post-filter
    if ma_score_min and ma_score_min > 0:
        # Pre-filter: only companies that COULD score (avoids full table scan)
        # A company can score if: has a senior director, is old, or is listed for sale
        cutoff_year = str(CURRENT_YEAR - 25)  # age ≥25 → score ≥8
        senior_birth_max = CURRENT_YEAR - 65   # director ≥65 → score ≥25
        senior_subq = select(Director.company_id).where(
            Director.birth_year <= senior_birth_max
        ).scalar_subquery()
        q = q.where(
            or_(
                Company.creation_date <= cutoff_year,
                Company.id.in_(senior_subq),
            )
        )
        q_all = q.distinct()
        result = await session.execute(q_all)
        all_companies = result.scalars().unique().all()

        scored = []
        for c in all_companies:
            s, sigs = _compute_ma(c)
            if s >= ma_score_min:
                scored.append((c, s, sigs))

        # Sort by score desc, then by revenue desc as tiebreaker
        scored.sort(key=lambda x: (x[1], x[0].revenue_eur or 0), reverse=True)

        total = len(scored)
        offset = (page - 1) * page_size
        page_companies = scored[offset: offset + page_size]

        items = []
        for c, ma_score, ma_signals in page_companies:
            d = c.to_dict()
            d["ma_score"] = ma_score
            d["ma_signals"] = ma_signals
            items.append(CompanySummary(**d))
    else:
        # Normal paginated path
        count_result = await session.execute(count_q)
        total = count_result.scalar() or 0

        q = q.distinct().offset((page - 1) * page_size).limit(page_size)
        result = await session.execute(q)
        companies = result.scalars().unique().all()

        items = []
        for c in companies:
            d = c.to_dict()
            ma_score, ma_signals = _compute_ma(c)
            d["ma_score"] = ma_score
            d["ma_signals"] = ma_signals
            items.append(CompanySummary(**d))

    return PagedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, math.ceil(total / page_size)),
    )


@router.get("/stats")
async def get_stats(session: AsyncSession = Depends(db_dep)):
    total = (await session.execute(select(func.count()).select_from(Company))).scalar()
    revenue_count = (await session.execute(
        select(func.count()).select_from(Company).where(Company.revenue_eur.isnot(None))
    )).scalar()
    avg_revenue = (await session.execute(
        select(func.avg(Company.revenue_eur)).where(Company.revenue_eur.isnot(None))
    )).scalar()

    # By country
    by_country_result = await session.execute(
        select(Company.country, func.count()).group_by(Company.country).order_by(func.count().desc())
    )
    by_country = {row[0]: row[1] for row in by_country_result}

    # By sector
    by_sector_result = await session.execute(
        select(Company.sector, func.count()).where(Company.sector.isnot(None))
        .group_by(Company.sector).order_by(func.count().desc()).limit(20)
    )
    by_sector = {row[0]: row[1] for row in by_sector_result}

    # Directors and MA stats
    director_count = (await session.execute(
        select(func.count()).select_from(Director)
    )).scalar()
    companies_with_directors = (await session.execute(
        select(func.count()).select_from(Company)
        .where(Company.id.in_(select(Director.company_id).distinct()))
    )).scalar()

    return {
        "total_companies": total,
        "revenue_available": revenue_count,
        "avg_revenue_eur": round(avg_revenue, 0) if avg_revenue else None,
        "by_country": by_country,
        "by_sector": by_sector,
        "directors_total": director_count,
        "companies_with_directors": companies_with_directors,
    }


@router.get("/sectors")
async def list_sectors(session: AsyncSession = Depends(db_dep)):
    result = await session.execute(
        select(Company.sector, func.count())
        .where(Company.sector.isnot(None))
        .group_by(Company.sector)
        .order_by(func.count().desc())
    )
    return [{"sector": row[0], "count": row[1]} for row in result]


@router.get("/export/csv")
async def export_csv(
    country: list[str] = Query(default=None),
    sector: list[str] = Query(default=None),
    revenue_min: Optional[float] = Query(default=None),
    revenue_max: Optional[float] = Query(default=None),
    employees_min: Optional[int] = Query(default=None),
    employees_max: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    ma_score_min: Optional[int] = Query(default=None),
    session: AsyncSession = Depends(db_dep),
):
    filters = _build_filters(
        country, sector, revenue_min, revenue_max, employees_min, employees_max,
        None, None, None, None, search, True,
    )

    q = select(Company).options(selectinload(Company.directors), selectinload(Company.financial_history))
    if filters:
        q = q.where(and_(*filters))
    q = q.order_by(Company.revenue_eur.desc().nulls_last())

    result = await session.execute(q)
    companies = result.scalars().unique().all()

    # Apply MA score filter in memory if needed
    if ma_score_min and ma_score_min > 0:
        scored_companies = []
        for c in companies:
            s, sigs = _compute_ma(c)
            if s >= ma_score_min:
                scored_companies.append((c, s, sigs))
        # Sort by score desc
        scored_companies.sort(key=lambda x: x[1], reverse=True)
    else:
        scored_companies = [(c, *_compute_ma(c)) for c in companies]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Score M&A", "Signaux", "Pays", "Nom", "N° enregistrement", "Secteur", "Code NACE",
        "CA (EUR)", "Année CA", "Employés", "Date création",
        "Ville", "Adresse", "Objet social",
        "Dirigeant 1", "Rôle 1", "Naissance 1", "Age 1",
        "Dirigeant 2", "Rôle 2", "Naissance 2", "Age 2",
        "Dirigeant 3", "Rôle 3", "Naissance 3", "Age 3",
        "URL source",
    ])

    for c, ma_score, ma_signals in scored_companies:
        dirs = c.directors[:3]
        dir_cols = []
        for i in range(3):
            if i < len(dirs):
                d = dirs[i]
                age = (CURRENT_YEAR - d.birth_year) if d.birth_year else ""
                dir_cols += [d.name, d.role or "", d.birth_year or "", age]
            else:
                dir_cols += ["", "", "", ""]

        writer.writerow([
            ma_score, " | ".join(ma_signals),
            c.country, c.name, c.registration_number,
            c.sector or "", c.nace_code or "",
            c.revenue_eur or "", c.revenue_year or "",
            c.employees or "", c.creation_date or "",
            c.city or "", c.address or "", c.activity_description or "",
            *dir_cols,
            c.source_url or "",
        ])

    output.seek(0)
    filename = "cibles_ma.csv" if ma_score_min else "entreprises_europe.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{company_id}/financial-memo")
async def get_financial_memo(company_id: int, session: AsyncSession = Depends(db_dep)):
    """Retourne le Seller Financial Memo pour une entreprise."""
    # Récupérer l'historique financier
    result = await session.execute(
        select(FinancialHistory)
        .where(FinancialHistory.company_id == company_id)
        .order_by(FinancialHistory.year)
    )
    history = result.scalars().all()

    if not history:
        return {
            "company_id": company_id,
            "has_data": False,
            "score": 0,
            "signals": [],
            "memo": "Données financières historiques non disponibles pour cette entreprise.",
        }

    snaps_raw = [
        {
            "year": fh.year,
            "revenue_eur": fh.revenue_eur,
            "operating_income_eur": fh.operating_income_eur,
            "net_income_eur": fh.net_income_eur,
            "cash_eur": fh.cash_eur,
            "debt_eur": fh.debt_eur,
            "ebitda_eur": fh.ebitda_eur,
        }
        for fh in history
    ]

    fin_score, fin_signals, memo, plateau = compute_financial_signals(snaps_raw)

    return {
        "company_id": company_id,
        "has_data": True,
        "years": len(history),
        "score": fin_score,
        "plateau_business": plateau,
        "signals": fin_signals,
        "memo": memo,
        "snapshots": snaps_raw,
    }


@router.get("/{company_id}/contracts")
async def get_company_contracts(company_id: int, session: AsyncSession = Depends(db_dep)):
    """Retourne les marchés publics (DECP) pour une entreprise."""
    result = await session.execute(
        text("""
            SELECT objet, montant, acheteur_id, date_notification,
                   cpv_code, is_infra_critique, infra_signals
            FROM public_contracts
            WHERE company_id = :cid
            ORDER BY date_notification DESC
            LIMIT 50
        """),
        {"cid": company_id},
    )
    rows = result.fetchall()

    contracts = []
    total_montant = 0.0
    infra_count = 0
    for r in rows:
        objet, montant, acheteur_id, date_notif, cpv_code, is_infra, signals = r
        contracts.append({
            "objet": objet,
            "montant": montant,
            "acheteur_id": acheteur_id,
            "date_notification": date_notif,
            "cpv_code": cpv_code,
            "is_infra_critique": bool(is_infra),
            "infra_signals": signals,
        })
        if montant:
            total_montant += montant
        if is_infra:
            infra_count += 1

    return {
        "company_id": company_id,
        "total": len(contracts),
        "total_montant": total_montant,
        "infra_count": infra_count,
        "contracts": contracts,
    }


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(company_id: int, session: AsyncSession = Depends(db_dep)):
    q = select(Company).where(Company.id == company_id).options(selectinload(Company.directors), selectinload(Company.financial_history))
    result = await session.execute(q)
    company = result.scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(404, "Company not found")
    return CompanyOut(**company.to_dict())


@router.get("/{company_id}/onepager")
async def get_company_onepager(
    company_id: int,
    format: str = Query(default="pdf", description="pdf ou html"),
    session: AsyncSession = Depends(db_dep),
):
    """Génère un one-pager A4 (PDF ou HTML) pour une cible M&A."""
    from fastapi import HTTPException
    from fastapi.responses import Response
    from api.lib.onepager import generate_onepager_html
    from api.lib.email_generator import generate_email

    # Charger l'entreprise
    q = (
        select(Company)
        .where(Company.id == company_id)
        .options(selectinload(Company.directors), selectinload(Company.financial_history))
    )
    result = await session.execute(q)
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")

    # Charger le profil FI
    fi_result = await session.execute(
        select(FounderIntelligence).where(FounderIntelligence.company_id == company_id)
    )
    fi_obj = fi_result.scalar_one_or_none()

    # Calculer le score M&A
    ma_score, ma_signals = _compute_ma(company)

    # Signaux financiers
    fin_signals = []
    if company.financial_history:
        snaps = [
            {"year": r.year, "revenue_eur": r.revenue_eur,
             "operating_income_eur": r.operating_income_eur,
             "net_income_eur": r.net_income_eur}
            for r in company.financial_history
        ]
        try:
            from api.lib.financial_signals import compute_financial_signals
            _, fin_signals, _, _ = compute_financial_signals(snaps)
            ma_signals = list(set(ma_signals + fin_signals))
        except Exception:
            pass

    # Préparer les dicts
    company_dict = company.to_dict()
    fi_dict = None
    if fi_obj:
        fi_dict = {
            "enrichment_status": fi_obj.enrichment_status,
            "full_name": fi_obj.full_name,
            "current_role": fi_obj.current_role,
            "estimated_age": fi_obj.estimated_age,
            "years_in_role": fi_obj.years_in_role,
            "founder_status": fi_obj.founder_status,
            "operator_type": fi_obj.operator_type,
            "seller_signal_strength": fi_obj.seller_signal_strength,
            "seller_signal_reason": fi_obj.seller_signal_reason,
            "main_why_now_hypothesis": fi_obj.main_why_now_hypothesis,
            "recommended_approach_angle": fi_obj.recommended_approach_angle,
            "avoid_in_outreach": fi_obj.avoid_in_outreach,
        }

    fh_list = [
        {
            "year": r.year,
            "revenue_eur": r.revenue_eur,
            "net_income_eur": r.net_income_eur,
            "ebitda_eur": r.ebitda_eur,
            "source": r.source,
        }
        for r in (company.financial_history or [])
    ]

    # Générer l'email (extrait)
    email_subject = ""
    email_body = ""
    try:
        directors_for_email = [
            {"name": d.name, "role": d.role, "birth_year": d.birth_year, "appointed_at": d.appointed_at}
            for d in company.directors
        ]
        email_result = generate_email(
            company=company_dict,
            directors=directors_for_email,
            fi_profile=fi_dict,
            financial_signals=fin_signals,
            ma_signals=ma_signals,
        )
        email_subject = email_result.get("subject", "")
        email_body = email_result.get("body", "")
    except Exception:
        pass

    # HTML brut (debug)
    if format == "html":
        html = generate_onepager_html(
            company=company_dict,
            fi=fi_dict,
            ma_score=ma_score,
            ma_signals=ma_signals,
            financial_history=fh_list,
            email_subject=email_subject,
            email_body_excerpt=email_body,
        )
        return Response(content=html, media_type="text/html")

    # PDF → ReportLab (pure Python, no system dependencies)
    from api.lib.onepager_pdf import generate_onepager_pdf
    company_name_safe = (company_dict.get("name") or "company").replace(" ", "_")[:40]
    pdf_bytes = generate_onepager_pdf(
        company=company_dict,
        fi=fi_dict,
        ma_score=ma_score,
        ma_signals=ma_signals,
        financial_history=fh_list,
        email_subject=email_subject,
        email_body_excerpt=email_body,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={company_name_safe}_onepager.pdf"},
    )
