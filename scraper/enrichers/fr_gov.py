"""Enrichissement des entreprises françaises via API gouvernementale gratuite.

Source: https://recherche-entreprises.api.gouv.fr
Données: dirigeants (avec annee_de_naissance), CA réel, effectif, code NAF, date création.
Aucune clé API requise. Rate limit généreux (~50 req/s).
"""
import asyncio
import logging
from datetime import date, datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, Director, EquansScore
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

GOV_API = "https://recherche-entreprises.api.gouv.fr/search"

# Tranche effectif → estimation du nombre d'employés (milieu de fourchette)
EFFECTIF_MAP = {
    "00": 0, "01": 2, "02": 5, "03": 10, "11": 15, "12": 30,
    "21": 75, "22": 150, "31": 350, "32": 750, "41": 1500,
    "42": 3500, "51": 7500, "52": 15000, "53": 40000,
}


async def _fetch_company(client: httpx.AsyncClient, siren: str) -> dict | None:
    """Récupère les données d'une entreprise via SIREN (retry sur 429)."""
    for attempt in range(5):
        try:
            resp = await client.get(GOV_API, params={"q": siren, "per_page": 1}, timeout=15)
            if resp.status_code == 429:
                wait = 2 ** attempt  # 1, 2, 4, 8, 16 s
                logger.debug(f"[FR-GOV] 429 SIREN {siren}, retry in {wait}s (attempt {attempt+1})")
                await asyncio.sleep(wait)
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return None
            r = results[0]
            # Vérifie qu'on a le bon SIREN
            if str(r.get("siren", "")).strip() != siren.strip():
                return None
            return r
        except Exception as e:
            logger.debug(f"[FR-GOV] Erreur SIREN {siren}: {e}")
            return None
    logger.warning(f"[FR-GOV] Abandon après 5 tentatives SIREN {siren}")
    return None


async def _process_one(
    factory, client: httpx.AsyncClient,
    company_id: int, siren: str,
) -> tuple[bool, int]:
    """Enrichit une entreprise. Retourne (updated, dirs_count)."""
    data = await _fetch_company(client, siren)
    if not data:
        return False, 0

    # ── CA réel ─────────────────────────────────────────────────────────────
    revenue_eur = None
    revenue_year = None
    finances = data.get("finances") or {}
    if finances:
        latest_year = max(finances.keys(), key=int)
        ca = finances[latest_year].get("ca")
        if ca and ca > 0:
            revenue_eur = float(ca)
            revenue_year = int(latest_year)

    # ── Effectif ─────────────────────────────────────────────────────────────
    tranche = str(data.get("tranche_effectif_salarie") or "").zfill(2)
    employees = EFFECTIF_MAP.get(tranche)

    # ── NAF / Secteur ─────────────────────────────────────────────────────────
    naf_raw = data.get("activite_principale") or (
        data.get("siege", {}) or {}
    ).get("activite_principale")
    nace = normalize_code(naf_raw) if naf_raw else None
    sector = code_to_sector_label(nace) if nace else None

    # ── Dirigeants ────────────────────────────────────────────────────────────
    raw_dirs = [
        d for d in (data.get("dirigeants") or [])
        if d.get("type_dirigeant") != "personne morale"
    ]
    dir_objects = []
    for d in raw_dirs:
        prenom = d.get("prenoms", "") or ""
        nom = d.get("nom", "") or ""
        full_name = f"{prenom} {nom}".strip()
        if not full_name:
            continue
        birth_year = None
        raw_by = d.get("annee_de_naissance")
        if raw_by:
            try:
                birth_year = int(str(raw_by)[:4])
            except Exception:
                pass
        dir_objects.append(Director(
            company_id=company_id,
            name=full_name,
            role=d.get("qualite"),
            birth_year=birth_year,
            nationality=d.get("nationalite"),
        ))

    # ── Écriture en DB ───────────────────────────────────────────────────────
    try:
        async with factory() as session:
            async with session.begin():
                upd: dict = {}
                if revenue_eur:
                    upd.update({"revenue_eur": revenue_eur, "revenue_year": revenue_year,
                                "revenue_estimated": False})
                if employees:
                    upd["employees"] = employees
                if nace:
                    upd["nace_code"] = nace
                if sector:
                    upd["sector"] = sector
                if upd:
                    await session.execute(
                        update(Company).where(Company.id == company_id).values(**upd)
                    )
                if dir_objects:
                    await session.execute(
                        Director.__table__.delete().where(Director.company_id == company_id)
                    )
                    session.add_all(dir_objects)
    except Exception as e:
        logger.debug(f"[FR-GOV] DB error company {company_id}: {e}")
        return False, 0

    return True, len(dir_objects)


async def enrich_fr_companies(
    db_path: str = "companies.db",
    limit: int | None = None,
    concurrency: int = 8,
    only_without_revenue: bool = False,
    min_revenue: float | None = None,
    max_revenue: float | None = None,
    min_score: int | None = None,
) -> dict:
    """
    Enrichit toutes les entreprises françaises en parallèle (concurrency=8).
    Données : dirigeants + annee_de_naissance, CA réel, effectif, NAF.
    Source : recherche-entreprises.api.gouv.fr (gratuit, sans clé).
    """
    factory = get_session_factory(db_path)
    updated = 0
    directors_added = 0
    skipped = 0

    async with factory() as session:
        q = (
            select(Company.id, Company.registration_number, Company.name)
            .where(Company.country == "FR")
        )
        if only_without_revenue:
            q = q.where(Company.revenue_eur.is_(None))
        if min_revenue is not None:
            q = q.where(Company.revenue_eur >= min_revenue)
        if max_revenue is not None:
            q = q.where(Company.revenue_eur <= max_revenue)
        # Always join for score-based ordering
        q = q.outerjoin(EquansScore, EquansScore.company_id == Company.id)
        if min_score is not None:
            q = q.where(EquansScore.total_score >= min_score)
        if only_without_revenue or min_revenue is not None:
            q = q.order_by(EquansScore.total_score.desc().nulls_last())
        else:
            q = q.order_by(Company.revenue_eur.desc().nulls_last())
        result = await session.execute(q)
        companies = result.fetchall()

    if limit:
        companies = companies[:limit]

    total = len(companies)
    logger.info(f"[FR-GOV] Enrichissement de {total} entreprises FR (concurrency={concurrency})")

    sem = asyncio.Semaphore(concurrency)

    async def bounded(company_id, siren, name, client):
        nonlocal updated, directors_added, skipped
        if not siren or len(str(siren).strip()) < 9:
            skipped += 1
            return
        async with sem:
            ok, dirs = await _process_one(factory, client, company_id, str(siren).strip())
            if ok:
                updated += 1
                directors_added += dirs
            else:
                skipped += 1
            await asyncio.sleep(0.3)  # throttle par worker (~3 req/s par slot)

    async with httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_connections=8)) as client:
        tasks = [
            bounded(company_id, siren, name, client)
            for company_id, siren, name in companies
        ]
        # Traitement par chunks de 500 pour logger la progression
        chunk = 500
        for start in range(0, len(tasks), chunk):
            await asyncio.gather(*tasks[start:start + chunk])
            logger.info(
                f"[FR-GOV] {min(start+chunk, total)}/{total} — "
                f"ok={updated} dirs={directors_added} skip={skipped}"
            )

    logger.info(f"[FR-GOV] ✅ Terminé — {updated} entreprises, {directors_added} dirigeants")
    return {"updated": updated, "directors_added": directors_added, "skipped": skipped}
