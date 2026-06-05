"""
Equans M&A scoring engine — multi-country, NACE-based.

Scoring breakdown (0-100):
  sector_score      /30  — NACE / PKD primary or secondary match
  revenue_score     /20  — CA sweet spot 30-300M€ (avec plafond >1B€)
  integration_score /15  — ing + installation + maintenance (3 piliers)
  critical_score    /15  — présence secteurs critiques (datacenter, hôpital…)
  founder_score     /10  — profil fondateur/famille, dirigeant senior
  longevity_score   /10  — société ≥ 15 ans

v2 changes:
  - Revenue cap: >1B€ réduit, >3B€ = 0 (Equans cible les PME/ETI)
  - Pénalité géographique: non-EU → sector_score divisé par 2
  - Exclusion de secteurs: Transport, Finance, Utilities ne scorent pas
  - Suppression 35.11/35.13 de NACE_SECONDARY (utilities ≠ cibles Equans)
  - Ajout is_european + revenue_bracket dans le résultat
"""

import json
from typing import Optional

# ── Géographie ────────────────────────────────────────────────────────────────

EU_COUNTRIES = {
    "FR", "DE", "IT", "GB", "PL", "BE", "NL", "ES", "PT", "CH",
    "AT", "NO", "SE", "DK", "FI", "IE", "LU", "CZ", "SK", "RO",
    "HU", "HR", "SI", "EE", "LV", "LT", "BG", "GR", "CY", "MT",
    "LI", "IS",  # Liechtenstein + Islande (EEA)
}

# ── NACE / PKD codes ──────────────────────────────────────────────────────────

# Cœur de métier Equans (multi-technique, installation, exploitation)
NACE_PRIMARY = {
    "43.21",  # Installation électrique
    "43.22",  # Plomberie, chauffage, climatisation
    "43.29",  # Autres travaux d'installation
    "33.20",  # Installation de machines et équipements industriels
    "81.10",  # Activités de services de bâtiments combinés (FM)
}

# Métiers adjacents Equans (ingénierie, construction spécialisée)
# Note: 35.11 et 35.13 retirés (utilities = clients d'Equans, pas cibles M&A)
NACE_SECONDARY = {
    "71.12",  # Ingénierie et conseil technique
    "43.99",  # Autres travaux de construction spécialisés
    "42.22",  # Construction de réseaux électriques et télécommunications
    "26.51",  # Instruments de mesure et de contrôle
    "28.11",  # Fabrication de moteurs et turbines (industriel)
    "28.22",  # Fabrication de matériel de levage
    "28.29",  # Autres machines industrielles
    "38.12",  # Collecte de déchets dangereux (environnement)
    "41.20",  # Construction de bâtiments résidentiels/non résidentiels (entreprise générale)
    "43.91",  # Travaux de couverture
}

# Secteurs à exclure du scoring Equans (ne sont pas des cibles M&A pour Equans)
# La comparaison se fait en lowercase, correspondance partielle
EXCLUDED_SECTOR_KEYWORDS = [
    "transport", "logistique", "fret", "ferroviaire", "railway", "autoroute",
    "finance", "banque", "assurance", "investissement", "capital-risque",
    "immobilier", "promotion immobilière", "foncier",
    "distribution", "commerce de détail", "supermarché", "grande distribution",
    "alimentation", "agroalimentaire", "agriculture",
    "médias", "presse", "édition", "publicité", "broadcast",
    "tourisme", "hôtellerie", "restauration",
    "télécommunication", "telecom",  # opérateurs télécom (≠ intégrateurs)
    # Secteurs de services sociaux / santé / éducation (clients d'Equans, pas cibles M&A)
    "santé humaine", "action sociale", "activités sociales",
    "enseignement", "éducation nationale",
    "administration publique", "organismes extraterritoriaux",
]

# Secteurs utility : Equans TRAVAILLE pour eux mais ne les rachète pas
UTILITY_SECTOR_KEYWORDS = [
    "production d'électricité", "distribution d'électricité",
    "eau/assainissement", "distribution de gaz", "réseau de chaleur",
    "centrale électrique", "power plant", "kraftwerk",
    "nucléaire",  # opérateur (≠ prestataire nucléaire)
]

# ── Keywords (5 langues : FR / DE / IT / PL / EN) ────────────────────────────

KW_INSTALLATION = [
    # Électricité
    "installation électrique", "électricité", "électricien",
    "elektroinstallation", "elektrik", "elektriker",
    "impianti elettrici", "elettricità", "elettricista",
    "instalacje elektryczne", "elektroinstalacje",
    "electrical installation", "electrical contractor", "electrician",
    # CVC / HVAC
    "CVC", "HVAC", "hvac", "chauffage", "climatisation", "ventilation",
    "Heizung", "Klimatechnik", "Lüftung", "Kältetechnik",
    "riscaldamento", "climatizzazione", "ventilazione",
    "ogrzewanie", "klimatyzacja", "wentylacja",
    "air conditioning", "heating", "cooling",
    # Plomberie / Sanitaire
    "plomberie", "sanitaire", "Sanitär", "Sanitärinstallation",
    "impianti idraulici", "idraulico", "instalacje sanitarne",
    # Montage général
    "installateur", "Montage", "montaggio", "montaż",
    "pompe à chaleur", "heat pump", "Wärmepumpe", "pompa di calore",
    # Norvégien / Scandinave
    "elektroinstallasjon", "installasjonsarbeid", "VVS",
    "rørlegging", "rørlegger", "el-installasjon",
    "installasjonsvirksomhet",  # general installation business
]

KW_ENGINEERING = [
    # Bureau d'études / Ingénierie
    "bureau d'études", "ingénierie", "conception",
    "Ingenieursbüro", "Ingenieurbüro", "Planung", "Projektierung",
    "ingegneria", "progettazione",
    "biuro projektowe", "projektowanie",
    "engineering", "design office",
    # Automatisme / BMS / SCADA
    "automatisme", "automate", "automation", "Automatisierung",
    "automazione", "automatyka", "sterownik", "PLC",
    "GTB", "GTC", "BMS", "SCADA", "building management",
    "instrumentation", "Messtechnik", "strumentazione",
    # Génie technique
    "génie électrique", "génie climatique", "génie thermique",
    "Elektrotechnik", "Versorgungstechnik", "Gebäudetechnik",
    "impianti tecnologici", "technika budowlana",
    # Norvégien / Scandinave
    "prosjektering", "automatisering", "automasjon",
    "ingeniørtjenest", "rådgivende ingeniør",
]

KW_MAINTENANCE = [
    "maintenance", "entretien", "exploitation",
    "Wartung", "Instandhaltung", "Betrieb",
    "manutenzione", "gestione impianti",
    "utrzymanie ruchu", "serwis", "eksploatacja",
    "facility", "facility management", "FM ",
    "multi-technique", "multitechnique", "multiservices",
    "contrat de service", "Servicevertrag", "contratto di manutenzione",
    # Norvégien / Scandinave
    "vedlikehold", "driftskontroll", "driftsvedlikehold",
]

KW_CRITICAL = [
    # Numérique / Data
    "data center", "datacenter", "centre de données", "salle blanche",
    "cleanroom", "salle informatique",
    "Rechenzentrum", "Reinraum",
    "data centre", "sala bianca", "sala server",
    "centrum danych", "serwerownia",
    # Santé / Pharma
    "hôpital", "clinique", "santé", "pharmaceutique",
    "Krankenhaus", "Klinik", "pharmazeutisch",
    "ospedale", "clinica", "farmaceutico",
    "szpital", "farmaceutyczny",
    "hospital", "healthcare", "pharmaceutical",
    # Industrie lourde / Énergie (en tant que prestataire)
    "pétrochimie", "raffinerie", "Raffinerie", "raffineria",
    "centrale nucléaire", "nuclear plant", "Atomkraftwerk",
    # Infrastructure critique (en tant que prestataire)
    "aéroport", "airport", "Flughafen", "aeroporto", "lotnisko",
    # Défense / Sécurité
    "défense", "défense nationale", "armée",
    "Verteidigung", "Bundeswehr",
    "difesa", "esercito",
    "obronność", "wojsko",
    "defence", "defense", "military",
]
# Note : "ferroviaire"/"railway" retiré de KW_CRITICAL pour éviter les faux positifs
# sur les opérateurs ferroviaires (JR West etc.) — le railway est un secteur client,
# pas un signal d'activité Equans.

CURRENT_YEAR = 2026


def _lower(text: str | None) -> str:
    return (text or "").lower()


def _has(corpus: str, keywords: list[str]) -> bool:
    return any(kw.lower() in corpus for kw in keywords)


def _revenue_bracket(rev: float) -> str:
    """Catégorie de taille pour filtrage UI."""
    if rev >= 3_000_000_000:
        return ">3B€"
    elif rev >= 1_000_000_000:
        return "1-3B€"
    elif rev >= 300_000_000:
        return "300M-1B€"
    elif rev >= 75_000_000:
        return "75-300M€"
    elif rev >= 30_000_000:
        return "30-75M€"
    elif rev >= 10_000_000:
        return "10-30M€"
    elif rev > 0:
        return "<10M€"
    return "inconnu"


def score_company(
    nace_code: str | None,
    sector: str | None,
    activity_description: str | None,
    revenue_eur: float | None,
    creation_date: str | None,
    directors: list,
    fi=None,
    nace_inferred: str | None = None,
    name: str | None = None,
    country: str | None = None,
    has_public_infra_contracts: bool = False,
) -> dict:
    """
    Compute Equans M&A score for one company.

    Returns a dict matching all EquansScore columns (except id/company_id/scored_at).
    """
    reasons: list[str] = []

    corpus = _lower(" ".join(filter(None, [
        nace_code or "",
        nace_inferred or "",
        sector or "",
        activity_description or "",
        name or "",          # les noms DE/IT/PL encodent souvent le métier
    ])))

    # Use nace_code if available, else fall back to inferred
    effective_nace = (nace_code or nace_inferred or "").strip().replace(" ", "")
    nace_clean = effective_nace
    nace_source = "inferred" if (not nace_code and nace_inferred) else "official"

    # ── Géographie ────────────────────────────────────────────────────────────
    is_european = (country in EU_COUNTRIES) if country else True  # prudent: True si inconnu

    # ── Détection secteur exclu ───────────────────────────────────────────────
    sector_lower = _lower(sector or "")
    activity_lower = _lower(activity_description or "")
    combined_sector = sector_lower + " " + activity_lower

    is_excluded = (
        any(kw in sector_lower for kw in EXCLUDED_SECTOR_KEYWORDS)
        and nace_clean not in NACE_PRIMARY  # sauf si NACE primaire officiel
    )
    is_utility = (
        any(kw in combined_sector for kw in UTILITY_SECTOR_KEYWORDS)
        and nace_clean not in NACE_PRIMARY  # exception : prestataire en NACE primaire ≠ opérateur
    )

    # ── Sector score (0–30) ───────────────────────────────────────────────────
    if is_excluded or is_utility:
        # Secteur exclu : pas de score métier Equans
        sector_score = 0
        if is_excluded:
            reasons.append(f"Secteur exclu ({sector}) — hors périmètre Equans")
        else:
            reasons.append(f"Secteur utility ({sector}) — client Equans, pas cible M&A")
    elif nace_clean in NACE_PRIMARY:
        sector_score = 30 if nace_source == "official" else 25
        label = nace_clean + (" (inféré)" if nace_source == "inferred" else "")
        reasons.append(f"NACE {label} — cœur de métier Equans")
    elif nace_clean in NACE_SECONDARY:
        sector_score = 20 if nace_source == "official" else 16
        label = nace_clean + (" (inféré)" if nace_source == "inferred" else "")
        reasons.append(f"NACE {label} — activité adjacente Equans")
    elif _has(corpus, KW_INSTALLATION + KW_ENGINEERING):
        sector_score = 10
        reasons.append("Activité installation/ingénierie détectée (description)")
    else:
        sector_score = 0

    # Pénalité géographique: hors EU → score secteur divisé par 2
    if not is_european and sector_score > 0:
        sector_score = sector_score // 2
        reasons.append(f"Pénalité géographique (pays hors UE/Europe: {country})")

    # ── Revenue score (0–20) — avec plafond ──────────────────────────────────
    rev = revenue_eur or 0
    rb = _revenue_bracket(rev)

    if rev >= 3_000_000_000:
        # >3B€ : bien trop grand pour une acquisition Equans
        revenue_score = 0
        reasons.append(f"CA {rev/1e9:.1f}Md€ — trop élevé (hors cible PME/ETI)")
    elif rev >= 1_000_000_000:
        # 1-3B€ : grande entreprise, possible mais peu probable
        revenue_score = 4
        reasons.append(f"CA {rev/1e6:.0f}M€ (grand groupe — cible atypique)")
    elif rev >= 300_000_000:
        # 300M-1B€ : ETI, possible
        revenue_score = 12
        reasons.append(f"CA ≥ 300M€ ({rev/1e6:.0f}M€) — ETI")
    elif rev >= 75_000_000:
        # 75-300M€ : sweet spot Equans
        revenue_score = 20
        reasons.append(f"CA ≥ 75M€ ({rev/1e6:.0f}M€) — cible idéale")
    elif rev >= 30_000_000:
        revenue_score = 14
        reasons.append(f"CA ≥ 30M€ ({rev/1e6:.0f}M€)")
    elif rev >= 10_000_000:
        revenue_score = 6
        reasons.append(f"CA ≥ 10M€ ({rev/1e6:.0f}M€)")
    else:
        revenue_score = 0

    # ── Integration pillars (0–15) ────────────────────────────────────────────
    has_installation = _has(corpus, KW_INSTALLATION) or nace_clean in {"43.21", "43.22", "43.29", "33.20"}
    has_engineering = _has(corpus, KW_ENGINEERING) or nace_clean in {"71.12"}
    has_maintenance = _has(corpus, KW_MAINTENANCE) or nace_clean in {"81.10"}

    # Pas de piliers si secteur exclu
    if is_excluded or is_utility:
        has_installation = False
        has_engineering = False
        has_maintenance = False

    pillars = sum([has_installation, has_engineering, has_maintenance])
    integration_score = {3: 15, 2: 10, 1: 5, 0: 0}[pillars]
    if pillars > 0:
        parts = []
        if has_installation:
            parts.append("installation")
        if has_engineering:
            parts.append("ingénierie")
        if has_maintenance:
            parts.append("maintenance")
        reasons.append("Piliers: " + " + ".join(parts))

    # ── Critical sectors (0–15) ───────────────────────────────────────────────
    # Le critical_score ne compte que si l'entreprise a aussi une activité
    # installation/ingénierie (sinon, elle EST le secteur critique = client, pas cible)
    has_critical_sectors = (_has(corpus, KW_CRITICAL) or has_public_infra_contracts) if not is_excluded else False
    if has_critical_sectors and (sector_score > 0 or integration_score > 0):
        critical_score = 15   # Prestataire technique pour clients critiques → fort signal
        if has_public_infra_contracts:
            reasons.append("Marchés publics sur infra critique prouvés (HTA/hôpital/datacenter/défense…)")
        else:
            reasons.append("Présence secteurs critiques (data, santé, défense…)")
    elif has_critical_sectors:
        # Est soi-même l'institution critique (hôpital, aéroport…) → faible signal
        critical_score = 3
        reasons.append("Secteur critique (client potentiel Equans, pas prestataire)")
    else:
        critical_score = 0

    # ── Founder signal (0–10) ─────────────────────────────────────────────────
    founder_score = 0
    is_founder_owned = False

    # Always compute director heuristic (used as fallback/supplement)
    _heuristic_score = 0
    _heuristic_owned = False
    _heuristic_reason = ""
    _ages = [CURRENT_YEAR - d.birth_year for d in directors if getattr(d, "birth_year", None)]
    if _ages:
        _max_age = max(_ages)
        if _max_age >= 65:
            _heuristic_score = 8
            _heuristic_owned = True
            _heuristic_reason = f"Dirigeant ~{_max_age} ans (heuristique)"
        elif _max_age >= 55:
            _heuristic_score = 5
            _heuristic_reason = f"Dirigeant ~{_max_age} ans (heuristique)"
        elif _max_age >= 45:
            _heuristic_score = 2

    if fi is not None:
        fs = getattr(fi, "founder_status", "unknown")
        if fs in ("founder", "family_successor"):
            is_founder_owned = True
            founder_score += 6
        age = getattr(fi, "estimated_age", None)
        if age and age >= 60:
            founder_score += 4
            reasons.append(f"Fondateur ~{age} ans (Founder Intelligence)")
        elif age and age >= 50:
            founder_score += 2
        seller = getattr(fi, "seller_signal_strength", "unknown")
        if seller == "high":
            founder_score = min(founder_score + 3, 10)
            reasons.append("Signal vendeur ÉLEVÉ (FI)")
        elif seller == "moderate":
            founder_score = min(founder_score + 1, 10)
        if is_founder_owned and founder_score > 0:
            reasons.append(f"Profil {fs.replace('_', ' ')}")
        # If FI quality is low (unknown status, age below scoring threshold), fall back to heuristic if better
        if fs == "unknown" and (age is None or age < 50) and _heuristic_score > founder_score:
            founder_score = _heuristic_score
            is_founder_owned = _heuristic_owned
            if _heuristic_reason:
                reasons.append(_heuristic_reason)
    else:
        # Heuristic from director birth years
        founder_score = _heuristic_score
        is_founder_owned = _heuristic_owned
        if _heuristic_reason:
            reasons.append(_heuristic_reason)

    founder_score = min(founder_score, 10)

    # ── Longevity (0–10) ──────────────────────────────────────────────────────
    longevity_score = 0
    if creation_date:
        try:
            year = int(str(creation_date)[:4])
            age_co = CURRENT_YEAR - year
            if age_co >= 30:
                longevity_score = 10
                reasons.append(f"Fondée en {year} ({age_co} ans d'existence)")
            elif age_co >= 15:
                longevity_score = 5
                reasons.append(f"Fondée en {year} ({age_co} ans d'existence)")
        except (ValueError, TypeError):
            pass

    # ── Totals & thesis ───────────────────────────────────────────────────────
    total = sector_score + revenue_score + integration_score + critical_score + founder_score + longevity_score
    total = min(total, 100)

    thesis_parts = []
    if nace_clean:
        thesis_parts.append(f"NACE {nace_clean}")
    elif sector:
        thesis_parts.append(sector)
    if is_founder_owned:
        thesis_parts.append("fondateur/famille")
    if has_critical_sectors:
        thesis_parts.append("secteurs critiques")
    if pillars == 3:
        thesis_parts.append("ing+install+maint")
    if not is_european:
        thesis_parts.append(f"hors UE ({country})")
    thesis = " · ".join(thesis_parts) if thesis_parts else None

    return {
        "total_score": total,
        "sector_score": sector_score,
        "revenue_score": revenue_score,
        "integration_score": integration_score,
        "critical_score": critical_score,
        "founder_score": founder_score,
        "longevity_score": longevity_score,
        "has_engineering": has_engineering,
        "has_installation": has_installation,
        "has_maintenance": has_maintenance,
        "has_critical_sectors": has_critical_sectors,
        "is_founder_owned": is_founder_owned,
        "is_european": is_european,
        "revenue_bracket": rb,
        "thesis": thesis,
        "match_reasons": json.dumps(reasons, ensure_ascii=False),
    }
