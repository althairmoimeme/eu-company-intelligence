"""Denmark scraper via cvrapi.dk (no auth) + Virk CVR Elasticsearch (optional).
Simple endpoint: https://cvrapi.dk/api — no revenue, but fast.
Full CVR: requires credentials from Danish Business Authority (free, email request).
"""
import asyncio
import logging
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label
from ..enrichers.currency import to_eur
from ..utils.http_client import safe_get, safe_post

logger = logging.getLogger(__name__)

CVR_ES_URL = "http://distribution.virk.dk/cvr-permanent/virksomhed/_search"
CVRAPI_URL = "https://cvrapi.dk/api"
PAGE_SIZE = 100


class DenmarkScraper(BaseScraper):
    name = "cvr_dk"
    country = "DK"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        cvr_user = self.config.get("CVR_USERNAME")
        cvr_pass = self.config.get("CVR_PASSWORD")

        if cvr_user and cvr_pass:
            async for record in self._run_elasticsearch(cvr_user, cvr_pass, resume):
                yield record
        else:
            logger.info("[DK] No CVR credentials — using public cvrapi.dk (limited data)")
            async for record in self._run_cvrapi(resume):
                yield record

    async def _run_elasticsearch(self, username: str, password: str,
                                   resume: bool) -> AsyncIterator[CompanyRecord]:
        """Full CVR via official Elasticsearch API."""
        checkpoint = self.load_checkpoint() if resume else {}
        start_from = checkpoint.get("from", 0)
        min_employees = self.config.get("MIN_EMPLOYEES_PROXY", 200)

        auth = httpx.BasicAuth(username, password)
        async with httpx.AsyncClient(auth=auth, timeout=30) as client:
            offset = start_from
            while True:
                query = {
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"Vrvirksomhed.status": "NORMAL"}},
                                {"range": {
                                    "Vrvirksomhed.virksomhedMetadata.nyesteKvartalsbeskaeftigelse.antalAarsvaerk":
                                        {"gte": min_employees}
                                }},
                            ]
                        }
                    },
                    "_source": [
                        "Vrvirksomhed.cvrNummer",
                        "Vrvirksomhed.virksomhedMetadata.nyesteNavn",
                        "Vrvirksomhed.virksomhedMetadata.nyesteBranche",
                        "Vrvirksomhed.virksomhedMetadata.nyesteKvartalsbeskaeftigelse",
                        "Vrvirksomhed.virksomhedMetadata.stiftelsesDato",
                        "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse",
                        "Vrvirksomhed.deltagerRelation",
                    ],
                    "size": PAGE_SIZE,
                    "from": offset,
                }
                data = await safe_post(client, CVR_ES_URL, json=query)
                if not data:
                    break

                hits = data.get("hits", {}).get("hits", [])
                if not hits:
                    break

                for hit in hits:
                    record = self._parse_es(hit.get("_source", {}).get("Vrvirksomhed", {}))
                    if record:
                        yield record

                self.save_checkpoint({"from": offset + PAGE_SIZE})
                logger.info(f"[DK] ES offset {offset} — {len(hits)} companies")

                if len(hits) < PAGE_SIZE:
                    break
                offset += PAGE_SIZE
                await asyncio.sleep(0.5)

    def _parse_es(self, v: dict) -> CompanyRecord | None:
        try:
            meta = v.get("virksomhedMetadata", {}) or {}
            navn_obj = meta.get("nyesteNavn", {}) or {}
            branche = meta.get("nyesteBranche", {}) or {}
            beskaeftigelse = meta.get("nyesteKvartalsbeskaeftigelse", {}) or {}
            adresse = meta.get("nyesteBeliggenhedsadresse", {}) or {}

            cvr_nr = str(v.get("cvrNummer", ""))
            nace_raw = branche.get("branchekode")
            nace = normalize_code(nace_raw)

            directors = []
            for relation in v.get("deltagerRelation", []) or []:
                deltager = relation.get("deltager", {}) or {}
                person = deltager.get("navne", [{}])[0] if deltager.get("navne") else {}
                name = f"{person.get('forNavn', '')} {person.get('efterNavn', '')}".strip()
                if not name:
                    continue
                birth = deltager.get("foedselsDato", "")
                birth_year = int(birth[:4]) if birth and len(birth) >= 4 else None
                for org in relation.get("organisationer", []):
                    for member in org.get("medlemsData", []):
                        attrs = member.get("attributter", [])
                        role = next(
                            (a.get("vaerdier", [{}])[0].get("vaerdi", "")
                             for a in attrs if a.get("type") == "FUNKTION"), ""
                        )
                        directors.append(DirectorRecord(
                            name=name, role=role, birth_year=birth_year
                        ))

            addr_parts = [
                adresse.get("vejnavn", ""),
                str(adresse.get("husnummerFra", "")),
                adresse.get("etage", ""),
            ]
            address = " ".join(p for p in addr_parts if p).strip() or None

            return CompanyRecord(
                name=navn_obj.get("navn", ""),
                country="DK",
                registration_number=cvr_nr,
                revenue_eur=None,
                revenue_estimated=True,
                employees=beskaeftigelse.get("antalAarsvaerk"),
                sector=code_to_sector_label(nace) or branche.get("branchetekst"),
                nace_code=nace,
                creation_date=meta.get("stiftelsesDato"),
                address=address,
                city=adresse.get("postdistrikt"),
                postal_code=str(adresse.get("postnummer", "")) or None,
                source_url=f"https://datacvr.virk.dk/enhed/virksomhed/{cvr_nr}",
                directors=directors,
            )
        except Exception as e:
            logger.error(f"[DK] ES parse error: {e}")
            return None

    async def _run_cvrapi(self, resume: bool) -> AsyncIterator[CompanyRecord]:
        """Fallback: search known large Danish companies by CVR number ranges.
        cvrapi.dk is for individual lookups, not bulk — this is a limited approach.
        """
        logger.warning("[DK] cvrapi.dk fallback is for enrichment only, not bulk scraping.")
        logger.warning("[DK] Request CVR credentials at: https://data.virk.dk/datahenter/selvbetjening/")
        return
        yield  # make it a generator
