"""Rule-based personalized M&A outreach email generator (no LLM)."""
import re


def _first_name(full_name: str) -> str:
    """Extrait le prénom utilisable depuis un nom complet (souvent en MAJUSCULES)."""
    if not full_name:
        return ""
    parts = full_name.strip().split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].capitalize()
    # Dernier token = nom de famille → prendre le premier prénom seulement
    return parts[0].capitalize()


def _clean_sector(sector: str) -> str:
    if not sector:
        return "votre secteur"
    sector = re.split(r"[;/\-–]", sector)[0].strip()
    return sector[:55].lower() if len(sector) > 55 else sector.lower()


def _format_revenue(revenue_eur) -> str:
    """Ex: 125_000_000 → '125M€'"""
    if not revenue_eur:
        return ""
    m = revenue_eur / 1_000_000
    if m >= 1000:
        return f"{m/1000:.1f}Md€"
    if m >= 10:
        return f"{int(round(m))}M€"
    return f"{m:.1f}M€"


# ── Templates par operator_type ──────────────────────────────────────────────

TEMPLATES = {
    "patrimonial": {
        "subject": "Transmission de {company} — une réflexion sur l'avenir",
        "opener": (
            "Je vous contacte car {company}{revenue_ctx} est une entreprise que j'ai "
            "identifiée avec soin dans le secteur {sector}. "
            "Après {tenure} années à la tête de l'entreprise, vous avez bâti quelque chose de solide."
        ),
        "angle": (
            "Notre fonds accompagne des dirigeants fondateurs dans leur réflexion sur la transmission — "
            "en préservant l'indépendance de l'entreprise, les équipes en place et le travail de toute une vie."
        ),
        "cta": "Seriez-vous disponible 20 minutes pour un premier échange confidentiel ?",
    },
    "builder": {
        "subject": "Accélérer la croissance de {company}",
        "opener": (
            "J'ai étudié {company}{revenue_ctx} dans le secteur {sector} et "
            "j'ai été impressionné par ce que vous avez construit."
        ),
        "angle": (
            "Nous accompagnons des entrepreneurs ambitieux pour franchir des caps de croissance "
            "via des partenariats capitalistiques : ressources, réseau, acquisitions complémentaires."
        ),
        "cta": "Auriez-vous 20 minutes pour discuter de la trajectoire de {company} ?",
    },
    "operator": {
        "subject": "{company} — partenariat stratégique",
        "opener": (
            "Je vous contacte au sujet de {company}{revenue_ctx}, acteur reconnu dans le secteur {sector}."
        ),
        "angle": (
            "Nous travaillons avec des dirigeants d'entreprises performantes sur des projets "
            "de partenariat ou de transmission dans des conditions optimales — "
            "pour les actionnaires et pour les équipes."
        ),
        "cta": "Seriez-vous ouvert à un échange de 20 minutes ?",
    },
    "disengaged": {
        "subject": "L'avenir de {company} — une conversation",
        "opener": (
            "Après {tenure} années à la tête de {company}{revenue_ctx}, "
            "vous avez traversé plusieurs cycles et construit une entreprise de valeur réelle."
        ),
        "angle": (
            "Nous accompagnons des dirigeants expérimentés qui réfléchissent à leur prochain chapitre — "
            "transmission partielle ou totale, dans des conditions qui protègent ce qu'ils ont construit."
        ),
        "cta": "Je serais ravi d'échanger 20 minutes avec vous sur ce sujet, en toute confidentialité.",
    },
    "founder": {
        "subject": "{company} — réflexion sur la pérennité",
        "opener": (
            "Je vous contacte car {company}{revenue_ctx} dans le secteur {sector} "
            "correspond à notre mandat d'investissement actuel."
        ),
        "angle": (
            "Nous investissons aux côtés de fondateurs qui souhaitent accélérer, structurer ou "
            "préparer une transition — en respectant l'ADN et les équipes de l'entreprise."
        ),
        "cta": "Seriez-vous disponible 20 minutes pour un échange exploratoire ?",
    },
    "unknown": {
        "subject": "{company} — prise de contact",
        "opener": "Je me permets de vous contacter concernant {company}{revenue_ctx}.",
        "angle": (
            "Nous travaillons avec des dirigeants d'entreprises européennes sur des projets "
            "de développement et de transmission. "
            "Votre entreprise présente des caractéristiques qui correspondent à notre mandat d'investissement actuel."
        ),
        "cta": "Auriez-vous 20 minutes pour un premier échange ?",
    },
}


def _fill(template: str, vars: dict) -> str:
    for key, value in vars.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def generate_email(fi: dict, company: dict) -> dict:
    """
    Génère un email d'approche M&A personnalisé depuis le profil Founder Intelligence.

    Retourne:
        {
            "subject": str,
            "body": str,       # texte brut, ~120-160 mots
            "html": str,       # body avec <br> à la place des sauts de ligne
            "word_count": int,
        }
    """
    operator_type = (fi.get("operator_type") or "unknown").lower()
    template = TEMPLATES.get(operator_type, TEMPLATES["unknown"])

    # ── Variables de substitution ──────────────────────────────────────────
    company_name = company.get("name") or "votre entreprise"
    years_in_role = fi.get("years_in_role")
    tenure = str(years_in_role) if years_in_role else "de nombreuses"
    sector = _clean_sector(company.get("sector") or "")
    full_name = fi.get("full_name") or ""
    first_name = _first_name(full_name)

    rev_str = _format_revenue(company.get("revenue_eur"))
    revenue_ctx = f" ({rev_str} de CA)" if rev_str else ""

    tvars = {
        "company": company_name,
        "tenure": tenure,
        "sector": sector,
        "revenue_ctx": revenue_ctx,
        "director_name": first_name or full_name,
    }

    subject = _fill(template["subject"], tvars)

    # ── Construction du corps ─────────────────────────────────────────────
    paragraphs = []

    # Salutation
    paragraphs.append(f"Bonjour {first_name}," if first_name else "Bonjour,")

    # P1 : opener
    paragraphs.append(_fill(template["opener"], tvars))

    # P2 : angle
    paragraphs.append(template["angle"])

    # P3 (optionnel) : why_now — contexte de timing, uniquement si orienté faits externes
    why_now = (fi.get("main_why_now_hypothesis") or "").strip()
    _why_now_blacklist = [
        "données insuffisantes", "profil standard", "indéterminé", "aucun signal",
        "profil de manager", "la décision appartient", "approche à orienter",
        "signal précoce", "à surveiller", "fondateur patrimonial sans",
    ]
    why_now_ok = (
        why_now
        and len(why_now) > 30
        and not any(bl in why_now.lower() for bl in _why_now_blacklist)
        # Doit contenir au moins un fait concret (âge, ancienneté, années)
        and any(kw in why_now.lower() for kw in ["ans", "plateau", "recul", "baisse", "cession", "horizon"])
    )
    if why_now_ok:
        paragraphs.append(f"Notre timing n'est pas anodin : {why_now.rstrip('.')}.")

    # P4 : CTA
    paragraphs.append(_fill(template["cta"], tvars))

    # Signature
    paragraphs.append("Cordialement,\n[Votre nom]")

    body = "\n\n".join(paragraphs)

    # P.S. : meilleur hook (si disponible et pertinent)
    hooks = fi.get("approach_hooks") or []
    if isinstance(hooks, list):
        for h in hooks:
            h = str(h).strip().rstrip(".")
            # Ignorer les hooks trop courts, tout en majuscules, ou génériques
            if len(h) > 35 and not h.isupper() and "valorisation" not in h.lower():
                h_lc = h[0].lower() + h[1:]
                body += f"\n\nP.S. J'ai notamment noté que {h_lc}."
                break

    html = body.replace("\n", "<br>")
    word_count = len(body.split())

    return {
        "subject": subject,
        "body": body,
        "html": html,
        "word_count": word_count,
        "operator_type": operator_type,
        "director_name": first_name or full_name,
    }
