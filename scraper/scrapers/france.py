"""France scraper via Pappers API v2.
Pro subscription: https://www.pappers.fr/api
Pro tier: full access to annee_de_naissance, date_prise_de_poste, capital_social, etc.
"""
import asyncio
import logging
from typing import AsyncIterator
import httpx
from .base import BaseScraper
from ..pipeline.normalizer import CompanyRecord, DirectorRecord
from ..enrichers.nace import normalize_code, code_to_sector_label
from ..utils.http_client import safe_get

logger = logging.getLogger(__name__)

BASE_URL = "https://api.pappers.fr/v2"
PAGE_SIZE = 100

# All fields available with Pro subscription
PRO_FIELDS = (
    "siren,nom_entreprise,"
    "chiffre_affaires,chiffre_affaires_annee,"
    "variation_chiffre_affaires,"
    "effectif,effectif_min,effectif_max,"
    "code_naf,libelle_code_naf,"
    "date_creation,siege,"
    "dirigeants,"
    "objet_social,"
    "capital,devise_capital,"
    "statut_rcs,forme_juridique"
)


class FranceScraper(BaseScraper):
    name = "pappers_fr"
    country = "FR"

    async def run(self, resume: bool = True) -> AsyncIterator[CompanyRecord]:
        api_key = self.config.get("PAPPERS_API_KEY")
        if not api_key:
            logger.warning("No PAPPERS_API_KEY set — skipping France scraper")
            return

        checkpoint = self.load_checkpoint() if resume else {}
        cursor = checkpoint.get("cursor", "*")
        total_done = checkpoint.get("total_done", 0)
        min_revenue = self.config.get("MIN_REVENUE_EUR", 75_000_000)

        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {
                    "api_token": api_key,
                    "chiffre_affaires_min": int(min_revenue),
                    "par_page": PAGE_SIZE,
                    "curseur": cursor,
                    "_fields": PRO_FIELDS,
                }
                data = await safe_get(client, f"{BASE_URL}/recherche", params=params)
                if not data:
                    break

                results = data.get("resultats", [])
                if not results:
                    logger.info(f"[FR] No more results — {total_done} companies total")
                    break

                for raw in results:
                    record = self._parse(raw)
                    if record:
                        yield record

                total_done += len(results)
                next_cursor = data.get("curseurSuivant")
                logger.info(f"[FR] {total_done}/{data.get('total','?')} — cursor: {str(next_cursor)[:20]}")

                if not next_cursor:
                    break

                cursor = next_cursor
                self.save_checkpoint({"cursor": cursor, "total_done": total_done})
                await asyncio.sleep(0.2)   # Pro = plus de crédits, on peut aller plus vite

    @staticmethod
    def _parse_date(raw_date) -> str | None:
        """Normalize date strings from Pappers (YYYY, YYYY-MM, YYYY-MM-DD)."""
        if not raw_date:
            return None
        try:
            parts = str(raw_date).split("-")
            if len(parts) == 1:
                return f"{parts[0]}-01-01"
            elif len(parts) == 2:
                return f"{parts[0]}-{parts[1]}-01"
            return str(raw_date)[:10]
        except Exception:
            return None

    def _parse(self, raw: dict) -> CompanyRecord | None:
        try:
            siege = raw.get("siege", {}) or {}
            directors = []

            for d in raw.get("dirigeants", []) or []:
                # Nom : personne physique ou morale
                name_parts = [d.get("prenom", ""), d.get("nom", "")]
                name = " ".join(p for p in name_parts if p).strip()
                if not name:
                    name = d.get("denomination", "")
                if not name:
                    continue

                # Année de naissance (Pro: champ disponible)
                birth_year = None
                raw_by = d.get("annee_de_naissance") or d.get("date_de_naissance")
                if raw_by:
                    try:
                        birth_year = int(str(raw_by)[:4])
                    except Exception:
                        pass

                # Date de prise de poste (Pro: champ disponible)
                appointed_at = self._parse_date(
                    d.get("date_prise_de_poste") or d.get("date_nomination")
                )

                directors.append(DirectorRecord(
                    name=name,
                    role=d.get("qualite"),
                    birth_year=birth_year,
                    appointed_at=appointed_at,
                    nationality=d.get("nationalite"),
                ))

            nace = normalize_code(raw.get("code_naf"))

            # Effectif — Pro retourne effectif direct en plus de min/max
            employees = None
            if raw.get("effectif"):
                try:
                    employees = int(raw["effectif"])
                except Exception:
                    pass
            if not employees:
                emin = raw.get("effectif_min")
                emax = raw.get("effectif_max")
                if emin and emax:
                    employees = (int(emin) + int(emax)) // 2
                elif emin:
                    employees = int(emin)

            siren = raw.get("siren", "")
            revenue = raw.get("chiffre_affaires")

            return CompanyRecord(
                name=raw.get("nom_entreprise", ""),
                country="FR",
                registration_number=siren,
                revenue_eur=float(revenue) if revenue else None,
                revenue_year=raw.get("chiffre_affaires_annee"),
                employees=employees,
                sector=code_to_sector_label(nace) or raw.get("libelle_code_naf"),
                nace_code=nace,
                activity_description=raw.get("objet_social"),
                creation_date=raw.get("date_creation"),
                address=siege.get("adresse_ligne_1"),
                city=siege.get("ville"),
                postal_code=siege.get("code_postal"),
                source_url=f"https://www.pappers.fr/entreprise/{siren}" if siren else None,
                directors=directors,
            )
        except Exception as e:
            logger.error(f"[FR] Parse error: {e} — {raw.get('siren')}")
            return None
