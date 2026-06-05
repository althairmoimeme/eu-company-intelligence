"""Seller signal scrapers — finds companies announced for sale/transfer.

Sources:
1. BODACC (France) — official government bulletin of civil & commercial notices
   870K+ cession records, fully searchable via open API.
   Endpoint: https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/

2. BORME (Spain) — Boletín Oficial del Registro Mercantil
   Official Spanish commercial registry notices.

Results are stored in broker_listings and matched against companies DB via SIREN
(exact match for FR) or fuzzy name matching for others.
"""
import asyncio
import logging
import re
import unicodedata
from datetime import datetime, date, timedelta

import httpx
from sqlalchemy import select, update

from ..db.session import get_session_factory
from ..db.models import BrokerListing, Company

logger = logging.getLogger(__name__)

BODACC_API = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"


# ── Name normalisation ────────────────────────────────────────────────────────

LEGAL_FORMS = re.compile(
    r"\b(sas|sarl|sa|sca|scop|eurl|snc|sci|se|nv|bv|gmbh|ltd|plc|"
    r"spa|srl|ab|as|oy|sl|sa de cv|inc|corp|llc|ag)\b",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    name = LEGAL_FORMS.sub("", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def extract_siren(registre) -> str | None:
    """Extract a clean 9-digit SIREN from BODACC registre field."""
    if not registre:
        return None
    if isinstance(registre, list):
        for item in registre:
            clean = re.sub(r"\s", "", str(item))
            if re.match(r"^\d{9}$", clean):
                return clean
        return None
    clean = re.sub(r"\s", "", str(registre))
    if re.match(r"^\d{9}$", clean):
        return clean
    # Try to extract 9-digit sequence
    match = re.search(r"\b(\d{9})\b", clean)
    return match.group(1) if match else None


# ── BODACC scraper ────────────────────────────────────────────────────────────

async def scrape_bodacc_cessions(
    client: httpx.AsyncClient,
    days_back: int = 365 * 3,
    max_records: int = 10000,
) -> list[dict]:
    """Fetch company cession notices from BODACC open data API."""
    listings = []
    since_date = (date.today() - timedelta(days=days_back)).isoformat()
    page_size = 100
    offset = 0

    logger.info(f"[BODACC] Fetching cessions since {since_date}...")

    while offset < max_records:
        params = {
            "limit": page_size,
            "offset": offset,
            "where": f"familleavis_lib='Ventes et cessions' AND dateparution>='{since_date}'",
            "order_by": "dateparution desc",
            "select": "commercant,registre,ville,cp,numerodepartement,dateparution,url_complete",
        }
        try:
            resp = await client.get(BODACC_API, params=params, timeout=20)
            if resp.status_code != 200:
                logger.warning(f"[BODACC] HTTP {resp.status_code} at offset {offset}")
                break

            data = resp.json()
            total = data.get("total_count", 0)
            results = data.get("results", [])
            if not results:
                break

            for rec in results:
                name = (rec.get("commercant") or "").strip()
                if not name or len(name) < 3:
                    continue

                siren_raw = rec.get("registre")
                siren = extract_siren(siren_raw)
                ville = rec.get("ville", "")
                dept = rec.get("numerodepartement", "")
                parution = rec.get("dateparution", "")
                url = rec.get("url_complete", "")

                listings.append({
                    "source": "BODACC",
                    "listing_name": name[:200],
                    "listing_url": url,
                    "sector_hint": "",
                    "region_hint": f"{ville} ({dept})" if ville else dept,
                    "price_hint": parution,  # reuse field for date
                    "siren": siren,  # extra field for exact matching
                })

            fetched = offset + len(results)
            logger.info(f"[BODACC] {fetched}/{min(total, max_records)} cessions fetched")

            if len(results) < page_size or fetched >= min(total, max_records):
                break

            offset += page_size
            await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"[BODACC] Error at offset {offset}: {e}")
            break

    logger.info(f"[BODACC] Done — {len(listings)} cessions collected")
    return listings


# ── Fuzzy matching ────────────────────────────────────────────────────────────

async def match_listings_to_companies(db_path: str, threshold: float = 80.0) -> int:
    """Match broker listings to companies using SIREN (exact) or fuzzy name."""
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        logger.error("[BROKER] rapidfuzz not installed — pip install rapidfuzz")
        return 0

    factory = get_session_factory(db_path)

    async with factory() as session:
        # Load unmatched listings
        result = await session.execute(
            select(BrokerListing).where(BrokerListing.matched_company_id.is_(None))
        )
        listings = result.scalars().all()

        # Load all FR companies with SIREN
        result2 = await session.execute(
            select(Company.id, Company.name, Company.registration_number, Company.country)
        )
        companies = result2.all()

    if not listings:
        logger.info("[BROKER] No unmatched listings")
        return 0

    # Build index: siren -> company_id and normalized_name -> company_id
    siren_index: dict[str, int] = {}
    name_index: list[tuple[str, int]] = []

    for c in companies:
        if c.registration_number:
            siren_index[c.registration_number] = c.id
        name_index.append((normalize_name(c.name), c.id))

    choices = [n[0] for n in name_index]
    matched = 0
    updates = []

    for listing in listings:
        company_id = None
        score = None

        # 1. Try exact SIREN match for BODACC listings
        # We stored siren in listing_url field temporarily (handled below)
        # Instead look for SIREN in listing_name pattern or source
        if listing.source == "BODACC":
            # Try to extract SIREN from listing_url field (we stored it there)
            siren = extract_siren(listing.listing_url)
            if siren and siren in siren_index:
                company_id = siren_index[siren]
                score = 100.0
                logger.debug(f"[BROKER] Exact SIREN match: {siren} → {company_id}")

        # 2. Fuzzy name match
        if company_id is None:
            query = normalize_name(listing.listing_name)
            if query and len(query) >= 4:
                result = process.extractOne(
                    query, choices,
                    scorer=fuzz.token_sort_ratio,
                    score_cutoff=threshold,
                )
                if result:
                    best_name, best_score, idx = result
                    company_id = name_index[idx][1]
                    score = best_score
                    logger.info(
                        f"[BROKER] Fuzzy match ({best_score:.0f}%): "
                        f"'{listing.listing_name}' → ID {company_id}"
                    )

        if company_id is not None:
            updates.append({
                "id": listing.id,
                "matched_company_id": company_id,
                "match_score": score,
            })
            matched += 1

    if updates:
        async with factory() as session:
            async with session.begin():
                for upd in updates:
                    await session.execute(
                        update(BrokerListing)
                        .where(BrokerListing.id == upd["id"])
                        .values(
                            matched_company_id=upd["matched_company_id"],
                            match_score=upd["match_score"],
                        )
                    )

    logger.info(f"[BROKER] Matching done — {matched}/{len(listings)} matched")
    return matched


# ── Main orchestration ────────────────────────────────────────────────────────

async def run_broker_scrape(db_path: str, days_back: int = 365 * 3):
    """Fetch BODACC cession notices and match to DB companies."""
    factory = get_session_factory(db_path)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        listings = await scrape_bodacc_cessions(client, days_back=days_back)

    logger.info(f"[BROKER] Saving {len(listings)} listings to DB...")

    if listings:
        async with factory() as session:
            async with session.begin():
                for lst in listings:
                    # Store SIREN in listing_url for the matcher to use
                    siren = lst.pop("siren", None)
                    session.add(BrokerListing(
                        source=lst["source"],
                        listing_name=lst["listing_name"],
                        listing_url=siren or "",   # SIREN stored here for exact matching
                        sector_hint=lst.get("sector_hint", ""),
                        region_hint=lst.get("region_hint", ""),
                        price_hint=lst.get("price_hint", ""),
                        scraped_at=datetime.utcnow(),
                    ))

    matched = await match_listings_to_companies(db_path)

    return {
        "listings_scraped": len(listings),
        "listings_matched": matched,
        "source": "BODACC",
        "days_back": days_back,
    }
