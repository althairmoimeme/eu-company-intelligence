"""Génération PDF one-pager M&A via ReportLab (100% Python, pas de dépendance système).

Produit un document A4 avec :
- En-tête entreprise + KPIs
- Score M&A avec barre visuelle
- Profil Founder Intelligence
- Historique financier (table)
- Extrait email suggéré
"""
import io
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.platypus.flowables import HRFlowable

# ── Palette ───────────────────────────────────────────────────────────────────
C_DARK   = colors.HexColor("#111827")
C_GRAY   = colors.HexColor("#6b7280")
C_LIGHT  = colors.HexColor("#f3f4f6")
C_BLUE   = colors.HexColor("#1d4ed8")
C_RED    = colors.HexColor("#dc2626")
C_AMBER  = colors.HexColor("#d97706")
C_GREEN  = colors.HexColor("#15803d")
C_PURPLE = colors.HexColor("#7c3aed")
C_INDIGO = colors.HexColor("#4338ca")
C_WHITE  = colors.white

SIGNAL_COLOR  = {"high": C_RED, "moderate": C_AMBER, "low": C_GRAY, "unknown": C_GRAY}
OP_COLOR = {
    "patrimonial": C_PURPLE,
    "builder":     C_BLUE,
    "operator":    C_INDIGO,
    "founder":     C_GREEN,
    "disengaged":  C_GRAY,
    "unknown":     C_GRAY,
}
OP_LABEL = {
    "patrimonial": "🏛 Patrimonial",
    "builder":     "🚀 Builder",
    "operator":    "⚙ Opérateur",
    "founder":     "💡 Fondateur",
    "disengaged":  "😴 Désengagé",
    "unknown":     "? Inconnu",
}
SIGNAL_LABEL = {
    "high":     "🔴 Signal fort",
    "moderate": "🟡 Modéré",
    "low":      "⚪ Faible",
    "unknown":  "? Inconnu",
}
FLAG = {
    "FR":"🇫🇷","DE":"🇩🇪","GB":"🇬🇧","IT":"🇮🇹","ES":"🇪🇸","NL":"🇳🇱",
    "BE":"🇧🇪","SE":"🇸🇪","NO":"🇳🇴","DK":"🇩🇰","AT":"🇦🇹","CH":"🇨🇭",
    "PL":"🇵🇱","PT":"🇵🇹","RO":"🇷🇴",
}


def _fmt_rev(v) -> str:
    if not v:
        return "N/A"
    if v >= 1e9:
        return f"{v/1e9:.1f} Md€"
    if v >= 1e6:
        return f"{v/1e6:.0f} M€"
    return f"{v/1e3:.0f} K€"


def _fmt_date(d) -> str:
    if not d:
        return "N/A"
    try:
        return datetime.fromisoformat(str(d)[:10]).strftime("%d/%m/%Y")
    except Exception:
        return str(d)[:10]


def _score_bar_table(score: int, width: float = 120) -> Table:
    """Barre de score horizontale sous forme de Table 2 cellules."""
    pct = min(score, 100) / 100
    bar_color = C_RED if score >= 70 else C_AMBER if score >= 45 else C_GRAY
    label_color = bar_color

    filled = width * pct
    empty  = width * (1 - pct)

    data = [[" ", " ", f" {score}/100"]]
    t = Table(data, colWidths=[filled, empty, 30], rowHeights=[10])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), bar_color),
        ("BACKGROUND", (1, 0), (1, 0), C_LIGHT),
        ("TEXTCOLOR",  (2, 0), (2, 0), label_color),
        ("FONTNAME",   (2, 0), (2, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (2, 0), (2, 0), 9),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    return t


def generate_onepager_pdf(
    company: dict,
    fi: dict | None,
    ma_score: int,
    ma_signals: list[str],
    financial_history: list[dict],
    email_subject: str = "",
    email_body_excerpt: str = "",
) -> bytes:
    """Génère le PDF one-pager via ReportLab. Retourne les bytes PDF."""

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm,  bottomMargin=12*mm,
    )

    W = A4[0] - 30*mm  # largeur utile

    styles = getSampleStyleSheet()

    def S(name="Normal", **kw):
        return ParagraphStyle(name, parent=styles["Normal"], **kw)

    title_style  = S("T", fontSize=18, fontName="Helvetica-Bold", textColor=C_DARK, spaceAfter=2)
    sub_style    = S("Sub", fontSize=9, textColor=C_GRAY, spaceAfter=4)
    head_style   = S("H", fontSize=8, fontName="Helvetica-Bold", textColor=C_GRAY,
                     spaceAfter=3, spaceBefore=8, leading=10,
                     borderPad=2)
    body_style   = S("B", fontSize=8, textColor=C_DARK, leading=11)
    small_style  = S("Sm", fontSize=7, textColor=C_GRAY, leading=10)
    label_style  = S("Lb", fontSize=8, fontName="Helvetica-Bold", textColor=C_DARK)
    italic_style = S("It", fontSize=8, textColor=C_GRAY, leading=10)
    mono_style   = S("Mo", fontSize=7, fontName="Courier", textColor=C_DARK, leading=10)

    story = []

    # ── HEADER ────────────────────────────────────────────────────────────────
    name    = company.get("name", "N/A")
    country = company.get("country", "")
    flag    = FLAG.get(country, "🌍")
    rev     = _fmt_rev(company.get("revenue_eur"))
    sector  = company.get("sector") or "N/A"
    created = _fmt_date(company.get("creation_date"))
    city    = company.get("city") or ""
    emp     = company.get("employees")
    emp_str = f"{emp:,}".replace(",", " ") if emp else "N/A"
    website = company.get("website") or ""
    gen_dt  = datetime.utcnow().strftime("%d/%m/%Y %H:%M")

    header_data = [[
        Paragraph(f"<b>{flag} {name}</b>", title_style),
        Paragraph(f"One-pager M&A<br/><font color='#9ca3af'>{gen_dt} UTC</font>",
                  S("Hr", fontSize=7, textColor=C_GRAY, alignment=TA_RIGHT)),
    ]]
    header_t = Table(header_data, colWidths=[W * 0.75, W * 0.25])
    header_t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(header_t)
    story.append(Paragraph(
        f"{country} · {sector} · Créée le {created}"
        + (f" · {city}" if city else "")
        + (f" · <font color='#3b82f6'>{website[:35]}</font>" if website else ""),
        sub_style
    ))
    story.append(HRFlowable(width=W, thickness=1, color=C_LIGHT, spaceAfter=6))

    # ── KPIs ──────────────────────────────────────────────────────────────────
    kpi_data = [[
        Paragraph(f"<b><font color='#1d4ed8' size='14'>{rev}</font></b><br/><font color='#6b7280' size='7'>Chiffre d'affaires</font>", body_style),
        Paragraph(f"<b><font size='14'>{emp_str}</font></b><br/><font color='#6b7280' size='7'>Effectifs</font>", body_style),
        Paragraph(f"<b><font size='14'>{ma_score}/100</font></b><br/><font color='#6b7280' size='7'>Score M&amp;A</font>", body_style),
        Paragraph(f"<b><font size='14'>{len(financial_history)}</font></b><br/><font color='#6b7280' size='7'>Années fin.</font>", body_style),
    ]]
    kpi_t = Table(kpi_data, colWidths=[W/4]*4, rowHeights=[28])
    kpi_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_LIGHT),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [4]),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("INNERGRID",   (0, 0), (-1, -1), 0.5, C_WHITE),
    ]))
    story.append(kpi_t)
    story.append(Spacer(1, 4))

    # ── SCORE M&A ─────────────────────────────────────────────────────────────
    story.append(Paragraph("SCORE M&A", head_style))
    story.append(_score_bar_table(ma_score, width=W - 40))
    if ma_signals:
        signals_txt = "  ·  ".join(ma_signals[:6])
        story.append(Paragraph(f"<font color='#6b7280'>{signals_txt}</font>", small_style))
    story.append(Spacer(1, 4))

    # ── FOUNDER INTELLIGENCE ──────────────────────────────────────────────────
    story.append(Paragraph("FOUNDER INTELLIGENCE", head_style))

    if fi and fi.get("enrichment_status") == "done":
        op_type  = fi.get("operator_type", "unknown")
        signal   = fi.get("seller_signal_strength", "unknown")
        op_lbl   = OP_LABEL.get(op_type, op_type)
        sig_lbl  = SIGNAL_LABEL.get(signal, signal)
        op_col   = OP_COLOR.get(op_type, C_GRAY)
        sig_col  = SIGNAL_COLOR.get(signal, C_GRAY)

        full_nm  = fi.get("full_name") or "—"
        role_txt = fi.get("current_role") or ""
        age      = fi.get("estimated_age")
        tenure   = fi.get("years_in_role")
        why_now  = (fi.get("main_why_now_hypothesis") or "")[:220]
        sig_rsn  = (fi.get("seller_signal_reason") or "")[:150]
        approach = (fi.get("recommended_approach_angle") or "")[:200]
        avoid    = (fi.get("avoid_in_outreach") or "")[:150]

        age_str    = f"{age} ans" if age else ""
        tenure_str = f" · {tenure} ans en poste" if tenure else ""

        fi_data = [
            [
                Paragraph(f"<b>{full_nm}</b><br/><font color='#6b7280'>{role_txt}</font><br/>{age_str}{tenure_str}", body_style),
                Table(
                    [[Paragraph(f"<b><font color='#{op_col.hexval()[1:]}'>{op_lbl}</font></b>", label_style)],
                     [Paragraph(f"<b><font color='#{sig_col.hexval()[1:]}'>{sig_lbl}</font></b>", label_style)]],
                    colWidths=[W * 0.3],
                ),
            ]
        ]
        fi_row = Table(fi_data, colWidths=[W * 0.55, W * 0.45])
        fi_row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING",   (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ]))
        story.append(fi_row)
        story.append(Spacer(1, 3))

        if why_now:
            story.append(Paragraph(f"<b>Why now :</b> {why_now}", body_style))
        if sig_rsn and "insuffisantes" not in sig_rsn:
            story.append(Paragraph(f"<b>Signal :</b> {sig_rsn}", small_style))

        approach_data = [[
            Paragraph(f"<b><font color='#15803d'>✓ Angle</font></b><br/>{approach}", S("Ap", fontSize=7, leading=10, textColor=C_DARK)),
            Paragraph(f"<b><font color='#dc2626'>✗ Éviter</font></b><br/>{avoid}",   S("Av", fontSize=7, leading=10, textColor=C_DARK)),
        ]]
        approach_t = Table(approach_data, colWidths=[W/2 - 3, W/2 - 3])
        approach_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#f0fdf4")),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#fef2f2")),
            ("VALIGN",     (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING",   (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
            ("INNERGRID",   (0, 0), (-1, -1), 0.5, C_WHITE),
        ]))
        story.append(Spacer(1, 3))
        story.append(approach_t)
    else:
        story.append(Paragraph("<font color='#9ca3af'>Profil Founder Intelligence non disponible</font>", italic_style))

    story.append(Spacer(1, 4))

    # ── HISTORIQUE FINANCIER ──────────────────────────────────────────────────
    story.append(Paragraph("HISTORIQUE FINANCIER", head_style))

    if financial_history:
        rows = sorted(financial_history, key=lambda x: x.get("year", 0))[-5:]
        fh_header = [
            Paragraph("<b>Année</b>", S("FH", fontSize=7, fontName="Helvetica-Bold", textColor=C_GRAY)),
            Paragraph("<b>Chiffre d'affaires</b>", S("FH2", fontSize=7, fontName="Helvetica-Bold", textColor=C_GRAY, alignment=TA_RIGHT)),
            Paragraph("<b>Résultat net</b>", S("FH3", fontSize=7, fontName="Helvetica-Bold", textColor=C_GRAY, alignment=TA_RIGHT)),
            Paragraph("<b>EBITDA</b>", S("FH4", fontSize=7, fontName="Helvetica-Bold", textColor=C_GRAY, alignment=TA_RIGHT)),
            Paragraph("<b>Source</b>", S("FH5", fontSize=7, fontName="Helvetica-Bold", textColor=C_GRAY)),
        ]
        fh_data = [fh_header]
        for r in rows:
            fh_data.append([
                Paragraph(str(r.get("year", "?")), body_style),
                Paragraph(f"<b>{_fmt_rev(r.get('revenue_eur'))}</b>",
                          S("R", fontSize=8, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
                Paragraph(_fmt_rev(r.get("net_income_eur")),
                          S("R2", fontSize=8, alignment=TA_RIGHT)),
                Paragraph(_fmt_rev(r.get("ebitda_eur")),
                          S("R3", fontSize=8, alignment=TA_RIGHT)),
                Paragraph(str(r.get("source", ""))[:8],
                          S("R4", fontSize=7, textColor=C_GRAY)),
            ])

        fh_t = Table(fh_data, colWidths=[16*mm, W*0.28, W*0.23, W*0.20, 20*mm])
        fh_t.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), C_LIGHT),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, colors.HexColor("#f9fafb")]),
            ("LINEBELOW",    (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
            ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING",   (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ]))
        story.append(fh_t)
    else:
        story.append(Paragraph("<font color='#9ca3af'>Aucun historique financier disponible</font>", italic_style))

    story.append(Spacer(1, 4))

    # ── EMAIL ─────────────────────────────────────────────────────────────────
    if email_subject and email_body_excerpt:
        story.append(Paragraph("TEMPLATE EMAIL SUGGÉRÉ", head_style))
        excerpt = email_body_excerpt[:500] + ("…" if len(email_body_excerpt) > 500 else "")
        email_data = [[
            Paragraph(
                f"<b>Objet :</b> {email_subject}<br/><br/>{excerpt}",
                S("Em", fontSize=7, leading=10, textColor=C_DARK)
            )
        ]]
        email_t = Table(email_data, colWidths=[W])
        email_t.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (0, 0), colors.HexColor("#f8fafc")),
            ("LINEAFTER",    (0, 0), (0, 0), 3,   C_BLUE),
            ("LEFTPADDING",  (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING",   (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 6),
        ]))
        story.append(email_t)
        story.append(Spacer(1, 4))

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(HRFlowable(width=W, thickness=0.5, color=C_LIGHT, spaceAfter=3))
    story.append(Paragraph(
        "Document confidentiel — généré automatiquement — ne pas diffuser",
        S("Ft", fontSize=7, textColor=C_GRAY, alignment=TA_CENTER)
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()
