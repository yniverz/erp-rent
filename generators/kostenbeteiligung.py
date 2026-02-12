from __future__ import annotations

from datetime import date
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Flowable


class HLine(Flowable):
    def __init__(self, width: float, thickness: float = 0.7, color=colors.black, space_before=6, space_after=6):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color
        self.space_before = space_before
        self.space_after = space_after
        self.height = self.space_before + self.thickness + self.space_after

    def draw(self):
        self.canv.saveState()
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        y = self.space_after
        self.canv.line(0, y, self.width, y)
        self.canv.restoreState()


def build_rechnung_pdf_bytes(
    issuer_name: str,
    issuer_address_lines: list[str],
    issuer_contact_lines: list[str],
    bank_lines: list[str],

    recipient_lines: list[str],

    reference_no: str,
    bereitstellungszeitraum: tuple[str, str],
    rechnungsbetrag_eur: float,
    rechnungsdatum: str = date.today().strftime("%d.%m.%Y"),
) -> bytes:
    buf = BytesIO()
    page_w, page_h = A4

    # Ränder wie Vorlage-Feeling
    left_margin = 17.7 * mm
    right_margin = 17.7 * mm

    # Wir zeichnen Header/Footer mit canvas -> Frame lässt Platz
    header_space = 72 * mm
    footer_space = 34 * mm

    top = 12 * mm
    bottom = footer_space

    styles = getSampleStyleSheet()

    # Styles (Helvetica)
    s_recipient = ParagraphStyle(
        "Recipient",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=11.0,
        leading=13.2,
        spaceAfter=0,
    )
    s_normal = ParagraphStyle(
        "NormalCustom",
        parent=s_recipient,
        spaceAfter=0,
    )
    s_title = ParagraphStyle(
        "Title",
        parent=s_normal,
        fontName="Helvetica-Bold",
        fontSize=16.1,
        leading=18.0,
        spaceAfter=0,
    )
    s_meta = ParagraphStyle(
        "Meta",
        parent=s_normal,
        spaceAfter=0,
    )
    s_amount = ParagraphStyle(
        "Amount",
        parent=s_normal,
        fontName="Helvetica-Bold",
        fontSize=12.0,
        leading=14.0,
        leftIndent=12.7 * mm,
        spaceAfter=0,
    )

    # Footer Typo
    footer_font = "Helvetica"
    footer_size = 9.12

    # Hilfsfunktion: y von "oben gemessen" in y von unten umrechnen
    def yb(y_from_top_pt: float) -> float:
        return page_h - y_from_top_pt

    # ----- Koordinaten (an der Vorlage orientiert) -----
    x_left = 50.6
    x_dates_1 = 406.3
    x_dates_2 = 358.5

    x_footer_left = 55.4
    x_footer_mid = 226.3
    x_footer_right = 397.4
    y_footer_first_line = yb(763.43)

    y_senderline = yb(76.07)
    y_recipient_first = yb(106.31)
    y_dates_1 = yb(78.23)
    y_dates_2 = yb(95.51)

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(colors.black)

        # ---------- HEADER ----------
        # Absenderzeile klein über Empfänger (wie Vorlage)
        sender_line = f"{issuer_name} – {issuer_address_lines[0] if issuer_address_lines else ''} – {issuer_address_lines[1] if len(issuer_address_lines) > 1 else ''}".strip(" –")
        canvas.setFont("Helvetica", 7.9)
        canvas.drawString(x_left, y_senderline, sender_line)

        # Empfängerblock oben links
        canvas.setFont("Helvetica", 11.0)
        line_h = 13.2
        for i, line in enumerate(recipient_lines):
            canvas.drawString(x_left, y_recipient_first - i * line_h, line)

        # Datumsblock oben rechts
        canvas.setFont("Helvetica", 10.1)
        canvas.drawString(x_dates_1, y_dates_1, f"Rechnungsdatum: {rechnungsdatum}")
        canvas.drawString(x_dates_2, y_dates_2, f"Lieferdatum/Leistungsdatum: {bereitstellungszeitraum[0]}")

        # ---------- FOOTER (3 Spalten) ----------
        canvas.setFont(footer_font, footer_size)

        left_lines = [issuer_name] + issuer_address_lines[:3]
        mid_lines = issuer_contact_lines[:3]
        right_lines = bank_lines[:3]

        footer_dy = 10.32
        for idx, t in enumerate(left_lines):
            canvas.drawString(x_footer_left, y_footer_first_line - idx * footer_dy, t)
        for idx, t in enumerate(mid_lines):
            canvas.drawString(x_footer_mid, y_footer_first_line - idx * footer_dy, t)
        for idx, t in enumerate(right_lines):
            canvas.drawString(x_footer_right, y_footer_first_line - idx * footer_dy, t)

        canvas.restoreState()

    frame = Frame(
        left_margin,
        bottom,
        page_w - left_margin - right_margin,
        page_h - top - bottom,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="main",
    )

    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top,
        bottomMargin=bottom,
        title="Rechnung",
        author=issuer_name,
        pageTemplates=[],
    )
    doc.addPageTemplates([PageTemplate(id="Page1", frames=[frame], onPage=on_page)])

    story: list = []

    # Start exakt „unter“ dem Headerbereich (damit Titel an richtiger Stelle sitzt)
    story.append(Spacer(1, header_space))

    # Inhalt wie Vorlage
    story.append(Paragraph("Rechnung", s_title))
    story.append(Spacer(1, 6))

    story.append(Paragraph("Referenznummer:", s_meta))
    story.append(Paragraph(reference_no, s_meta))
    story.append(Spacer(1, 6))

    story.append(Paragraph(f"Bereitstellungszeitraum: {bereitstellungszeitraum[0]} – {bereitstellungszeitraum[1]}", s_meta))
    story.append(Spacer(1, 14))

    story.append(Paragraph("Sehr geehrte Damen und Herren,", s_normal))
    story.append(Spacer(1, 14))

    story.append(Paragraph(
        "hiermit stelle ich Ihnen eine Aufwands- und Kostenbeteiligung für die private Bereitstellung von",
        s_normal
    ))
    story.append(Paragraph("Veranstaltungsquipment in Rechnung.", s_normal))
    story.append(Spacer(1, 18))

    story.append(Paragraph(f"Rechnungsbetrag (pauschal):  {rechnungsbetrag_eur:.2f} €", s_amount))
    story.append(Spacer(1, 12))

    story.append(Paragraph(
        "Es handelt sich um einen privaten Aufwandsersatz. Es wird keine Umsatzsteuer ausgewiesen.",
        s_normal
    ))
    story.append(Spacer(1, 16))

    story.append(Paragraph(
        "Der Gesamtbetrag ist, sofern nicht anderweitig vereinbart, bis 4 Wochen nach Rechnungsdatum mit der "
        "oben genannten Rechnungsnummer als Verwendungszweck auf das unten genannte Konto zu zahlen.",
        s_normal
    ))
    story.append(Spacer(1, 30))

    story.append(Paragraph("Mit freundlichen Grüßen", s_normal))
    story.append(Spacer(1, 22))
    story.append(Paragraph(issuer_name, s_normal))

    doc.build(story)
    return buf.getvalue()







# ---------------- Example Flask endpoint ----------------
def file():
    pdf_bytes = build_rechnung_pdf_bytes(
        issuer_name="######",
        issuer_address_lines=[
            "######", 
            "######", 
            "Deutschland"
        ],
        issuer_contact_lines=[
            "Tel.: ######", 
            "E-Mail:",
            "######"
        ],
        bank_lines=[
            "Bank: Kreissparkasse Waiblingen",
            "IBAN: ######",
            "BIC: ######",
        ],
        recipient_lines=[
            "Max Musterfrau e.V.", 
            "Adenauerring 4", 
            "76131 Karlsruhe", 
            "Deutschland",
        ],
        reference_no="RE2025120601",
        bereitstellungszeitraum=("XX.XX.20XX", "XX.XX.20XX"),
        rechnungsbetrag_eur=127.50,
        # rechnungsdatum="XX.XX.20XX",
    )

    from flask import send_file
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="rechnung_pauschal.pdf",
        max_age=0,
    )


if __name__ == "__main__":
    from flask import Flask
    app = Flask(__name__)
    app.add_url_rule("/", "file", file)
    app.run(debug=True, host="0.0.0.0", port=5001)
