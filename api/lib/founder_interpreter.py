"""Moteur d'interprétation M&A pour les dirigeants — 100% rule-based.

Produit une lecture actionnelle du profil dirigeant sans dépendance LLM :
- founder_status, operator_type, successor_signal
- seller_signal_strength + raison
- why_now hypothesis
- angle d'approche + ce qu'il faut éviter

Les règles sont calibrées sur des patterns M&A réels (deal origination).
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


CURRENT_YEAR = 2026


@dataclass
class FounderProfile:
    # ── Identité ──────────────────────────────────────────────────────────────
    full_name: str = ""
    current_role: str = ""
    estimated_age: Optional[int] = None
    founder_status: str = "unknown"      # founder / family_successor / hired_manager / unknown
    years_in_role: Optional[int] = None

    # ── Transmission ──────────────────────────────────────────────────────────
    children_signal: str = "unknown"       # yes / no / unknown
    children_in_business: str = "unknown"  # yes / no / unknown
    successor_signal: str = "unknown"      # none / possible_internal / likely_family / likely_operational / unknown

    # ── Profil dirigeant ──────────────────────────────────────────────────────
    operator_type: str = "unknown"         # builder / operator / patrimonial / disengaged / unknown
    public_visibility: str = "unknown"     # low / medium / high
    relationship_to_company: str = ""

    # ── Signaux vendeurs ──────────────────────────────────────────────────────
    main_why_now_hypothesis: str = ""
    seller_signal_strength: str = "unknown"  # low / moderate / high
    seller_signal_reason: str = ""

    # ── Outreach ──────────────────────────────────────────────────────────────
    recommended_approach_angle: str = ""
    avoid_in_outreach: str = ""
    approach_hooks: list[str] = field(default_factory=list)  # 2-3 accroches

    # ── Contact ───────────────────────────────────────────────────────────────
    professional_email: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None

    # ── Meta ──────────────────────────────────────────────────────────────────
    confidence_score: int = 0  # 0-100


# ── Règles : founder_status ───────────────────────────────────────────────────
def _detect_founder_status(
    director_name: str,
    company_name: str,
    appointed_year: Optional[int],
    company_creation_year: Optional[int],
    role: str,
    revenue_eur: float = 0,
) -> str:
    role_lower = (role or "").lower()

    # Fondateur explicite dans le titre
    if any(kw in role_lower for kw in ["fondateur", "founder", "créateur", "associé gérant",
                                        "gérant majoritaire", "gérant associé"]):
        return "founder"

    # Même patronyme que la société → family successor
    if director_name and company_name:
        name_parts = director_name.upper().split()
        co_words = company_name.upper().split()
        for part in name_parts:
            if len(part) > 3 and any(part in cw or cw in part for cw in co_words):
                return "family_successor"

    # Grande entreprise (>500M€) avec rôle exécutif standard → hired manager
    if revenue_eur and revenue_eur >= 500_000_000:
        exec_keywords = ["directeur général", "ceo", "chief executive", "chairman and ceo",
                         "président du conseil d'administration et directeur général"]
        if any(kw in role_lower for kw in exec_keywords):
            return "hired_manager"

    # Nommé dans les 2 ans suivant la création → probable fondateur
    if appointed_year and company_creation_year:
        if abs(appointed_year - company_creation_year) <= 2:
            return "founder"
        # Nommé bien après → hired manager
        if appointed_year - company_creation_year > 10:
            return "hired_manager"

    # Gérant/PDG d'une société récente
    if any(kw in role_lower for kw in ["gérant", "président", "pdg", "dg "]):
        if company_creation_year and CURRENT_YEAR - company_creation_year < 10:
            return "founder"

    # Gérant simple → fort signal fondateur (structure PME)
    if "gérant" in role_lower and not revenue_eur or (revenue_eur and revenue_eur < 50_000_000):
        if "gérant" in role_lower:
            return "founder"

    return "unknown"


# ── Règles : operator_type ────────────────────────────────────────────────────
def _detect_operator_type(
    founder_status: str,
    years_in_role: Optional[int],
    age: Optional[int],
    financial_signals: list[str],
    company_age: Optional[int],
    revenue_eur: float = 0,
    role: str = "",
    has_directors: bool = True,
) -> str:
    fin_str = " ".join(financial_signals)
    has_plateau = "Plateau Business" in fin_str or "stagnant" in fin_str.lower()
    has_decline = "baisse" in fin_str.lower() or "recul" in fin_str.lower()
    role_lower = (role or "").lower()

    if founder_status == "founder":
        if years_in_role and years_in_role >= 20:
            if has_plateau or has_decline:
                return "patrimonial"  # fondateur historique, plus en croissance
            return "builder"          # fondateur encore actif sur la trajectoire
        if years_in_role and years_in_role >= 10:
            return "builder"
        # Fondateur sans tenure connue → âge pour affiner
        if age and age >= 65:
            return "patrimonial"
        return "founder"  # fondateur récent / indéterminé

    if founder_status == "family_successor":
        return "patrimonial"

    if founder_status == "hired_manager":
        if years_in_role and years_in_role >= 15:
            return "disengaged"
        return "operator"

    # ── unknown founder_status : heuristiques supplémentaires ────────────────
    # Très grande entreprise sans données dirigeant → PDG salarié
    if revenue_eur and revenue_eur >= 500_000_000 and not has_directors:
        return "operator"

    # Grande entreprise listée (>1B€) avec rôle exécutif standard → salarié
    if revenue_eur and revenue_eur >= 1_000_000_000:
        exec_keywords = ["directeur général", "ceo", "chief executive", "pdg",
                         "president and ceo", "chairman"]
        if any(kw in role_lower for kw in exec_keywords):
            return "operator"

    # Rôles typiques de fondateurs/gérants de PME
    founder_role_keywords = ["gérant", "gérant associé", "gérant majoritaire",
                              "président de sas", "président sas", "dirigeant"]
    if any(kw in role_lower for kw in founder_role_keywords):
        # Petite / moyenne entreprise
        if not revenue_eur or revenue_eur < 200_000_000:
            if age and age >= 65:
                return "patrimonial"
            if company_age and company_age >= 20:
                return "patrimonial"
            return "founder"

    # Âge seul (sans tenure connue)
    if age:
        if age >= 70:
            return "patrimonial"      # très probablement long-tenured
        if age >= 62:
            if company_age and company_age >= 25:
                return "patrimonial"  # dirigeant senior d'une société ancienne
            return "operator"         # signal modéré
        if age >= 50:
            if company_age and company_age >= 30:
                return "patrimonial"  # société ancienne, profil patrimonial probable
            return "operator"

    # Entreprise très ancienne sans données dirigeant → patrimonial par défaut
    if company_age and company_age >= 50:
        return "patrimonial"
    if company_age and company_age >= 30:
        return "operator"             # société mature, management professionnel probable

    # Tenure sans founder_status
    if years_in_role and years_in_role >= 20:
        return "patrimonial"
    if years_in_role and years_in_role >= 10:
        return "operator"

    # Entreprise moyenne/grande avec données dirigeant → management professionnel probable
    if revenue_eur and revenue_eur >= 200_000_000:
        return "operator"

    # Pas de données dirigeant (no directors) + entreprise de taille significative
    if not has_directors and revenue_eur and revenue_eur >= 30_000_000:
        return "operator"

    # Rôles de directeur subsidiaire FR → opérateur salarié
    subsidiary_role_keywords = ["dirigeant en france", "directeur de filiale", "country manager",
                                 "responsable france", "président de sas"]
    if any(kw in role_lower for kw in subsidiary_role_keywords):
        if revenue_eur and revenue_eur >= 100_000_000:
            return "operator"

    return "unknown"


# ── Règles : successor_signal ─────────────────────────────────────────────────
def _detect_successor_signal(
    directors: list[dict],
    founder_name: str,
    founder_status: str,
) -> tuple[str, str, str]:
    """Returns (successor_signal, children_signal, children_in_business)."""
    if not directors or len(directors) <= 1:
        return "none", "unknown", "no"

    other_directors = [d for d in directors if d.get("name", "").upper() != founder_name.upper()]
    if not other_directors:
        return "none", "unknown", "no"

    # Chercher même patronyme → transmission familiale probable
    founder_parts = founder_name.upper().split()
    for d in other_directors:
        d_parts = (d.get("name") or "").upper().split()
        for fp in founder_parts:
            if len(fp) > 3 and fp in d_parts:
                return "likely_family", "yes", "yes"

    # Plusieurs dirigeants avec rôles opérationnels → successeur interne possible
    has_operational = any(
        any(kw in (d.get("role") or "").lower() for kw in ["directeur", "dg", "coo", "cfo", "adjoint"])
        for d in other_directors
    )
    if has_operational:
        return "possible_internal", "unknown", "unknown"

    return "unknown", "unknown", "unknown"


# ── Règles : seller_signal_strength ──────────────────────────────────────────
def _compute_seller_signal(
    age: Optional[int],
    years_in_role: Optional[int],
    founder_status: str,
    successor_signal: str,
    company_age: Optional[int],
    financial_signals: list[str],
    ma_score: int,
    operator_type: str = "unknown",
    revenue_eur: float = 0,
) -> tuple[str, str]:
    """Returns (strength, reason)."""
    reasons = []
    score = 0

    fin_str = " ".join(financial_signals)

    # ── Âge du dirigeant (cap à 90 ans — au-delà = donnée aberrante) ─────────
    if age:
        effective_age = min(age, 90)  # plafond anti-aberrations (ex: Companies House)
        if effective_age >= 80:
            score += 45; reasons.append(f"dirigeant de {effective_age} ans")
        elif effective_age >= 75:
            score += 35; reasons.append(f"dirigeant de {effective_age} ans")
        elif effective_age >= 70:
            score += 25; reasons.append(f"dirigeant de {effective_age} ans")
        elif effective_age >= 65:
            score += 18; reasons.append(f"dirigeant de {effective_age} ans")
        elif effective_age >= 62:
            score += 10; reasons.append(f"dirigeant de {effective_age} ans, horizon de cession proche")

    # ── Ancienneté en poste ───────────────────────────────────────────────────
    if years_in_role:
        if years_in_role >= 30:
            score += 20; reasons.append(f"{years_in_role} ans en poste")
        elif years_in_role >= 20:
            score += 12; reasons.append(f"{years_in_role} ans en poste")
        elif years_in_role >= 15:
            score += 6

    # ── Successeur ────────────────────────────────────────────────────────────
    if successor_signal == "none":
        score += 12; reasons.append("aucun successeur visible")
    elif successor_signal == "possible_internal":
        score += 4

    # ── Profil fondateur / patrimonial ────────────────────────────────────────
    if founder_status in ("founder", "family_successor"):
        score += 8; reasons.append("profil patrimonial")
    if operator_type == "patrimonial":
        score += 8  # bonus supplémentaire pour patrimonial
    elif operator_type == "disengaged":
        score += 6; reasons.append("manager désengagé")

    # ── Ancienneté société ────────────────────────────────────────────────────
    if company_age:
        if company_age >= 50:
            score += 6; reasons.append(f"société de {company_age} ans")
        elif company_age >= 35:
            score += 3

    # ── Signaux financiers ────────────────────────────────────────────────────
    if "Plateau Business" in fin_str:
        score += 12; reasons.append("plateau financier (cash cow sans croissance)")
    elif "baisse" in fin_str.lower():
        score += 10; reasons.append("recul du chiffre d'affaires")
    elif "stagnant" in fin_str.lower():
        score += 6; reasons.append("stagnation du CA")

    # ── Signal exceptionnel ───────────────────────────────────────────────────
    if "broker" in fin_str.lower() or ma_score >= 80:
        score += 40; reasons.append("mise en vente détectée")

    # ── Revenue : boost ou pénalité ──────────────────────────────────────────
    if revenue_eur and revenue_eur > 0:
        if revenue_eur >= 50_000_000:
            score += 12; reasons.append(f"CA {revenue_eur/1e6:.0f}M€ (PME significative)")
        elif revenue_eur >= 10_000_000:
            score += 8; reasons.append(f"CA {revenue_eur/1e6:.0f}M€")
        elif revenue_eur >= 2_000_000:
            score += 4
    else:
        # Pas de revenue connu → impossible de valider la taille, pénalité
        score -= 12

    # ── Thresholds calibrés ───────────────────────────────────────────────────
    if score >= 42:
        strength = "high"
    elif score >= 22:
        strength = "moderate"
    else:
        strength = "low"

    # ── Plancher : sans revenue, jamais "high" ────────────────────────────────
    if not revenue_eur and strength == "high":
        strength = "moderate"
        reasons.append("CA non vérifié — signal réduit à moderate")

    reason = " · ".join(reasons) if reasons else "Données insuffisantes pour évaluer le signal"
    return strength, reason


# ── Règles : why_now hypothesis ───────────────────────────────────────────────
def _build_why_now(
    age: Optional[int],
    years_in_role: Optional[int],
    founder_status: str,
    successor_signal: str,
    company_age: Optional[int],
    financial_signals: list[str],
    operator_type: str,
) -> str:
    fin_str = " ".join(financial_signals)
    parts = []

    if age and age >= 65:
        parts.append(f"dirigeant de {age} ans")
    if years_in_role and years_in_role >= 20:
        parts.append(f"en poste depuis {years_in_role} ans")
    if successor_signal == "none":
        parts.append("sans successeur identifié")
    if "Plateau Business" in fin_str:
        parts.append("sur un plateau financier (CA stable, marges saines)")
    elif "baisse" in fin_str.lower():
        parts.append("dans un contexte de recul du CA")
    if company_age and company_age >= 40:
        parts.append(f"à la tête d'une société de {company_age} ans")

    if not parts:
        if founder_status == "founder" and operator_type == "patrimonial":
            return "Fondateur patrimonial sans transmission visible — timing de cession naturel à surveiller"
        return "Données insuffisantes pour formuler une hypothèse de why now"

    hypothesis = " · ".join(parts[:4])

    # Qualificatif final
    if len(parts) >= 3:
        return f"{hypothesis} — fenêtre de cession naturelle, horizon 1-3 ans"
    elif len(parts) >= 2:
        return f"{hypothesis} — signal à surveiller sur 3-5 ans"
    else:
        return f"{hypothesis} — signal précoce"


# ── Secteurs et leurs spécificités d'approche ────────────────────────────────
_SECTOR_KEYWORDS = {
    "industrie": ["manufactur", "industri", "product", "fabricat", "usine", "atelier"],
    "distribution": ["commerc", "distribut", "négoce", "grossist", "détail", "retail", "wholesale"],
    "construction": ["construct", "bâtiment", "travaux", "immobil", "btp", "génie civil"],
    "services_b2b": ["conseil", "consulting", "informatique", "it ", "logiciel", "software", "ingénier", "service"],
    "transport": ["transport", "logistique", "freight", "shipping", "entreposage"],
    "sante": ["santé", "médical", "pharma", "clinique", "soins", "healthcare"],
    "alimentaire": ["alimentaire", "agroaliment", "food", "restaur", "epicer", "boulanger"],
    "energie": ["énergie", "energy", "électricité", "gaz", "pétrole", "oil", "renouvelable"],
}


def _detect_sector_type(sector: str) -> str:
    """Classe le secteur de l'entreprise."""
    if not sector:
        return "unknown"
    s = sector.lower()
    for key, keywords in _SECTOR_KEYWORDS.items():
        if any(k in s for k in keywords):
            return key
    return "other"


def _build_approach(
    operator_type: str,
    sector: str,
    age: Optional[int],
    revenue_eur: float,
    company_age: Optional[int],
    company_name: str,
    founder_status: str,
    financial_signals: list[str],
) -> tuple[str, str, list[str]]:
    """Génère un angle d'approche, des points à éviter et des accroches personnalisés.

    Returns: (approach_angle, avoid, hooks)
    """
    sector_type = _detect_sector_type(sector)
    fin_str = " ".join(financial_signals)
    age_eff = min(age, 90) if age else None

    # ── Accroches sectorielles ────────────────────────────────────────────────
    sector_hooks = {
        "industrie": "consolidation du tissu industriel régional",
        "distribution": "tendances de consolidation dans la distribution",
        "construction": "dynamique M&A dans le BTP et les matériaux",
        "services_b2b": "consolidation des acteurs de services B2B",
        "transport": "mouvements stratégiques dans la logistique et le transport",
        "sante": "consolidation du secteur santé/médical",
        "alimentaire": "tendances de consolidation agroalimentaire",
        "energie": "transition énergétique et consolidation sectorielle",
        "other": "dynamique de consolidation dans le secteur",
        "unknown": "tendances de marché dans votre secteur",
    }
    sector_hook = sector_hooks.get(sector_type, sector_hooks["other"])

    # ── Templates par operator_type ───────────────────────────────────────────
    if operator_type == "patrimonial":
        # Personnalisation selon l'âge
        if age_eff and age_eff >= 75:
            urgency = "La question de la transmission est probablement déjà en réflexion."
            timing = "La transmission est désormais une priorité de court terme."
        elif age_eff and age_eff >= 68:
            urgency = "La planification successorale devient un sujet actif."
            timing = "Horizon de transmission naturel dans les 3-5 prochaines années."
        else:
            urgency = "Le sujet de la pérennité mérite d'être abordé avec tact."
            timing = "Signal à surveiller, horizon 5-7 ans."

        # Personnalisation selon le secteur
        if sector_type in ("industrie", "construction"):
            patrimony_angle = f"l'outil industriel bâti au fil des années et son maintien en France"
        elif sector_type == "distribution":
            patrimony_angle = "le réseau commercial construit et les relations clients de long terme"
        elif sector_type == "services_b2b":
            patrimony_angle = "les équipes et le savoir-faire accumulés"
        else:
            patrimony_angle = "ce qui a été construit et l'empreinte locale"

        approach = (
            f"Approche confidentielle, centrée sur {patrimony_angle}. "
            f"{timing} {urgency} "
            f"Référencer la {sector_hook} pour contextualiser sans alarmer."
        )
        avoid = (
            "Tout registre financier/transactionnel d'emblée. "
            "Ne pas parler de restructuration ou de rationalisation des effectifs. "
            + ("Laisser du temps — ne pas créer d'urgence artificielle." if age_eff and age_eff < 72
               else "Ne pas attendre trop longtemps — la fenêtre peut se fermer rapidement.")
        )
        hooks = [
            f"« Nous suivons les évolutions dans {sector if sector else 'votre secteur'} — votre parcours chez {company_name} nous a marqués »",
            f"Référence à {sector_hook} et au positionnement unique de {company_name}",
            "Question ouverte sur la vision à 5 ans et les projets de transmission",
        ]
        if age_eff and age_eff >= 70:
            hooks.append(f"Discussion sur l'héritage et la pérennité après {company_age or 'plusieurs'} ans de développement")

    elif operator_type == "builder":
        approach = (
            f"Discussion stratégique sur la vision long terme et les ambitions pour {company_name}. "
            f"Positionner le partenariat comme un accélérateur de croissance, pas une fin. "
            f"Insister sur les ressources (capital, réseau, international) pour aller plus loin."
        )
        avoid = (
            "Ne pas présenter la cession comme une sortie définitive. "
            "Éviter le registre purement transactionnel au premier contact. "
            "Ne pas sous-estimer le projet ni parler de 'consolidation' trop tôt."
        )
        hooks = [
            f"« Avec notre réseau, {company_name} pourrait accélérer son développement sur [marché cible] »",
            "Partage de cas similaires où l'adossement a multiplié la croissance",
            "Question sur les freins actuels à l'expansion",
        ]

    elif operator_type == "operator":
        fin_note = ""
        if "baisse" in fin_str.lower() or "Plateau" in fin_str:
            fin_note = " Dans ce contexte de " + ("recul du CA" if "baisse" in fin_str.lower() else "plateau financier") + ", la question de la valeur actuelle vs. future est centrale."
        approach = (
            f"Approche directe et professionnelle centrée sur la valeur actionnariale.{fin_note} "
            f"Valorisation optimale dans les conditions actuelles, processus structuré et efficace."
        )
        avoid = (
            "Ne pas présupposer d'attachement émotionnel fort. "
            "Éviter un discours trop centré sur l'héritage ou la culture. "
            "Rester factuel et centré sur les chiffres."
        )
        hooks = [
            "Benchmark de valorisation sectorielle (multiples récents)",
            f"Analyse des transactions comparables dans {sector if sector else 'le secteur'}",
            "Processus clair, confidentiel et efficace",
        ]

    elif operator_type == "disengaged":
        approach = (
            f"Approche ouverte et non pressante. Créer un espace de dialogue sur l'avenir de {company_name} "
            f"sans agenda apparent. Le dirigeant cherche peut-être une porte de sortie discrète."
        )
        avoid = (
            "Ne pas brusquer ni paraître opportuniste. "
            "Laisser du temps de réflexion. "
            "Éviter les deadlines artificielles au premier contact."
        )
        hooks = [
            f"Question ouverte : « Où voyez-vous {company_name} dans 5 ans ? »",
            f"Partage d'observations sur la {sector_hook}",
            "Référence à des transactions récentes et réussies dans le secteur",
        ]

    else:  # unknown
        approach = (
            "Approche exploratoire et bienveillante. "
            "Comprendre le profil et les motivations avant d'avancer un angle transactionnel."
        )
        avoid = "Éviter toute supposition sur les motivations. Rester dans l'écoute."
        hooks = [
            "Prise de contact informelle, sans agenda transactionnel apparent",
            f"Partage d'une analyse sur {sector_hook}",
            "Question ouverte sur les priorités actuelles",
        ]

    return approach, avoid, hooks


def _build_relationship_sentence(
    founder_status: str,
    years_in_role: Optional[int],
    operator_type: str,
    company_name: str,
) -> str:
    if founder_status == "founder" and years_in_role:
        return f"A bâti {company_name} depuis {years_in_role} ans — identité personnelle probablement très liée à l'entreprise"
    if founder_status == "family_successor":
        return f"Successeur familial — attachement patrimonial et symbolique fort à {company_name}"
    if founder_status == "hired_manager" and years_in_role:
        if years_in_role >= 10:
            return f"Manager de longue date chez {company_name} ({years_in_role} ans) — connaissance profonde mais distance affective"
        return f"Manager professionnel récemment nommé chez {company_name} — logique de performance et de valeur actionnariale"
    return f"Profil dirigeant à préciser pour {company_name}"


# ── Entrée principale ─────────────────────────────────────────────────────────
def interpret_founder(
    company: dict,
    directors: list[dict],
    financial_signals: list[str],
    ma_score: int,
    web_data: dict | None = None,
) -> FounderProfile:
    """
    Produit un FounderProfile complet depuis les données disponibles.

    Args:
        company: dict avec name, creation_date, country, revenue_eur, sector
        directors: liste de dicts avec name, role, birth_year, appointed_at, tenure_years
        financial_signals: signaux du moteur financial_signals
        ma_score: score M&A global
        web_data: données collectées depuis le web (linkedin_url, email, phone, articles)
    """
    web = web_data or {}
    profile = FounderProfile()

    # ── Données entreprise ────────────────────────────────────────────────────
    company_name = company.get("name", "")
    creation_str = company.get("creation_date") or ""
    company_creation_year = int(creation_str[:4]) if creation_str and len(creation_str) >= 4 else None
    company_age = CURRENT_YEAR - company_creation_year if company_creation_year else None
    revenue_eur = float(company.get("revenue_eur") or 0)

    if not directors:
        # Pas de données dirigeant — classifier uniquement sur les données entreprise
        profile.operator_type = _detect_operator_type(
            founder_status="unknown",
            years_in_role=None,
            age=None,
            financial_signals=financial_signals,
            company_age=company_age,
            revenue_eur=revenue_eur,
            role="",
            has_directors=False,
        )
        profile.seller_signal_strength, profile.seller_signal_reason = _compute_seller_signal(
            age=None, years_in_role=None, founder_status="unknown",
            successor_signal="unknown", company_age=company_age,
            financial_signals=financial_signals, ma_score=ma_score,
            operator_type=profile.operator_type,
            revenue_eur=revenue_eur,
        )
        profile.main_why_now_hypothesis = "Données dirigeant insuffisantes — classification basée sur l'entreprise uniquement"
        profile.confidence_score = 5
        return profile

    # ── Choisir le dirigeant principal ────────────────────────────────────────
    # Priorité : rôle clé > âge cohérent (25-90 ans) > mandat le plus long
    def score_dir(d):
        s = 0
        by = d.get("birth_year")
        if by:
            age_est = CURRENT_YEAR - by
            # Ignorer les birth_years aberrants (age < 20 ou > 90)
            if 20 <= age_est <= 90:
                s += age_est  # plus âgé = plus probable vendeur
            # Ne pas pénaliser, juste ne pas scorer les aberrants
        if d.get("tenure_years"): s += d["tenure_years"] * 2
        role = (d.get("role") or "").lower()
        if any(k in role for k in ["président", "pdg", "gérant", "dg", "ceo", "fondateur", "managing director", "md"]):
            s += 50
        return s

    main_dir = max(directors, key=score_dir)

    # Si l'âge calculé est aberrant (> 90 ou < 20), effacer le birth_year
    if main_dir.get("birth_year"):
        age_check = CURRENT_YEAR - main_dir["birth_year"]
        if age_check > 90 or age_check < 20:
            main_dir = {**main_dir, "birth_year": None}

    # ── Identité ──────────────────────────────────────────────────────────────
    profile.full_name = main_dir.get("name", "")
    profile.current_role = main_dir.get("role", "")
    profile.estimated_age = (
        CURRENT_YEAR - main_dir["birth_year"] if main_dir.get("birth_year") else None
    )
    profile.years_in_role = main_dir.get("tenure_years")

    appointed_year = None
    if main_dir.get("appointed_at"):
        try:
            appointed_year = int(str(main_dir["appointed_at"])[:4])
        except (ValueError, TypeError):
            pass

    # ── Founder status ────────────────────────────────────────────────────────
    profile.founder_status = _detect_founder_status(
        director_name=profile.full_name,
        company_name=company_name,
        appointed_year=appointed_year,
        company_creation_year=company_creation_year,
        role=profile.current_role,
        revenue_eur=revenue_eur,
    )

    # ── Successor signal ──────────────────────────────────────────────────────
    profile.successor_signal, profile.children_signal, profile.children_in_business = (
        _detect_successor_signal(directors, profile.full_name, profile.founder_status)
    )

    # ── Operator type ─────────────────────────────────────────────────────────
    profile.operator_type = _detect_operator_type(
        founder_status=profile.founder_status,
        years_in_role=profile.years_in_role,
        age=profile.estimated_age,
        financial_signals=financial_signals,
        company_age=company_age,
        revenue_eur=revenue_eur,
        role=profile.current_role,
        has_directors=bool(directors),
    )

    # ── Seller signal ─────────────────────────────────────────────────────────
    profile.seller_signal_strength, profile.seller_signal_reason = _compute_seller_signal(
        age=profile.estimated_age,
        years_in_role=profile.years_in_role,
        founder_status=profile.founder_status,
        successor_signal=profile.successor_signal,
        company_age=company_age,
        financial_signals=financial_signals,
        ma_score=ma_score,
        operator_type=profile.operator_type,
        revenue_eur=revenue_eur,
    )

    # ── Why now ───────────────────────────────────────────────────────────────
    profile.main_why_now_hypothesis = _build_why_now(
        age=profile.estimated_age,
        years_in_role=profile.years_in_role,
        founder_status=profile.founder_status,
        successor_signal=profile.successor_signal,
        company_age=company_age,
        financial_signals=financial_signals,
        operator_type=profile.operator_type,
    )

    # ── Relation to company ───────────────────────────────────────────────────
    profile.relationship_to_company = _build_relationship_sentence(
        founder_status=profile.founder_status,
        years_in_role=profile.years_in_role,
        operator_type=profile.operator_type,
        company_name=company_name,
    )

    # ── Angle d'approche (personnalisé par secteur, âge, revenue) ────────────
    profile.recommended_approach_angle, profile.avoid_in_outreach, profile.approach_hooks = (
        _build_approach(
            operator_type=profile.operator_type,
            sector=company.get("sector", ""),
            age=profile.estimated_age,
            revenue_eur=revenue_eur,
            company_age=company_age,
            company_name=company_name,
            founder_status=profile.founder_status,
            financial_signals=financial_signals,
        )
    )

    # ── Public visibility (heuristique) ──────────────────────────────────────
    if web.get("article_count", 0) >= 3 or web.get("linkedin_url"):
        profile.public_visibility = "medium"
    if web.get("article_count", 0) >= 8:
        profile.public_visibility = "high"
    else:
        profile.public_visibility = "low"

    # ── Contact ───────────────────────────────────────────────────────────────
    profile.professional_email = web.get("email")
    profile.phone = web.get("phone")
    profile.linkedin_url = web.get("linkedin_url")

    # ── Confidence score ──────────────────────────────────────────────────────
    conf = 0
    if profile.estimated_age: conf += 20
    if profile.years_in_role: conf += 20
    if profile.founder_status != "unknown": conf += 20
    if profile.successor_signal != "unknown": conf += 15
    if financial_signals: conf += 15
    if profile.linkedin_url or profile.professional_email: conf += 10
    profile.confidence_score = min(conf, 100)

    return profile
