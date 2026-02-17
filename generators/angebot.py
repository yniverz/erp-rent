"""
PDF generator for Angebot (Quote / Offer).

Produces a professional quote PDF with:
- Header: Logo, sender, recipient, meta (date, validity, reference)
- Positions table: Pos | Bezeichnung | Menge | Tage | EP/Tag | Gesamt
  - Bundles: shown with pauschal price, sub-items indented without price
- Discount, subtotal, optional MwSt, total
- Notes, payment terms
- Footer: business info, contact, bank, tax number
"""
from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO

from reportlab.lib import colors
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from generators.pdf_base import (
    _base_styles, HLine, build_base_doc,
    _draw_header, _draw_footer,
    CONTENT_W, CLR_TABLE_HEADER_BG, CLR_GREY_DARK, CLR_BLACK,
    PAGE_W, PAGE_H,
    fmt_eur, fmt_percent,
)


def build_angebot_pdf(
    *,
    # Business / issuer
    issuer_name: str,
    issuer_address: list[str],
    contact_lines: list[str],
    bank_lines: list[str],
    tax_number: str | None = None,
    tax_mode: str = "kleinunternehmer",  # 'kleinunternehmer' or 'regular'
    logo_path: str | None = None,

    # Recipient
    recipient_lines: list[str],

    # Document meta
    reference_number: str,
    angebot_datum: str | None = None,
    gueltig_bis: str | None = None,
    quote_validity_days: int = 14,

    # Rental period
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    rental_days: int = 1,

    # Positions: list of dicts
    # Each: { 'name': str, 'quantity': int, 'price_per_day': float, 'total': float,
    #          'is_bundle': bool, 'bundle_components': [{'name': str, 'quantity': int}] }
    positions: list[dict],

    # Discount
    discount_percent: float = 0,
    discount_label: str | None = None,
    discount_amount: float = 0,

    # Totals
    subtotal: float = 0,
    total: float = 0,

    # Payment
    payment_terms_days: int = 14,

    # Notes
    notes: str | None = None,

    # AGB (Terms & Conditions) – basic markdown
    terms_and_conditions_text: str | None = None,
) -> bytes:
    """Build and return the Angebot PDF bytes."""
    buf = BytesIO()
    styles = _base_styles()

    if not angebot_datum:
        angebot_datum = date.today().strftime("%d.%m.%Y")
    if not gueltig_bis:
        gueltig_bis = (date.today() + timedelta(days=quote_validity_days)).strftime("%d.%m.%Y")

    zeitraum = "—"
    if start_date_str and end_date_str:
        zeitraum = f"{start_date_str} – {end_date_str}"

    # Meta lines for header
    meta_lines = [
        ("Angebot-Nr.:", reference_number),
        ("Datum:", angebot_datum),
        ("Gültig bis:", gueltig_bis),
    ]
    if start_date_str and end_date_str:
        meta_lines.append(("Mietzeitraum:", zeitraum))
        meta_lines.append(("Miettage:", str(rental_days)))

    def on_page(canvas, doc):
        _draw_header(canvas, doc,
                     issuer_name=issuer_name,
                     issuer_address=issuer_address,
                     recipient_lines=recipient_lines,
                     meta_lines=meta_lines,
                     logo_path=logo_path)
        _draw_footer(canvas, doc,
                     issuer_name=issuer_name,
                     issuer_address=issuer_address,
                     contact_lines=contact_lines,
                     bank_lines=bank_lines,
                     tax_number=tax_number)

    doc, cw = build_base_doc(buf, title="Angebot", author=issuer_name,
                             on_page_callback=on_page)

    # Register AGB two-column template (used via NextPageTemplate if AGB present)
    if terms_and_conditions_text:
        doc.addPageTemplates([_build_agb_page_template()])

    story: list = []

    # ── Title ──
    story.append(Paragraph("Angebot", styles["title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Sehr geehrte Damen und Herren,", styles["normal"]))
    story.append(Paragraph("vielen Dank für Ihre Anfrage. Wir unterbreiten Ihnen folgendes Angebot:", styles["normal"]))
    story.append(Spacer(1, 10))

    # ── Positions table ──
    col_widths = [
        22,           # Pos
        cw - 22 - 30 - 30 - 50 - 58,  # Bezeichnung (flexible)
        30,           # Menge
        30,           # Tage
        50,           # EP/Tag
        58,           # Gesamt
    ]

    header_row = [
        Paragraph("Pos", styles["table_header"]),
        Paragraph("Bezeichnung", styles["table_header"]),
        Paragraph("Menge", styles["table_header"]),
        Paragraph("Tage", styles["table_header"]),
        Paragraph("EP/Tag", styles["table_header"]),
        Paragraph("Gesamt", styles["table_header"]),
    ]
    table_data = [header_row]

    pos_nr = 1
    for item in positions:
        if item.get("is_bundle"):
            # Bundle header row – price only as pauschal in Gesamt
            bundle_total = item["total"]
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(f"<b>{item['name']}</b>", styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph(str(rental_days), styles["table_cell"]),
                Paragraph("pauschal", styles["table_cell"]),
                Paragraph(f"<b>{fmt_eur(bundle_total)}</b>", styles["table_cell_right"]),
            ])
            # Sub-items indented, no price
            for comp in item.get("bundle_components", []):
                table_data.append([
                    Paragraph("", styles["table_cell"]),
                    Paragraph(f"↳ {comp['name']}", styles["table_cell_indent"]),
                    Paragraph(str(comp["quantity"]), styles["table_cell_indent"]),
                    Paragraph("", styles["table_cell"]),
                    Paragraph("", styles["table_cell"]),
                    Paragraph("", styles["table_cell"]),
                ])
        else:
            # Regular item
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(item["name"], styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph(str(rental_days), styles["table_cell"]),
                Paragraph(fmt_eur(item["price_per_day"]), styles["table_cell_right"]),
                Paragraph(fmt_eur(item["total"]), styles["table_cell_right"]),
            ])
        pos_nr += 1

    table = Table(table_data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        # Grid
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        # Padding
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)
    story.append(Spacer(1, 8))

    # ── Totals block (right-aligned) ──
    summary_col_w = [cw - 120, 120]

    summary_data = []
    summary_data.append([
        Paragraph("Zwischensumme", styles["right"]),
        Paragraph(fmt_eur(subtotal), styles["right_bold"]),
    ])

    if discount_percent > 0:
        dl = "Rabatt"
        if discount_label:
            dl += f" – {discount_label}"
        dl += f" ({fmt_percent(discount_percent)})"
        summary_data.append([
            Paragraph(dl, styles["right"]),
            Paragraph(f"– {fmt_eur(discount_amount)}", styles["right"]),
        ])
        brutto = subtotal - discount_amount
    else:
        brutto = subtotal

    if tax_mode == "regular":
        netto = round(brutto / 1.19, 2)
        mwst = round(brutto - netto, 2)
        summary_data.append([
            Paragraph("<b>Gesamtbetrag</b>", styles["right"]),
            Paragraph(f"<b>{fmt_eur(brutto)}</b>", styles["right"]),
        ])
        summary_data.append([
            Paragraph("darin enthaltene 19 % MwSt.", styles["right"]),
            Paragraph(fmt_eur(mwst), styles["right"]),
        ])
    else:
        summary_data.append([
            Paragraph("<b>Gesamtbetrag</b>", styles["right"]),
            Paragraph(f"<b>{fmt_eur(brutto)}</b>", styles["right"]),
        ])

    total_row_idx = -2 if tax_mode == 'regular' else -1
    summary_table = Table(summary_data, colWidths=summary_col_w, hAlign="RIGHT")
    summary_table.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEABOVE", (0, total_row_idx), (-1, total_row_idx), 0.8, CLR_BLACK),
    ]))
    story.append(summary_table)

    if tax_mode == "kleinunternehmer":
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.",
            styles["small"]
        ))

    story.append(Spacer(1, 14))

    # ── Notes ──
    if notes:
        story.append(Paragraph("<b>Bemerkungen:</b>", styles["normal"]))
        for line in notes.strip().split("\n"):
            story.append(Paragraph(line, styles["normal"]))
        story.append(Spacer(1, 8))

    # ── Payment & validity ──
    story.append(HLine(width=cw))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Dieses Angebot ist gültig bis zum {gueltig_bis}.",
        styles["normal"]
    ))
    story.append(Paragraph(
        f"Zahlungsziel: {payment_terms_days} Tage nach Rechnungsstellung.",
        styles["normal"]
    ))
    story.append(Spacer(1, 14))
    if terms_and_conditions_text:
        story.append(Paragraph(
            "Es gelten unsere Allgemeinen Geschäftsbedingungen (siehe Anlage).",
            styles["normal"]
        ))
        story.append(Spacer(1, 8))

    story.append(Paragraph("Wir freuen uns auf Ihre Rückmeldung.", styles["normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Mit freundlichen Grüßen", styles["normal"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(issuer_name, styles["bold"]))

    # ── AGB appendix ──
    if terms_and_conditions_text:
        from reportlab.platypus import NextPageTemplate
        story.append(NextPageTemplate("agb"))
        from reportlab.platypus import PageBreak
        story.append(PageBreak())
        _render_agb_markdown(story, terms_and_conditions_text, styles)

    doc.build(story)
    return buf.getvalue()


def _build_agb_page_template():
    """Create a two-column page template with reduced margins for AGB pages."""
    from reportlab.platypus import Frame, PageTemplate
    from reportlab.lib.units import mm

    agb_margin_left = 12 * mm
    agb_margin_right = 12 * mm
    agb_margin_top = 12 * mm
    agb_margin_bottom = 14 * mm
    col_gap = 8 * mm

    content_w = PAGE_W - agb_margin_left - agb_margin_right
    col_w = (content_w - col_gap) / 2
    frame_h = PAGE_H - agb_margin_top - agb_margin_bottom

    frame_left = Frame(
        agb_margin_left, agb_margin_bottom,
        col_w, frame_h,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="agb_left",
    )
    frame_right = Frame(
        agb_margin_left + col_w + col_gap, agb_margin_bottom,
        col_w, frame_h,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="agb_right",
    )

    def _draw_agb_page(canvas, doc):
        """Minimal page callback – just page number, no header/footer."""
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(CLR_GREY_DARK)
        canvas.drawRightString(PAGE_W - agb_margin_right, 6 * mm,
                               f"Seite {canvas.getPageNumber()}")
        canvas.restoreState()

    return PageTemplate(id="agb", frames=[frame_left, frame_right],
                        onPage=_draw_agb_page)


def _render_agb_markdown(story: list, text: str, styles: dict):
    """Parse basic markdown (# headings, paragraphs) into reportlab flowables."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Spacer, Paragraph

    agb_title = ParagraphStyle("AGBTitle", parent=styles["base"], fontName="Helvetica-Bold",
                                fontSize=10, leading=12, spaceAfter=6, spaceBefore=0)
    agb_h1 = ParagraphStyle("AGBH1", parent=styles["base"], fontName="Helvetica-Bold",
                             fontSize=8.5, leading=10.5, spaceAfter=2, spaceBefore=6)
    agb_h2 = ParagraphStyle("AGBH2", parent=styles["base"], fontName="Helvetica-Bold",
                             fontSize=7.5, leading=9.5, spaceAfter=2, spaceBefore=5)
    agb_h3 = ParagraphStyle("AGBH3", parent=styles["base"], fontName="Helvetica-Bold",
                             fontSize=7, leading=9, spaceAfter=1, spaceBefore=4)
    agb_body = ParagraphStyle("AGBBody", parent=styles["base"], fontSize=6.5, leading=8.5,
                               spaceAfter=2)

    story.append(Paragraph("Anlage: Allgemeine Geschäftsbedingungen", agb_title))
    story.append(Spacer(1, 4))

    lines = text.split("\n")
    paragraph_buf: list[str] = []

    def flush_paragraph():
        if paragraph_buf:
            body = "<br/>".join(paragraph_buf)
            story.append(Paragraph(body, agb_body))
            paragraph_buf.clear()

    for line in lines:
        stripped = line.strip()

        # Empty line → flush paragraph
        if not stripped:
            flush_paragraph()
            continue

        # Headings
        if stripped.startswith("### "):
            flush_paragraph()
            story.append(Paragraph(stripped[4:], agb_h3))
        elif stripped.startswith("## "):
            flush_paragraph()
            story.append(Paragraph(stripped[3:], agb_h2))
        elif stripped.startswith("# "):
            flush_paragraph()
            story.append(Paragraph(stripped[2:], agb_h1))
        else:
            # Bold: **text** → <b>text</b>
            import re
            formatted = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', stripped)
            paragraph_buf.append(formatted)

    flush_paragraph()
