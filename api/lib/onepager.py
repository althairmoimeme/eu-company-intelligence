"""Générateur de one-pager PDF M&A par cible.

Produit un document d'une page A4 combinant :
- Fiche entreprise (nom, pays, secteur, CA, création)
- Score M&A + signaux déclencheurs
- Profil Founder Intelligence (operator_type, seller_signal, why_now)
- Historique financier (mini-table 5 ans)
- Template email personnalisé (extrait)
"""
import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Palette couleurs ──────────────────────────────────────────────────────────
COLORS = {
    "high": "#dc2626",     # rouge
    "moderate": "#d97706", # ambre
    "low": "#6b7280",      # gris
    "patrimonial": "#7c3aed",
    "builder": "#2563eb",
    "operator": "#0891b2",
    "founder": "#059669",
    "disengaged": "#9ca3af",
    "unknown": "#6b7280",
}

OPERATOR_LABELS = {
    "patrimonial": "🏛️ Patrimonial",
    "builder": "🚀 Builder",
    "operator": "⚙️ Opérateur",
    "founder": "💡 Fondateur",
    "disengaged": "😴 Désengagé",
    "unknown": "❓ Inconnu",
}

SIGNAL_LABELS = {
    "high": "🔴 Signal fort",
    "moderate": "🟡 Signal modéré",
    "low": "⚪ Signal faible",
    "unknown": "❓ Inconnu",
}

FLAG_EMOJIS = {
    "FR": "🇫🇷", "DE": "🇩🇪", "GB": "🇬🇧", "UK": "🇬🇧",
    "IT": "🇮🇹", "ES": "🇪🇸", "NL": "🇳🇱", "BE": "🇧🇪",
    "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰", "FI": "🇫🇮",
    "AT": "🇦🇹", "CH": "🇨🇭", "PL": "🇵🇱", "PT": "🇵🇹",
    "RO": "🇷🇴",
}


def _fmt_revenue(v: float | None) -> str:
    if not v:
        return "N/A"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} Md€"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.0f} M€"
    return f"{v / 1_000:.0f} K€"


def _fmt_date(d: str | None) -> str:
    if not d:
        return "N/A"
    try:
        return datetime.fromisoformat(d[:10]).strftime("%d/%m/%Y")
    except Exception:
        return d[:10] if d else "N/A"


def _score_bar(score: int) -> str:
    """Génère une barre de score HTML."""
    color = "#dc2626" if score >= 70 else "#d97706" if score >= 45 else "#6b7280"
    return f"""
    <div style="display:flex; align-items:center; gap:8px;">
      <div style="flex:1; background:#e5e7eb; border-radius:4px; height:10px;">
        <div style="width:{min(score,100)}%; background:{color}; border-radius:4px; height:10px;"></div>
      </div>
      <span style="font-weight:700; color:{color}; min-width:36px;">{score}/100</span>
    </div>
    """


def _financial_table(fh_rows: list[dict]) -> str:
    if not fh_rows:
        return "<p style='color:#9ca3af; font-size:11px;'>Aucun historique financier disponible</p>"

    # Trier par année, garder les 5 dernières
    rows = sorted(fh_rows, key=lambda x: x.get("year", 0), reverse=True)[:5]
    rows = sorted(rows, key=lambda x: x.get("year", 0))

    headers = ["Année", "CA", "Résultat net", "EBITDA", "Source"]
    cells = ""
    for r in rows:
        rev = _fmt_revenue(r.get("revenue_eur"))
        net = _fmt_revenue(r.get("net_income_eur"))
        ebitda = _fmt_revenue(r.get("ebitda_eur"))
        source = r.get("source", "")[:8]
        cells += f"""
        <tr>
          <td>{r.get('year', '?')}</td>
          <td><b>{rev}</b></td>
          <td>{net}</td>
          <td>{ebitda}</td>
          <td style="color:#6b7280;font-size:10px;">{source}</td>
        </tr>
        """

    return f"""
    <table style="width:100%; border-collapse:collapse; font-size:11px;">
      <thead>
        <tr style="background:#f3f4f6;">
          {''.join(f'<th style="text-align:left;padding:3px 6px;color:#374151;">{h}</th>' for h in headers)}
        </tr>
      </thead>
      <tbody>{cells}</tbody>
    </table>
    """


def generate_onepager_html(
    company: dict,
    fi: dict | None,
    ma_score: int,
    ma_signals: list[str],
    financial_history: list[dict],
    email_subject: str = "",
    email_body_excerpt: str = "",
) -> str:
    """Génère le HTML du one-pager."""

    name = company.get("name", "N/A")
    country = company.get("country", "")
    flag = FLAG_EMOJIS.get(country, "🌍")
    revenue = _fmt_revenue(company.get("revenue_eur"))
    sector = company.get("sector") or "N/A"
    creation = _fmt_date(company.get("creation_date"))
    city = company.get("city") or ""
    website = company.get("website") or ""
    employees = company.get("employees")
    emp_str = f"{employees:,}" if employees else "N/A"

    # Score bar
    score_bar = _score_bar(ma_score)
    signals_html = ""
    if ma_signals:
        signals_html = "".join(
            f'<span style="display:inline-block;background:#f3f4f6;border-radius:12px;padding:2px 8px;'
            f'font-size:10px;color:#374151;margin:2px;">{s}</span>'
            for s in ma_signals[:6]
        )

    # FI Section
    fi_html = ""
    if fi and fi.get("enrichment_status") == "done":
        op_type = fi.get("operator_type", "unknown")
        op_label = OPERATOR_LABELS.get(op_type, op_type)
        op_color = COLORS.get(op_type, "#6b7280")
        signal = fi.get("seller_signal_strength", "unknown")
        signal_label = SIGNAL_LABELS.get(signal, signal)
        signal_color = COLORS.get(signal, "#6b7280")
        why_now = fi.get("main_why_now_hypothesis", "") or ""
        full_name = fi.get("full_name", "") or "—"
        current_role = fi.get("current_role", "") or ""
        age = fi.get("estimated_age")
        age_str = f"{age} ans" if age else ""
        years_role = fi.get("years_in_role")
        tenure_str = f"· {years_role} ans en poste" if years_role else ""
        signal_reason = fi.get("seller_signal_reason", "") or ""
        approach = fi.get("recommended_approach_angle", "") or ""
        avoid = fi.get("avoid_in_outreach", "") or ""

        fi_html = f"""
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:4px;">
          <div>
            <div style="font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:0.5px;">Dirigeant principal</div>
            <div style="font-weight:600; font-size:13px; color:#111827;">{full_name}</div>
            <div style="font-size:11px; color:#6b7280;">{current_role}</div>
            <div style="font-size:11px; color:#374151; margin-top:2px;">{age_str} {tenure_str}</div>
          </div>
          <div>
            <div style="font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:0.5px;">Profil</div>
            <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:2px;">
              <span style="background:{op_color}20; color:{op_color}; border:1px solid {op_color}40;
                           border-radius:12px; padding:2px 10px; font-size:11px; font-weight:600;">{op_label}</span>
              <span style="background:{signal_color}20; color:{signal_color}; border:1px solid {signal_color}40;
                           border-radius:12px; padding:2px 10px; font-size:11px; font-weight:600;">{signal_label}</span>
            </div>
          </div>
        </div>
        <div style="margin-top:8px; font-size:11px; color:#374151;">
          <b>Why now :</b> {why_now[:200] + '...' if len(why_now) > 200 else why_now}
        </div>
        {"<div style='margin-top:4px; font-size:11px; color:#6b7280;'><b>Signal :</b> " + signal_reason[:150] + "</div>" if signal_reason and signal_reason != "Données insuffisantes pour évaluer le signal" else ""}
        <div style="margin-top:6px; display:grid; grid-template-columns:1fr 1fr; gap:8px; font-size:10px;">
          <div style="background:#f0fdf4; border-radius:6px; padding:6px 8px;">
            <div style="color:#15803d; font-weight:600; margin-bottom:2px;">✅ Angle d'approche</div>
            <div style="color:#374151;">{approach[:200] + '...' if len(approach) > 200 else approach}</div>
          </div>
          <div style="background:#fef2f2; border-radius:6px; padding:6px 8px;">
            <div style="color:#dc2626; font-weight:600; margin-bottom:2px;">⚠️ À éviter</div>
            <div style="color:#374151;">{avoid[:200] + '...' if len(avoid) > 200 else avoid}</div>
          </div>
        </div>
        """
    else:
        fi_html = '<p style="color:#9ca3af; font-size:11px;">Profil Founder Intelligence non disponible</p>'

    # Email excerpt
    email_html = ""
    if email_subject and email_body_excerpt:
        excerpt = email_body_excerpt[:400] + "..." if len(email_body_excerpt) > 400 else email_body_excerpt
        excerpt_escaped = excerpt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        email_html = f"""
        <div style="background:#f8fafc; border-left:3px solid #3b82f6; padding:8px 12px; margin-top:4px; border-radius:0 6px 6px 0;">
          <div style="font-size:10px; color:#6b7280; margin-bottom:4px;">Objet : <b>{email_subject}</b></div>
          <div style="font-size:10px; color:#374151; line-height:1.5;">{excerpt_escaped}</div>
        </div>
        """

    # Financial table
    fin_table = _financial_table(financial_history)

    generated_at = datetime.utcnow().strftime("%d/%m/%Y à %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<style>
  @page {{
    size: A4;
    margin: 12mm 14mm;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
    font-size: 12px;
    color: #111827;
    line-height: 1.4;
  }}
  .section {{
    margin-bottom: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid #e5e7eb;
  }}
  .section:last-child {{ border-bottom: none; }}
  .section-title {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #6b7280;
    font-weight: 700;
    margin-bottom: 6px;
  }}
  .badge {{
    display: inline-block;
    background: #f3f4f6;
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
    color: #374151;
  }}
  table td, table th {{ padding: 4px 6px; border-bottom: 1px solid #f3f4f6; }}
</style>
</head>
<body>

<!-- HEADER -->
<div class="section" style="display:flex; justify-content:space-between; align-items:flex-start; padding-bottom:10px;">
  <div>
    <div style="font-size:22px; font-weight:800; color:#111827;">{flag} {name}</div>
    <div style="font-size:12px; color:#6b7280; margin-top:2px;">
      {country} · {sector} · Créée le {creation}
      {' · ' + city if city else ''}
      {' · <a href="' + website + '" style="color:#3b82f6;">' + website[:30] + '</a>' if website else ''}
    </div>
  </div>
  <div style="text-align:right;">
    <div style="font-size:10px; color:#9ca3af;">One-pager M&A</div>
    <div style="font-size:10px; color:#9ca3af;">{generated_at}</div>
  </div>
</div>

<!-- KPIs -->
<div class="section" style="display:grid; grid-template-columns: repeat(4, 1fr); gap:8px;">
  <div style="background:#f8fafc; border-radius:8px; padding:8px 10px; text-align:center;">
    <div style="font-size:18px; font-weight:800; color:#1d4ed8;">{revenue}</div>
    <div style="font-size:10px; color:#6b7280;">Chiffre d'affaires</div>
  </div>
  <div style="background:#f8fafc; border-radius:8px; padding:8px 10px; text-align:center;">
    <div style="font-size:18px; font-weight:800; color:#374151;">{emp_str}</div>
    <div style="font-size:10px; color:#6b7280;">Effectifs</div>
  </div>
  <div style="background:#f8fafc; border-radius:8px; padding:8px 10px; text-align:center;">
    <div style="font-size:18px; font-weight:800; color:#374151;">{ma_score}/100</div>
    <div style="font-size:10px; color:#6b7280;">Score M&A</div>
  </div>
  <div style="background:#f8fafc; border-radius:8px; padding:8px 10px; text-align:center;">
    <div style="font-size:18px; font-weight:800; color:#374151;">{len(financial_history)}</div>
    <div style="font-size:10px; color:#6b7280;">Années financières</div>
  </div>
</div>

<!-- MA SCORE -->
<div class="section">
  <div class="section-title">Score M&A</div>
  {score_bar}
  <div style="margin-top:6px;">{signals_html}</div>
</div>

<!-- FOUNDER INTELLIGENCE -->
<div class="section">
  <div class="section-title">Founder Intelligence</div>
  {fi_html}
</div>

<!-- HISTORIQUE FINANCIER -->
<div class="section">
  <div class="section-title">Historique financier</div>
  {fin_table}
</div>

<!-- EMAIL -->
{"<div class='section'><div class='section-title'>Template email suggéré</div>" + email_html + "</div>" if email_html else ""}

<!-- FOOTER -->
<div style="font-size:9px; color:#d1d5db; text-align:center; margin-top:8px;">
  Document confidentiel — généré automatiquement — ne pas diffuser
</div>

</body>
</html>
"""


def generate_onepager_printable_html(
    company: dict,
    fi: dict | None,
    ma_score: int,
    ma_signals: list[str],
    financial_history: list[dict],
    email_subject: str = "",
    email_body_excerpt: str = "",
) -> str:
    """Génère une page HTML imprimable avec bouton d'impression vers PDF."""
    inner = generate_onepager_html(
        company=company,
        fi=fi,
        ma_score=ma_score,
        ma_signals=ma_signals,
        financial_history=financial_history,
        email_subject=email_subject,
        email_body_excerpt=email_body_excerpt,
    )
    # Injecte le bouton d'impression avant </body>
    print_btn = """
<div style="position:fixed; top:10px; right:10px; z-index:9999; display:flex; gap:8px;">
  <button onclick="window.print()"
    style="background:#1d4ed8; color:white; border:none; padding:8px 16px; border-radius:8px;
           font-size:13px; font-weight:600; cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,0.2);">
    🖨️ Imprimer / Enregistrer en PDF
  </button>
  <button onclick="window.close()"
    style="background:#6b7280; color:white; border:none; padding:8px 16px; border-radius:8px;
           font-size:13px; cursor:pointer;">
    ✕ Fermer
  </button>
</div>
<style>
@media print {
  [style*="position:fixed"] { display: none !important; }
}
</style>
"""
    return inner.replace("</body>", print_btn + "</body>")
