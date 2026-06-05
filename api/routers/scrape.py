"""Scrape control endpoints."""
import asyncio
import logging
import os
from fastapi import APIRouter, BackgroundTasks
from sqlalchemy import select
from ..settings import get_settings
from ..schemas.company import ScrapeRunOut, PagedResponse
from scraper.db.session import get_session_factory
from scraper.db.models import ScrapeRun
from scraper.pipeline.orchestrator import run_all, SCRAPER_MAP
from scraper.enrichers.uk_revenue import enrich_uk_revenues
from scraper.enrichers.gb_lei_revenue import enrich_gb_lei_revenues
from scraper.enrichers.pl_es_revenue import enrich_pl_es_revenues
from scraper.enrichers.no_revenue import enrich_no_revenues
from scraper.scrapers.brokers import run_broker_scrape
from scraper.enrichers.fr_gov import enrich_fr_companies
from scraper.enrichers.fr_mcp import enrich_fr_tenure
from scraper.enrichers.yf_financials import enrich_financial_history

router = APIRouter(prefix="/scrape", tags=["scrape"])
logger = logging.getLogger(__name__)


@router.post("/run")
async def start_scrape(
    background_tasks: BackgroundTasks,
    scrapers: list[str] | None = None,
    resume: bool = True,
):
    settings = get_settings()
    config = settings.model_dump()
    targets = scrapers or list(SCRAPER_MAP.keys())

    background_tasks.add_task(
        run_all,
        config=config,
        scrapers=targets,
        resume=resume,
        db_path=settings.DATABASE_PATH,
    )

    return {
        "status": "started",
        "scrapers": targets,
        "resume": resume,
        "message": "Scraping started in background. Check /api/v1/scrape/status for progress.",
    }


@router.get("/status")
async def get_scrape_status():
    settings = get_settings()
    factory = get_session_factory(settings.DATABASE_PATH)
    async with factory() as session:
        result = await session.execute(
            select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(20)
        )
        runs = result.scalars().all()
        return [
            {
                "id": r.id,
                "scraper": r.scraper,
                "status": r.status,
                "companies_added": r.companies_added,
                "companies_updated": r.companies_updated,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "error_message": r.error_message,
            }
            for r in runs
        ]


@router.post("/enrich-uk")
async def enrich_uk(background_tasks: BackgroundTasks, limit: int = None):
    """Enrich UK companies with revenue data from Yahoo Finance + iXBRL."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_uk_revenues,
        db_path=settings.DATABASE_PATH,
        ch_api_key=settings.COMPANIES_HOUSE_API_KEY,
        limit=limit,
    )
    return {"status": "started", "message": "UK revenue enrichment running in background"}


@router.post("/enrich-gb-lei")
async def enrich_gb_lei(
    background_tasks: BackgroundTasks,
    min_score: int = 50,
    limit: int = 100,
    concurrency: int = 4,
):
    """Enrich GB companies identified by LEI: resolve CH number via GLEIF, then pull iXBRL revenue."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_gb_lei_revenues,
        db_path=settings.DATABASE_PATH,
        ch_api_key=settings.COMPANIES_HOUSE_API_KEY,
        min_score=min_score,
        limit=limit,
        concurrency=concurrency,
    )
    return {"status": "started", "message": f"GB LEI revenue enrichment started (min_score={min_score}, limit={limit})"}


@router.post("/enrich-no")
async def enrich_no(background_tasks: BackgroundTasks, limit: int = None):
    """Enrich Norwegian companies with revenue from Brønnøysund accounts register."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_no_revenues,
        db_path=settings.DATABASE_PATH,
        limit=limit,
    )
    return {"status": "started", "message": "Norway revenue enrichment running in background"}


@router.post("/enrich-pl-es")
async def enrich_pl_es(
    background_tasks: BackgroundTasks,
    country: str = "ALL",
):
    """Enrich Polish and Spanish companies with revenue data from Yahoo Finance."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_pl_es_revenues,
        db_path=settings.DATABASE_PATH,
        country=country,
    )
    return {
        "status": "started",
        "country": country,
        "message": f"PL/ES revenue enrichment running in background for country={country}",
    }


@router.post("/enrich-fr")
async def enrich_fr(background_tasks: BackgroundTasks, limit: int = None,
                    only_without_revenue: bool = False,
                    min_revenue: float = None, max_revenue: float = None,
                    min_score: int = None):
    """Enrichit les entreprises FR avec dirigeants + CA réel via API gouvernementale gratuite."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_fr_companies,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        only_without_revenue=only_without_revenue,
        min_revenue=min_revenue,
        max_revenue=max_revenue,
        min_score=min_score,
    )
    return {
        "status": "started",
        "message": "Enrichissement FR démarré — dirigeants + CA via api.gouv.fr (gratuit)",
    }


@router.post("/enrich-fr-tenure")
async def enrich_fr_tenure_endpoint(background_tasks: BackgroundTasks, limit: int = None):
    """Enrichit les mandats (date_prise_de_poste) des dirigeants FR via MCP Pappers."""
    settings = get_settings()
    if not settings.PAPPERS_API_KEY:
        return {"status": "error", "message": "PAPPERS_API_KEY manquant dans .env"}
    background_tasks.add_task(
        enrich_fr_tenure,
        db_path=settings.DATABASE_PATH,
        api_key=settings.PAPPERS_API_KEY,
        limit=limit,
        concurrency=5,
    )
    return {
        "status": "started",
        "message": "Enrichissement tenure FR démarré — date_prise_de_poste via MCP Pappers (gratuit 2 semaines)",
    }


@router.post("/enrich-financials")
async def enrich_financials(
    background_tasks: BackgroundTasks,
    countries: list[str] | None = None,
    limit: int | None = None,
):
    """Enrichit l'historique financier (4-5 ans) via Yahoo Finance pour les entreprises cotées."""
    settings = get_settings()
    background_tasks.add_task(
        enrich_financial_history,
        db_path=settings.DATABASE_PATH,
        countries=countries,
        limit=limit,
    )
    return {
        "status": "started",
        "message": "Enrichissement financier historique démarré via Yahoo Finance",
    }


@router.post("/scrape-brokers")
async def scrape_brokers(background_tasks: BackgroundTasks):
    """Scrape cession-pme.fr, fusac.fr, bsale.fr and match listings to DB companies."""
    settings = get_settings()
    background_tasks.add_task(run_broker_scrape, db_path=settings.DATABASE_PATH)
    return {
        "status": "started",
        "message": "Broker scraping started — cession-pme.fr, fusac.fr, bsale.fr",
    }


@router.post("/enrich-fr-financials")
async def enrich_fr_financials_endpoint(background_tasks: BackgroundTasks, limit: int = 0):
    """Enrichit l'historique financier des sociétés FR via Pappers MCP comptes-entreprise."""
    settings = get_settings()
    from scraper.enrichers.fr_financials_mcp import enrich_fr_financials
    background_tasks.add_task(
        enrich_fr_financials,
        db_path=settings.DATABASE_PATH,
        pappers_api_key=settings.PAPPERS_API_KEY,
        limit=limit,
    )
    return {"status": "started", "message": f"Enrichissement financier FR démarré (limit={limit or 'all'})"}


@router.post("/enrich-contacts")
async def enrich_contacts_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 500,
    skip_existing: bool = True,
):
    """Enrichit les contacts dirigeants (email/tél) via patterns nom+domaine + scraping site."""
    settings = get_settings()
    from scraper.enrichers.contact_guesser import batch_enrich_contacts
    background_tasks.add_task(
        batch_enrich_contacts,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        skip_existing=skip_existing,
    )
    return {"status": "started", "message": f"Enrichissement contacts démarré ({limit} profils max)"}


@router.post("/reinterpret-fi")
async def reinterpret_fi_endpoint(background_tasks: BackgroundTasks, limit: int = 0):
    """Re-interprète les profils FI existants avec les heuristiques améliorées (rapide, sans re-scraping)."""
    settings = get_settings()
    from scraper.enrichers.reinterpret_fi import reinterpret_all_fi
    background_tasks.add_task(reinterpret_all_fi, db_path=settings.DATABASE_PATH, limit=limit)
    return {"status": "started", "message": f"Ré-interprétation FI démarrée ({limit or 'tous les'} profils)"}


@router.post("/enrich-fr-dgfip")
async def enrich_fr_dgfip_endpoint(background_tasks: BackgroundTasks, limit: int = 0):
    """Enrichit l'historique financier des sociétés FR via l'API DGFiP/INPI (gratuit, sans clé)."""
    settings = get_settings()
    from scraper.enrichers.fr_dgfip import enrich_fr_financials_dgfip
    background_tasks.add_task(
        enrich_fr_financials_dgfip,
        db_path=settings.DATABASE_PATH,
        limit=limit,
    )
    return {"status": "started", "message": f"Enrichissement financier FR DGFiP démarré ({limit or 'toutes les'} entreprises)"}


@router.post("/batch-fi")
async def batch_fi_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 0,
    country: str = "",
    priority: str = "revenue",
):
    """Crée en masse les profils FI manquants (heuristiques DB uniquement, très rapide)."""
    settings = get_settings()
    from scraper.enrichers.batch_fi_creator import create_missing_fi_profiles
    background_tasks.add_task(
        create_missing_fi_profiles,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        country=country,
        priority=priority,
    )
    label = f"{limit or 'tous'}" + (f" [{country}]" if country else "")
    return {"status": "started", "message": f"Batch FI démarré — {label} profils (mode heuristique rapide)"}


@router.post("/enrich-websites")
async def enrich_websites_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    only_with_fi: bool = True,
):
    """Trouve les sites web officiels via DuckDuckGo. Priorité aux entreprises avec profil FI."""
    settings = get_settings()
    from scraper.enrichers.website_enricher import enrich_websites
    background_tasks.add_task(
        enrich_websites,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        only_with_fi=only_with_fi,
    )
    return {
        "status": "started",
        "message": f"Website enrichment démarré ({limit} entreprises, only_with_fi={only_with_fi})",
    }


@router.post("/enrich-pl-revenue")
async def enrich_pl_revenue_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 500,
    skip_existing: bool = True,
    priority: str = "fi_signal",
):
    """Enrichit le CA des entreprises PL via rejestr.io (500 req/jour gratuit).
    Nécessite REJESTR_IO_API_KEY dans .env — inscription gratuite sur rejestr.io/rejestracja
    """
    settings = get_settings()
    api_key = getattr(settings, "REJESTR_IO_API_KEY", "") or os.environ.get("REJESTR_IO_API_KEY", "")
    if not api_key:
        return {
            "status": "error",
            "message": "REJESTR_IO_API_KEY manquant. Inscription gratuite sur https://rejestr.io/rejestracja puis ajouter la clé dans .env"
        }
    from scraper.enrichers.pl_revenue import enrich_pl_revenues
    background_tasks.add_task(
        enrich_pl_revenues,
        db_path=settings.DATABASE_PATH,
        api_key=api_key,
        limit=limit,
        skip_existing=skip_existing,
        priority=priority,
    )
    return {"status": "started", "message": f"Enrichissement CA PL démarré — {limit} sociétés (quota: {limit}/500 req/jour)"}


@router.post("/enrich-pl-equans")
async def enrich_pl_equans_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 700,
    min_score: int = 0,
    min_revenue_eur: float = 100_000,
):
    """Enrichit et score les cibles Equans en Pologne via rejestr.io (ogolny).
    Extrait codes PKD complets, actionnaires + âge fondateur, dirigeants + CA réel.
    Nécessite REJESTR_IO_API_KEY dans .env.
    """
    settings = get_settings()
    api_key = getattr(settings, "REJESTR_IO_API_KEY", "") or os.environ.get("REJESTR_IO_API_KEY", "")
    if not api_key:
        return {"status": "error", "message": "REJESTR_IO_API_KEY manquant dans .env"}
    from scraper.enrichers.pl_equans import enrich_pl_equans
    background_tasks.add_task(
        enrich_pl_equans,
        db_path=settings.DATABASE_PATH,
        api_key=api_key,
        limit=limit,
        min_equans_score=min_score,
        min_revenue_eur=min_revenue_eur,
    )
    return {"status": "started", "message": f"Analyse Equans PL démarrée — {limit} sociétés (seuil CA ≥{min_revenue_eur/1e3:.0f}K€)"}


@router.get("/enrich-pl-equans-status")
async def enrich_pl_equans_status_endpoint():
    """Statut de l'enrichissement Equans PL."""
    from scraper.enrichers.pl_equans import get_pl_equans_status
    return get_pl_equans_status()


@router.post("/import-fr-bulk")
async def import_fr_bulk_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 0,
    min_employees_tranche: str = "21",
    only_with_revenue: bool = False,
):
    """Import en masse des entreprises FR via API Recherche Entreprises (gratuit, sans clé).
    Itère sur 101 départements × tranches d'effectif. Objectif : 30 000-60 000 entreprises.
    """
    settings = get_settings()
    from scraper.scrapers.france_bulk import import_fr_bulk
    background_tasks.add_task(
        import_fr_bulk,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_employees_tranche=min_employees_tranche,
        only_with_revenue=only_with_revenue,
    )
    return {
        "status": "started",
        "message": f"Import FR bulk démarré (tranche≥{min_employees_tranche}, limit={limit or 'all'}) — ~30-60K entreprises attendues",
    }


@router.post("/import-gleif")
async def import_gleif_endpoint(
    background_tasks: BackgroundTasks,
    countries: str = "",
    limit_per_country: int = 0,
    min_age_years: int = 5,
):
    """Importe en masse les sociétés depuis GLEIF pour n'importe quel pays (gratuit, sans auth).
    Passer countries comme chaîne CSV: "DE,IT,GB,NL,AT,BE,CH,PL,ES".
    Pas de CA ni dirigeants — enrichir ensuite avec nace-inferrer + revenue enrichers.
    """
    settings = get_settings()
    from scraper.scrapers.gleif_bulk import import_gleif_all
    targets = [c.strip().upper() for c in countries.split(",") if c.strip()] or ["DE", "IT", "ES"]
    background_tasks.add_task(
        import_gleif_all,
        db_path=settings.DATABASE_PATH,
        countries=targets,
        limit_per_country=limit_per_country,
        min_age_years=min_age_years,
    )
    return {
        "status": "started",
        "countries": targets,
        "limit_per_country": limit_per_country or "all",
        "message": f"Import GLEIF démarré pour {'+'.join(targets)} (limit={limit_per_country or 'all'}/pays, min_age={min_age_years}ans)",
    }


@router.post("/enrich-de-northdata")
async def enrich_de_northdata_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 100,
    min_equans_score: int = 0,
    skip_existing: bool = True,
):
    """Enrichit les entreprises DE via Northdata.de (Unternehmensgegenstand + dirigeants + CA).
    Nécessite NORTHDATA_API_KEY dans .env — inscription gratuite sur northdata.de/api
    Free tier : 100 req/jour.
    """
    settings = get_settings()
    api_key = settings.NORTHDATA_API_KEY or os.environ.get("NORTHDATA_API_KEY", "")
    if not api_key or api_key == "your_northdata_key_here":
        return {
            "status": "error",
            "message": "NORTHDATA_API_KEY manquant. Inscription gratuite sur https://www.northdata.de/api puis ajouter la clé dans .env",
        }
    from scraper.enrichers.de_northdata import enrich_de_northdata
    background_tasks.add_task(
        enrich_de_northdata,
        db_path=settings.DATABASE_PATH,
        api_key=api_key,
        limit=limit,
        only_equans_targets=min_equans_score > 0,
        min_equans_score=min_equans_score,
        skip_existing=skip_existing,
    )
    return {
        "status": "started",
        "message": f"Enrichissement DE Northdata démarré — {limit} entreprises (quota free: 100/jour)",
    }


@router.post("/infer-nace")
async def infer_nace_endpoint(
    background_tasks: BackgroundTasks,
    countries: str = "",
    limit: int = 0,
    overwrite: bool = False,
):
    """Infère les codes NACE manquants depuis les codes SIC (GB) ou les descriptions (autres pays).
    Stocke le résultat dans Company.nace_inferred sans toucher à nace_code.
    countries: CSV string ex. "NL,GB" ou vide = tous pays.
    """
    settings = get_settings()
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()] or None
    from scraper.enrichers.nace_inferrer import infer_nace_codes
    background_tasks.add_task(
        infer_nace_codes,
        db_path=settings.DATABASE_PATH,
        countries=country_list,
        limit=limit,
        overwrite=overwrite,
    )
    target = "+".join(country_list) if country_list else "tous pays"
    return {
        "status": "started",
        "message": f"Inférence NACE démarrée ({target}, limit={limit or 'all'})",
    }


@router.get("/infer-nace-status")
async def infer_nace_status_endpoint():
    from scraper.enrichers.nace_inferrer import get_nace_status
    return get_nace_status()


@router.post("/import-de-directories")
async def import_de_directories_endpoint(
    background_tasks: BackgroundTasks,
    sources: list[str] | None = None,
    nace_filter: list[str] | None = None,
    max_pages_per_keyword: int = 5,
    delay_factor: float = 1.0,
    limit: int = 0,
):
    """Scrape WLW.de, Europages, Gelbeseiten, Kompass pour les entreprises DE Equans.

    - sources: ["wlw","europages","gelbeseiten","kompass"] (None = toutes)
    - nace_filter: ["43.21","43.22",...] (None = tous codes WZ)
    - max_pages_per_keyword: pages par mot-clé (défaut 5)
    - delay_factor: multiplicateur délai (1.0 normal, 2.0 lent)
    - limit: max entreprises insérées (0 = illimité)
    """
    settings = get_settings()
    from scraper.scrapers.de_directories import import_de_directories
    background_tasks.add_task(
        import_de_directories,
        db_path=settings.DATABASE_PATH,
        sources=sources,
        nace_filter=nace_filter,
        max_pages_per_keyword=max_pages_per_keyword,
        delay_factor=delay_factor,
        limit=limit,
    )
    src_label = "+".join(sources) if sources else "wlw+europages+gelbeseiten+kompass"
    nace_label = "+".join(nace_filter) if nace_filter else "tous codes WZ/NACE"
    return {
        "status": "started",
        "message": f"Scraping annuaires DE démarré — {src_label} / {nace_label}",
    }


@router.post("/enrich-de-websites")
async def enrich_de_websites_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    min_score: int = 30,
    concurrency: int = 5,
    overwrite: bool = False,
):
    """Crawl les sites des entreprises DE pour extraire description, effectifs, CA estimé."""
    settings = get_settings()
    from scraper.enrichers.de_website_content import enrich_de_website_content
    background_tasks.add_task(
        enrich_de_website_content,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_score=min_score,
        concurrency=concurrency,
        overwrite=overwrite,
    )
    return {
        "status": "started",
        "message": f"Crawl sites DE démarré — {limit} cibles (score≥{min_score})",
    }


@router.post("/enrich-de-bundesanzeiger")
async def enrich_de_bundesanzeiger_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    min_score: int = 40,
):
    """Scrape Bundesanzeiger pour récupérer les CA officiels (Umsatzerlöse) des GmbH/AG."""
    settings = get_settings()
    from scraper.enrichers.de_bundesanzeiger import enrich_de_bundesanzeiger
    background_tasks.add_task(
        enrich_de_bundesanzeiger,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_score=min_score,
    )
    return {
        "status": "started",
        "message": f"Recherche CA Bundesanzeiger démarrée — {limit} cibles GmbH/AG (score≥{min_score})",
    }


@router.post("/enrich-de-web-search")
async def enrich_de_web_search_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    min_score: int = 40,
    delay: float = 5.0,
):
    """Recherche CA des sociétés DE via DuckDuckGo (scraping HTML, pas de clé API)."""
    settings = get_settings()
    from scraper.enrichers.de_web_search import enrich_de_web_search
    background_tasks.add_task(
        enrich_de_web_search,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_score=min_score,
        delay=delay,
    )
    est_time = round(limit * delay / 60, 1)
    return {
        "status": "started",
        "message": f"Recherche CA DuckDuckGo démarrée — {limit} cibles DE (score≥{min_score}, ~{est_time}min)",
    }


@router.get("/enrich-de-web-search-status")
async def enrich_de_web_search_status_endpoint():
    """Statut de l'enrichisseur CA web search DE."""
    from scraper.enrichers.de_web_search import get_de_web_search_status
    return get_de_web_search_status()


@router.post("/enrich-gb-web-search")
async def enrich_gb_web_search_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    min_score: int = 30,
    delay: float = 6.0,
):
    """Recherche CA des sociétés GB via DuckDuckGo (turnover/revenue £ millions)."""
    settings = get_settings()
    from scraper.enrichers.gb_web_search import enrich_gb_web_search
    background_tasks.add_task(
        enrich_gb_web_search,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_score=min_score,
        delay=delay,
    )
    est_time = round(limit * delay / 60, 1)
    return {
        "status": "started",
        "message": f"Recherche CA DDG GB démarrée — {limit} cibles (score≥{min_score}, ~{est_time}min)",
    }


@router.get("/enrich-gb-web-search-status")
async def gb_web_search_status_endpoint():
    """Statut de l'enrichisseur CA web search GB."""
    from scraper.enrichers.gb_web_search import get_gb_web_search_status
    return get_gb_web_search_status()


@router.post("/enrich-it-web-search")
async def enrich_it_web_search_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 200,
    min_score: int = 25,
    delay: float = 6.0,
):
    """Recherche CA des sociétés IT via DuckDuckGo (fatturato milioni)."""
    settings = get_settings()
    from scraper.enrichers.it_web_search import enrich_it_web_search
    background_tasks.add_task(
        enrich_it_web_search,
        db_path=settings.DATABASE_PATH,
        limit=limit,
        min_score=min_score,
        delay=delay,
    )
    est_time = round(limit * delay / 60, 1)
    return {
        "status": "started",
        "message": f"Recherche CA DDG IT démarrée — {limit} cibles (score≥{min_score}, ~{est_time}min)",
    }


@router.get("/enrich-it-web-search-status")
async def it_web_search_status_endpoint():
    """Statut de l'enrichisseur CA web search IT."""
    from scraper.enrichers.it_web_search import get_it_web_search_status
    return get_it_web_search_status()


@router.post("/enrich-nl-web-search")
async def enrich_nl_web_search_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 300,
    min_score: int = 25,
    delay: float = 5.0,
):
    """Recherche CA des sociétés NL via DuckDuckGo (omzet miljoenen)."""
    settings = get_settings()
    from scraper.enrichers.nl_web_search import enrich_nl_web_search
    background_tasks.add_task(enrich_nl_web_search, db_path=settings.DATABASE_PATH,
                               limit=limit, min_score=min_score, delay=delay)
    est_time = round(limit * delay / 60, 1)
    return {"status": "started", "message": f"Recherche CA DDG NL démarrée — {limit} cibles (score≥{min_score}, ~{est_time}min)"}


@router.get("/enrich-nl-web-search-status")
async def enrich_nl_web_search_status_endpoint():
    from scraper.enrichers.nl_web_search import get_nl_web_search_status
    return get_nl_web_search_status()


@router.post("/enrich-dach-web-search")
async def enrich_dach_web_search_endpoint(
    background_tasks: BackgroundTasks,
    countries: str = "AT,BE,CH",
    limit: int = 300,
    min_score: int = 25,
    delay: float = 5.0,
):
    """Recherche CA des sociétés AT/BE/CH via DuckDuckGo (Umsatz / chiffre d'affaires)."""
    settings = get_settings()
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()] or ["AT", "BE", "CH"]
    from scraper.enrichers.dach_web_search import enrich_dach_web_search
    background_tasks.add_task(enrich_dach_web_search, db_path=settings.DATABASE_PATH,
                               countries=country_list, limit=limit, min_score=min_score, delay=delay)
    est_time = round(limit * delay / 60, 1)
    return {"status": "started", "message": f"Recherche CA DDG {'+'.join(country_list)} démarrée — {limit} cibles (~{est_time}min)"}


@router.get("/enrich-dach-web-search-status")
async def enrich_dach_web_search_status_endpoint():
    from scraper.enrichers.dach_web_search import get_dach_web_search_status
    return get_dach_web_search_status()


@router.post("/enrich-gleif-dates")
async def enrich_gleif_dates_endpoint(
    background_tasks: BackgroundTasks,
    countries: str = "",
    limit: int = 2000,
    delay: float = 0.2,
):
    """Récupère les dates de création GLEIF pour les sociétés sans creation_date.
    Cible les sociétés dont registration_number est un LEI (18-20 chars alphanumériques).
    Améliore le longevity_score après rescore.
    """
    settings = get_settings()
    from scraper.enrichers.gleif_dates import enrich_gleif_dates
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()] or None
    background_tasks.add_task(
        enrich_gleif_dates,
        db_path=settings.DATABASE_PATH,
        countries=country_list,
        limit=limit,
        delay=delay,
    )
    target = "+".join(country_list) if country_list else "tous pays"
    return {
        "status": "started",
        "message": f"Enrichissement dates GLEIF démarré — {target}, limit={limit}",
    }


@router.get("/enrich-gleif-dates-status")
async def gleif_dates_status_endpoint():
    """Statut de l'enrichisseur dates GLEIF."""
    from scraper.enrichers.gleif_dates import get_gleif_dates_status
    return get_gleif_dates_status()


@router.post("/enrich-gb-sic")
async def enrich_gb_sic_endpoint(
    background_tasks: BackgroundTasks,
    limit: int = 2000,
    delay: float = 0.6,
):
    """Récupère les codes SIC Companies House pour les sociétés GB sans activity_description.
    Les codes SIC sont ensuite convertis en NACE par l'inféreur NACE (43210 → 43.21).
    Cible les sociétés avec numéro d'enregistrement CH (≤8 chars).
    Rate limit CH API : 600 req/min → delay=0.12s recommandé.
    """
    settings = get_settings()
    from scraper.enrichers.gb_sic import enrich_gb_sic
    api_key = settings.model_dump().get("COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        return {"error": "COMPANIES_HOUSE_API_KEY non configurée"}
    background_tasks.add_task(
        enrich_gb_sic,
        db_path=settings.DATABASE_PATH,
        api_key=api_key,
        limit=limit,
        delay=delay,
    )
    return {"status": "started", "message": f"Enrichissement SIC GB démarré — limit={limit}"}


@router.get("/enrich-gb-sic-status")
async def enrich_gb_sic_status_endpoint():
    """Statut de l'enrichisseur SIC GB (Companies House)."""
    from scraper.enrichers.gb_sic import get_gb_sic_status
    return get_gb_sic_status()


@router.get("/import-de-directories-status")
async def get_de_directories_status():
    """Retourne le statut du scraping annuaires DE en cours."""
    from scraper.scrapers.de_directories import get_import_status
    return get_import_status()


@router.post("/import-eu-directories")
async def import_eu_directories_endpoint(
    background_tasks: BackgroundTasks,
    countries: list[str] | None = None,
    limit_per_kw: int = 30,
    delay: float = 3.0,
):
    """Scrape Europages pour IT, AT, BE, NL, CH — ciblage Equans.

    - countries: ["IT","AT","BE","NL","CH"] (None = tous)
    - limit_per_kw: max entreprises par mot-clé (défaut 30 ≈ 3 pages)
    - delay: délai entre requêtes en secondes (défaut 3.0)
    """
    settings = get_settings()
    from scraper.scrapers.eu_directories import import_eu_directories
    target = countries or ["IT", "AT", "BE", "NL", "CH"]
    background_tasks.add_task(
        import_eu_directories,
        db_path=settings.DATABASE_PATH,
        countries=target,
        limit_per_kw=limit_per_kw,
        delay=delay,
    )
    return {
        "status": "started",
        "countries": target,
        "message": f"Scraping annuaires EU démarré — Europages · {'+'.join(target)} · limit_per_kw={limit_per_kw}",
    }


@router.get("/import-eu-directories-status")
async def get_eu_directories_status():
    """Retourne le statut du scraping annuaires EU en cours."""
    from scraper.scrapers.eu_directories import get_import_eu_status
    return get_import_eu_status()


@router.post("/scrape-zefix-ch")
async def scrape_zefix_endpoint(
    background_tasks: BackgroundTasks,
    max_entries_per_kw: int = 500,
    delay: float = 1.0,
):
    """Scrape le registre du commerce suisse (Zefix) — ciblage Equans CH.
    API publique, sans clé. Retourne 50-200 entreprises par keyword secteur.
    """
    settings = get_settings()
    from scraper.scrapers.zefix_ch import scrape_zefix
    background_tasks.add_task(
        scrape_zefix,
        db_path=settings.DATABASE_PATH,
        max_entries_per_kw=max_entries_per_kw,
        delay=delay,
    )
    return {"status": "started", "message": f"Zefix CH — {len(['elektroinstallation','elektrotechnik','klimatechnik','haustechnik','lueftungstechnik','sanitaertechnik','gebaeudeautomation','brandschutz','sicherheitstechnik','anlagenbau','industrieservice','facility','gebaeudeinformatik','elektriker'])} keywords, max {max_entries_per_kw} par kw"}


@router.get("/scrape-zefix-ch-status")
async def zefix_status_endpoint():
    """Statut du scraping Zefix CH."""
    from scraper.scrapers.zefix_ch import get_zefix_status
    return get_zefix_status()


@router.post("/scrape-goudengids-nl")
async def scrape_goudengids_endpoint(
    background_tasks: BackgroundTasks,
    max_pages_per_kw: int = 5,
    delay: float = 2.0,
):
    """Scrape Goudengids.nl (pages jaunes NL) — ciblage Equans NL.
    JSON-LD ItemList/LocalBusiness. 14 keywords, max {max_pages_per_kw} pages par kw.
    """
    settings = get_settings()
    from scraper.scrapers.goudengids_nl import scrape_goudengids
    background_tasks.add_task(
        scrape_goudengids,
        db_path=settings.DATABASE_PATH,
        max_pages_per_kw=max_pages_per_kw,
        delay=delay,
    )
    return {"status": "started", "message": f"Goudengids NL — 14 keywords, max {max_pages_per_kw} pages par kw"}


@router.get("/scrape-goudengids-nl-status")
async def goudengids_status_endpoint():
    """Statut du scraping Goudengids NL."""
    from scraper.scrapers.goudengids_nl import get_goudengids_status
    return get_goudengids_status()


@router.post("/scrape-gleif-targeted")
async def scrape_gleif_targeted_endpoint(
    background_tasks: BackgroundTasks,
    countries: str = "AT,BE",
    max_pages_per_kw: int = 5,
    delay: float = 0.5,
):
    """Scrape GLEIF par mots-clés Equans pour les pays faibles (AT, BE).
    API publique GLEIF, sans clé. NACE inférée depuis le keyword.
    """
    settings = get_settings()
    from scraper.scrapers.gleif_targeted import scrape_gleif_targeted
    country_list = [c.strip().upper() for c in countries.split(",") if c.strip()]
    background_tasks.add_task(
        scrape_gleif_targeted,
        db_path=settings.DATABASE_PATH,
        countries=country_list,
        max_pages_per_kw=max_pages_per_kw,
        delay=delay,
    )
    return {"status": "started", "message": f"GLEIF ciblé — pays: {country_list}"}


@router.get("/scrape-gleif-targeted-status")
async def gleif_targeted_status_endpoint():
    """Statut du scraping GLEIF ciblé."""
    from scraper.scrapers.gleif_targeted import get_gleif_targeted_status
    return get_gleif_targeted_status()


@router.get("/available")
async def list_available_scrapers():
    return {
        "scrapers": [
            {
                "name": "pappers_fr",
                "country": "FR",
                "description": "France — Pappers API (revenue + directors + ages)",
                "requires_key": "PAPPERS_API_KEY",
                "free_registration": "https://www.pappers.fr/api",
            },
            {
                "name": "companies_house_uk",
                "country": "GB",
                "description": "UK — Companies House (directors + SIC codes, no revenue)",
                "requires_key": "COMPANIES_HOUSE_API_KEY",
                "free_registration": "https://developer.company-information.service.gov.uk/",
            },
            {
                "name": "brreg_no",
                "country": "NO",
                "description": "Norway — Brønnøysund (revenue in NOK + directors)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "cvr_dk",
                "country": "DK",
                "description": "Denmark — CVR Elasticsearch (employees, no revenue without credentials)",
                "requires_key": "CVR_USERNAME + CVR_PASSWORD",
                "free_registration": "Email cvrselvbetjening@erst.dk",
            },
            {
                "name": "opencorporates_be",
                "country": "BE",
                "description": "Belgium — OpenCorporates (identity only, no revenue)",
                "requires_key": "OPENCORPORATES_API_TOKEN",
                "free_registration": "https://opencorporates.com/api_accounts/new",
            },
            {
                "name": "krs_pl",
                "country": "PL",
                "description": "Poland — KRS (directors + activity, no revenue)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "anaf_ro",
                "country": "RO",
                "description": "Romania — ANAF open data (revenue available!)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "borme_es",
                "country": "ES",
                "description": "Spain — BORME/open data (directors + activity, no revenue)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "hr_de",
                "country": "DE",
                "description": "Germany — DAX 40 + MDAX + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "cciaa_it",
                "country": "IT",
                "description": "Italy — FTSE MIB + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "swe_se",
                "country": "SE",
                "description": "Sweden — OMX Stockholm + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "uid_ch",
                "country": "CH",
                "description": "Switzerland — SMI / SMI Expanded + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "kvk_nl",
                "country": "NL",
                "description": "Netherlands — KVK (directors + activity, no revenue)",
                "requires_key": "KVK_API_KEY",
                "free_registration": "https://developers.kvk.nl/",
            },
            {
                "name": "kvk_nl_curated",
                "country": "NL",
                "description": "Netherlands — AEX large caps + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "rnpc_pt",
                "country": "PT",
                "description": "Portugal — PSI 20 + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "yf_bootstrap",
                "country": "PL+ES+RO",
                "description": "Poland/Spain/Romania — Yahoo Finance listed companies (real revenue)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "be_curated",
                "country": "BE",
                "description": "Belgium — BEL20 + MIDCAP + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "dk_curated",
                "country": "DK",
                "description": "Denmark — OMX C25 + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "at_curated",
                "country": "AT",
                "description": "Austria — ATX + grandes entreprises privées (Yahoo Finance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
            {
                "name": "tse_jp",
                "country": "JP",
                "description": "Japan — TSE (Tokyo Stock Exchange) ~3 500 sociétés cotées (yfinance)",
                "requires_key": None,
                "free_registration": "No registration needed",
            },
        ]
    }
