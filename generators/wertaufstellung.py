"""
PDF generator for the Wertaufstellung (replacement value statement) of a quote,
e.g. for insurance purposes.

Features:
- Header: Logo, sender, recipient, meta (Referenz, date, rental period)
- Table: Pos | Bezeichnung | Menge | Einzelwert | Gesamtwert
  - device rows with assigned serial numbers, Lieferumfang as indented sub-rows
- Grand total; hint if some values are missing
- Footer: business info
"""
from __future__ import annotations

from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from generators.pdf_base import (
    _base_styles, build_base_doc,
    _draw_header, _draw_footer,
    NumberedCanvas,
    CLR_TABLE_HEADER_BG, CLR_BLACK,
    fmt_eur,
)


def build_wertaufstellung_pdf(
    *,
    # Business / issuer
    issuer_name: str,
    issuer_address: list[str],
    contact_lines: list[str],
    tax_number: str | None = None,
    vat_id: str | None = None,
    logo_path: str | None = None,

    # Recipient
    recipient_lines: list[str],

    # Document meta
    reference_number: str,
    start_date_str: str | None = None,
    end_date_str: str | None = None,

    # Rows: list of dicts
    # Each: { 'kind': 'heading'|'item'|'bundle'|'accessory', 'level': 0|1|2,
    #         'pos': int|None, 'name': str, 'quantity': int|None,
    #         'unit_value': float|None, 'total': float|None, 'units': [str] }
    rows: list[dict],
    grand_total: float,
    missing_count: int = 0,
    value_basis: str = "brutto",  # 'netto' (regelbesteuert) or 'brutto' (Kleinunternehmer)
) -> bytes:
    """Build and return the Wertaufstellung PDF bytes."""
    buf = BytesIO()
    styles = _base_styles()

    zeitraum = "—"
    if start_date_str and end_date_str:
        zeitraum = start_date_str if start_date_str == end_date_str else f"{start_date_str} – {end_date_str}"

    meta_lines = [
        ("Referenz:", reference_number),
        ("Datum:", date.today().strftime("%d.%m.%Y")),
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
                     tax_number=tax_number,
                     vat_id=vat_id)

    doc, cw = build_base_doc(buf, title="Wertaufstellung", author=issuer_name,
                             on_page_callback=on_page)

    story: list = []
    story.append(Paragraph("Wertaufstellung", styles["title"]))
    basis_text = "Nettowerte (ohne MwSt.)" if value_basis == "netto" else "Bruttowerte (inkl. MwSt.)"
    story.append(Paragraph(
        "Wiederbeschaffungswerte der für den angegebenen Mietzeitraum überlassenen Gegenstände "
        f"inklusive Zubehör, z.B. zur Vorlage bei einer Versicherung. Alle Werte sind {basis_text}.",
        styles["normal"]
    ))
    story.append(Spacer(1, 10))

    col_widths = [26, cw - 26 - 42 - 70 - 75, 42, 70, 75]
    table_data = [[
        Paragraph("Pos", styles["table_header"]),
        Paragraph("Bezeichnung", styles["table_header"]),
        Paragraph("Menge", styles["table_header"]),
        Paragraph("Einzelwert", styles["table_header"]),
        Paragraph("Gesamtwert", styles["table_header"]),
    ]]
    heading_row_idxs: list = []

    def _money(value):
        return fmt_eur(value) if value is not None else "—"

    for row in rows:
        if row["kind"] == "heading":
            heading_row_idxs.append(len(table_data))
            table_data.append([
                Paragraph("", styles["table_cell"]),
                Paragraph(f"<b>{_esc(row['name'])}</b>", styles["table_cell"]),
                "", "", "",
            ])
            continue

        level = row.get("level", 0)
        cell = styles["table_cell"] if level == 0 else styles["table_cell_indent"]
        indent = "&nbsp;&nbsp;&nbsp;" * max(0, level - 1)
        name = _esc(row["name"])
        if row["kind"] == "bundle":
            label = f"<b>{name}</b>"
        elif row["kind"] == "accessory":
            label = f"{indent}+ {name}"
        else:
            label = f"↳ {name}" if level else name
        if row.get("units"):
            label += f'<br/><font size="7" color="#555555">SN: {_esc(", ".join(row["units"]))}</font>'

        is_bundle = row["kind"] == "bundle"
        table_data.append([
            Paragraph(str(row["pos"]) if row.get("pos") else "", styles["table_cell"]),
            Paragraph(label, cell),
            Paragraph(str(row["quantity"]) if row.get("quantity") is not None else "", cell),
            Paragraph("" if is_bundle else _money(row.get("unit_value")), styles["table_cell_right"]),
            Paragraph("" if is_bundle else _money(row.get("total")), styles["table_cell_right"]),
        ])

    total_idx = len(table_data)
    table_data.append([
        "",
        Paragraph(f"<b>Gesamtwert ({value_basis})</b>", styles["table_cell"]),
        "", "",
        Paragraph(f"<b>{fmt_eur(grand_total)}</b>", styles["table_cell_right"]),
    ])

    table = Table(table_data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
        ("GRID", (0, 0), (-1, total_idx - 1), 0.3, colors.HexColor("#cccccc")),
        ("BOX", (0, 0), (-1, -1), 0.6, CLR_BLACK),
        ("LINEABOVE", (0, total_idx), (-1, total_idx), 0.8, CLR_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for ridx in heading_row_idxs:
        style_cmds.append(("BACKGROUND", (0, ridx), (-1, ridx), CLR_TABLE_HEADER_BG))
    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    story.append(Spacer(1, 8))

    if missing_count:
        story.append(Paragraph(
            f"Hinweis: Für {missing_count} Zeile(n) ist kein Wert hinterlegt (—); "
            "diese sind im Gesamtwert nicht enthalten.",
            styles["small"]
        ))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buf.getvalue()
