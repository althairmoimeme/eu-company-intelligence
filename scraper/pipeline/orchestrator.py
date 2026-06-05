"""Orchestrate all scrapers and upsert results into the database."""
import asyncio
import logging
from datetime import datetime
from sqlalchemy import insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from ..db.models import Company, Director, ScrapeRun
from ..db.session import get_session_factory
from ..pipeline.normalizer import CompanyRecord
from ..scrapers.france import FranceScraper
from ..scrapers.uk import UKScraper
from ..scrapers.norway import NorwayScraper
from ..scrapers.denmark import DenmarkScraper
from ..scrapers.belgium import BelgiumScraper
from ..scrapers.poland import PolandScraper
from ..scrapers.romania import RomaniaScraper
from ..scrapers.spain import SpainScraper
from ..scrapers.yf_bootstrap import YFBootstrapScraper
from ..scrapers.germany import GermanyScraper
from ..scrapers.italy import ItalyScraper
from ..scrapers.sweden import SwedenScraper
from ..scrapers.switzerland import SwitzerlandScraper
from ..scrapers.netherlands_curated import NetherlandsCuratedScraper
from ..scrapers.portugal import PortugalScraper
from ..scrapers.belgium_curated import BelgiumCuratedScraper
from ..scrapers.denmark_curated import DenmarkCuratedScraper
from ..scrapers.austria_curated import AustriaCuratedScraper
from ..scrapers.japan import JapanScraper

logger = logging.getLogger(__name__)

SCRAPER_MAP = {
    "pappers_fr": FranceScraper,
    "companies_house_uk": UKScraper,
    "brreg_no": NorwayScraper,
    "cvr_dk": DenmarkScraper,
    "opencorporates_be": BelgiumScraper,
    "krs_pl": PolandScraper,
    "anaf_ro": RomaniaScraper,
    "borme_es": SpainScraper,
    "hr_de": GermanyScraper,
    "cciaa_it": ItalyScraper,
    "swe_se": SwedenScraper,
    "uid_ch": SwitzerlandScraper,
    "kvk_nl_curated": NetherlandsCuratedScraper,
    "rnpc_pt": PortugalScraper,
    "yf_bootstrap": YFBootstrapScraper,
    "be_curated": BelgiumCuratedScraper,
    "dk_curated": DenmarkCuratedScraper,
    "at_curated": AustriaCuratedScraper,
    "tse_jp": JapanScraper,
}


async def upsert_company(session, record: CompanyRecord) -> tuple[int, bool]:
    """Upsert a company record. Returns (company_id, is_new)."""
    stmt = (
        sqlite_insert(Company)
        .values(
            name=record.name,
            country=record.country,
            registration_number=record.registration_number,
            revenue_eur=record.revenue_eur,
            revenue_year=record.revenue_year,
            revenue_estimated=record.revenue_estimated,
            employees=record.employees,
            sector=record.sector,
            nace_code=record.nace_code,
            activity_description=record.activity_description,
            creation_date=record.creation_date,
            address=record.address,
            city=record.city,
            postal_code=record.postal_code,
            website=record.website,
            email=record.email,
            phone=record.phone,
            source_url=record.source_url,
            scraped_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["country", "registration_number"],
            set_={
                "name": record.name,
                "employees": record.employees,
                "sector": record.sector,
                "nace_code": record.nace_code,
                "activity_description": record.activity_description,
                "scraped_at": datetime.utcnow(),
                # Revenue fields: only update when scraper provides real values
                # Never overwrite enriched revenue data with NULL from scrapers that don't provide it
                **({
                    "revenue_eur": record.revenue_eur,
                    "revenue_year": record.revenue_year,
                    "revenue_estimated": record.revenue_estimated,
                } if record.revenue_eur is not None else {}),
            },
        )
    )
    result = await session.execute(stmt)
    is_new = result.rowcount == 1

    # Get the company id
    q = await session.execute(
        select(Company.id).where(
            Company.country == record.country,
            Company.registration_number == record.registration_number,
        )
    )
    company_id = q.scalar_one()

    # Replace directors
    if record.directors:
        await session.execute(
            Director.__table__.delete().where(Director.company_id == company_id)
        )
        for d in record.directors:
            if not d.name:
                continue
            from datetime import date as _date
            appointed = None
            if d.appointed_at:
                try:
                    appointed = _date.fromisoformat(d.appointed_at)
                except Exception:
                    pass
            session.add(Director(
                company_id=company_id,
                name=d.name,
                role=d.role,
                birth_year=d.birth_year,
                nationality=d.nationality,
                appointed_at=appointed,
            ))

    return company_id, is_new


async def run_scraper(scraper_name: str, config: dict, resume: bool = True,
                      db_path: str = "companies.db",
                      run_id: int | None = None) -> dict:
    """Run a single scraper and persist results. Returns stats dict."""
    scraper_cls = SCRAPER_MAP.get(scraper_name)
    if not scraper_cls:
        return {"error": f"Unknown scraper: {scraper_name}"}

    scraper = scraper_cls(config=config)
    factory = get_session_factory(db_path)

    added = 0
    updated = 0
    errors = 0
    batch = []
    BATCH_SIZE = 50

    async def flush_batch():
        nonlocal added, updated
        async with factory() as session:
            async with session.begin():
                for record in batch:
                    try:
                        _, is_new = await upsert_company(session, record)
                        if is_new:
                            added += 1
                        else:
                            updated += 1
                    except Exception as e:
                        logger.error(f"DB error for {record.name}: {e}")
        batch.clear()

        # Update run stats
        if run_id:
            async with factory() as session:
                async with session.begin():
                    run = await session.get(ScrapeRun, run_id)
                    if run:
                        run.companies_added = added
                        run.companies_updated = updated

    try:
        async for record in scraper.run(resume=resume):
            batch.append(record)
            if len(batch) >= BATCH_SIZE:
                await flush_batch()

        if batch:
            await flush_batch()

        # Mark run as done
        if run_id:
            async with factory() as session:
                async with session.begin():
                    run = await session.get(ScrapeRun, run_id)
                    if run:
                        run.status = "done"
                        run.finished_at = datetime.utcnow()
                        run.companies_added = added
                        run.companies_updated = updated

        logger.info(f"[{scraper_name}] Done — added={added}, updated={updated}")
        return {"added": added, "updated": updated, "errors": errors}

    except Exception as e:
        logger.error(f"[{scraper_name}] Fatal error: {e}", exc_info=True)
        if run_id:
            async with factory() as session:
                async with session.begin():
                    run = await session.get(ScrapeRun, run_id)
                    if run:
                        run.status = "failed"
                        run.finished_at = datetime.utcnow()
                        run.error_message = str(e)
        return {"error": str(e), "added": added, "updated": updated}


async def run_all(config: dict, scrapers: list[str] | None = None,
                  resume: bool = True, db_path: str = "companies.db") -> dict:
    """Run multiple scrapers sequentially (to respect rate limits)."""
    targets = scrapers or list(SCRAPER_MAP.keys())
    results = {}
    factory = get_session_factory(db_path)

    for name in targets:
        # Create run record
        run_id = None
        async with factory() as session:
            async with session.begin():
                run = ScrapeRun(scraper=name, status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        logger.info(f"Starting scraper: {name} (run_id={run_id})")
        result = await run_scraper(name, config, resume=resume,
                                    db_path=db_path, run_id=run_id)
        results[name] = result

    return results
