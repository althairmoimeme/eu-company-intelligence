"""Collecte multi-source des données dirigeant pour Founder Intelligence.

Sources (dans l'ordre) :
1. DB existante    → age, mandat, secteur, CA
2. Pappers MCP     → qualité du rôle (fondateur, PPE…) — FR only
3. DuckDuckGo      → LinkedIn public, articles, interviews
4. Site corporate  → page about/management/team
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db.session import get_session_factory
from ..db.models import Company, Director, FounderIntelligence
from api.lib.founder_interpreter import interpret_founder

logger = logging.getLogger(__name__)

MCP_BASE = "https://mcp.pappers.fr"
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

# ── Pappers MCP ───────────────────────────────────────────────────────────────
async def _pappers_get_director_info(client: httpx.AsyncClient, api_key: str, siren: str) -> dict:
    """Récupère les infos dirigeant depuis Pappers MCP (FR only)."""
    if not api_key or not siren:
        return {}
    payload = {
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": "informations-entreprise", "arguments": {"siren": siren}},
    }
    try:
        resp = await client.post(
            f"{MCP_BASE}/{api_key}", headers=MCP_HEADERS, json=payload, timeout=15
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        content = data.get("result", {}).get("content", [])
        for block in content:
            if block.get("type") == "text":
                raw = json.loads(block["text"])
                reps = raw.get("representants", [])
                if reps:
                    main = reps[0]
                    return {
                        "pappers_role": main.get("qualite", ""),
                        "pappers_ppe": bool(main.get("personne_politiquement_exposee")),
                        "pappers_date_poste": main.get("date_prise_de_poste"),
                        "pappers_nom": main.get("nom", ""),
                        "pappers_prenom": main.get("prenom", ""),
                    }
    except Exception as e:
        logger.debug(f"[FOUNDER] Pappers MCP error: {e}")
    return {}


# ── DuckDuckGo search ─────────────────────────────────────────────────────────
def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Recherche DuckDuckGo sans clé API."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        logger.debug(f"[FOUNDER] DDG search error: {e}")
        return []


def _extract_linkedin_url(results: list[dict]) -> Optional[str]:
    """Extrait un profil LinkedIn depuis les résultats DDG."""
    for r in results:
        url = r.get("href", "")
        if "linkedin.com/in/" in url:
            # Nettoyer l'URL
            m = re.search(r"(https?://[a-z]+\.linkedin\.com/in/[^/?&]+)", url)
            if m:
                return m.group(1)
    return None


def _extract_articles_info(results: list[dict]) -> dict:
    """Extrait le nombre d'articles et les titres récents."""
    titles = [r.get("title", "") for r in results if r.get("title")]
    return {
        "article_count": len(results),
        "article_titles": titles[:3],
    }


async def _search_founder_web(
    director_name: str,
    company_name: str,
    country: str,
) -> dict:
    """Cherche LinkedIn + articles publics sur le dirigeant."""
    collected = {}

    # LinkedIn search
    linkedin_query = f'"{director_name}" "{company_name}" site:linkedin.com'
    linkedin_results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _ddg_search(linkedin_query, max_results=3)
    )
    linkedin_url = _extract_linkedin_url(linkedin_results)
    if linkedin_url:
        collected["linkedin_url"] = linkedin_url

    # Articles / interviews
    article_query = f'"{director_name}" {company_name} dirigeant interview cession'
    if country not in ("FR",):
        article_query = f'"{director_name}" {company_name} CEO founder interview'

    article_results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _ddg_search(article_query, max_results=5)
    )
    collected.update(_extract_articles_info(article_results))

    return collected


# ── Corporate website ─────────────────────────────────────────────────────────
async def _scrape_corporate_site(client: httpx.AsyncClient, website: str) -> dict:
    """Scrape la page about/team/management du site officiel."""
    if not website:
        return {}

    # Normaliser l'URL
    if not website.startswith("http"):
        website = "https://" + website

    # Pages à tester
    paths = ["/about", "/about-us", "/management", "/team", "/leadership",
             "/qui-sommes-nous", "/direction", "/equipe", "/dirigeants"]

    for path in paths:
        try:
            url = website.rstrip("/") + path
            resp = await client.get(url, timeout=8, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text(separator=" ", strip=True)[:3000]

                # Chercher email
                email_m = re.search(
                    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text
                )
                # Chercher téléphone (FR format)
                phone_m = re.search(
                    r'(?:0|\+33|00\s*33)\s*[1-9](?:[\s.\-]?\d{2}){4}', text
                )

                result = {}
                if email_m:
                    email = email_m.group()
                    # Filtrer les emails génériques
                    if not any(
                        x in email.lower()
                        for x in ["noreply", "no-reply", "contact@", "info@", "support@"]
                    ):
                        result["email"] = email
                if phone_m:
                    result["phone"] = phone_m.group().strip()

                if result:
                    return result
        except Exception:
            continue

    return {}


# ── Pipeline principal ────────────────────────────────────────────────────────
async def enrich_founder(
    company_id: int,
    db_path: str,
    pappers_api_key: str = "",
) -> bool:
    """
    Enrichit la table founder_intelligence pour une entreprise.
    Retourne True si succès, False sinon.
    """
    factory = get_session_factory(db_path)

    # 1. Charger données DB
    async with factory() as session:
        co_result = await session.execute(
            select(Company).where(Company.id == company_id)
        )
        company = co_result.scalar_one_or_none()
        if not company:
            return False

        dir_result = await session.execute(
            select(Director).where(Director.company_id == company_id)
        )
        directors = dir_result.scalars().all()

    if not directors:
        logger.info(f"[FOUNDER] No directors for company {company_id}")

    # Sérialiser pour l'interpréteur
    company_dict = {
        "id": company.id,
        "name": company.name,
        "country": company.country,
        "creation_date": company.creation_date,
        "revenue_eur": company.revenue_eur,
        "sector": company.sector,
        "website": company.website,
        "registration_number": company.registration_number,
    }

    directors_list = [
        {
            "name": d.name,
            "role": d.role,
            "birth_year": d.birth_year,
            "appointed_at": d.appointed_at.isoformat() if d.appointed_at else None,
            "tenure_years": (
                (__import__("datetime").date.today() - d.appointed_at).days // 365
                if d.appointed_at else None
            ),
            "nationality": d.nationality,
        }
        for d in directors
    ]

    # Choisir le dirigeant principal (même logique que l'interpréteur)
    def score_dir(d):
        s = 0
        if d.get("birth_year"): s += (2026 - d["birth_year"])
        if d.get("tenure_years"): s += d["tenure_years"] * 2
        role = (d.get("role") or "").lower()
        if any(k in role for k in ["président", "pdg", "gérant", "dg", "ceo", "fondateur"]):
            s += 50
        return s

    main_dir = max(directors_list, key=score_dir) if directors_list else {}

    sources_used = ["db"]
    web_data = {}

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        timeout=12,
    ) as client:

        # 2. Pappers MCP (FR)
        pappers_data = {}
        if company.country == "FR" and company.registration_number and pappers_api_key:
            pappers_data = await _pappers_get_director_info(
                client, pappers_api_key, company.registration_number
            )
            if pappers_data:
                sources_used.append("pappers_mcp")
                # Enrichir le rôle depuis Pappers
                if pappers_data.get("pappers_role") and directors_list:
                    directors_list[0]["role"] = pappers_data["pappers_role"]

        # 3. DuckDuckGo (LinkedIn + articles)
        if main_dir.get("name"):
            try:
                ddg_data = await _search_founder_web(
                    director_name=main_dir["name"],
                    company_name=company.name,
                    country=company.country,
                )
                web_data.update(ddg_data)
                if ddg_data:
                    sources_used.append("duckduckgo")
            except Exception as e:
                logger.debug(f"[FOUNDER] DDG error: {e}")

        # 4. Site corporate
        if company.website:
            try:
                site_data = await _scrape_corporate_site(client, company.website)
                web_data.update(site_data)
                if site_data:
                    sources_used.append("corporate_site")
            except Exception as e:
                logger.debug(f"[FOUNDER] Corporate site error: {e}")

    # 5. Récupérer les signaux financiers existants
    from api.lib.financial_signals import compute_financial_signals
    financial_signals = []
    try:
        from scraper.db.session import get_session_factory as gsf
        fac2 = gsf(db_path)
        from scraper.db.models import FinancialHistory
        async with fac2() as session2:
            fh_result = await session2.execute(
                select(FinancialHistory)
                .where(FinancialHistory.company_id == company_id)
                .order_by(FinancialHistory.year)
            )
            fh_rows = fh_result.scalars().all()
        if fh_rows:
            snaps = [
                {"year": r.year, "revenue_eur": r.revenue_eur,
                 "operating_income_eur": r.operating_income_eur,
                 "net_income_eur": r.net_income_eur}
                for r in fh_rows
            ]
            _, financial_signals, _, _ = compute_financial_signals(snaps)
    except Exception as e:
        logger.debug(f"[FOUNDER] Financial signals error: {e}")

    # Récupérer le score M&A approximatif
    from api.routers.companies import _compute_ma
    class FakeCompany:
        def __init__(self):
            self.directors = directors
            self.creation_date = company.creation_date
    fake = FakeCompany()
    fake.__dict__["broker_listings"] = None
    try:
        ma_score, ma_signals = _compute_ma(fake)
        financial_signals = list(set(financial_signals + ma_signals))
    except Exception:
        ma_score = 0

    # 6. Interpréter
    profile = interpret_founder(
        company=company_dict,
        directors=directors_list,
        financial_signals=financial_signals,
        ma_score=ma_score,
        web_data=web_data,
    )

    # 7. Sauvegarder
    sources_snapshot = json.dumps({
        "sources": sources_used,
        "web_data_keys": list(web_data.keys()),
        "pappers_data": pappers_data,
        "article_titles": web_data.get("article_titles", []),
        "enriched_at": datetime.utcnow().isoformat(),
    })

    async with factory() as session:
        stmt = sqlite_insert(FounderIntelligence).values(
            company_id=company_id,
            full_name=profile.full_name,
            current_role=profile.current_role,
            estimated_age=profile.estimated_age,
            founder_status=profile.founder_status,
            years_in_role=profile.years_in_role,
            children_signal=profile.children_signal,
            children_in_business=profile.children_in_business,
            successor_signal=profile.successor_signal,
            operator_type=profile.operator_type,
            public_visibility=profile.public_visibility,
            relationship_to_company=profile.relationship_to_company,
            main_why_now_hypothesis=profile.main_why_now_hypothesis,
            seller_signal_strength=profile.seller_signal_strength,
            seller_signal_reason=profile.seller_signal_reason,
            recommended_approach_angle=profile.recommended_approach_angle,
            avoid_in_outreach=profile.avoid_in_outreach,
            approach_hooks=json.dumps(profile.approach_hooks),
            professional_email=profile.professional_email,
            phone=profile.phone,
            linkedin_url=profile.linkedin_url,
            confidence_score=profile.confidence_score,
            sources_snapshot=sources_snapshot,
            enrichment_status="done",
            last_enriched_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id"],
            set_=dict(
                full_name=stmt.excluded.full_name,
                current_role=stmt.excluded.current_role,
                estimated_age=stmt.excluded.estimated_age,
                founder_status=stmt.excluded.founder_status,
                years_in_role=stmt.excluded.years_in_role,
                children_signal=stmt.excluded.children_signal,
                children_in_business=stmt.excluded.children_in_business,
                successor_signal=stmt.excluded.successor_signal,
                operator_type=stmt.excluded.operator_type,
                public_visibility=stmt.excluded.public_visibility,
                relationship_to_company=stmt.excluded.relationship_to_company,
                main_why_now_hypothesis=stmt.excluded.main_why_now_hypothesis,
                seller_signal_strength=stmt.excluded.seller_signal_strength,
                seller_signal_reason=stmt.excluded.seller_signal_reason,
                recommended_approach_angle=stmt.excluded.recommended_approach_angle,
                avoid_in_outreach=stmt.excluded.avoid_in_outreach,
                approach_hooks=stmt.excluded.approach_hooks,
                professional_email=stmt.excluded.professional_email,
                phone=stmt.excluded.phone,
                linkedin_url=stmt.excluded.linkedin_url,
                confidence_score=stmt.excluded.confidence_score,
                sources_snapshot=stmt.excluded.sources_snapshot,
                enrichment_status=stmt.excluded.enrichment_status,
                last_enriched_at=stmt.excluded.last_enriched_at,
            ),
        )
        await session.execute(stmt)
        await session.commit()

    logger.info(
        f"[FOUNDER] {company.name} ({company.country}) enrichi — "
        f"signal={profile.seller_signal_strength} conf={profile.confidence_score}% "
        f"sources={sources_used}"
    )
    return True


async def batch_enrich_founders(
    db_path: str,
    pappers_api_key: str = "",
    limit: int = 50,
    min_ma_score: int = 30,
    concurrency: int = 3,
):
    """Enrichit en batch les sociétés avec le meilleur score M&A."""
    from scraper.db.models import FounderIntelligence as FI
    factory = get_session_factory(db_path)

    # Prendre les sociétés déjà scorées avec dirigeants, sans FI existante
    async with factory() as session:
        already = await session.execute(select(FI.company_id))
        done_ids = {r[0] for r in already.fetchall()}

        result = await session.execute(
            select(Company.id)
            .where(Company.id.notin_(done_ids))
            .order_by(Company.revenue_eur.desc().nulls_last())
            .limit(limit * 3)
        )
        candidates = [r[0] for r in result.fetchall()]

    sem = asyncio.Semaphore(concurrency)
    enriched = 0

    async def _one(cid):
        nonlocal enriched
        async with sem:
            ok = await enrich_founder(cid, db_path, pappers_api_key)
            if ok:
                enriched += 1

    tasks = [_one(cid) for cid in candidates[:limit]]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"[FOUNDER] Batch terminé — {enriched}/{len(tasks)} enrichis")
    return enriched
