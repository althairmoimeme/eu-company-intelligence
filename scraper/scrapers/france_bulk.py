"""Import en masse des entreprises françaises via API Recherche Entreprises (gratuite, sans clé).

Source : https://recherche-entreprises.api.gouv.fr
Retourne : SIREN, nom, CA, effectif, NAF, dirigeants (avec annee_de_naissance), siège.
Stratégie : itère sur les 101 départements × tranches d'effectif pour éviter la limite 10K/requête.
Objectif : 30 000-60 000 entreprises avec CA > €5M et dirigeants.
"""
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select

from ..db.session import get_session_factory
from ..db.models import Company, Director
from ..enrichers.nace import normalize_code, code_to_sector_label

logger = logging.getLogger(__name__)

GOV_SEARCH = "https://recherche-entreprises.api.gouv.fr/search"
PAGE_SIZE = 25   # max autorisé par l'API
MAX_PAGES = 400  # = 10 000 résultats max par requête

# Tranches effectif à cibler (code INSEE → nb estimé employés)
# On cible 50 employés et plus = PME / ETI avec potentiel M&A
TARGET_TRANCHES = ["21", "22", "31", "32", "41", "42", "51", "52", "53"]

EFFECTIF_MAP = {
    "00": 0, "01": 2, "02": 5, "03": 10, "11": 15, "12": 30,
    "21": 75, "22": 150, "31": 350, "32": 750, "41": 1500,
    "42": 3500, "51": 7500, "52": 15000, "53": 40000,
}

# Tous les départements français (01-95 + DOM-TOM)
DEPARTEMENTS = [
    *[f"{i:02d}" for i in range(1, 96) if i != 20],
    "2A", "2B",
    "971", "972", "973", "974",
]

MIN_REVENUE = 5_000_000  # €5M minimum


def _parse_directors(raw_dirs: list) -> list[dict]:
    """Parse les dirigeants depuis l'API Recherche Entreprises."""
    directors = []
    for d in raw_dirs or []:
        if d.get("type_dirigeant") == "personne morale":
            continue
        prenom = (d.get("prenoms") or "").strip()
        nom = (d.get("nom") or "").strip()
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

        appointed_raw = d.get("date_prise_de_poste") or d.get("date_nomination")
        appointed_at = None
        if appointed_raw:
            try:
                appointed_at = str(appointed_raw)[:10]
            except Exception:
                pass

        directors.append({
            "name": full_name[:200],
            "role": d.get("qualite") or d.get("role"),
            "birth_year": birth_year,
            "appointed_at": appointed_at,
            "nationality": d.get("nationalite"),
        })
    return directors


def _parse_company(raw: dict) -> tuple[dict, list[dict]] | None:
    """Parse une entrée API → (company_row, directors_list) ou None."""
    siren = str(raw.get("siren") or "").strip()
    name = (raw.get("nom_complet") or "").strip()
    if not siren or not name:
        return None

    # CA depuis finances
    revenue_eur = None
    revenue_year = None
    finances = raw.get("finances") or {}
    if finances:
        try:
            latest_year = max(finances.keys(), key=int)
            ca = finances[latest_year].get("ca")
            if ca and float(ca) >= MIN_REVENUE:
                revenue_eur = float(ca)
                revenue_year = int(latest_year)
        except Exception:
            pass

    # Effectif
    tranche = str(raw.get("tranche_effectif_salarie") or "").zfill(2)
    employees = EFFECTIF_MAP.get(tranche)

    # NAF / secteur
    naf_raw = (raw.get("activite_principale")
               or (raw.get("siege") or {}).get("activite_principale"))
    nace = normalize_code(naf_raw) if naf_raw else None
    sector = code_to_sector_label(nace) if nace else None

    # Siège
    siege = raw.get("siege") or {}
    city = siege.get("libelle_commune") or siege.get("commune")
    postal_code = siege.get("code_postal")
    address = siege.get("adresse")

    # Date création
    creation_date = raw.get("date_creation") or siege.get("date_debut_activite")
    if creation_date:
        creation_date = str(creation_date)[:10]

    company_row = {
        "name": name[:200],
        "country": "FR",
        "registration_number": siren,
        "revenue_eur": revenue_eur,
        "revenue_year": revenue_year,
        "employees": employees,
        "sector": sector,
        "nace_code": nace,
        "creation_date": creation_date,
        "city": city,
        "postal_code": postal_code,
        "address": address,
        "source_url": f"https://www.pappers.fr/entreprise/{siren}",
    }

    directors = _parse_directors(raw.get("dirigeants") or [])
    return company_row, directors


async def _upsert_batch(factory, companies: list[tuple[dict, list]]) -> tuple[int, int]:
    """Insère ou met à jour un batch de sociétés. Retourne (inserted, updated)."""
    if not companies:
        return 0, 0

    inserted = 0
    updated = 0

    async with factory() as session:
        async with session.begin():
            for company_row, directors in companies:
                # Upsert company
                stmt = sqlite_insert(Company).values([company_row])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["country", "registration_number"],
                    set_={
                        k: company_row[k] for k in company_row
                        if k not in ("country", "registration_number")
                        and company_row[k] is not None
                    }
                )
                result = await session.execute(stmt)

                # Récupérer l'ID de la société
                company_id_q = await session.execute(
                    select(Company.id).where(
                        Company.country == "FR",
                        Company.registration_number == company_row["registration_number"]
                    )
                )
                company_id = company_id_q.scalar()
                if not company_id:
                    continue

                # Supprimer anciens dirigeants et réinsérer
                if directors:
                    await session.execute(
                        Director.__table__.delete().where(Director.company_id == company_id)
                    )
                    await session.execute(
                        Director.__table__.insert().values([
                            {"company_id": company_id, **d} for d in directors
                        ])
                    )

                if result.rowcount and result.rowcount > 0:
                    inserted += 1
                else:
                    updated += 1

    return inserted, updated


async def import_fr_bulk(
    db_path: str,
    limit: int = 0,
    min_employees_tranche: str = "21",  # 50+ employés
    concurrency: int = 8,
    only_with_revenue: bool = False,
) -> dict:
    """Import en masse des entreprises FR par département.

    Args:
        db_path: chemin DB SQLite
        limit: max sociétés importées (0 = tout)
        min_employees_tranche: tranche effectif minimum (21 = 50-99)
        concurrency: requêtes parallèles
        only_with_revenue: ne garder que les sociétés avec CA

    Returns:
        {"imported": N, "updated": N, "skipped": N, "depts_done": N}
    """
    factory = get_session_factory(db_path)
    sem = asyncio.Semaphore(concurrency)

    total_imported = 0
    total_updated = 0
    total_skipped = 0
    depts_done = 0

    # Tranches à cibler selon le seuil minimum
    tranche_codes = TARGET_TRANCHES[TARGET_TRANCHES.index(min_employees_tranche):]

    async def fetch_dept_tranche(dept: str, tranche: str):
        nonlocal total_imported, total_updated, total_skipped, depts_done
        async with sem:
            async with httpx.AsyncClient(timeout=30) as client:
                for page in range(1, MAX_PAGES + 1):
                    if limit and (total_imported + total_updated) >= limit:
                        return
                    try:
                        resp = await client.get(GOV_SEARCH, params={
                            "departement": dept,
                            "tranche_effectif_salarie": tranche,
                            "per_page": PAGE_SIZE,
                            "page": page,
                            "minimal": "false",
                        }, timeout=15)
                        if resp.status_code == 429:
                            await asyncio.sleep(5)
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.debug(f"[FR-BULK] {dept}/{tranche} p{page}: {e}")
                        await asyncio.sleep(2)
                        break

                    results = data.get("results") or []
                    if not results:
                        break

                    # Parser les résultats
                    batch = []
                    for raw in results:
                        parsed = _parse_company(raw)
                        if parsed is None:
                            total_skipped += 1
                            continue
                        company_row, directors = parsed
                        if only_with_revenue and not company_row.get("revenue_eur"):
                            total_skipped += 1
                            continue
                        batch.append((company_row, directors))

                    if batch:
                        ins, upd = await _upsert_batch(factory, batch)
                        total_imported += ins
                        total_updated += upd

                    total_pages = (data.get("total_results", 0) + PAGE_SIZE - 1) // PAGE_SIZE
                    if page >= min(total_pages, MAX_PAGES):
                        break

                    await asyncio.sleep(0.3)

            depts_done += 1
            if depts_done % 10 == 0:
                logger.info(
                    f"[FR-BULK] {depts_done}/{len(DEPARTEMENTS)*len(tranche_codes)} combos "
                    f"— imported={total_imported}, updated={total_updated}"
                )

    # Générer toutes les combinaisons dept × tranche
    tasks = [
        fetch_dept_tranche(dept, tranche)
        for dept in DEPARTEMENTS
        for tranche in tranche_codes
    ]

    logger.info(
        f"[FR-BULK] Démarrage — {len(tasks)} combinaisons dept×tranche, "
        f"concurrency={concurrency}, limit={limit or 'all'}"
    )

    # Traiter par batch de 50 tâches
    batch_size = 50
    for i in range(0, len(tasks), batch_size):
        if limit and (total_imported + total_updated) >= limit:
            break
        batch = tasks[i:i + batch_size]
        await asyncio.gather(*batch, return_exceptions=True)

    result = {
        "imported": total_imported,
        "updated": total_updated,
        "skipped": total_skipped,
        "depts_done": depts_done,
    }
    logger.info(f"[FR-BULK] Terminé — {result}")
    return result
