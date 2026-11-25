"""
PDF generator for the Unverbindliche Preisübersicht (non-binding price overview).

This is deliberately NOT an Angebot (offer): the binding offer is created in the
external accounting tool. The document only gives the customer an early overview
and states that it does not become part of any contract.

Produces a PDF with:
- Header: Logo, sender, recipient, meta (date, reference)
- Positions table: Pos | Bezeichnung | Menge | Tage | EP/Tag | Gesamt
  - Bundles: shown with pauschal price, sub-items indented without price
- Discount, subtotal, optional MwSt, estimated total
- Gesamtwert (replacement value incl. Lieferumfang) of the rented goods
- Notes, non-binding disclaimer
- Footer: business info, contact, bank, tax number
"""
from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from generators.pdf_base import (
    _base_styles, HLine, build_base_doc,
    _draw_header, _draw_footer,
    NumberedCanvas,
    CONTENT_W, CLR_TABLE_HEADER_BG, CLR_GREY_DARK, CLR_BLACK,
    PAGE_W, PAGE_H,
    fmt_eur, fmt_percent,
)


def build_preisuebersicht_pdf(
    *,
    # Business / issuer
    issuer_name: str,
    issuer_address: list[str],
    contact_lines: list[str],
    tax_number: str | None = None,
    vat_id: str | None = None,
    tax_mode: str = "kleinunternehmer",  # 'kleinunternehmer' or 'regular'
    tax_rate: float = 19.0,
    prices_are_net: bool = False,  # If True (regular mode only): stored prices are NET, VAT added on top
    logo_path: str | None = None,

    # Recipient
    recipient_lines: list[str],

    # Document meta
    reference_number: str,
    datum: str | None = None,

    # Rental period
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    rental_days: int = 1,
    is_pauschale: bool = False,
    leistungszeitraum: str | None = None,

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

    # Replacement value of all rented goods incl. Lieferumfang (as in the Wertaufstellung)
    value_total: float | None = None,
    value_incomplete: bool = False,  # some lines have no value -> shown as "mind."
    value_basis: str = "brutto",  # 'netto' (regelbesteuert) or 'brutto' (Kleinunternehmer)

    # Notes
    notes: str | None = None,

    # Appendix documents (AGB, Mietbedingungen, …) as (title, basic markdown) pairs
    attachments: list[tuple[str, str]] | None = None,
) -> bytes:
    """Build and return the Preisübersicht PDF bytes."""
    buf = BytesIO()
    styles = _base_styles()

    if not datum:
        datum = date.today().strftime("%d.%m.%Y")

    zeitraum = "—"
    if start_date_str and end_date_str:
        zeitraum = f"{start_date_str} – {end_date_str}"

    # Meta lines for header – no validity date, since nothing is offered
    meta_lines = [
        ("Referenz:", reference_number),
        ("Datum:", datum),
    ]
    if start_date_str and end_date_str:
        meta_lines.append(("Mietzeitraum:", zeitraum))
        if not is_pauschale:
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
                     tax_number=tax_number,
                     vat_id=vat_id)

    doc, cw = build_base_doc(buf, title="Unverbindliche Preisübersicht", author=issuer_name,
                             on_page_callback=on_page)

    attachments = [(t, c) for t, c in (attachments or []) if (c or "").strip()]

    # Register the two-column appendix template (used via NextPageTemplate)
    if attachments:
        doc.addPageTemplates([_build_appendix_page_template(page_numbers=False)])

    story: list = []

    # ── Title ──
    story.append(Paragraph("Unverbindliche Preisübersicht", styles["title"]))
    story.append(Paragraph("Kein Angebot – nur zur Vorabinformation", styles["small"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Sehr geehrte Damen und Herren,", styles["normal"]))
    intro = ("vielen Dank für Ihre Anfrage. Vorab erhalten Sie eine unverbindliche Übersicht "
             "der angefragten Leistungen und der voraussichtlichen Kosten")
    if is_pauschale:
        # Stated once here instead of "(Nutzung …)" / "pauschal" on every line,
        # which also reads wrong on custom lines like Lieferung / Abholung
        period = f" ({leistungszeitraum})" if leistungszeitraum else ""
        intro += (". Alle Preise sind Pauschalpreise; die Miete gilt jeweils für den "
                  f"gesamten Mietzeitraum{period}.")
    else:
        intro += ":"
    story.append(Paragraph(intro, styles["normal"]))
    story.append(Spacer(1, 10))

    # ── Positions table ──
    is_regular = (tax_mode == "regular")
    tax_factor = 1 + tax_rate / 100

    # Split: headings stay inline, optional positions go to a separate section
    main_positions = [p for p in positions if not p.get("is_optional")]
    optional_positions = [p for p in positions if p.get("is_optional") and not p.get("is_heading")]
    billable = [p for p in main_positions if not p.get("is_heading")]

    # ── Pre-compute netto values (over billable positions only) ──
    if is_regular and prices_are_net:
        # Stored prices ARE net – tax is added on top
        netto_subtotal = round(subtotal, 2)
        netto_discount = round(discount_amount, 2) if discount_percent > 0 else 0.0
        netto_total = round(netto_subtotal - netto_discount, 2)
        brutto_total = round(netto_total * tax_factor, 2)
        mwst = round(brutto_total - netto_total, 2)
        position_nettos = [round(item["total"], 2) for item in billable]
    elif is_regular:
        import math as _math

        # Legacy: stored prices are brutto; derive netto rückwärts
        brutto_total = subtotal - (discount_amount if discount_percent > 0 else 0)
        netto_total = round(brutto_total / tax_factor, 2)
        mwst = round(brutto_total - netto_total, 2)

        netto_subtotal = round(subtotal / tax_factor, 2)
        netto_discount = round(netto_subtotal - netto_total, 2) if discount_percent > 0 else 0.0

        position_bruttos = [item["total"] for item in billable]
        brutto_sum = sum(position_bruttos) or 1  # avoid div-by-zero
        raw_nettos = [netto_subtotal * (pb / brutto_sum) for pb in position_bruttos]

        floored = [_math.floor(r * 100) / 100 for r in raw_nettos]
        deficit_cents = round((netto_subtotal - sum(floored)) * 100)
        idx_by_remainder = sorted(
            range(len(raw_nettos)),
            key=lambda i: -(raw_nettos[i] * 100 - _math.floor(raw_nettos[i] * 100)),
        )
        position_nettos = list(floored)
        for k in range(max(0, deficit_cents)):
            position_nettos[idx_by_remainder[k]] += 0.01
        position_nettos = [round(n, 2) for n in position_nettos]
    else:
        position_nettos = None  # not used in Kleinunternehmer mode

    # Pos / Menge wide enough that their bold headers don't wrap (4pt padding each side)
    col_widths = [
        26,           # Pos
        cw - 26 - 36 - 30 - 50 - 58,  # Bezeichnung (flexible)
        36,           # Menge
        30,           # Tage
        50,           # EP/Tag
        58,           # Gesamt
    ]

    def _visible_cols(rows):
        """Pauschale: drop the Tage and EP/Tag columns – the flat price is explained in the intro."""
        return [[r[i] for i in (0, 1, 2, 5)] for r in rows] if is_pauschale else rows

    if is_pauschale:
        col_widths = [26, cw - 26 - 36 - 70, 36, 70]

    header_row = [
        Paragraph("Pos", styles["table_header"]),
        Paragraph("Bezeichnung", styles["table_header"]),
        Paragraph("Menge", styles["table_header"]),
        Paragraph("Tage" if not is_pauschale else "", styles["table_header"]),
        Paragraph("EP/Tag" if not is_pauschale else "Preis", styles["table_header"]),
        Paragraph("Gesamt", styles["table_header"]),
    ]
    table_data = [header_row]
    heading_row_idxs = []

    pos_nr = 1
    alloc_i = 0  # index into position_nettos (billable positions only)
    for item in main_positions:
        if item.get("is_heading"):
            heading_row_idxs.append(len(table_data))
            table_data.append([
                Paragraph("", styles["table_cell"]),
                Paragraph(f"<b>{item['name']}</b>", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
            ])
            continue
        pos_idx = alloc_i
        alloc_i += 1
        if item.get("is_bundle"):
            # Bundle header row – price only as pauschal in Gesamt
            display_total = position_nettos[pos_idx] if is_regular else item["total"]
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(f"<b>{item['name']}</b>", styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph("" if is_pauschale else str(rental_days), styles["table_cell"]),
                Paragraph("pauschal", styles["table_cell"]),
                Paragraph(f"<b>{fmt_eur(display_total)}</b>", styles["table_cell_right"]),
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
            if is_regular and prices_are_net:
                display_ppd = round(item["price_per_day"], 2)
                display_total = position_nettos[pos_idx]
            elif is_regular:
                display_ppd = round(item["price_per_day"] / tax_factor, 2)
                display_total = position_nettos[pos_idx]
            else:
                display_ppd = item["price_per_day"]
                display_total = item["total"]

            if is_pauschale:
                table_data.append([
                    Paragraph(str(pos_nr), styles["table_cell"]),
                    Paragraph(item["name"], styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph("", styles["table_cell"]),
                    Paragraph("pauschal", styles["table_cell"]),
                    Paragraph(fmt_eur(display_total), styles["table_cell_right"]),
                ])
            elif item.get("is_one_off"):
                # Custom line (Lieferung, Abholung …): one-off amount, not per day
                table_data.append([
                    Paragraph(str(pos_nr), styles["table_cell"]),
                    Paragraph(item["name"], styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph("", styles["table_cell"]),
                    Paragraph("einmalig", styles["table_cell_right"]),
                    Paragraph(fmt_eur(display_total), styles["table_cell_right"]),
                ])
            else:
                table_data.append([
                    Paragraph(str(pos_nr), styles["table_cell"]),
                    Paragraph(item["name"], styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph(str(rental_days), styles["table_cell"]),
                    Paragraph(fmt_eur(display_ppd), styles["table_cell_right"]),
                    Paragraph(fmt_eur(display_total), styles["table_cell_right"]),
                ])
        pos_nr += 1

    table = Table(_visible_cols(table_data), colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    table_style_cmds = [
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
    ]
    for ridx in heading_row_idxs:
        table_style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), CLR_TABLE_HEADER_BG))
        table_style_cmds.append(("TOPPADDING", (0, ridx), (-1, ridx), 5))
    table.setStyle(TableStyle(table_style_cmds))
    story.append(table)

    # ── Optional positions (not included in totals) ──
    if optional_positions:
        story.append(Spacer(1, 10))
        story.append(Paragraph("<b>Optionale Positionen</b> (nicht im Gesamtbetrag enthalten)", styles["normal"]))
        story.append(Spacer(1, 4))
        opt_data = [header_row]
        for item in optional_positions:
            if is_regular and not prices_are_net:
                opt_ppd = round(item.get("price_per_day", 0) / tax_factor, 2)
                opt_total = round(item["total"] / tax_factor, 2)
            else:
                opt_ppd = round(item.get("price_per_day", 0), 2)
                opt_total = round(item["total"], 2)
            if item.get("is_bundle"):
                opt_data.append([
                    Paragraph("+", styles["table_cell"]),
                    Paragraph(f"<b>{item['name']}</b>", styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph("" if is_pauschale else str(rental_days), styles["table_cell"]),
                    Paragraph("pauschal", styles["table_cell"]),
                    Paragraph(fmt_eur(opt_total), styles["table_cell_right"]),
                ])
                for comp in item.get("bundle_components", []):
                    opt_data.append([
                        Paragraph("", styles["table_cell"]),
                        Paragraph(f"↳ {comp['name']}", styles["table_cell_indent"]),
                        Paragraph(str(comp["quantity"]), styles["table_cell_indent"]),
                        Paragraph("", styles["table_cell"]),
                        Paragraph("", styles["table_cell"]),
                        Paragraph("", styles["table_cell"]),
                    ])
            else:
                one_off = item.get("is_one_off")
                opt_data.append([
                    Paragraph("+", styles["table_cell"]),
                    Paragraph(item["name"], styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph("" if is_pauschale or one_off else str(rental_days), styles["table_cell"]),
                    Paragraph("pauschal" if is_pauschale else "einmalig" if one_off else fmt_eur(opt_ppd),
                              styles["table_cell_right"]),
                    Paragraph(fmt_eur(opt_total), styles["table_cell_right"]),
                ])
        opt_table = Table(_visible_cols(opt_data), colWidths=col_widths, hAlign="LEFT", repeatRows=1)
        opt_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(opt_table)

    if is_regular:
        story.append(Paragraph(
            "Alle Einzelpreise verstehen sich als Nettobeträge.",
            styles["small"]
        ))

    story.append(Spacer(1, 8))

    # ── Totals block (right-aligned) ──
    summary_col_w = [cw - 120, 120]

    summary_data = []

    if is_regular:
        summary_data.append([
            Paragraph("Zwischensumme (netto)", styles["right"]),
            Paragraph(fmt_eur(netto_subtotal), styles["right_bold"]),
        ])

        if discount_percent > 0:
            dl = "Rabatt"
            if discount_label:
                dl += f" – {discount_label}"
            dl += f" ({fmt_percent(discount_percent)})"
            summary_data.append([
                Paragraph(dl, styles["right"]),
                Paragraph(f"– {fmt_eur(netto_discount)}", styles["right"]),
            ])

        summary_data.append([
            Paragraph("Nettobetrag", styles["right"]),
            Paragraph(fmt_eur(netto_total), styles["right"]),
        ])
        summary_data.append([
            Paragraph(f"zzgl. {tax_rate:g} % MwSt.", styles["right"]),
            Paragraph(fmt_eur(mwst), styles["right"]),
        ])
        summary_data.append([
            Paragraph("<b>Voraussichtlicher Gesamtbetrag</b>", styles["right"]),
            Paragraph(f"<b>{fmt_eur(brutto_total)}</b>", styles["right"]),
        ])
    else:
        # Kleinunternehmer: brutto layout
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

        summary_data.append([
            Paragraph("<b>Voraussichtlicher Gesamtbetrag</b>", styles["right"]),
            Paragraph(f"<b>{fmt_eur(subtotal - discount_amount)}</b>", styles["right"]),
        ])

    total_row_idx = -1  # Gesamtbetrag is always the last row
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

    # ── Replacement value of the rented goods (kept visually apart from the price) ──
    if value_total:
        value_text = f"{'mind. ' if value_incomplete else ''}{fmt_eur(value_total)}"
        basis_text = "netto, ohne MwSt." if value_basis == "netto" else "brutto, inkl. MwSt."
        value_hint = (
            f"Wiederbeschaffungswert ({basis_text}) aller oben aufgeführten Mietgegenstände inkl. Zubehör"
            f"{' (ohne optionale Positionen)' if optional_positions else ''} – "
            "zur Orientierung, z. B. für Ihre Versicherung im Verlust- oder Schadensfall. "
            "Eine detaillierte Wertaufstellung erhalten Sie auf Anfrage."
        )
        value_table = Table([
            [Paragraph(f"<b>Gesamtwert der Mietgegenstände ({value_basis})</b>", styles["normal"]),
             Paragraph(f"<b>{value_text}</b>", styles["right"])],
            [Paragraph(value_hint, styles["small"]), ""],
        ], colWidths=[cw - 120, 120], hAlign="LEFT")
        value_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CLR_TABLE_HEADER_BG),
            ("SPAN", (0, 1), (-1, 1)),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(Spacer(1, 12))
        story.append(value_table)

    story.append(Spacer(1, 14))

    # ── Notes ──
    if notes:
        story.append(Paragraph("<b>Bemerkungen:</b>", styles["normal"]))
        for line in notes.strip().split("\n"):
            story.append(Paragraph(line, styles["normal"]))
        story.append(Spacer(1, 8))

    # ── Non-binding disclaimer ──
    story.append(HLine(width=cw))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "<b>Hinweis:</b> Diese Preisübersicht ist unverbindlich und stellt kein Angebot im "
        "rechtlichen Sinne dar. Sie dient ausschließlich Ihrer Vorabinformation und wird nicht "
        "Bestandteil eines Vertrags. Preise und Verfügbarkeit sind freibleibend. Maßgeblich ist "
        "allein unser gesondert übermitteltes Angebot.",
        styles["normal"]
    ))
    story.append(Spacer(1, 14))
    if attachments:
        titles = [_esc(t) for t, _ in attachments]
        if len(titles) == 1:
            hint = f"Zur Information finden Sie in der Anlage: {titles[0]}."
        else:
            hint = ("Zur Information finden Sie in der Anlage:<br/>"
                    + "<br/>".join(f"{i}. {t}" for i, t in enumerate(titles, 1)))
        story.append(Paragraph(hint, styles["normal"]))
        story.append(Spacer(1, 8))

    story.append(Paragraph("Wir freuen uns auf Ihre Rückmeldung.", styles["normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Mit freundlichen Grüßen", styles["normal"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(issuer_name, styles["bold"]))

    # ── Appendix: each document starts on its own two-column page ──
    if attachments:
        from reportlab.platypus import NextPageTemplate, PageBreak
        story.append(NextPageTemplate("appendix"))
        numbered = len(attachments) > 1
        for i, (title, content) in enumerate(attachments, 1):
            story.append(PageBreak())
            label = f"Anlage {i}" if numbered else "Anlage"
            _render_markdown_document(story, content, styles, title=f"{label}: {_esc(title)}",
                                      skip_heading=title)

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def build_attachment_pdf(*, title: str, content: str, issuer_name: str = "") -> bytes:
    """Build a standalone PDF of one appendix document in the same two-column
    layout as the Preisübersicht appendix, e.g. to attach to Angebote from the
    accounting tool."""
    from reportlab.platypus import BaseDocTemplate

    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=(PAGE_W, PAGE_H), title=title, author=issuer_name,
                          pageTemplates=[_build_appendix_page_template()])
    story: list = []
    _render_markdown_document(story, content or "", _base_styles(), title=_esc(title),
                              skip_heading=title)
    doc.build(story)
    return buf.getvalue()


def _build_appendix_page_template(page_numbers: bool = True):
    """Create a two-column page template with reduced margins for appendix pages.
    ``page_numbers=False`` when the document's canvas already numbers pages."""
    from reportlab.platypus import Frame, PageTemplate
    from reportlab.lib.units import mm

    margin_left = 12 * mm
    margin_right = 12 * mm
    margin_top = 12 * mm
    margin_bottom = 14 * mm
    col_gap = 8 * mm

    content_w = PAGE_W - margin_left - margin_right
    col_w = (content_w - col_gap) / 2
    frame_h = PAGE_H - margin_top - margin_bottom

    frame_left = Frame(
        margin_left, margin_bottom,
        col_w, frame_h,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="appendix_left",
    )
    frame_right = Frame(
        margin_left + col_w + col_gap, margin_bottom,
        col_w, frame_h,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="appendix_right",
    )

    def _draw_appendix_page(canvas, doc):
        """Minimal page callback – just page number, no header/footer."""
        canvas.saveState()
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(CLR_GREY_DARK)
        canvas.drawRightString(PAGE_W - margin_right, 6 * mm,
                               f"Seite {canvas.getPageNumber()}")
        canvas.restoreState()

    return PageTemplate(id="appendix", frames=[frame_left, frame_right],
                        onPage=_draw_appendix_page if page_numbers else (lambda canvas, doc: None))


def _esc(text: str) -> str:
    """Escape text for reportlab Paragraph markup."""
    from xml.sax.saxutils import escape
    return escape(text or "")


def _render_markdown_document(story: list, text: str, styles: dict, *, title: str,
                              skip_heading: str | None = None):
    """Parse basic markdown (# headings, **bold**, paragraphs) into reportlab
    flowables. ``title`` is Paragraph markup; a leading ``# …`` heading equal to
    ``skip_heading`` is dropped so the title isn't printed twice."""
    import re
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Spacer, Paragraph

    doc_title = ParagraphStyle("DocTitle", parent=styles["base"], fontName="Helvetica-Bold",
                               fontSize=10, leading=12, spaceAfter=6, spaceBefore=0)
    doc_h1 = ParagraphStyle("DocH1", parent=styles["base"], fontName="Helvetica-Bold",
                            fontSize=8.5, leading=10.5, spaceAfter=2, spaceBefore=6)
    doc_h2 = ParagraphStyle("DocH2", parent=styles["base"], fontName="Helvetica-Bold",
                            fontSize=7.5, leading=9.5, spaceAfter=2, spaceBefore=5)
    doc_h3 = ParagraphStyle("DocH3", parent=styles["base"], fontName="Helvetica-Bold",
                            fontSize=7, leading=9, spaceAfter=1, spaceBefore=4)
    doc_body = ParagraphStyle("DocBody", parent=styles["base"], fontSize=6.5, leading=8.5,
                              spaceAfter=2)

    story.append(Paragraph(title, doc_title))
    story.append(Spacer(1, 4))

    text = (text or "").replace("\r", "").strip()
    if skip_heading:
        first, _, rest = text.partition("\n")
        if first.strip().startswith("# ") and first.strip()[2:].strip() == skip_heading.strip():
            text = rest

    paragraph_buf: list[str] = []

    def inline(s: str) -> str:
        return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', _esc(s))

    def flush_paragraph():
        if paragraph_buf:
            story.append(Paragraph("<br/>".join(paragraph_buf), doc_body))
            paragraph_buf.clear()

    for line in text.split("\n"):
        stripped = line.strip()

        if not stripped:  # empty line ends the paragraph
            flush_paragraph()
        elif stripped.startswith("### "):
            flush_paragraph()
            story.append(Paragraph(inline(stripped[4:]), doc_h3))
        elif stripped.startswith("## "):
            flush_paragraph()
            story.append(Paragraph(inline(stripped[3:]), doc_h2))
        elif stripped.startswith("# "):
            flush_paragraph()
            story.append(Paragraph(inline(stripped[2:]), doc_h1))
        else:
            paragraph_buf.append(inline(stripped))

    flush_paragraph()
