"""Analyse financière pour signaux vendeurs M&A.

Entrée : liste de snapshots financiers (multi-années)
Sortie : (score: int 0-30, signals: list[str], memo: str)
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class FinSnap:
    year: int
    revenue: Optional[float]
    operating_income: Optional[float]
    net_income: Optional[float]
    cash: Optional[float]
    debt: Optional[float]
    ebitda: Optional[float]


def _fmt_eur(v: float) -> str:
    if v is None:
        return "N/D"
    if abs(v) >= 1e9:
        return f"{v/1e9:.1f} Md€"
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f} M€"
    return f"{v/1e3:.0f} K€"


def _cagr(v_start: float, v_end: float, n_years: int) -> Optional[float]:
    if v_start and v_end and n_years > 0 and v_start > 0:
        return (v_end / v_start) ** (1 / n_years) - 1
    return None


def compute_financial_signals(snaps_raw: list[dict]) -> tuple[int, list[str], str, bool]:
    """
    Analyse financière pour signaux vendeurs M&A.

    Args:
        snaps_raw: liste de dicts {year, revenue_eur, operating_income_eur,
                                    net_income_eur, cash_eur, debt_eur, ebitda_eur}

    Returns:
        (score 0-30, signals list, seller_financial_memo string, plateau_detected bool)
    """
    if not snaps_raw:
        return 0, [], "", False

    snaps = sorted([
        FinSnap(
            year=s["year"],
            revenue=s.get("revenue_eur"),
            operating_income=s.get("operating_income_eur"),
            net_income=s.get("net_income_eur"),
            cash=s.get("cash_eur"),
            debt=s.get("debt_eur"),
            ebitda=s.get("ebitda_eur"),
        )
        for s in snaps_raw
    ], key=lambda x: x.year)

    score = 0
    signals = []
    analysis = {}

    # ── A. Croissance du CA ──────────────────────────────────────────────────
    rev_snaps = [(s.year, s.revenue) for s in snaps if s.revenue is not None]
    ca_trend = "N/D"
    cagr_val = None
    if len(rev_snaps) >= 2:
        y0, r0 = rev_snaps[0]
        yn, rn = rev_snaps[-1]
        n = yn - y0
        cagr_val = _cagr(r0, rn, n)

        if cagr_val is not None:
            if cagr_val < -0.05:
                ca_trend = f"📉 Baisse prononcée ({cagr_val*100:.1f}%/an)"
                score += 12
                signals.append(f"📉 CA en baisse ({cagr_val*100:.1f}%/an en moyenne)")
            elif cagr_val < -0.01:
                ca_trend = f"📉 Légère baisse ({cagr_val*100:.1f}%/an)"
                score += 7
                signals.append(f"📉 CA en recul ({cagr_val*100:.1f}%/an)")
            elif cagr_val < 0.02:
                ca_trend = f"➡️ Stagnation ({cagr_val*100:.1f}%/an)"
                score += 5
                signals.append(f"📊 CA stagnant ({cagr_val*100:.1f}%/an)")
            elif cagr_val < 0.05:
                ca_trend = f"↗️ Croissance modeste ({cagr_val*100:.1f}%/an)"
            else:
                ca_trend = f"🚀 Forte croissance ({cagr_val*100:.1f}%/an)"

        # Détection baisse récente (dernière année)
        if len(rev_snaps) >= 3:
            _, r_prev = rev_snaps[-2]
            if r_prev and rn and rn < r_prev * 0.95:
                if "📉 CA en baisse" not in " ".join(signals):
                    score += 4
                    signals.append("📉 Recul du CA l'année dernière")

    analysis["ca"] = {
        "serie": rev_snaps,
        "tendance": ca_trend,
        "cagr": cagr_val,
    }

    # ── B. Rentabilité (marge opérationnelle) ───────────────────────────────
    margin_trend = "N/D"
    margins = []
    for s in snaps:
        if s.revenue and s.revenue > 0:
            op = s.operating_income if s.operating_income is not None else s.ebitda
            if op is not None:
                margins.append((s.year, op / s.revenue))

    if len(margins) >= 2:
        m_first = margins[0][1]
        m_last = margins[-1][1]
        delta_margin = m_last - m_first

        if delta_margin < -0.05:
            margin_trend = f"📉 Érosion significative ({delta_margin*100:+.1f}pp)"
            score += 8
            signals.append(f"📉 Érosion des marges ({delta_margin*100:+.1f}pp sur {len(margins)} ans)")
        elif delta_margin < -0.02:
            margin_trend = f"↘️ Légère érosion ({delta_margin*100:+.1f}pp)"
            score += 4
            signals.append(f"↘️ Marges en compression ({delta_margin*100:+.1f}pp)")
        elif delta_margin > 0.03:
            margin_trend = f"↗️ Amélioration des marges ({delta_margin*100:+.1f}pp)"
        else:
            margin_trend = f"➡️ Marges stables ({m_last*100:.1f}%)"

    analysis["margins"] = {"serie": margins, "tendance": margin_trend}

    # ── C. Résultat net ──────────────────────────────────────────────────────
    ni_trend = "N/D"
    ni_snaps = [(s.year, s.net_income) for s in snaps if s.net_income is not None]
    if len(ni_snaps) >= 2:
        ni_first = ni_snaps[0][1]
        ni_last = ni_snaps[-1][1]
        # Tendance baisse
        neg_count = sum(1 for _, ni in ni_snaps if ni < 0)

        if neg_count >= 2:
            ni_trend = "🔴 Pertes récurrentes"
            score += 6
            signals.append("🔴 Résultat net négatif récurrent")
        elif ni_last < 0:
            ni_trend = "🔴 Résultat net négatif"
            score += 4
            signals.append("🔴 Résultat net négatif dernière année")
        elif ni_first and ni_last < ni_first * 0.7:
            ni_trend = f"↘️ Baisse résultat net (-{(1-ni_last/ni_first)*100:.0f}%)"
            score += 5
            signals.append(f"↘️ Résultat net en fort recul")
        elif ni_first and ni_last > ni_first * 1.3:
            ni_trend = "↗️ Hausse du résultat net"
        else:
            ni_trend = "➡️ Résultat net stable"
            # Profit stable sans croissance = signal positif vendeur
            if cagr_val is not None and -0.02 <= cagr_val < 0.02:
                score += 3
                signals.append("💰 Profit stable sans croissance (cash cow)")

    analysis["net_income"] = {"serie": ni_snaps, "tendance": ni_trend}

    # ── D. Structure financière ──────────────────────────────────────────────
    fin_trend = "N/D"
    last_cash = next((s.cash for s in reversed(snaps) if s.cash is not None), None)
    last_debt = next((s.debt for s in reversed(snaps) if s.debt is not None), None)
    last_rev = next((s.revenue for s in reversed(snaps) if s.revenue is not None), None)

    cash_signals = []
    if last_cash is not None and last_rev and last_rev > 0:
        cash_ratio = last_cash / last_rev
        if cash_ratio > 0.25:
            cash_signals.append(f"💰 Trésorerie élevée ({cash_ratio*100:.0f}% du CA)")
            score += 4
            signals.append(f"💰 Trésorerie non déployée ({_fmt_eur(last_cash)})")

    debt_signals = []
    if last_debt is not None:
        # Debt trend
        early_debt = next((s.debt for s in snaps if s.debt is not None), None)
        last_snap = next((s for s in reversed(snaps) if s.debt is not None), None)
        if early_debt and last_snap and last_snap.debt > early_debt * 1.3:
            debt_signals.append(f"📈 Dette en hausse ({_fmt_eur(last_snap.debt)})")
            score += 3
            signals.append(f"📈 Hausse de l'endettement")

    fin_parts = cash_signals + debt_signals
    fin_trend = " | ".join(fin_parts) if fin_parts else "Structure financière normale"
    analysis["fin"] = {"cash": last_cash, "debt": last_debt, "tendance": fin_trend}

    # ── E. Pattern "Plateau Business" ───────────────────────────────────────
    # Conditions : CA stable ET marge stable ET profit stable (≥3 ans de données)
    plateau_detected = False
    ca_stable = cagr_val is not None and -0.02 <= cagr_val < 0.02
    margin_stable = (
        len(margins) >= 2
        and abs(margins[-1][1] - margins[0][1]) < 0.02   # delta <2pp
        and all(m > 0 for _, m in margins)                 # toujours positif
    )
    profit_stable = (
        len(ni_snaps) >= 2
        and ni_snaps[0][1] and ni_snaps[-1][1]
        and ni_snaps[-1][1] > 0                            # toujours profitable
        and 0.80 <= ni_snaps[-1][1] / ni_snaps[0][1] <= 1.25  # variation <20%
        and sum(1 for _, ni in ni_snaps if ni < 0) == 0   # aucune perte
    )

    if ca_stable and margin_stable and profit_stable and len(snaps) >= 3:
        plateau_detected = True
        score += 8   # bonus plateau
        signals.append("🎯 Plateau Business — Prime M&A Target")

    # ── Construction du Seller Financial Memo ───────────────────────────────
    score = min(score, 30)

    if plateau_detected:
        niveau = "Élevé"
        conclusion = "🎯 Prime M&A Target — profil «plateau business» idéal pour une cession (CA stable, marges saines, profit récurrent)"
    elif score >= 20:
        niveau = "Élevé"
        conclusion = "⭐ Cible intéressante — profil financier compatible avec une cession"
    elif score >= 10:
        niveau = "Modéré"
        conclusion = "👀 À surveiller — quelques signaux financiers vendeurs détectés"
    else:
        niveau = "Faible"
        conclusion = "⚠️ Peu pertinente financièrement — pas de signaux vendeurs marqués"

    # Résumé 2-3 phrases
    resume_parts = []
    if ca_trend != "N/D":
        resume_parts.append(f"Chiffre d'affaires : {ca_trend}.")
    if margin_trend != "N/D":
        resume_parts.append(f"Rentabilité : {margin_trend}.")
    if ni_trend != "N/D":
        resume_parts.append(f"Résultat net : {ni_trend}.")
    resume = " ".join(resume_parts[:2]) if resume_parts else "Données financières insuffisantes pour une analyse complète."

    # Série CA formatée
    ca_serie_txt = ""
    if analysis["ca"]["serie"]:
        ca_serie_txt = " → ".join(
            f"{y}: {_fmt_eur(r)}" for y, r in analysis["ca"]["serie"]
        )

    # Série marges formatée
    margin_serie_txt = ""
    if analysis["margins"]["serie"]:
        margin_serie_txt = " → ".join(
            f"{y}: {m*100:.1f}%" for y, m in analysis["margins"]["serie"]
        )

    # Série résultat net
    ni_serie_txt = ""
    if analysis["net_income"]["serie"]:
        ni_serie_txt = " → ".join(
            f"{y}: {_fmt_eur(ni)}" for y, ni in analysis["net_income"]["serie"]
        )

    signals_txt = "\n".join(f"- {s}" for s in signals) if signals else "- Aucun signal vendeur financier détecté"

    plateau_banner = ""
    if plateau_detected:
        plateau_banner = """
╔══════════════════════════════════════════════╗
║  🎯  PRIME M&A TARGET — PLATEAU BUSINESS     ║
║  CA stable · Marges saines · Profit récurrent║
╚══════════════════════════════════════════════╝
"""

    memo = f"""---
Seller Financial Memo
{plateau_banner}
Résumé
{resume}

Analyse

Chiffre d'affaires
{ca_serie_txt or "Données non disponibles"}
Tendance : {ca_trend}

Rentabilité
{margin_serie_txt or "Données non disponibles"}
Tendance : {margin_trend}

Résultat net
{ni_serie_txt or "Données non disponibles"}
Tendance : {ni_trend}

Structure financière
Trésorerie : {_fmt_eur(last_cash)} | Dettes : {_fmt_eur(last_debt)}
{fin_trend}

Signaux vendeurs détectés
{signals_txt}

Interprétation M&A
{"Le profil financier de cette entreprise présente plusieurs caractéristiques typiques d'un vendeur potentiel : " + ", ".join(s.split(" ", 1)[1] if " " in s else s for s in signals[:3]) + "." if signals else "Aucun signal vendeur financier significatif détecté dans les données disponibles."}

Score vendeur financier
- Score : {score} / 30
- Niveau : {niveau}

Conclusion
{conclusion}
---"""

    return score, signals, memo, plateau_detected
