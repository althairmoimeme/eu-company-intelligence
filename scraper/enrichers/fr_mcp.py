"""Enrichissement des entreprises françaises via MCP Pappers.

Source: https://mcp.pappers.fr/{api_key}
Données: representants avec date_prise_de_poste → appointed_at des directeurs.
Utilise le protocole MCP (JSON-RPC over HTTP/SSE).
Free & unlimited pendant 2 semaines, puis sur crédits.
"""
import asyncio
import json
import logging
from datetime import date

import httpx
from sqlalchemy import select, update

from ..db.session import get_session_factory
from ..db.models import Company, Director

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
            timeout=20,
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
        logger.debug(f"[FR-MCP] Erreur outil {tool}: {e}")
        return None


async def _process_one(
    factory,
    client: httpx.AsyncClient,
    api_key: str,
    company_id: int,
    siren: str,
    sem: asyncio.Semaphore,
) -> int:
    """Enrichit appointed_at des dirigeants d'une entreprise. Retourne nb mis à jour."""
    async with sem:
        data = await _call_mcp(client, api_key, "informations-entreprise", {"siren": siren})
        if not data:
            return 0

        representants = data.get("representants", [])
        if not representants:
            return 0

        updated = 0
        async with factory() as session:
            # Récupérer les dirigeants existants de cette entreprise
            result = await session.execute(
                select(Director).where(Director.company_id == company_id)
            )
            directors = result.scalars().all()
            if not directors:
                return 0

            for rep in representants:
                nom = (rep.get("nom") or "").strip().upper()
                prenom = (rep.get("prenom") or "").strip()
                date_poste = rep.get("date_prise_de_poste")

                if not nom or not date_poste:
                    continue

                # Trouver le directeur correspondant dans la DB
                for d in directors:
                    d_name = (d.name or "").upper()
                    if nom in d_name or d_name in nom:
                        if d.appointed_at is None:
                            try:
                                parsed = date.fromisoformat(date_poste)
                                await session.execute(
                                    update(Director)
                                    .where(Director.id == d.id)
                                    .values(appointed_at=parsed)
                                )
                                updated += 1
                            except Exception:
                                pass
                        break

            if updated:
                await session.commit()

        return updated


async def enrich_fr_tenure(db_path: str, api_key: str, limit: int | None = None, concurrency: int = 5):
    """
    Enrichit appointed_at (date_prise_de_poste) des dirigeants FR via MCP Pappers.

    Args:
        db_path: Chemin vers la base SQLite
        api_key: Clé API Pappers (même que REST)
        limit: Limiter le nombre d'entreprises (None = toutes)
        concurrency: Requêtes simultanées (défaut 5 pour ne pas surcharger)
    """
    factory = get_session_factory(db_path)
    sem = asyncio.Semaphore(concurrency)

    # Récupérer les entreprises FR avec SIREN et des dirigeants sans appointed_at
    async with factory() as session:
        # Sous-requête : companies FR avec au moins 1 directeur sans appointed_at
        result = await session.execute(
            select(Company.id, Company.registration_number, Company.name)
            .where(
                Company.country == "FR",
                Company.registration_number.isnot(None),
            )
            .order_by(Company.id)
        )
        companies = result.all()

    if limit:
        companies = companies[:limit]

    logger.info(f"[FR-MCP] {len(companies)} entreprises FR à enrichir (tenure)")

    total_updated = 0
    total_processed = 0
    chunk_size = 200

    async with httpx.AsyncClient() as client:
        for i in range(0, len(companies), chunk_size):
            chunk = companies[i : i + chunk_size]
            tasks = [
                _process_one(factory, client, api_key, c.id, c.registration_number, sem)
                for c in chunk
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, int):
                    total_updated += r
            total_processed += len(chunk)
            logger.info(
                f"[FR-MCP] Progression: {total_processed}/{len(companies)} "
                f"| Mandats enrichis: {total_updated}"
            )

    logger.info(f"[FR-MCP] Terminé — {total_updated} mandats avec date_prise_de_poste")
    return total_updated
