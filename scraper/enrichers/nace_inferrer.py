"""NACE code inference from SIC codes (GB) and activity description keywords.

Strategy:
  - GB: activity_description contains raw SIC codes (e.g. "43210, 43320").
        UK SIC 2007 maps directly to NACE Rev.2 — strip last digit and add dot.
        43210 → 43.21, 71120 → 71.12, etc.
  - Other countries: keyword matching on activity_description + sector + name.

Result stored in Company.nace_inferred (never overwrites nace_code).
"""
import asyncio
import logging
import re
from datetime import datetime

from sqlalchemy import select, or_

from ..db.session import get_session_factory
from ..db.models import Company

logger = logging.getLogger(__name__)

# ── Status tracking ───────────────────────────────────────────────────────────

_nace_status: dict = {
    "running": False,
    "processed": 0,
    "total": 0,
    "inferred": 0,
    "skipped": 0,
    "countries": [],
    "error": None,
}

def get_nace_status() -> dict:
    return _nace_status.copy()

# ── SIC → NACE conversion (UK) ────────────────────────────────────────────────

def _sic_to_nace(sic_str: str) -> str | None:
    """Convert 5-digit UK SIC code to 4-digit NACE string (e.g. '43210' → '43.21')."""
    sic = re.sub(r"\s+", "", sic_str)[:5]
    if len(sic) < 4 or not sic[:4].isdigit():
        return None
    return f"{sic[:2]}.{sic[2:4]}"


def _best_nace_from_sic_list(raw: str) -> str | None:
    """Parse a comma-separated SIC list, return the most Equans-relevant NACE."""
    EQUANS_PRIORITY = {
        "43.21", "43.22", "43.29", "33.20", "81.10",
        "71.12", "43.99", "35.11", "35.13", "42.22",
        "26.51", "28.11", "28.22", "28.29",
    }
    candidates = re.findall(r"\b\d{4,5}\b", raw or "")
    nace_list = [_sic_to_nace(c) for c in candidates]
    nace_list = [n for n in nace_list if n]

    # Priorité aux codes Equans
    for n in nace_list:
        if n in EQUANS_PRIORITY:
            return n
    # Sinon le premier code valide
    return nace_list[0] if nace_list else None


# ── Keyword → NACE inference for text descriptions ───────────────────────────

# Rules: (nace_code, [keywords to match], minimum_hits)
# Ordered from most specific to most general.
KEYWORD_RULES: list[tuple[str, list[str], int]] = [
    # ── Installation électrique ───────────────────────────────────────────────
    ("43.21", [
        "electrical installation", "electrical contractor", "electrical work",
        "elektroinstallation", "instalacje elektryczne", "installation électrique",
        "impianti elettrici", "electrical engineer", "electrician", "wiring",
        "cable contractor", "power installation", "Elektrotechnik",
        "electrical services", "lighting installation", "EV charging",
        "low voltage", "high voltage", "HV installation",
        # IT — noms d'entreprises courants
        "elettrotecnica", "elettrotecnico", "elettrica", "elettrico",
        "impianti elettrici", "installazioni elettriche",
        # NL
        "elektrotechniek", "elektro installatie", "elektricien",
        # DE
        "elektroinstallation", "elektriker", "elektro gmbh",
        # PL
        "instalacje elektryczne", "elektroinstalacje", "elektrotechnika",
        "elektryczny", "elektryczna", "elektryk", "energetyczny",
        "oświetlenie", "niskie napięcie", "wysokie napięcie",
        "instalator elektryczny", "roboty elektryczne",
        # Short tokens common in PL/DE/AT company names
        " elektro ", "elektro sp", "elektro s.a", "elektro s.c", "elektro s.k",
        "elektro gmbh", "elektro-", "-elektro",
        "elektrotechnik", "elektromont", "elektroenergetyka",
        "elektromontaż", "elektrobudowa", "elektrob", "teletechnika", "niskonapięciow",
        "energetyk sp", "energetyk s.",
    ], 1),
    # ── Plomberie / CVC ───────────────────────────────────────────────────────
    ("43.22", [
        "plumbing", "heating", "air conditioning", "hvac", "cvc",
        "climatisation", "ventilation", "Heizung", "Klimatechnik", "Lüftung",
        "mechanical contractor", "refrigeration", "heat pump",
        "Wärmepumpe", "Kältetechnik", "sanitaire", "Sanitärinstallation",
        "riscaldamento", "climatizzazione", "ogrzewanie", "klimatyzacja",
        # IT
        "termoidraulica", "impianti termici", "impianti climatizzazione",
        "condizionamento", "climatizzatore", "refrigerazione",
        "impianti idrici", "idrotermosanitario", "termosanitaria",
        # NL
        "klimaatinstallatie", "installatiebedrijf", "cv-installatie",
        "luchtbehandeling", "sanitair techniek",
        # AT/DE
        "haustechnik", "sanitärtechnik", "kältetechnik",
        # PL
        "ogrzewanie", "klimatyzacja", "wentylacja", "hydraulika",
        "instalacje sanitarne", "instalacje grzewcze", "instalacje gazowe",
        "c.o.", "pompa ciepła", "chłodnictwo", "kotłownia",
        "roboty sanitarne", "instalacje wod-kan",
        " instal ", "instal-", "-instal",
        "instal sp", "instal s.a", "instal s.c", "instal s.k",
        "instal gmbh", "instalacje budowlane",
        "usługi instalacyjne", "roboty instalacyjne",
    ], 1),
    # ── Autres travaux d'installation ─────────────────────────────────────────
    ("43.29", [
        "fire protection", "fire sprinkler", "fire detection",
        "alarm installation", "security installation", "cctv installation",
        "Brandschutz", "alarme incendie", "détection incendie",
        "burglar alarm", "access control installation", "PA system",
        # IT
        "antincendio", "rilevazione incendi", "sicurezza antincendio",
        "impianti di sicurezza", "videosorveglianza", "antifurto",
        # NL
        "brandbeveiliging", "beveiligingstechniek", "toegangscontrole",
        # AT/DE
        "sicherheitstechnik", "einbruchschutz", "videoüberwachung",
        "gebaeudeautomation", "gebäudeautomation",
        # PL
        "ochrona przeciwpożarowa", "systemy alarmowe", "alarmy",
        "telewizja przemysłowa", "kontrola dostępu", "automatyka budynkowa",
        "sygnalizacja pożaru", "oddymianie",
    ], 1),
    # ── Installation de machines industrielles ────────────────────────────────
    ("33.20", [
        "industrial installation", "plant installation", "machinery installation",
        "equipment installation", "Anlagenmontage", "Maschinenmontage",
        "industrial maintenance", "process equipment", "pipeline installation",
        "Rohrleitungsbau", "piping contractor", "montaż przemysłowy",
        # IT
        "manutenzione industriale", "impianti industriali",
        "installazione macchinari", "automazione industriale",
        # NL
        "industrieel onderhoud", "machinebouw", "installatie industrie",
        # AT/DE
        "anlagenbau", "industrieservice", "maschinenbau",
        # PL
        "montaż przemysłowy", "serwis przemysłowy", "utrzymanie ruchu",
        "konserwacja maszyn", "instalacje przemysłowe", "automatyzacja",
        "roboty montażowe", "maszyny przemysłowe",
    ], 1),
    # ── Facility management / services de bâtiments ───────────────────────────
    ("81.10", [
        "facility management", "facilities management", "building services",
        "Gebäudemanagement", "Hausmeister", "multi-technical services",
        "integrated facilities", "FM services", "building maintenance",
        "property services", "hard services", "soft services",
        "gestione edifici",
        # IT
        "gestione impianti", "global service", "manutenzione edifici",
        "servizi integrati", "property management",
        # NL
        "facilitair management", "gebouwbeheer",
        # AT/DE
        "gebäudeservice", "facility services",
        # PL
        "zarządzanie nieruchomościami", "zarządzanie budynkiem",
        "obsługa techniczna", "eksploatacja budynków",
        "serwis nieruchomości", "obsługa obiektów",
    ], 1),
    # ── Ingénierie et conseil technique ───────────────────────────────────────
    ("71.12", [
        "engineering consultancy", "engineering services", "technical consultancy",
        "bureau d'études", "ingénierie technique", "Ingenieurbüro",
        "ingegneria", "civil engineering", "structural engineering",
        "MEP engineering", "BIM consultant", "technical design",
        "energy consultant", "sustainability consultant",
        # IT
        "ingegneria impiantistica", "progettazione impianti",
        "consulenza tecnica", "ingegneria tecnica",
        # NL
        "technisch advies", "installatietechniek advies",
        # AT/DE
        "gebäudetechnik", "technische beratung",
        # PL
        "inżynieria", "projektowanie instalacji", "projektowanie techniczne",
        "doradztwo techniczne", "nadzór techniczny", "technologia budowlana",
    ], 1),
    # ── Autres travaux de construction spécialisés ────────────────────────────
    ("43.99", [
        "scaffolding", "insulation", "waterproofing", "specialist contractor",
        "construction specialist", "roofing", "cladding installation",
    ], 1),
    # ── Production d'électricité ──────────────────────────────────────────────
    ("35.11", [
        "power generation", "electricity generation", "renewable energy",
        "solar farm", "wind farm", "power plant operator",
        "Stromerzeugung", "production d'électricité",
    ], 1),
    # ── Fabrication de machines industrielles ─────────────────────────────────
    ("28.29", [
        "industrial machinery", "Maschinenbau", "fabrication mécanique",
        "machine manufacturer", "special purpose machines",
    ], 1),
    # ── Instruments de mesure ─────────────────────────────────────────────────
    ("26.51", [
        "measurement equipment", "instruments de mesure", "Messtechnik",
        "instrumentation", "sensors", "process control equipment",
    ], 1),
    # ── Construction de réseaux électriques ──────────────────────────────────
    ("42.22", [
        "overhead line", "transmission line", "electricity network",
        "cable laying", "substation construction", "HV network",
        "network contractor",
    ], 1),
]


def _infer_from_keywords(text: str, name: str = "") -> str | None:
    """Match keyword rules against lowercased text + name."""
    # Strip quotes from company names (e.g. PL KRS names: "ARES ELEKTRO" SP. Z O.O.)
    corpus = (text + " " + name).lower().replace('"', ' ').replace("'", ' ')
    for nace, keywords, min_hits in KEYWORD_RULES:
        hits = sum(1 for kw in keywords if kw.lower() in corpus)
        if hits >= min_hits:
            return nace
    return None


# ── Main enrichment function ──────────────────────────────────────────────────

async def infer_nace_codes(
    db_path: str,
    countries: list[str] | None = None,
    limit: int = 0,
    overwrite: bool = False,
) -> dict:
    """Infer and store NACE codes for companies that don't have one.

    Args:
        db_path: Path to SQLite database.
        countries: List of ISO-2 country codes to process (None = all).
        limit: Max companies to process (0 = all).
        overwrite: If True, also overwrite existing nace_inferred values.

    Returns:
        dict with stats: {"processed": N, "inferred": N, "skipped": N}
    """
    factory = get_session_factory(db_path)

    # ── Build query ───────────────────────────────────────────────────────────
    async with factory() as session:
        q = select(Company).where(Company.nace_code.is_(None))

        if not overwrite:
            q = q.where(Company.nace_inferred.is_(None))

        # Only process companies with some text to work with
        q = q.where(
            or_(
                Company.activity_description.isnot(None),
                Company.sector.isnot(None),
                Company.name.isnot(None),
            )
        )

        if countries:
            q = q.where(Company.country.in_(countries))

        q = q.order_by(Company.revenue_eur.desc().nulls_last())

        if limit:
            q = q.limit(limit)

        companies = (await session.execute(q)).scalars().all()

    total = len(companies)
    logger.info(f"[NACE] {total} entreprises à traiter")

    global _nace_status
    _nace_status.update({
        "running": True, "processed": 0, "total": total,
        "inferred": 0, "skipped": 0,
        "countries": countries or ["all"],
        "error": None,
    })

    inferred = 0
    skipped = 0
    BATCH = 500

    try:
        for i in range(0, total, BATCH):
            batch = companies[i : i + BATCH]
            async with factory() as session:
                for co in batch:
                    desc = co.activity_description or ""
                    name = co.name or ""
                    sector = co.sector or ""
                    nace = None

                    if co.country == "GB":
                        # GB: SIC codes are stored in activity_description
                        nace = _best_nace_from_sic_list(desc)
                        if not nace:
                            # Fallback to keyword if description also has text
                            nace = _infer_from_keywords(desc + " " + sector, name)
                    else:
                        # Other countries: keyword matching
                        nace = _infer_from_keywords(desc + " " + sector, name)

                    db_obj = await session.get(Company, co.id)
                    if db_obj is None:
                        skipped += 1
                        continue

                    if nace:
                        db_obj.nace_inferred = nace
                        inferred += 1
                    else:
                        skipped += 1

                await session.commit()

            _nace_status["processed"] = min(i + BATCH, total)
            _nace_status["inferred"] = inferred
            _nace_status["skipped"] = skipped
            logger.info(f"[NACE] {min(i + BATCH, total)}/{total} — inférés: {inferred}")

    except Exception as e:
        _nace_status["error"] = str(e)
        logger.error(f"[NACE] Erreur: {e}")
        raise
    finally:
        _nace_status["running"] = False

    logger.info(f"[NACE] Terminé: {inferred} inférés, {skipped} sans correspondance")
    return {"processed": total, "inferred": inferred, "skipped": skipped}
