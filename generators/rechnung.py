"""
PDF generator for Rechnung (Invoice).

Similar layout to Angebot but:
- Title: "Rechnung"
- Leistungszeitraum instead of Gültig-bis
- Payment terms more prominent
- Bundles: pauschal price, sub-items indented without price
"""
from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from generators.pdf_base import (
    _base_styles, HLine, build_base_doc,
    _draw_header, _draw_footer,
    CONTENT_W, CLR_TABLE_HEADER_BG, CLR_BLACK, CLR_GREY_DARK,
    fmt_eur, fmt_percent,
)


def build_rechnung_pdf(
    *,
    # Business / issuer
    issuer_name: str,
    issuer_address: list[str],
    contact_lines: list[str],
    bank_lines: list[str],
    tax_number: str | None = None,
    vat_id: str | None = None,
    tax_mode: str = "kleinunternehmer",
    tax_rate: float = 19.0,
    logo_path: str | None = None,

    # Recipient
    recipient_lines: list[str],

    # Document meta
    reference_number: str,
    rechnungs_datum: str | None = None,

    # Rental / service period
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    rental_days: int = 1,
    is_pauschale: bool = False,
    leistungszeitraum: str | None = None,

    # Positions (same structure as Angebot)
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
) -> bytes:
    """Build and return the Rechnung PDF bytes."""
    buf = BytesIO()
    styles = _base_styles()

    if not rechnungs_datum:
        rechnungs_datum = date.today().strftime("%d.%m.%Y")

    leistungszeitraum_display = "—"
    if start_date_str and end_date_str:
        if start_date_str == end_date_str:
            leistungszeitraum_display = start_date_str
        else:
            leistungszeitraum_display = f"{start_date_str} – {end_date_str}"

    meta_lines = [
        ("Rechnungs-Nr.:", reference_number),
        ("Rechnungsdatum:", rechnungs_datum),
        ("Leistungszeitraum:", leistungszeitraum_display),
    ]
    if not is_pauschale and rental_days > 1:
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
                     tax_number=tax_number,
                     vat_id=vat_id)

    doc, cw = build_base_doc(buf, title="Rechnung", author=issuer_name,
                             on_page_callback=on_page)

    story: list = []

    # ── Title ──
    story.append(Paragraph("Rechnung", styles["title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Sehr geehrte Damen und Herren,", styles["normal"]))
    story.append(Paragraph(
        "wir stellen Ihnen die nachfolgend aufgeführten Leistungen in Rechnung:",
        styles["normal"]
    ))
    story.append(Spacer(1, 10))

    # ── Positions table ──
    is_regular = (tax_mode == "regular")
    tax_factor = 1 + tax_rate / 100

    # ── Pre-compute netto values from brutto (rückwärts) ──
    if is_regular:
        import math as _math

        # 1. Tax from known brutto total
        brutto_total = subtotal - (discount_amount if discount_percent > 0 else 0)
        netto_total = round(brutto_total / tax_factor, 2)
        mwst = round(brutto_total - netto_total, 2)

        # 2. Derive netto subtotal so that netto_subtotal - netto_discount == netto_total
        netto_subtotal = round(subtotal / tax_factor, 2)
        netto_discount = round(netto_subtotal - netto_total, 2) if discount_percent > 0 else 0.0

        # 3. Distribute netto_subtotal across positions (largest-remainder)
        position_bruttos = [item["total"] for item in positions]
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

    # Build compact period label for Pauschale descriptions
    pauschale_suffix = ""
    if is_pauschale and leistungszeitraum:
        pauschale_suffix = f" (Nutzung {leistungszeitraum})"

    col_widths = [
        22,
        cw - 22 - 30 - 30 - 50 - 58,
        30,
        30,
        50,
        58,
    ]

    header_row = [
        Paragraph("Pos", styles["table_header"]),
        Paragraph("Bezeichnung", styles["table_header"]),
        Paragraph("Menge", styles["table_header"]),
        Paragraph("Tage" if not is_pauschale else "", styles["table_header"]),
        Paragraph("EP/Tag" if not is_pauschale else "Preis", styles["table_header"]),
        Paragraph("Gesamt", styles["table_header"]),
    ]
    table_data = [header_row]

    pos_nr = 1
    for pos_idx, item in enumerate(positions):
        if item.get("is_bundle"):
            display_total = position_nettos[pos_idx] if is_regular else item["total"]
            name_label = f"<b>{item['name']}{pauschale_suffix}</b>" if is_pauschale else f"<b>{item['name']}</b>"
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(name_label, styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph("" if is_pauschale else str(rental_days), styles["table_cell"]),
                Paragraph("pauschal", styles["table_cell"]),
                Paragraph(f"<b>{fmt_eur(display_total)}</b>", styles["table_cell_right"]),
            ])
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
            if is_regular:
                display_ppd = round(item["price_per_day"] / tax_factor, 2)
                display_total = position_nettos[pos_idx]
            else:
                display_ppd = item["price_per_day"]
                display_total = item["total"]

            if is_pauschale:
                name_label = f"{item['name']}{pauschale_suffix}"
                table_data.append([
                    Paragraph(str(pos_nr), styles["table_cell"]),
                    Paragraph(name_label, styles["table_cell"]),
                    Paragraph(str(item["quantity"]), styles["table_cell"]),
                    Paragraph("", styles["table_cell"]),
                    Paragraph("pauschal", styles["table_cell"]),
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

    table = Table(table_data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle([
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
    story.append(table)

    if is_regular:
        story.append(Paragraph(
            "Alle Einzelpreise verstehen sich als Nettobeträge.",
            styles["small"]
        ))

    story.append(Spacer(1, 8))

    # ── Totals block ──
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
            Paragraph("<b>Rechnungsbetrag</b>", styles["right"]),
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
            Paragraph("<b>Rechnungsbetrag</b>", styles["right"]),
            Paragraph(f"<b>{fmt_eur(subtotal - discount_amount)}</b>", styles["right"]),
        ])

    total_row_idx = -1  # Gesamtbetrag/Rechnungsbetrag is always the last row
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

    # ── Payment terms ──
    story.append(HLine(width=cw))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Bitte überweisen Sie den Rechnungsbetrag innerhalb von {payment_terms_days} Tagen "
        f"unter Angabe der Rechnungsnummer <b>{reference_number}</b> auf das unten genannte Konto.",
        styles["normal"]
    ))
    story.append(Spacer(1, 10))

    # Bank details prominent
    story.append(Paragraph("<b>Bankverbindung:</b>", styles["normal"]))
    for line in bank_lines[:4]:
        story.append(Paragraph(line, styles["normal"]))

    story.append(Spacer(1, 16))
    story.append(Paragraph("Mit freundlichen Grüßen", styles["normal"]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(issuer_name, styles["bold"]))

    doc.build(story)
    return buf.getvalue()
