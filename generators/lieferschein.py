"""
PDF generator for Lieferschein / Übergabeprotokoll (Delivery Note / Handover Protocol).

Features:
- Header: Logo, sender, recipient, meta (Lieferschein-Nr, date, rental period)
- Item table: Pos | Bezeichnung | Menge | Zustand/Kommentar
  - NO prices
- Optional Kaution (deposit) field
- Übergabe (handover) section with signature fields
- Rückgabe (return) section with signature fields
- Footer: business info
"""
from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from generators.pdf_base import (
    _base_styles, HLine, build_base_doc,
    _draw_header, _draw_footer,
    PAGE_W, CONTENT_W, MARGIN_LEFT, MARGIN_RIGHT,
    CLR_TABLE_HEADER_BG, CLR_BLACK, CLR_GREY_DARK, CLR_GREY_MID,
    fmt_eur,
)


def build_lieferschein_pdf(
    *,
    # Business / issuer
    issuer_name: str,
    issuer_address: list[str],
    contact_lines: list[str],
    bank_lines: list[str],
    tax_number: str | None = None,
    vat_id: str | None = None,
    logo_path: str | None = None,

    # Recipient
    recipient_lines: list[str],

    # Document meta
    reference_number: str,
    lieferschein_datum: str | None = None,

    # Rental period
    start_date_str: str | None = None,
    end_date_str: str | None = None,

    # Items: list of dicts
    # Each: { 'name': str, 'quantity': int, 'is_bundle': bool,
    #          'bundle_components': [{'name': str, 'quantity': int}] }
    items: list[dict],

    # Kaution (optional)
    kaution: float | None = None,

    # Notes
    notes: str | None = None,
) -> bytes:
    """Build and return the Lieferschein PDF bytes."""
    buf = BytesIO()
    styles = _base_styles()

    if not lieferschein_datum:
        lieferschein_datum = date.today().strftime("%d.%m.%Y")

    zeitraum = "—"
    if start_date_str and end_date_str:
        if start_date_str == end_date_str:
            zeitraum = start_date_str
        else:
            zeitraum = f"{start_date_str} – {end_date_str}"

    meta_lines = [
        ("Lieferschein-Nr.:", reference_number),
        ("Datum:", lieferschein_datum),
        ("Mietzeitraum:", zeitraum),
    ]

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

    doc, cw = build_base_doc(buf, title="Lieferschein", author=issuer_name,
                             on_page_callback=on_page)

    story: list = []

    # ── Title ──
    story.append(Paragraph("Lieferschein / Übergabeprotokoll", styles["title"]))
    story.append(Spacer(1, 10))

    # ── Item table ──
    col_widths = [
        22,           # Pos
        cw * 0.45,    # Bezeichnung
        30,           # Menge
        cw - 22 - cw * 0.45 - 30,  # Zustand/Kommentar
    ]

    header_row = [
        Paragraph("Pos", styles["table_header"]),
        Paragraph("Bezeichnung", styles["table_header"]),
        Paragraph("Menge", styles["table_header"]),
        Paragraph("Zustand / Kommentar", styles["table_header"]),
    ]
    table_data = [header_row]

    pos_nr = 1
    for item in items:
        if item.get("is_bundle"):
            # Bundle header
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(f"<b>{item['name']}</b>", styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph("", styles["table_cell"]),
            ])
            for comp in item.get("bundle_components", []):
                table_data.append([
                    Paragraph("", styles["table_cell"]),
                    Paragraph(f"↳ {comp['name']}", styles["table_cell_indent"]),
                    Paragraph(str(comp["quantity"]), styles["table_cell_indent"]),
                    Paragraph("", styles["table_cell"]),
                ])
        else:
            table_data.append([
                Paragraph(str(pos_nr), styles["table_cell"]),
                Paragraph(item["name"], styles["table_cell"]),
                Paragraph(str(item["quantity"]), styles["table_cell"]),
                Paragraph("", styles["table_cell"]),  # Empty for handwritten notes
            ])
        pos_nr += 1

    # Make the comment column taller for handwriting
    row_heights = [None] + [24] * (len(table_data) - 1)

    table = Table(table_data, colWidths=col_widths, hAlign="LEFT",
                  repeatRows=1, rowHeights=row_heights)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("BOX", (0, 0), (-1, -1), 0.6, CLR_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))

    # ── Kaution ──
    if kaution is not None and kaution > 0:
        story.append(Paragraph(
            f"<b>Kaution:</b> {fmt_eur(kaution)}",
            styles["normal"]
        ))
        story.append(Paragraph(
            "Die Kaution ist vor Übergabe zu leisten und wird nach ordnungsgemäßer Rückgabe erstattet, "
            "sofern keine Schäden oder offenen Forderungen bestehen.",
            styles["small"]
        ))
        story.append(Spacer(1, 6))

    # ── Notes ──
    if notes:
        story.append(Paragraph("<b>Bemerkungen:</b>", styles["normal"]))
        for line in notes.strip().split("\n"):
            story.append(Paragraph(line, styles["normal"]))
        story.append(Spacer(1, 6))

    # Extra space for handwritten notes
    story.append(Paragraph("<b>Bemerkungen bei Übergabe:</b>", styles["normal"]))
    story.append(Spacer(1, 4))
    for _ in range(3):
        story.append(HLine(width=cw, thickness=0.4, color=CLR_GREY_MID, space_before=0, space_after=14))

    story.append(Spacer(1, 6))

    # ── ÜBERGABE Section ──
    story.append(HLine(width=cw, thickness=0.8, color=CLR_BLACK, space_before=2, space_after=4))
    story.append(Paragraph("<b>ÜBERGABE</b>", styles["subtitle"]))
    story.append(Paragraph(
        "Der Mieter bestätigt den Empfang der oben aufgeführten Gegenstände in ordnungsgemäßem Zustand.",
        styles["small"]
    ))
    story.append(Spacer(1, 16))

    sig_w = (cw - 20) / 2
    sig_table = Table([
        [HLine(sig_w, thickness=0.6, space_before=0, space_after=2),
         HLine(sig_w, thickness=0.6, space_before=0, space_after=2)],
        [Paragraph("Ort, Datum", styles["small"]),
         Paragraph("Unterschrift Mieter", styles["small"])],
        [Spacer(1, 20), Spacer(1, 20)],
        [HLine(sig_w, thickness=0.6, space_before=0, space_after=2),
         HLine(sig_w, thickness=0.6, space_before=0, space_after=2)],
        [Paragraph("Ort, Datum", styles["small"]),
         Paragraph("Unterschrift Vermieter", styles["small"])],
    ], colWidths=[sig_w, sig_w], hAlign="LEFT")
    sig_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 20),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 14))

    # ── RÜCKGABE Section ──
    story.append(HLine(width=cw, thickness=0.8, color=CLR_BLACK, space_before=2, space_after=4))
    story.append(Paragraph("<b>RÜCKGABE</b>", styles["subtitle"]))
    story.append(Paragraph(
        "Der Vermieter bestätigt die Rückgabe der oben aufgeführten Gegenstände.",
        styles["small"]
    ))
    story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Bemerkungen bei Rückgabe:</b>", styles["normal"]))
    story.append(Spacer(1, 4))
    for _ in range(3):
        story.append(HLine(width=cw, thickness=0.4, color=CLR_GREY_MID, space_before=0, space_after=14))

    story.append(Spacer(1, 8))

    sig_table2 = Table([
        [HLine(sig_w, thickness=0.6, space_before=0, space_after=2),
         HLine(sig_w, thickness=0.6, space_before=0, space_after=2)],
        [Paragraph("Ort, Datum", styles["small"]),
         Paragraph("Unterschrift Mieter", styles["small"])],
        [Spacer(1, 20), Spacer(1, 20)],
        [HLine(sig_w, thickness=0.6, space_before=0, space_after=2),
         HLine(sig_w, thickness=0.6, space_before=0, space_after=2)],
        [Paragraph("Ort, Datum", styles["small"]),
         Paragraph("Unterschrift Vermieter", styles["small"])],
    ], colWidths=[sig_w, sig_w], hAlign="LEFT")
    sig_table2.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 20),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig_table2)

    doc.build(story)
    return buf.getvalue()
