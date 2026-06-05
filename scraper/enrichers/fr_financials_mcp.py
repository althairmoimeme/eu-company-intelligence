"""Enrichissement de l'historique financier des entreprises françaises via MCP Pappers.

Source: https://mcp.pappers.fr/{api_key}
Outil MCP: comptes-entreprise (param: siren)
Données: historique financier multi-années (CA, résultat net, résultat exploitation, trésorerie, dettes).
Utilise le protocole MCP (JSON-RPC over HTTP/SSE).
"""
import asyncio
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, FinancialHistory

logger = logging.getLogger(__name__)

MCP_BASE = "https://mcp.pappers.fr"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


async def _call_mcp(client: httpx.AsyncClient, api_key: str, tool: str, args: dict) -> dict | None:
    """Appelle un outil MCP Pappers. Retourne le dict résultat ou None."""
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "id": 1,
        "params": {"name": tool, "arguments": args},
    }
    try:
        resp = await client.post(
            f"{MCP_BASE}/{api_key}",
            headers=MCP_HEADERS,
            json=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = data.get("result", {}).get("content", [])
        for block in content:
            if block.get("type") == "text":
                return json.loads(block["text"])
        return None
    except Exception as e:
        logger.debug(f"[FR_FIN] Erreur outil {tool}: {e}")
        return None


async def _process_one(
    factory,
    client: httpx.AsyncClient,
    api_key: str,
    company_id: int,
    company_name: str,
    siren: str,
    sem: asyncio.Semaphore,
) -> int:
    """Enrichit l'historique financier d'une entreprise. Retourne nb d'années upsertées."""
    async with sem:
        data = await _call_mcp(client, api_key, "comptes-entreprise", {"siren": siren})
        if not data:
            return 0

    # La réponse peut être une liste directement ou un dict avec une clé contenant la liste
    comptes = None
    if isinstance(data, list):
        comptes = data
    elif isinstance(data, dict):
        # Chercher la clé qui contient la liste des comptes annuels
        for key in ("comptes", "exercices", "bilans", "resultats", "annees"):
            if key in data and isinstance(data[key], list):
                comptes = data[key]
                break
        if comptes is None:
            # Fallback: prendre la première valeur qui est une liste
            for v in data.values():
                if isinstance(v, list) and len(v) > 0:
                    comptes = v
                    break

    if not comptes:
        return 0

    snapshots = []
    for compte in comptes:
        if not isinstance(compte, dict):
            continue

        annee = compte.get("annee")
        if not annee:
            continue

        try:
            annee = int(annee)
        except (ValueError, TypeError):
            continue

        def _to_float(val):
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        snap = {
            "year": annee,
            "revenue_eur": _to_float(compte.get("chiffre_affaires")),
            "operating_income_eur": _to_float(compte.get("resultat_exploitation")),
            "net_income_eur": _to_float(compte.get("resultat")),
            "cash_eur": _to_float(compte.get("tresorerie")),
            "debt_eur": _to_float(compte.get("dettes_financieres")),
        }

        # Ne garder que les snapshots qui ont au moins une valeur financière
        if any(v is not None for k, v in snap.items() if k != "year"):
            snapshots.append(snap)

    if not snapshots:
        return 0

    async with factory() as session:
        for snap in snapshots:
            stmt = sqlite_insert(FinancialHistory).values(
                company_id=company_id,
                year=snap["year"],
                revenue_eur=snap.get("revenue_eur"),
                operating_income_eur=snap.get("operating_income_eur"),
                net_income_eur=snap.get("net_income_eur"),
                cash_eur=snap.get("cash_eur"),
                debt_eur=snap.get("debt_eur"),
                source="pappers_mcp",
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["company_id", "year"],
                set_=dict(
                    revenue_eur=stmt.excluded.revenue_eur,
                    operating_income_eur=stmt.excluded.operating_income_eur,
                    net_income_eur=stmt.excluded.net_income_eur,
                    cash_eur=stmt.excluded.cash_eur,
                    debt_eur=stmt.excluded.debt_eur,
                    source=stmt.excluded.source,
                ),
            )
            await session.execute(stmt)
        await session.commit()

    logger.info(f"[FR_FIN] {company_name} ({siren}): {len(snapshots)} années upserted")
    return len(snapshots)


async def enrich_fr_financials(
    db_path: str,
    pappers_api_key: str,
    limit: int = 0,  # 0 = all
    concurrency: int = 5,
):
    """
    Enrichit l'historique financier des entreprises françaises via MCP Pappers comptes-entreprise.

    Args:
        db_path: Chemin vers la base SQLite
        pappers_api_key: Clé API Pappers
        limit: Limiter le nombre d'entreprises (0 = toutes)
        concurrency: Requêtes simultanées (défaut 5)

    Returns:
        Tuple (total_companies_enriched, total_snapshots_inserted)
    """
    factory = get_session_factory(db_path)
    sem = asyncio.Semaphore(concurrency)

    # Récupérer les entreprises FR avec SIREN, en excluant celles déjà enrichies via pappers_mcp
    async with factory() as session:
        # Sous-requête: IDs des entreprises ayant déjà des données pappers_mcp
        already_enriched_subq = (
            select(FinancialHistory.company_id)
            .where(FinancialHistory.source == "pappers_mcp")
            .distinct()
            .scalar_subquery()
        )

        result = await session.execute(
            select(Company.id, Company.registration_number, Company.name)
            .where(
                Company.country == "FR",
                Company.registration_number.isnot(None),
                Company.id.not_in(already_enriched_subq),
            )
            .order_by(Company.id)
        )
        companies = result.all()

    if limit:
        companies = companies[:limit]

    logger.info(f"[FR_FIN] {len(companies)} entreprises FR à enrichir (financials via Pappers MCP)")

    total_companies_enriched = 0
    total_snapshots_inserted = 0
    total_processed = 0
    chunk_size = 200

    async with httpx.AsyncClient() as client:
        for i in range(0, len(companies), chunk_size):
            chunk = companies[i : i + chunk_size]
            tasks = [
                _process_one(
                    factory, client, pappers_api_key,
                    c.id, c.name, c.registration_number, sem
                )
                for c in chunk
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, int) and r > 0:
                    total_companies_enriched += 1
                    total_snapshots_inserted += r
            total_processed += len(chunk)
            logger.info(
                f"[FR_FIN] Progression: {total_processed}/{len(companies)} "
                f"| Entreprises enrichies: {total_companies_enriched} "
                f"| Snapshots insérés: {total_snapshots_inserted}"
            )

    logger.info(
        f"[FR_FIN] Terminé — {total_companies_enriched} entreprises enrichies, "
        f"{total_snapshots_inserted} snapshots financiers upsertés"
    )
    return total_companies_enriched, total_snapshots_inserted
