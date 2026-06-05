"""Dashboard statistics router."""
from fastapi import APIRouter
from sqlalchemy import select, func, and_, desc
from datetime import datetime, timedelta

from ..settings import get_settings
from scraper.db.session import get_session_factory
from scraper.db.models import Company, FounderIntelligence, FavoritesListItem, FinancialHistory

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _get_factory():
    return get_session_factory(get_settings().DATABASE_PATH)


@router.get("/stats")
async def get_dashboard_stats():
    """Comprehensive statistics for the management dashboard."""
    factory = _get_factory()
    async with factory() as session:

        # ── Totals ────────────────────────────────────────────────────────────

        total_companies = (await session.execute(
            select(func.count(Company.id))
        )).scalar() or 0

        total_countries = (await session.execute(
            select(func.count(func.distinct(Company.country)))
        )).scalar() or 0

        with_revenue = (await session.execute(
            select(func.count(Company.id)).where(Company.revenue_eur.isnot(None))
        )).scalar() or 0

        fi_profiles = (await session.execute(
            select(func.count(FounderIntelligence.id))
        )).scalar() or 0

        financial_snapshots = (await session.execute(
            select(func.count(FinancialHistory.id))
        )).scalar() or 0

        # ── Seller signals ────────────────────────────────────────────────────

        signals_rows = (await session.execute(
            select(FounderIntelligence.seller_signal_strength, func.count(FounderIntelligence.id))
            .where(FounderIntelligence.seller_signal_strength.in_(["high", "moderate", "low"]))
            .group_by(FounderIntelligence.seller_signal_strength)
        )).all()
        signals = {"high": 0, "moderate": 0, "low": 0}
        for strength, count in signals_rows:
            signals[strength] = count

        # ── Operator types ────────────────────────────────────────────────────

        operator_rows = (await session.execute(
            select(FounderIntelligence.operator_type, func.count(FounderIntelligence.id))
            .where(
                FounderIntelligence.operator_type.isnot(None),
                FounderIntelligence.operator_type != "unknown",
            )
            .group_by(FounderIntelligence.operator_type)
            .order_by(func.count(FounderIntelligence.id).desc())
        )).all()
        operator_types = {row[0]: row[1] for row in operator_rows}

        # ── Top 10 countries ──────────────────────────────────────────────────

        country_rows = (await session.execute(
            select(Company.country, func.count(Company.id).label("count"))
            .group_by(Company.country)
            .order_by(func.count(Company.id).desc())
            .limit(10)
        )).all()
        top_countries = [{"country": row[0], "count": row[1]} for row in country_rows]

        # ── Top 10 sectors (non-null) ─────────────────────────────────────────

        sector_rows = (await session.execute(
            select(Company.sector, func.count(Company.id).label("count"))
            .where(Company.sector.isnot(None))
            .group_by(Company.sector)
            .order_by(func.count(Company.id).desc())
            .limit(10)
        )).all()
        top_sectors = [{"sector": row[0], "count": row[1]} for row in sector_rows]

        # ── Pipeline summary (all CRM statuses across all lists) ──────────────

        pipeline_rows = (await session.execute(
            select(FavoritesListItem.status, func.count(FavoritesListItem.id))
            .group_by(FavoritesListItem.status)
        )).all()
        pipeline_summary = {row[0]: row[1] for row in pipeline_rows if row[0]}

        # ── MA score distribution (revenue proxy) ─────────────────────────────
        # high: revenue > 10M AND creation_date < '2010'
        # medium: revenue > 1M (and not high)
        # low: otherwise

        high_count = (await session.execute(
            select(func.count(Company.id)).where(
                and_(
                    Company.revenue_eur > 10_000_000,
                    Company.creation_date < "2010",
                )
            )
        )).scalar() or 0

        medium_count = (await session.execute(
            select(func.count(Company.id)).where(
                and_(
                    Company.revenue_eur > 1_000_000,
                    ~and_(
                        Company.revenue_eur > 10_000_000,
                        Company.creation_date < "2010",
                    ),
                )
            )
        )).scalar() or 0

        low_count = (await session.execute(
            select(func.count(Company.id)).where(
                ~and_(
                    Company.revenue_eur > 1_000_000,
                )
            )
        )).scalar() or 0

        ma_score_distribution = {
            "high_60plus": high_count,
            "medium_30to60": medium_count,
            "low_under30": low_count,
        }

        # ── Recent high targets ───────────────────────────────────────────────
        # Companies with high/moderate seller_signal, ordered by revenue DESC

        targets_rows = (await session.execute(
            select(Company.id, Company.name, Company.country, Company.revenue_eur,
                   FounderIntelligence.seller_signal_strength)
            .join(FounderIntelligence, FounderIntelligence.company_id == Company.id)
            .where(FounderIntelligence.seller_signal_strength.in_(["high", "moderate"]))
            .order_by(Company.revenue_eur.desc().nulls_last())
            .limit(5)
        )).all()

        recent_high_targets = [
            {
                "id": row[0],
                "name": row[1],
                "country": row[2],
                "revenue_eur": row[3],
                "seller_signal": row[4],
            }
            for row in targets_rows
        ]

        # ── Duplicate detection ────────────────────────────────────────────────
        # Find companies appearing in 2+ countries with same name (>10 chars)
        dup_rows = (await session.execute(
            select(
                func.upper(func.trim(Company.name)).label("norm_name"),
                func.count(func.distinct(Company.country)).label("country_count"),
                func.group_concat(func.distinct(Company.country)).label("countries"),
                func.max(Company.id).label("id"),
                func.max(Company.revenue_eur).label("revenue_eur"),
            )
            .where(func.length(Company.name) > 10)
            .group_by(func.upper(func.trim(Company.name)))
            .having(func.count(func.distinct(Company.country)) > 1)
            .order_by(func.max(Company.revenue_eur).desc().nulls_last())
            .limit(50)
        )).all()

        duplicates = [
            {
                "id": row[3],
                "name": row[0],
                "countries": row[2],
                "revenue_eur": row[4],
                "reason": f"Présente dans {row[1]} pays",
            }
            for row in dup_rows
            if row[1] and row[1] > 1
        ]

        # ── Alertes : nouvelles cibles haute priorité (dernières 48h) ─────────
        cutoff = datetime.utcnow() - timedelta(hours=72)
        alerts_rows = (await session.execute(
            select(
                Company.id, Company.name, Company.country, Company.revenue_eur,
                Company.sector, Company.scraped_at,
                FounderIntelligence.seller_signal_strength,
                FounderIntelligence.operator_type,
                FounderIntelligence.full_name,
                FounderIntelligence.estimated_age,
            )
            .join(FounderIntelligence, FounderIntelligence.company_id == Company.id)
            .where(
                FounderIntelligence.seller_signal_strength.in_(["high", "moderate"]),
                Company.scraped_at >= cutoff,
            )
            .order_by(desc(Company.scraped_at))
            .limit(10)
        )).all()

        alerts = [
            {
                "id": r[0], "name": r[1], "country": r[2],
                "revenue_eur": r[3], "sector": r[4],
                "scraped_at": r[5].isoformat() if r[5] else None,
                "seller_signal": r[6], "operator_type": r[7],
                "director_name": r[8], "director_age": r[9],
            }
            for r in alerts_rows
        ]

    return {
        "totals": {
            "companies": total_companies,
            "countries": total_countries,
            "with_revenue": with_revenue,
            "fi_profiles": fi_profiles,
            "financial_snapshots": financial_snapshots,
        },
        "signals": signals,
        "operator_types": operator_types,
        "top_countries": top_countries,
        "top_sectors": top_sectors,
        "pipeline_summary": pipeline_summary,
        "ma_score_distribution": ma_score_distribution,
        "recent_high_targets": recent_high_targets,
        "duplicates": duplicates,
        "alerts": alerts,
    }
