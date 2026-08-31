"""
TruthScore — PDF Report Generator  (professional light-theme layout)
"""
from io import BytesIO
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# ── Palette ───────────────────────────────────────────────────────────
_INK      = colors.HexColor("#111827")   # near-black text
_INK2     = colors.HexColor("#6b7280")   # secondary text
_BORDER   = colors.HexColor("#e5e7eb")   # card borders
_BGLIGHT  = colors.HexColor("#f9fafb")   # card fill
_PURPLE   = colors.HexColor("#7c3aed")   # brand accent
_PURPLE_L = colors.HexColor("#ede9fe")   # light purple tint
_WHITE    = colors.white
_BLACK    = colors.black

_V_COLORS = {
    "TRUE":      (colors.HexColor("#15803d"), colors.HexColor("#dcfce7")),  # text, bg
    "FALSE":     (colors.HexColor("#b91c1c"), colors.HexColor("#fee2e2")),
    "UNCERTAIN": (colors.HexColor("#b45309"), colors.HexColor("#fef9c3")),
    "MIXED":     (colors.HexColor("#6d28d9"), colors.HexColor("#ede9fe")),
}

PAGE_W = A4[0] - 40*mm   # usable width


def _x(s):
    """Escape text for ReportLab's mini-XML paragraph parser."""
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Styles ────────────────────────────────────────────────────────────
def _styles():
    return {
        "title":   ParagraphStyle("title",   fontSize=9,  fontName="Helvetica",
                                   textColor=_WHITE, alignment=TA_LEFT),
        "brand":   ParagraphStyle("brand",   fontSize=14, fontName="Helvetica-Bold",
                                   textColor=_WHITE, alignment=TA_LEFT),
        "section": ParagraphStyle("section", fontSize=9,  fontName="Helvetica-Bold",
                                   textColor=_INK2, spaceAfter=6,
                                   wordWrap='CJK'),
        "body":    ParagraphStyle("body",    fontSize=10, fontName="Helvetica",
                                   textColor=_INK, leading=15, spaceAfter=4),
        "small":   ParagraphStyle("small",   fontSize=8,  fontName="Helvetica",
                                   textColor=_INK2, leading=12),
        "claim":   ParagraphStyle("claim",   fontSize=12, fontName="Helvetica-Bold",
                                   textColor=_INK, leading=18, spaceAfter=0),
        "vword":   ParagraphStyle("vword",   fontSize=26, fontName="Helvetica-Bold",
                                   alignment=TA_CENTER),
        "vscore":  ParagraphStyle("vscore",  fontSize=13, fontName="Helvetica-Bold",
                                   textColor=_INK2, alignment=TA_CENTER),
        "pub":     ParagraphStyle("pub",     fontSize=8,  fontName="Helvetica-Bold",
                                   textColor=_PURPLE),
        "srcttl":  ParagraphStyle("srcttl",  fontSize=9,  fontName="Helvetica",
                                   textColor=_INK, leading=12),
        "footer":  ParagraphStyle("footer",  fontSize=7,  fontName="Helvetica",
                                   textColor=_INK2, alignment=TA_CENTER),
    }


def _card(content_rows, col_widths, padding=10, bg=_BGLIGHT, border=_BORDER):
    tbl = Table(content_rows, colWidths=col_widths)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), bg),
        ("BOX",           (0, 0), (-1, -1), 0.5, border),
        ("ROUNDEDCORNERS", [6]),
        ("TOPPADDING",    (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
        ("LEFTPADDING",   (0, 0), (-1, -1), padding),
        ("RIGHTPADDING",  (0, 0), (-1, -1), padding),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


def generate_pdf_report(d: dict) -> bytes:
    buf    = BytesIO()
    verdict = (d.get("verdict") or "UNCERTAIN").upper()
    score   = int(d.get("score") or 50)
    claim   = d.get("claim") or d.get("text") or ""
    expl    = d.get("explanation") or ""
    conf    = (d.get("confidence") or "").upper()
    topic   = (d.get("topic") or "").title()
    # Per-claim breakdown: single-claim mode uses `sub_claim_results`,
    # paragraph mode (/analyze-text) uses `results` — both are lists of
    # objects carrying claim/score/verdict/confidence/explanation + sources.
    sub_res = d.get("sub_claim_results") or d.get("results") or []
    sup     = d.get("supporting") or []
    con     = d.get("contradicting") or []
    neu     = d.get("neutral_sources") or []
    latency = ((d.get("latency") or {}).get("total_ms") or 0)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    v_text_c, v_bg_c = _V_COLORS.get(verdict, _V_COLORS["UNCERTAIN"])
    S = _styles()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=0, bottomMargin=16*mm,
        title="TruthScore Fact-Check Report",
        author="TruthScore",
    )

    story = []

    # ── Header bar ────────────────────────────────────────────────────
    hdr = Table(
        [[
            Paragraph("<b>TruthScore</b>", S["brand"]),
            Paragraph(f"Fact-Check Report<br/>{now_str}", S["title"]),
        ]],
        colWidths=[PAGE_W * 0.55, PAGE_W * 0.45],
    )
    hdr.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _PURPLE),
        ("TOPPADDING",    (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING",   (0, 0), (-1, -1), 16),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 16),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (1, 0), (1, 0),   "RIGHT"),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 14))

    # ── Claim card ────────────────────────────────────────────────────
    story.append(Paragraph("CLAIM UNDER REVIEW", S["section"]))
    story.append(_card(
        [[Paragraph(f"“{_x(claim)}”", S["claim"])]],
        col_widths=[PAGE_W], padding=14,
    ))
    story.append(Spacer(1, 14))

    # ── Verdict + score side by side ──────────────────────────────────
    story.append(Paragraph("VERDICT", S["section"]))

    v_emoji = {"TRUE": "✓", "FALSE": "✗", "UNCERTAIN": "?", "MIXED": "~"}.get(verdict, "?")
    verdict_cell = Table(
        [
            [Paragraph(f'<font color="#{v_text_c.hexval()[2:]}">{v_emoji} {verdict}</font>', S["vword"])],
            [Paragraph(f"{score} / 100", S["vscore"])],
            [Paragraph(f"Confidence: {conf}", S["small"])],
        ],
        colWidths=[PAGE_W * 0.38],
    )
    verdict_cell.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), v_bg_c),
        ("BOX",           (0, 0), (-1, -1), 0.5, _BORDER),
        ("ROUNDEDCORNERS", [6]),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))

    # Score bar
    bar_w    = PAGE_W * 0.56 - 24
    filled   = max(4, int(bar_w * score / 100))
    meta_rows = [
        [Paragraph("EXPLANATION", S["section"])],
        [Paragraph(_x(expl[:600]) + ("…" if len(expl) > 600 else ""), S["body"])],
    ]
    # Correct answer for false claims — stated plainly so the reader sees the
    # accurate fact, not just that the claim was wrong.
    correct_answer = (d.get("correct_answer") or "").strip()
    if correct_answer:
        meta_rows += [
            [Spacer(1, 4)],
            [Paragraph("CORRECT ANSWER", S["section"])],
            [Paragraph("<b>" + _x(correct_answer[:400]) + "</b>",
                       ParagraphStyle("ca", fontSize=10, fontName="Helvetica-Bold",
                                      textColor=colors.HexColor("#15803d"), leading=14))],
        ]
    meta_rows += [
        [Spacer(1, 6)],
        [Table(
            [[
                Table([[""]], colWidths=[filled],
                      style=TableStyle([("BACKGROUND", (0,0),(-1,-1), v_text_c),
                                        ("TOPPADDING",(0,0),(-1,-1),4),
                                        ("BOTTOMPADDING",(0,0),(-1,-1),4)])),
                Table([[""]], colWidths=[max(1, bar_w - filled)],
                      style=TableStyle([("BACKGROUND",(0,0),(-1,-1),_BORDER),
                                        ("TOPPADDING",(0,0),(-1,-1),4),
                                        ("BOTTOMPADDING",(0,0),(-1,-1),4)])),
            ]],
            colWidths=[filled, max(1, bar_w - filled)],
            style=TableStyle([("TOPPADDING",(0,0),(-1,-1),0),
                               ("BOTTOMPADDING",(0,0),(-1,-1),0),
                               ("LEFTPADDING",(0,0),(-1,-1),0),
                               ("RIGHTPADDING",(0,0),(-1,-1),0)]),
        )],
        [Paragraph(f"Topic: {topic or '—'}  ·  "
                   f"{len(sup)} supporting  ·  {len(con)} contradicting  ·  "
                   f"{len(neu)} neutral", S["small"])],
    ]
    meta_cell = Table(meta_rows, colWidths=[PAGE_W * 0.56])
    meta_cell.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _BGLIGHT),
        ("BOX",           (0, 0), (-1, -1), 0.5, _BORDER),
        ("ROUNDEDCORNERS", [6]),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 14),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))

    verdict_row = Table(
        [[verdict_cell, Spacer(PAGE_W * 0.03, 1), meta_cell]],
        colWidths=[PAGE_W * 0.38, PAGE_W * 0.03, PAGE_W * 0.59],
    )
    verdict_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0,0),(-1,-1),0),
        ("BOTTOMPADDING", (0,0),(-1,-1),0),
        ("LEFTPADDING",   (0,0),(-1,-1),0),
        ("RIGHTPADDING",  (0,0),(-1,-1),0),
    ]))
    story.append(verdict_row)
    story.append(Spacer(1, 14))

    # ── Sub-claims ── one full-width card per claim (mirrors the UI) ─────
    if sub_res:
        story.append(Paragraph(f"PER-CLAIM BREAKDOWN  ({len(sub_res)})", S["section"]))
        story.append(HRFlowable(width=PAGE_W, thickness=0.5, color=_BORDER, spaceAfter=8))

        innerw = PAGE_W - 34   # content width inside card (padding 14+14 + 3 accent + slack)

        def _src_line(s, positive):
            col  = "#15803d" if positive else "#b91c1c"
            mark = "✓" if positive else "✗"
            pub   = _x((s.get("publisher") or s.get("source") or "Source"))[:48]
            title = _x((s.get("title") or ""))[:90]
            url   = s.get("url") or ""
            snip  = _x((s.get("snippet") or ""))[:120]
            pubtxt = (f'<link href="{url}" color="{col}"><b>{pub}</b></link>'
                      if url else f'<b>{pub}</b>')
            txt = f'<font color="{col}">{mark}</font> {pubtxt}'
            if title:
                txt += f' — {title}'
            if snip:
                txt += f'<br/><font color="#9ca3af">{snip}</font>'
            return Paragraph(txt, ParagraphStyle(
                "sl", fontSize=8.5, fontName="Helvetica",
                textColor=_INK, leading=12, spaceAfter=4, leftIndent=4))

        for i, sr in enumerate(sub_res, 1):
            sv   = (sr.get("verdict") or "UNCERTAIN").upper()
            sc   = int(sr.get("score") or 50)
            conf = (sr.get("confidence") or "").upper()
            sv_tc, sv_bg = _V_COLORS.get(sv, _V_COLORS["UNCERTAIN"])
            hexc = sv_tc.hexval()[2:]
            sr_sup = sr.get("supporting") or sr.get("supporting_sources") or []
            sr_con = sr.get("contradicting") or sr.get("contradicting_sources") or []
            sr_neu = sr.get("neutral_sources") or []
            sr_expl = _x((sr.get("explanation") or "").strip())

            rows = []

            # Header: CLAIM #i  |  VERDICT · score · confidence
            meta_txt = f"{sv} · {sc}/100" + (f" · {conf}" if conf else "")
            rows.append([Table(
                [[
                    Paragraph(f'<font color="#{hexc}"><b>CLAIM #{i}</b></font>',
                              ParagraphStyle("chl", fontSize=9, fontName="Helvetica-Bold",
                                             textColor=sv_tc)),
                    Paragraph(f'<font color="#{hexc}"><b>{meta_txt}</b></font>',
                              ParagraphStyle("chr", fontSize=9, fontName="Helvetica-Bold",
                                             textColor=sv_tc, alignment=TA_RIGHT)),
                ]],
                colWidths=[innerw * 0.28, innerw * 0.72],
                style=TableStyle([("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),
                                  ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                                  ("VALIGN",(0,0),(-1,-1),"MIDDLE")]),
            )])

            # Claim text
            rows.append([Paragraph(f"<b>{_x(sr.get('claim',''))}</b>",
                         ParagraphStyle("scl", fontSize=10.5, fontName="Helvetica-Bold",
                                        textColor=_INK, leading=14, spaceBefore=5))])

            # Explanation
            if sr_expl:
                rows.append([Paragraph(sr_expl[:500],
                             ParagraphStyle("sex", fontSize=9, fontName="Helvetica",
                                            textColor=_INK2, leading=13, spaceBefore=3))])

            # Score bar (full width)
            bw   = innerw
            bfil = max(3, int(bw * sc / 100))
            rows.append([Spacer(1, 4)])
            rows.append([Table(
                [[
                    Table([[""]], colWidths=[bfil],
                          style=TableStyle([("BACKGROUND",(0,0),(-1,-1),sv_tc),
                                            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)])),
                    Table([[""]], colWidths=[max(1, bw - bfil)],
                          style=TableStyle([("BACKGROUND",(0,0),(-1,-1),_BORDER),
                                            ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)])),
                ]], colWidths=[bfil, max(1, bw - bfil)],
                style=TableStyle([("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),
                                  ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0)]),
            )])

            # Supporting sources
            if sr_sup:
                rows.append([Paragraph(
                    f'<font color="#15803d"><b>✓ SUPPORTING SOURCES ({len(sr_sup)})</b></font>',
                    ParagraphStyle("ssh", fontSize=8, fontName="Helvetica-Bold",
                                   textColor=colors.HexColor("#15803d"), spaceBefore=9, spaceAfter=3))])
                for s in sr_sup[:4]:
                    rows.append([_src_line(s, True)])

            # Contradicting sources
            if sr_con:
                rows.append([Paragraph(
                    f'<font color="#b91c1c"><b>✗ CONTRADICTING SOURCES ({len(sr_con)})</b></font>',
                    ParagraphStyle("csh", fontSize=8, fontName="Helvetica-Bold",
                                   textColor=colors.HexColor("#b91c1c"), spaceBefore=9, spaceAfter=3))])
                for s in sr_con[:4]:
                    rows.append([_src_line(s, False)])

            # Neutral fallback when no directional evidence
            if not sr_sup and not sr_con:
                if sr_neu:
                    rows.append([Paragraph(
                        f'<font color="#6b7280"><b>— RELEVANT SOURCES ({len(sr_neu)})</b></font>',
                        ParagraphStyle("nsh", fontSize=8, fontName="Helvetica-Bold",
                                       textColor=_INK2, spaceBefore=9, spaceAfter=3))])
                    for s in sr_neu[:3]:
                        rows.append([_src_line(s, True)])
                else:
                    rows.append([Paragraph("No direct sources found for this claim.",
                                 ParagraphStyle("nosrc", fontSize=8, fontName="Helvetica-Oblique",
                                                textColor=_INK2, spaceBefore=8))])

            card = Table(rows, colWidths=[PAGE_W])
            card.setStyle(TableStyle([
                ("BACKGROUND",   (0,0),(-1,-1), _BGLIGHT),
                ("BOX",          (0,0),(-1,-1), 0.5, _BORDER),
                ("LINEBEFORE",   (0,0),(0,-1),  3, sv_tc),   # colored left accent
                ("ROUNDEDCORNERS",[6]),
                ("TOPPADDING",   (0,0),(-1,-1), 12),
                ("BOTTOMPADDING",(0,0),(-1,-1), 12),
                ("LEFTPADDING",  (0,0),(-1,-1), 14),
                ("RIGHTPADDING", (0,0),(-1,-1), 14),
                ("VALIGN",       (0,0),(-1,-1), "TOP"),
            ]))
            story.append(card)
            story.append(Spacer(1, 10))
        story.append(Spacer(1, 4))


    # ── Sources ───────────────────────────────────────────────────────
    def _sources_section(srcs, label, badge_color, badge_bg):
        if not srcs:
            return
        rows = []
        for s in srcs[:8]:
            pub   = _x((s.get("publisher") or s.get("source") or "Source"))[:40]
            title = _x((s.get("title") or ""))[:90]
            url   = s.get("url") or ""
            snip  = _x((s.get("snippet") or ""))[:100]
            pub_link = f'<link href="{url}">{pub}</link>' if url else pub
            rows.append([
                Table(
                    [[Paragraph(label, ParagraphStyle("badge", fontSize=7,
                                fontName="Helvetica-Bold", textColor=badge_color,
                                alignment=TA_CENTER))]],
                    colWidths=[16*mm],
                    style=TableStyle([
                        ("BACKGROUND",   (0,0),(-1,-1), badge_bg),
                        ("BOX",          (0,0),(-1,-1), 0.3, badge_color),
                        ("ROUNDEDCORNERS",[3]),
                        ("TOPPADDING",   (0,0),(-1,-1),2),
                        ("BOTTOMPADDING",(0,0),(-1,-1),2),
                    ])
                ),
                Paragraph(pub_link, S["pub"]),
                Paragraph(title + (f'<br/><font color="#9ca3af">{snip}</font>' if snip else ""),
                          S["srcttl"]),
            ])

        tbl = Table(rows, colWidths=[18*mm, 36*mm, PAGE_W - 54*mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",   (0,0),(-1,-1), _WHITE),
            ("BOX",          (0,0),(-1,-1), 0.5, _BORDER),
            ("LINEBELOW",    (0,0),(-1,-2), 0.3, _BORDER),
            ("TOPPADDING",   (0,0),(-1,-1), 7),
            ("BOTTOMPADDING",(0,0),(-1,-1), 7),
            ("LEFTPADDING",  (0,0),(-1,-1), 8),
            ("RIGHTPADDING", (0,0),(-1,-1), 8),
            ("VALIGN",       (0,0),(-1,-1), "TOP"),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 8))

    # Aggregate evidence — only for single claims (per-claim cards already
    # show their own sources, so this would be redundant when sub_res exists).
    if (sup or con or neu) and not sub_res:
        story.append(Paragraph("EVIDENCE SOURCES", S["section"]))
        _sources_section(sup, "SUPPORTS",     colors.HexColor("#15803d"), colors.HexColor("#dcfce7"))
        _sources_section(con, "CONTRADICTS",  colors.HexColor("#b91c1c"), colors.HexColor("#fee2e2"))
        _sources_section(neu[:4], "NEUTRAL",  colors.HexColor("#6b7280"), colors.HexColor("#f3f4f6"))
        story.append(Spacer(1, 6))

    # ── Footer ────────────────────────────────────────────────────────
    story.append(HRFlowable(width=PAGE_W, thickness=0.5, color=_BORDER, spaceAfter=6))
    story.append(Paragraph(
        f"Generated by <b>TruthScore</b> · truthscore.app · {now_str}"
        + (f" · Verified in {latency}ms" if latency else "")
        + " · Auto-generated — independently verify before using in critical decisions.",
        S["footer"],
    ))

    doc.build(story)
    return buf.getvalue()
