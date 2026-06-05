"""Seller signal detection endpoints.

Signals:
  - age_signal:    oldest director >= 65 years old
  - tenure_signal: longest-serving director >= 20 years
  - broker_signal: company found on a business-for-sale platform

Score:
  age:    75+ → 3pts | 70+ → 2pts | 65+ → 1pt
  tenure: 25+ → 3pts | 20+ → 2pts
  broker: matched on 1 platform → 2pts | 2+ platforms → 4pts
"""
import math
from typing import Optional
from fastapi import APIRouter, Query, Depends
from sqlalchemy import select, func, text, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from ..settings import get_settings
from scraper.db.session import get_session_factory
from scraper.db.models import Company, Director, BrokerListing

router = APIRouter(prefix="/signals", tags=["signals"])

CURRENT_YEAR = 2026


def _get_db_dep():
    settings = get_settings()
    factory = get_session_factory(settings.DATABASE_PATH)

    async def dep():
        async with factory() as session:
            yield session

    return dep


db_dep = _get_db_dep()


@router.get("/companies")
async def get_signal_companies(
    country: list[str] = Query(default=None),
    sector: list[str] = Query(default=None),
    revenue_min: Optional[float] = Query(default=None),
    revenue_max: Optional[float] = Query(default=None),
    min_score: int = Query(default=1, ge=1),
    age_min: Optional[int] = Query(default=None, description="Minimum director age"),
    tenure_min: Optional[int] = Query(default=None, description="Minimum director tenure (years)"),
    broker_only: bool = Query(default=False, description="Only show companies listed on broker platforms"),
    sort_by: str = Query(default="score"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(db_dep),
):
    """List companies with seller signals, sorted by score."""

    # Use raw SQL for the CTEs (SQLite-compatible)
    base_sql = """
    WITH age_scores AS (
        SELECT
            d.company_id,
            MAX(:cur_year - d.birth_year) AS max_director_age,
            CASE
                WHEN MAX(:cur_year - d.birth_year) >= 75 THEN 3
                WHEN MAX(:cur_year - d.birth_year) >= 70 THEN 2
                WHEN MAX(:cur_year - d.birth_year) >= 65 THEN 1
                ELSE 0
            END AS age_score,
            COUNT(CASE WHEN (:cur_year - d.birth_year) >= 65 THEN 1 END) AS directors_over_65
        FROM directors d
        WHERE d.birth_year IS NOT NULL
        GROUP BY d.company_id
    ),
    tenure_scores AS (
        SELECT
            d.company_id,
            MAX(CAST((julianday('now') - julianday(d.appointed_at)) / 365 AS INTEGER)) AS max_tenure,
            CASE
                WHEN MAX(CAST((julianday('now') - julianday(d.appointed_at)) / 365 AS INTEGER)) >= 25 THEN 3
                WHEN MAX(CAST((julianday('now') - julianday(d.appointed_at)) / 365 AS INTEGER)) >= 20 THEN 2
                ELSE 0
            END AS tenure_score
        FROM directors d
        WHERE d.appointed_at IS NOT NULL
        GROUP BY d.company_id
    ),
    broker_scores AS (
        SELECT
            bl.matched_company_id AS company_id,
            COUNT(DISTINCT bl.source) AS broker_count,
            CASE
                WHEN COUNT(DISTINCT bl.source) >= 2 THEN 4
                ELSE 2
            END AS broker_score,
            GROUP_CONCAT(DISTINCT bl.source) AS broker_sources
        FROM broker_listings bl
        WHERE bl.matched_company_id IS NOT NULL AND bl.match_score >= 75
        GROUP BY bl.matched_company_id
    )
    SELECT
        c.id,
        c.name,
        c.country,
        c.sector,
        c.revenue_eur,
        c.revenue_year,
        c.employees,
        c.city,
        c.creation_date,
        c.website,
        c.source_url,
        COALESCE(a.age_score, 0) + COALESCE(t.tenure_score, 0) + COALESCE(b.broker_score, 0) AS score,
        COALESCE(a.max_director_age, NULL) AS max_director_age,
        COALESCE(a.directors_over_65, 0) AS directors_over_65,
        COALESCE(t.max_tenure, NULL) AS max_tenure,
        CASE WHEN b.company_id IS NOT NULL THEN 1 ELSE 0 END AS has_broker_signal,
        COALESCE(b.broker_sources, '') AS broker_sources,
        COALESCE(b.broker_count, 0) AS broker_count
    FROM companies c
    LEFT JOIN age_scores a ON a.company_id = c.id
    LEFT JOIN tenure_scores t ON t.company_id = c.id
    LEFT JOIN broker_scores b ON b.company_id = c.id
    WHERE (COALESCE(a.age_score, 0) + COALESCE(t.tenure_score, 0) + COALESCE(b.broker_score, 0)) >= :min_score
    """

    params = {"cur_year": CURRENT_YEAR, "min_score": min_score}

    # Dynamic filters
    extra_where = []
    if country:
        placeholders = ", ".join(f":country_{i}" for i in range(len(country)))
        extra_where.append(f"c.country IN ({placeholders})")
        for i, c in enumerate(country):
            params[f"country_{i}"] = c
    if sector:
        placeholders = ", ".join(f":sector_{i}" for i in range(len(sector)))
        extra_where.append(f"c.sector IN ({placeholders})")
        for i, s in enumerate(sector):
            params[f"sector_{i}"] = s
    if revenue_min is not None:
        extra_where.append("(c.revenue_eur >= :revenue_min OR c.revenue_eur IS NULL)")
        params["revenue_min"] = revenue_min
    if revenue_max is not None:
        extra_where.append("(c.revenue_eur <= :revenue_max OR c.revenue_eur IS NULL)")
        params["revenue_max"] = revenue_max
    if age_min is not None:
        extra_where.append("a.max_director_age >= :age_min")
        params["age_min"] = age_min
    if tenure_min is not None:
        extra_where.append("t.max_tenure >= :tenure_min")
        params["tenure_min"] = tenure_min
    if broker_only:
        extra_where.append("b.company_id IS NOT NULL")

    if extra_where:
        base_sql += " AND " + " AND ".join(extra_where)

    # Count query
    count_sql = f"SELECT COUNT(*) FROM ({base_sql}) sub"
    count_result = await session.execute(text(count_sql), params)
    total = count_result.scalar() or 0

    # Order
    if sort_by == "score":
        order = "score DESC, c.revenue_eur DESC"
    elif sort_by == "age":
        order = "max_director_age DESC NULLS LAST"
    elif sort_by == "tenure":
        order = "max_tenure DESC NULLS LAST"
    elif sort_by == "revenue":
        order = "c.revenue_eur DESC NULLS LAST"
    else:
        order = "score DESC"

    paged_sql = base_sql + f" ORDER BY {order} LIMIT :limit OFFSET :offset"
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    result = await session.execute(text(paged_sql), params)
    rows = result.mappings().all()

    # Fetch directors for these company IDs
    company_ids = [r["id"] for r in rows]
    directors_map: dict[int, list] = {cid: [] for cid in company_ids}
    if company_ids:
        dirs_result = await session.execute(
            select(Director).where(Director.company_id.in_(company_ids))
        )
        for d in dirs_result.scalars():
            if d.company_id in directors_map:
                directors_map[d.company_id].append(d.to_dict())

    items = []
    for r in rows:
        cid = r["id"]
        score = r["score"]
        signals = []

        age = r["max_director_age"]
        if age and age >= 65:
            signals.append(f"Dirigeant âgé de {age} ans")

        tenure = r["max_tenure"]
        if tenure and tenure >= 20:
            signals.append(f"Dirigeant en poste depuis {tenure} ans")

        if r["has_broker_signal"]:
            sources = r["broker_sources"] or ""
            signals.append(f"Mis en vente ({sources})")

        items.append({
            "company_id": cid,
            "company_name": r["name"],
            "country": r["country"],
            "sector": r["sector"],
            "revenue_eur": r["revenue_eur"],
            "revenue_year": r["revenue_year"],
            "employees": r["employees"],
            "city": r["city"],
            "creation_date": r["creation_date"],
            "website": r["website"],
            "score": score,
            "signals": signals,
            "max_director_age": age,
            "directors_over_65": r["directors_over_65"],
            "max_tenure_years": tenure,
            "broker_signal": bool(r["has_broker_signal"]),
            "broker_sources": r["broker_sources"].split(",") if r["broker_sources"] else [],
            "directors": directors_map.get(cid, []),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, math.ceil(total / page_size)),
    }


@router.get("/stats")
async def get_signal_stats(session: AsyncSession = Depends(db_dep)):
    """Aggregate stats on seller signals."""
    total_companies = (await session.execute(
        select(func.count()).select_from(Company)
    )).scalar()

    # Age signal
    age_q = await session.execute(text("""
        SELECT COUNT(DISTINCT company_id)
        FROM directors
        WHERE birth_year IS NOT NULL AND (2026 - birth_year) >= 65
    """))
    age_signal_count = age_q.scalar() or 0

    # Tenure signal
    tenure_q = await session.execute(text("""
        SELECT COUNT(DISTINCT company_id)
        FROM directors
        WHERE appointed_at IS NOT NULL
          AND CAST((julianday('now') - julianday(appointed_at)) / 365 AS INTEGER) >= 20
    """))
    tenure_signal_count = tenure_q.scalar() or 0

    # Broker signal
    broker_q = await session.execute(text("""
        SELECT COUNT(DISTINCT matched_company_id)
        FROM broker_listings
        WHERE matched_company_id IS NOT NULL AND match_score >= 75
    """))
    broker_signal_count = broker_q.scalar() or 0

    # Multi-signal (age + broker OR age + tenure)
    multi_q = await session.execute(text("""
        WITH age_cos AS (
            SELECT DISTINCT company_id FROM directors
            WHERE birth_year IS NOT NULL AND (2026 - birth_year) >= 65
        ),
        broker_cos AS (
            SELECT DISTINCT matched_company_id AS company_id FROM broker_listings
            WHERE matched_company_id IS NOT NULL AND match_score >= 75
        ),
        tenure_cos AS (
            SELECT DISTINCT company_id FROM directors
            WHERE appointed_at IS NOT NULL
              AND CAST((julianday('now') - julianday(appointed_at)) / 365 AS INTEGER) >= 20
        )
        SELECT COUNT(DISTINCT a.company_id)
        FROM age_cos a
        WHERE a.company_id IN (SELECT company_id FROM broker_cos)
           OR a.company_id IN (SELECT company_id FROM tenure_cos)
    """))
    multi_signal_count = multi_q.scalar() or 0

    # Total listings scraped
    broker_total_q = await session.execute(
        select(func.count()).select_from(BrokerListing)
    )
    broker_listings_total = broker_total_q.scalar() or 0

    # Breakdown by source
    broker_by_source_q = await session.execute(text("""
        SELECT source, COUNT(*) as cnt
        FROM broker_listings
        GROUP BY source
        ORDER BY cnt DESC
    """))
    broker_by_source = {r[0]: r[1] for r in broker_by_source_q}

    return {
        "total_companies": total_companies,
        "age_signal_count": age_signal_count,
        "tenure_signal_count": tenure_signal_count,
        "broker_signal_count": broker_signal_count,
        "multi_signal_count": multi_signal_count,
        "broker_listings_total": broker_listings_total,
        "broker_by_source": broker_by_source,
    }
