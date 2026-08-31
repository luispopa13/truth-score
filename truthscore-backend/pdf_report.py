"""
TruthScore — PDF Report Generator
Generates a professional PDF fact-check report using ReportLab.
"""
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

_VERDICT_COLORS = {
    "TRUE":      colors.HexColor("#22c55e"),
    "FALSE":     colors.HexColor("#ef4444"),
    "UNCERTAIN": colors.HexColor("#f59e0b"),
    "MIXED":     colors.HexColor("#a855f7"),
}
_BG       = colors.HexColor("#0d0d1a")
_CARD     = colors.HexColor("#1a1a2e")
_ACCENT   = colors.HexColor("#7c3aed")
_TEXT     = colors.HexColor("#e5e7eb")
_TEXT2    = colors.HexColor("#9ca3af")
_WHITE    = colors.white


def generate_pdf_report(d: dict) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title="TruthScore Fact-Check Report",
    )

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("h1", fontSize=20, textColor=_WHITE, fontName="Helvetica-Bold",
                         spaceAfter=6, alignment=TA_CENTER)
    H2 = ParagraphStyle("h2", fontSize=13, textColor=_ACCENT, fontName="Helvetica-Bold",
                         spaceBefore=10, spaceAfter=4)
    BODY = ParagraphStyle("body", fontSize=10, textColor=_TEXT, fontName="Helvetica",
                           leading=15, spaceAfter=4)
    SMALL = ParagraphStyle("small", fontSize=8, textColor=_TEXT2, fontName="Helvetica",
                            leading=12)
    CLAIM_STYLE = ParagraphStyle("claim", fontSize=11, textColor=_WHITE,
                                  fontName="Helvetica-Oblique", leading=16,
                                  leftIndent=8, rightIndent=8, spaceAfter=6)

    verdict  = (d.get("verdict") or "UNCERTAIN").upper()
    score    = d.get("score", 50)
    claim    = d.get("claim", "")
    explanation = d.get("explanation", "")
    confidence  = d.get("confidence", "")
    topic       = d.get("topic", "")
    v_color     = _VERDICT_COLORS.get(verdict, _VERDICT_COLORS["UNCERTAIN"])

    story = []

    # Header
    story.append(Paragraph("🔍 TruthScore Fact-Check Report", H1))
    story.append(HRFlowable(width="100%", thickness=1, color=_ACCENT, spaceAfter=10))

    # Claim box
    story.append(Paragraph("Claim", H2))
    claim_table = Table(
        [[Paragraph(f'"{claim}"', CLAIM_STYLE)]],
        colWidths=[170*mm],
    )
    claim_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), _CARD),
        ("ROUNDEDCORNERS", [6]),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
    ]))
    story.append(claim_table)
    story.append(Spacer(1, 8))

    # Verdict + score row
    score_bar_w = 130  # pts
    filled_w    = max(4, int(score_bar_w * score / 100))

    verdict_tbl = Table(
        [[
            Paragraph(f"<b>{verdict}</b>", ParagraphStyle(
                "verd", fontSize=22, textColor=v_color,
                fontName="Helvetica-Bold", alignment=TA_CENTER)),
            Table(
                [
                    [Paragraph(f"<b>{score}/100</b>", ParagraphStyle(
                        "sc", fontSize=18, textColor=_WHITE,
                        fontName="Helvetica-Bold", alignment=TA_CENTER))],
                    [Table([[""]], colWidths=[filled_w],
                           style=TableStyle([("BACKGROUND",(0,0),(-1,-1),v_color),
                                             ("TOPPADDING",(0,0),(-1,-1),3),
                                             ("BOTTOMPADDING",(0,0),(-1,-1),3)]))],
                    [Paragraph(f"Confidence: {confidence} · Topic: {topic}", SMALL)],
                ],
                colWidths=[score_bar_w],
                style=TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),
                                   ("VALIGN",(0,0),(-1,-1),"MIDDLE")]),
            ),
        ]],
        colWidths=[55*mm, 115*mm],
    )
    verdict_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), _CARD),
        ("ALIGN",         (0,0), (0,-1),  "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 14),
        ("BOTTOMPADDING", (0,0), (-1,-1), 14),
        ("LEFTPADDING",   (0,0), (-1,-1), 12),
        ("RIGHTPADDING",  (0,0), (-1,-1), 12),
        ("LINEAFTER",     (0,0), (0,-1),  1, colors.HexColor("#2d2d4e")),
    ]))
    story.append(verdict_tbl)
    story.append(Spacer(1, 12))

    # Explanation
    story.append(Paragraph("Explanation", H2))
    story.append(Paragraph(explanation, BODY))
    story.append(Spacer(1, 8))

    # Sub-claims
    sub_results = d.get("sub_claim_results") or []
    if sub_results:
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#2d2d4e"), spaceAfter=8))
        story.append(Paragraph("Sub-claim Breakdown", H2))
        for i, sr in enumerate(sub_results, 1):
            sv = (sr.get("verdict") or "UNCERTAIN").upper()
            sc = sr.get("score", 50)
            sc_color = _VERDICT_COLORS.get(sv, _VERDICT_COLORS["UNCERTAIN"])
            row = Table(
                [[
                    Paragraph(f"<font color='#{sc_color.hexval()[2:]}'>●</font> <b>{sv}</b> {sc}/100",
                               ParagraphStyle("sv", fontSize=9, textColor=_WHITE,
                                              fontName="Helvetica", leading=13)),
                    Paragraph(sr.get("claim",""), SMALL),
                ]],
                colWidths=[35*mm, 135*mm],
            )
            row.setStyle(TableStyle([
                ("VALIGN", (0,0),(-1,-1),"TOP"),
                ("TOPPADDING", (0,0),(-1,-1),4),
                ("BOTTOMPADDING",(0,0),(-1,-1),4),
                ("LEFTPADDING",(0,0),(-1,-1),6),
            ]))
            story.append(row)
        story.append(Spacer(1, 8))

    # Sources
    supporting   = d.get("supporting", [])
    contradicting= d.get("contradicting", [])

    def _src_table(srcs, label, col):
        if not srcs:
            return
        story.append(Paragraph(label, H2))
        rows = []
        for s in srcs[:10]:
            pub   = s.get("publisher") or s.get("source","")
            title = (s.get("title") or "")[:80]
            url   = s.get("url","")
            link  = f'<link href="{url}" color="#818cf8">{pub}</link>' if url else pub
            rows.append([
                Paragraph(link, ParagraphStyle("pub", fontSize=8, textColor=_ACCENT,
                                                fontName="Helvetica-Bold", leading=11)),
                Paragraph(title, SMALL),
            ])
        tbl = Table(rows, colWidths=[45*mm, 125*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), _CARD),
            ("ROWBACKGROUNDS", (0,0), (-1,-1), [_CARD, colors.HexColor("#16162a")]),
            ("TOPPADDING", (0,0),(-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1),4),
            ("LEFTPADDING",(0,0),(-1,-1),6),
            ("LINEBELOW",(0,0),(-1,-2),0.3,colors.HexColor("#2d2d4e")),
        ]))
        story.append(tbl)
        story.append(Spacer(1,6))

    if supporting or contradicting:
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#2d2d4e"), spaceAfter=8))
        story.append(Paragraph("Evidence Sources", H2))
        _src_table(supporting,    f"✓ Supporting ({len(supporting)})", "#22c55e")
        _src_table(contradicting, f"✗ Contradicting ({len(contradicting)})", "#ef4444")

    # Footer
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=_ACCENT, spaceAfter=6))
    latency_ms = (d.get("latency") or {}).get("total_ms", 0)
    story.append(Paragraph(
        f"Generated by TruthScore · truthscore.app · "
        f"Verified in {latency_ms}ms · "
        f"This report is auto-generated and should be independently verified for critical decisions.",
        SMALL,
    ))

    doc.build(story)
    return buf.getvalue()
