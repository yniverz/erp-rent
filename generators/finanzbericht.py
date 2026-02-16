"""
PDF generator for Finance Summary Report (Finanzbericht).

A tabular overview of all invoices in a date range, with totals and 
per-owner breakdowns. Used for tax reporting / EÜR.
"""
from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
    PageBreak,
)

from generators.pdf_base import (
    _base_styles, HLine,
    CLR_TABLE_HEADER_BG, CLR_BLACK, CLR_GREY_DARK, CLR_GREY_MID,
    fmt_eur,
)


# ─── Page metrics for landscape A4 ──────────────────────────────
L_PAGE_W, L_PAGE_H = landscape(A4)
L_MARGIN = 15 * mm
L_CONTENT_W = L_PAGE_W - 2 * L_MARGIN


def _build_landscape_doc(buf, title, author, on_page_callback):
    """Create a landscape A4 doc for the finance report."""
    frame = Frame(
        L_MARGIN, L_MARGIN + 15 * mm,
        L_CONTENT_W, L_PAGE_H - 2 * L_MARGIN - 20 * mm,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="main",
    )
    doc = BaseDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=L_MARGIN, rightMargin=L_MARGIN,
        topMargin=L_MARGIN, bottomMargin=L_MARGIN + 15 * mm,
        title=title, author=author,
        pageTemplates=[PageTemplate(id="default", frames=[frame], onPage=on_page_callback)],
    )
    return doc


def build_finance_report_pdf(
    *,
    issuer_name: str,
    date_from: str,
    date_to: str,
    quotes: list[dict],
    owner_summaries: list[dict],
    totals: dict,
    tax_mode: str = "kleinunternehmer",
) -> bytes:
    """Build the finance summary report PDF.
    
    quotes: list of dicts with keys:
        ref, customer, start, end, created, finalized, paid, status, subtotal, discount, total, ext_cost
    owner_summaries: list of dicts with keys:
        name, item_count, investment, revenue_share, ext_cost
    totals: dict with keys:
        quote_count, total_revenue, total_cost, external_cost, profit
    """
    buf = BytesIO()
    styles = _base_styles()

    # Additional styles for report
    styles["h2"] = ParagraphStyle("H2", parent=styles["base"], fontName="Helvetica-Bold",
                                   fontSize=12, leading=15, spaceAfter=8, spaceBefore=16)
    styles["info"] = ParagraphStyle("Info", parent=styles["base"], fontSize=8.5, leading=11)

    def on_page(canvas, doc):
        canvas.saveState()
        # Header line
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawString(L_MARGIN, L_PAGE_H - L_MARGIN + 2 * mm, f"Finanzbericht – {issuer_name}")
        canvas.setFont("Helvetica", 9)
        canvas.drawString(L_MARGIN, L_PAGE_H - L_MARGIN - 5 * mm,
                          f"Zeitraum: {date_from} bis {date_to}")
        # Footer
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(CLR_GREY_DARK)
        canvas.drawString(L_MARGIN, 8 * mm, f"Erstellt am {date.today().strftime('%d.%m.%Y')}")
        canvas.drawRightString(L_PAGE_W - L_MARGIN, 8 * mm, f"Seite {canvas.getPageNumber()}")
        canvas.restoreState()

    doc = _build_landscape_doc(buf, "Finanzbericht", issuer_name, on_page)

    story = []

    # ── Summary Stats ──
    story.append(Paragraph("Zusammenfassung", styles["h2"]))

    summary_data = [
        [
            Paragraph("<b>Anzahl Rechnungen</b>", styles["info"]),
            Paragraph("<b>Gesamtumsatz</b>", styles["info"]),
            Paragraph("<b>Anschaffungskosten</b>", styles["info"]),
            Paragraph("<b>Ext. Mietkosten</b>", styles["info"]),
            Paragraph("<b>Gewinn/Verlust</b>", styles["info"]),
        ],
        [
            Paragraph(str(totals['quote_count']), styles["info"]),
            Paragraph(fmt_eur(totals['total_revenue']), styles["info"]),
            Paragraph(fmt_eur(totals['total_cost']), styles["info"]),
            Paragraph(fmt_eur(totals['external_cost']), styles["info"]),
            Paragraph(fmt_eur(totals['profit']), styles["info"]),
        ]
    ]
    sw = L_CONTENT_W / 5
    summary_table = Table(summary_data, colWidths=[sw] * 5, hAlign="LEFT")
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 10))

    if tax_mode == "kleinunternehmer":
        story.append(Paragraph(
            "Hinweis: Gemäß § 19 UStG wird keine Umsatzsteuer berechnet (Kleinunternehmerregelung).",
            styles["small"]
        ))
        story.append(Spacer(1, 8))

    # ── Owner Breakdown ──
    if owner_summaries:
        story.append(Paragraph("Aufschlüsselung nach Eigentümer", styles["h2"]))
        
        owner_header = [
            Paragraph("Eigentümer", styles["table_header"]),
            Paragraph("Artikel", styles["table_header"]),
            Paragraph("Investition", styles["table_header"]),
            Paragraph("Umsatzanteil", styles["table_header"]),
            Paragraph("Ext. Kosten", styles["table_header"]),
        ]
        owner_data = [owner_header]
        for os_item in owner_summaries:
            owner_data.append([
                Paragraph(os_item['name'], styles["table_cell"]),
                Paragraph(str(os_item['item_count']), styles["table_cell"]),
                Paragraph(fmt_eur(os_item['investment']), styles["table_cell_right"]),
                Paragraph(fmt_eur(os_item['revenue_share']), styles["table_cell_right"]),
                Paragraph(fmt_eur(os_item['ext_cost']), styles["table_cell_right"]),
            ])
        
        ow = L_CONTENT_W / 5
        owner_table = Table(owner_data, colWidths=[ow * 1.5, ow * 0.5, ow, ow, ow], hAlign="LEFT")
        owner_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(owner_table)
        story.append(Spacer(1, 10))

    # ── Invoice List ──
    if quotes:
        story.append(Paragraph("Rechnungsübersicht", styles["h2"]))

        inv_header = [
            Paragraph("Ref-Nr.", styles["table_header"]),
            Paragraph("Kunde", styles["table_header"]),
            Paragraph("Leistungszeitraum", styles["table_header"]),
            Paragraph("Erstellt", styles["table_header"]),
            Paragraph("Finalisiert", styles["table_header"]),
            Paragraph("Bezahlt", styles["table_header"]),
            Paragraph("Status", styles["table_header"]),
            Paragraph("Zwischens.", styles["table_header"]),
            Paragraph("Rabatt", styles["table_header"]),
            Paragraph("Gesamt", styles["table_header"]),
            Paragraph("Ext. Kosten", styles["table_header"]),
        ]
        inv_data = [inv_header]

        for q in quotes:
            period = ""
            if q.get('start') and q.get('end'):
                period = f"{q['start']} – {q['end']}"
            elif q.get('start'):
                period = q['start']

            inv_data.append([
                Paragraph(q['ref'], styles["table_cell"]),
                Paragraph(q['customer'][:30], styles["table_cell"]),
                Paragraph(period, styles["table_cell"]),
                Paragraph(q.get('created', '–'), styles["table_cell"]),
                Paragraph(q.get('finalized', '–'), styles["table_cell"]),
                Paragraph(q.get('paid', '–'), styles["table_cell"]),
                Paragraph(q['status'], styles["table_cell"]),
                Paragraph(fmt_eur(q['subtotal']), styles["table_cell_right"]),
                Paragraph(fmt_eur(q['discount']), styles["table_cell_right"]),
                Paragraph(fmt_eur(q['total']), styles["table_cell_right"]),
                Paragraph(fmt_eur(q['ext_cost']), styles["table_cell_right"]),
            ])

        # Column widths proportional
        cw_total = L_CONTENT_W
        inv_col_widths = [
            cw_total * 0.08,  # ref
            cw_total * 0.14,  # customer
            cw_total * 0.13,  # period
            cw_total * 0.08,  # created
            cw_total * 0.08,  # finalized
            cw_total * 0.08,  # paid
            cw_total * 0.07,  # status
            cw_total * 0.09,  # subtotal
            cw_total * 0.07,  # discount
            cw_total * 0.09,  # total
            cw_total * 0.09,  # ext cost
        ]

        inv_table = Table(inv_data, colWidths=inv_col_widths, hAlign="LEFT", repeatRows=1)
        inv_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), CLR_TABLE_HEADER_BG),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, CLR_BLACK),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ]))
        story.append(inv_table)

        # Totals row
        story.append(Spacer(1, 6))
        totals_data = [[
            Paragraph("", styles["table_cell"]),
            Paragraph("", styles["table_cell"]),
            Paragraph("", styles["table_cell"]),
            Paragraph("", styles["table_cell"]),
            Paragraph("", styles["table_cell"]),
            Paragraph("", styles["table_cell"]),
            Paragraph(f"<b>Summe ({totals['quote_count']})</b>", styles["table_cell"]),
            Paragraph(f"<b>{fmt_eur(sum(q['subtotal'] for q in quotes))}</b>", styles["table_cell_right"]),
            Paragraph(f"<b>{fmt_eur(sum(q['discount'] for q in quotes))}</b>", styles["table_cell_right"]),
            Paragraph(f"<b>{fmt_eur(totals['total_revenue'])}</b>", styles["table_cell_right"]),
            Paragraph(f"<b>{fmt_eur(totals['external_cost'])}</b>", styles["table_cell_right"]),
        ]]
        totals_table = Table(totals_data, colWidths=inv_col_widths, hAlign="LEFT")
        totals_table.setStyle(TableStyle([
            ("LINEABOVE", (0, 0), (-1, 0), 1.2, CLR_BLACK),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(totals_table)

    doc.build(story)
    return buf.getvalue()
