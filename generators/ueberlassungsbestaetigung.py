from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Flowable,
)


# ---------- Small helper flowables ----------
class HLine(Flowable):
    """A thin horizontal line with configurable width."""
    def __init__(self, width: float, thickness: float = 0.8, color=colors.black, space_before=2, space_after=6):
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
        y = self.space_after  # draw near bottom of the flowable box
        self.canv.line(0, y, self.width, y)
        self.canv.restoreState()


def _build_pdf_bytes(consignor_info: list[str], recipient_info: list[str], timeframe_str: str, items: list[(int, str)], total_sum: float = None, *args, **kwargs) -> bytes:
    buf = BytesIO()

    page_w, page_h = A4

    # Margins chosen to visually match the source PDF
    margin_horizontal = 13 * mm
    left = margin_horizontal
    right = margin_horizontal
    top = 20 * mm
    bottom = 18 * mm

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        spaceAfter=10,
    )

    normal = ParagraphStyle(
        "NormalCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=11,
        leading=14,
        spaceAfter=6,
    )

    normal_small = ParagraphStyle(
        "NormalSmall",
        parent=normal,
        fontSize=10.5,
        leading=13,
        spaceAfter=5,
    )

    small = ParagraphStyle(
        "SmallCustom",
        parent=normal,
        fontSize=7,
        leading=12,
        spaceAfter=4,
    )

    very_small = ParagraphStyle(
        "VerySmallCustom",
        parent=normal,
        fontSize=6,
        leading=10,
        spaceAfter=3,
    )

    bold = ParagraphStyle(
        "BoldCustom",
        parent=normal,
        fontName="Helvetica-Bold",
        spaceAfter=2,
    )

    section_head = ParagraphStyle(
        "SectionHead",
        parent=normal_small,
        fontName="Helvetica-Bold",
        spaceBefore=6,
        spaceAfter=2,
    )

    # Footer (blue "Seite X von Y")
    # footer_blue = colors.HexColor("#1f4e79")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 11)
        # canvas.setFillColor(footer_blue)
        page_no = canvas.getPageNumber()
        canvas.drawRightString(page_w - right, bottom / 2.2, f"Seite {page_no} von 2")
        canvas.restoreState()

    # ----- Define page templates -----
    # Page 1: single column
    frame_p1 = Frame(
        left,
        bottom,
        page_w - left - right,
        page_h - top - bottom,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="p1",
    )

    # Page 2: two columns
    gutter = 14 * mm
    col_w = (page_w - left - right - gutter) / 2.0
    frame_left = Frame(
        left,
        bottom,
        col_w,
        page_h - top - bottom,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="col_left",
    )
    frame_right = Frame(
        left + col_w + gutter,
        bottom,
        col_w,
        page_h - top - bottom,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="col_right",
    )

    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=left,
        rightMargin=right,
        topMargin=top,
        bottomMargin=bottom,
        pageTemplates=[],
        title="Überlassungsbestätigung",
        author="",
    )

    doc.addPageTemplates(
        [
            PageTemplate(id="Page1", frames=[frame_p1], onPage=on_page),
            PageTemplate(id="Page2", frames=[frame_left, frame_right], onPage=on_page),
        ]
    )

    story = []

    # ===================== PAGE 1 =====================
    story.append(Paragraph("Überlassungsbestätigung", title_style))
    story.append(Spacer(1, 6))

    # Two-column layout for Überlasser and Nutzer
    col_width = (page_w - left - right - 20) / 2
    
    # Build Überlasser column content
    ueberlasser_content = []
    for line in consignor_info:
        ueberlasser_content.append(Paragraph(line, bold))
    ueberlasser_content.append(Paragraph("(Überlasser)", normal))
    

    nutzer_content = []
    for line in recipient_info:
        nutzer_content.append(Paragraph(line, bold))
    nutzer_content.append(Paragraph("(Nutzer)", normal))
    
    # Create two-column table
    parties_table = Table(
        [[ueberlasser_content, nutzer_content]],
        colWidths=[col_width, col_width],
        hAlign="LEFT",
    )
    parties_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(parties_table)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Zwischen Überlasser und Nutzer wird folgende Vereinbarung getroffen:", normal_small))
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Der Überlasser überlässt dem Nutzer für den Zeitraum {timeframe_str} folgendes Equipment zur Nutzung:", normal_small))
    story.append(Spacer(1, 8))

    # Equipment table
    # Style for table cells to enable text wrapping
    table_cell_style = ParagraphStyle(
        "TableCell",
        parent=normal,
        fontSize=9.5,
        leading=11,
        spaceAfter=0,
    )
    
    data = [["Nr.", "Menge", "Bezeichnung", "Kommentar"]]

    # Wrap text in Paragraph objects to enable line breaking
    for i, (quantity, name) in enumerate(items, start=1):
        data.append([
            str(i),
            Paragraph(str(quantity), table_cell_style),
            Paragraph(name, table_cell_style),
            Paragraph("", table_cell_style)
        ])

    table = Table(
        data,
        colWidths=[10 * mm, 17 * mm, 120 * mm, (page_w - left - right) - (10 * mm + 17 * mm + 120 * mm)],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9d9d9")),
        ("FONT", (0, 0), (-1, 0), "Helvetica", 12),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    story.append(Paragraph(
        "Diese Überlassungsvereinbarung besteht aus 2 Seiten. Die Regelungen auf der Rückseite / "
        "Seite 2 sind Bestandteil dieser Vereinbarung.",
        normal_small
    ))
    story.append(Paragraph(
        "Der Nutzer bestätigt mit seiner Unterschrift, beide Seiten dieser Vereinbarung gelesen und akzeptiert zu "
        "haben.",
        normal_small
    ))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Bemerkungen:", normal))
    story.append(Spacer(1, 15))
    story.append(HLine(width=page_w - left - right, thickness=0.9, space_before=0, space_after=20))
    story.append(HLine(width=page_w - left - right, thickness=0.9, space_before=0, space_after=20))
    story.append(HLine(width=page_w - left - right, thickness=0.9, space_before=0, space_after=40))

    # Signature blocks (two columns, two rows)
    sig_line_w = (page_w - left - right - 25) / 2
    sig = Table(
        [
            [HLine(sig_line_w, thickness=0.9, space_before=0, space_after=2),
             HLine(sig_line_w, thickness=0.9, space_before=0, space_after=2)],
            [Paragraph("Ort, Datum Nutzer", small),
             Paragraph("Unterschrift Nutzer", small)],
            [Spacer(1, 25), Spacer(1, 25)],
            [HLine(sig_line_w, thickness=0.9, space_before=0, space_after=2),
             HLine(sig_line_w, thickness=0.9, space_before=0, space_after=2)],
            [Paragraph("Ort, Datum Überlasser", small),
             Paragraph("Unterschrift Überlasser", small)],
        ],
        colWidths=[sig_line_w, sig_line_w],
        hAlign="LEFT",
    )
    sig.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 25),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(sig)

    story.append(NextPageTemplate("Page2"))
    story.append(PageBreak())

    # ===================== PAGE 2 =====================
    story.append(Paragraph("Überlassungsbestätigung", normal))
    story.append(Spacer(1, 6))

    # Two-column legal text
    # sections = [
    #     ("§1 Überlassungsgegenstand",
    #      "Der Überlassende überlässt dem Nutzer die in der Materialliste aufgeführten beweglichen Sachen unentgeltlich und zeitlich begrenzt zur Nutzung. "
    #      "Die überlassenen Gegenstände bleiben uneingeschränkt Eigentum des Überlassenden. "
    #      "Der Zustand der überlassenen Sachen ist gebraucht. "
    #      "Vorschäden sind vor Übergabe zu dokumentieren."),
    #     # ("§2 Dauer der Überlassung",
    #     #  "Die Überlassung beginnt und endet zu dem oben genannten Zeitraum. Eine Verlängerung oder Verkürzung der "
    #     #  "Überlassungsdauer bedarf der schriftlichen Vereinbarung. Verzögert sich die Rückgabe durch schuldhaftes Verhalten "
    #     #  "des Nutzers, verlängert sich die Überlassung automatisch um 24 Stunden. Hierdurch entstehende Mehrkosten trägt der Nutzer."),
    #     ("§2 Dauer der Überlassung",
    #      "Die Überlassung beginnt und endet zu dem oben genannten Zeitraum. "
    #      "Eine Verlängerung oder Verkürzung der Überlassungsdauer bedarf der schriftlichen Vereinbarung. "
    #      "Der Nutzer gerät bei verspäteter Rückgabe in Erinnerung in Verzug und haftet für daraus entstehende Schäden."),
    #     ("§3 Kosten",
    #      "Die Überlassung erfolgt grundsätzlich unentgeltlich. "
    #      "Der Überlassende ist jedoch berechtigt, eine angemessene Aufwands- und Kostenbeteiligung " + (f"in Höhe von {total_sum:.2f} Euro" if total_sum is not None else "") + " zu erheben. "
    #      "Etwaige Kosten für Transport, Reinigung, Reparatur, Ersatz oder sonstige Aufwendungen, die durch die Nutzung entstehen, trägt der Nutzer, soweit er diese zu vertreten hat."),
    #     ("§4 Weitergabe und Haftung",
    #      "Eine Weitergabe oder Unterüberlassung an Dritte ist nicht zulässig. "
    #      "Der Nutzer haftet für Beschädigung, Verlust oder Diebstahl der überlassenen Sachen, auch wenn diese durch Dritte verursacht werden. "
    #      "Die überlassenen Gegenstände dürfen weder veräußert, verpfändet noch sicherungsübereignet werden."),
    #     ("§5 Nutzungspflichten",
    #      "Die Nutzung hat ausschließlich gemäß den gesetzlichen Vorschriften, insbesondere der Versammlungsstättenverordnung, zu erfolgen. "
    #      "Für Schäden, Sanktionen, Geldbußen oder sonstige Nachteile, die dem Überlassenden durch unsachgemäßen, fahrlässigen oder gesetzeswidrigen Gebrauch entstehen, haftet der Nutzer in vollem Umfang. "
    #      "Es wird ausdrücklich darauf hingewiesen, dass die Geräte nicht versichert sind. "
    #      "Der Nutzer ist für die sachgerechte Bedienung selbst verantwortlich. "
    #      "Der Überlassende schuldet keine Einweisung oder technische Betreuung. "
    #      "Dies gilt nicht, wenn der Überlassende vorsätzlich oder grob fahrlässig ungeeignete oder erkennbar mangelhafte Geräte überlässt."),
    #     ("§6 Schäden, Reparatur und Ersatz",
    #      "Der Nutzer haftet für Schäden, die durch schuldhafte Verletzung seiner Pflicht zur schonenden Behandlung, sorgfältigen Pflege, gesicherten Transportes und ordnungsgemäßen Lagerung entstehen. "
    #      "Der Überlassende ist berechtigt, nach eigener Wahl: eine fachgerechte Reparatur durchführen zu lassen oder Ersatz zum Wiederbeschaffungswert zu verlangen, sofern eine Reparatur technisch unmöglich oder wirtschaftlich nicht sinnvoll ist. "
    #      "Ein Abzug „neu für alt“ bleibt ausdrücklich vorbehalten."
    #      "<br/><br/>"
    #      "Zu den haftungsrelevanten Schäden zählen insbesondere Feuer-, Wasser- und Transportschäden sowie Diebstahl. "
    #      "Das Verschulden von Erfüllungsgehilfen, Beauftragten oder sonstigen Dritten wird dem Nutzer zugerechnet. "
    #      "Schäden sind dem Überlassenden unverzüglich anzuzeigen. "
    #      "Während der Überlassungsdauer dürfen Mängel ausschließlich durch den Überlassenden oder eine von ihm beauftragte Person behoben werden."
    #      "<br/><br/>"
    #      "Das Öffnen von Geräten oder das Herausschrauben aus Racks ist untersagt. "
    #      "Hiervon ausgenommen sind der Überlassende sowie von ihm ausdrücklich beauftragte Personen."),
    #     # ("§7 Haftung des Überlassenden",
    #     #  "Der Überlassende haftet – soweit gesetzlich zulässig – nicht für Schäden, die durch Ausfall oder Mängel der überlassenen Sachen entstehen, es sei denn, diese beruhen auf Vorsatz oder grober Fahrlässigkeit."),
    #     # ("§8 Technische Ausfälle",
    #     #  "Technische Ausfälle liegen im Bereich des Möglichen und begründen keine Ansprüche auf Schadensersatz oder sonstige Ersatzleistungen."),
    #     ("§7 Haftung des Überlassenden",
    #      "Der Überlassende haftet für Schäden des Nutzers nur bei Vorsatz oder grober Fahrlässigkeit. "
    #      "Bei einfacher Fahrlässigkeit haftet der Überlassende nur bei Verletzung wesentlicher Vertragspflichten (Kardinalpflichten) und begrenzt auf den vertragstypischen, vorhersehbaren Schaden. "
    #      "Die Haftung für Schäden aus der Verletzung des Lebens, des Körpers oder der Gesundheit bleibt unberührt. "
    #      "Eine Haftung für technische Ausfälle oder Mängel der überlassenen Gegenstände besteht nicht, soweit diese nicht auf vorsätzlichem oder grob fahrlässigem Verhalten des Überlassenden beruhen."),
    #     ("§9 Rechte Dritter",
    #      "Alle Forderungen der GEMA sowie Ansprüche Dritter (z. B. aus Urheberrechtsverletzungen oder Lizenzverstößen) trägt der Nutzer allein."),
    #     ("§10 Rückgabe",
    #      "Die Rückgabe darf ausschließlich an den Überlassenden oder dessen Beauftragte erfolgen. "
    #      "Eine vorzeitige Rückgabe begründet keinen Anspruch auf Kostenerstattung."),
    #     ("§11 Übergabebestätigung",
    #      "Der Nutzer bestätigt mit seiner Unterschrift, dass ihm die überlassenen Gegenstände zu beginn des oben genanten Zeitraumes im in §1 beschriebenen Zustand übergeben wurden."),
    #     ("§12 Kaution",
    #      "Es wird eine Kaution in Höhe von __________ Euro vereinbart. Diese ist vor Übergabe in bar zu leisten und wird nach ordnungsgemäßer Rückgabe erstattet, sofern keine Schäden oder Pflichtverletzungen vorliegen."),
    #     ("§13 Kündigung",
    #      "Verstößt der Nutzer wesentlich gegen seine Pflichten, ist der Überlassende zur fristlosen Beendigung der Überlassung berechtigt. "
    #      "Dies gilt insbesondere bei Zahlungsverzug von Schadensersatzforderungen oder bei Verdacht der Zahlungsunfähigkeit."),
    #     ("§14 Salvatorische Klausel",
    #      "Sollten einzelne Bestimmungen dieser Vereinbarung ganz oder teilweise unwirksam, nichtig oder undurchführbar sein oder werden, "
    #      "bleibt die Wirksamkeit der übrigen Regelungen hiervon unberührt."
    #      "<br/><br/>"
    #      "Anstelle der unwirksamen, nichtigen oder undurchführbaren Bestimmung gilt eine solche Regelung als vereinbart, die dem wirtschaftlichen Zweck der ursprünglichen Bestimmung in rechtlich zulässiger Weise am nächsten kommt. "
    #      "Entsprechendes gilt für etwaige Vertragslücken."),
    #     ("§15 Gerichtsstand",
    #      "Gerichtsstand für alle Streitigkeiten aus oder im Zusammenhang mit dieser Vereinbarung ist – soweit gesetzlich zulässig – Waiblingen."),
    # ]

    sections = [
        ("§1 Mietgegenstand",
            ("Der Vermieter überlässt dem Mieter die in der Materialliste aufgeführten beweglichen Sachen zeitlich befristet zur Nutzung.",
            "Die Mietgegenstände bleiben uneingeschränkt Eigentum des Vermieters.",
            "Der Zustand der Mietgegenstände ist gebraucht. Vorschäden sind vor Übergabe zu dokumentieren.")),

        ("§2 Mietdauer",
            ("Die Mietzeit beginnt und endet zu dem vereinbarten Zeitraum. Eine Verlängerung oder Verkürzung der Mietdauer bedarf der schriftlichen Vereinbarung.",
            "Gibt der Mieter die Mietgegenstände nicht rechtzeitig zurück, gerät er ohne weitere Mahnung in Verzug und haftet für daraus entstehende Schäden.")),

        ("§3 Miete und Kosten",
            # ("Für die Überlassung wird eine Miete in Höhe von " + (f"{total_sum:.2f} Euro" if total_sum is not None else "__________ Euro") + " vereinbart.",
            ("Der Überlassende ist berechtigt, eine angemessene Aufwands- und Kostenbeteiligung " + (f"in Höhe von {total_sum:.2f} Euro" if total_sum is not None else "") + " zu erheben. Ein teilweise oder ganzer Verzicht auf die Geltendmachung begründet keinen Rechtsanspruch für zukünftige Mietverhältnisse.",
            "Die Miete stellt einen pauschalen Abnutzungs- und Kostenbeitrag dar.",
            "Kosten für Transport, Auf- und Abbau, Reinigung, Reparatur, Ersatz oder sonstige Aufwendungen, die durch die Nutzung entstehen, trägt der Mieter, soweit er diese zu vertreten hat.",
            "Der Vermieter trägt die Instandhaltung für normalen Verschleiß; der Mieter nur bei von ihm zu vertretenden Schäden.")),

        ("§4 Obhutspflicht und Weitergabe",
            ("Der Mieter ist verpflichtet, die Mietgegenstände pfleglich zu behandeln und vor Verlust, Beschädigung und Zugriff Dritter zu schützen.",
            "Eine Weitergabe oder Untervermietung an Dritte ist ohne ausdrückliche Zustimmung des Vermieters unzulässig.",
            "Die Mietgegenstände dürfen weder veräußert, verpfändet noch sicherungsübereignet werden.")),

        ("§5 Nutzungspflichten",
            ("Der Mieter prüft bei Übergabe auf offensichtliche Mängel und zeigt diese unverzüglich an."
            "Die Nutzung hat ausschließlich gemäß den gesetzlichen Vorschriften und anerkannten technischen Regeln zu erfolgen.",
            "Der Mieter ist für die sachgerechte Bedienung selbst verantwortlich. Der Vermieter schuldet keine Einweisung oder technische Betreuung.",
            "Dies gilt nicht, wenn der Vermieter vorsätzlich oder grob fahrlässig ungeeignete oder erkennbar mangelhafte Geräte überlässt.")),

        ("§6 Haftung des Mieters für Schäden",
            ("Der Mieter haftet für Verlust und Schäden an den Mietgegenständen, soweit diese auf einer schuldhaften Pflichtverletzung des Mieters oder der Personen beruhen, derer er sich zur Nutzung bedient.",
            "Schäden sind dem Vermieter unverzüglich anzuzeigen. Reparaturen oder Eingriffe dürfen nur durch den Vermieter oder durch von ihm beauftragte Personen erfolgen.",
            "Das Öffnen von Geräten oder der Ausbau aus Racks ist untersagt.")),

        ("§7 Schäden, Reparatur und Ersatzpflicht",
            ("(1) Der Mieter trägt während der Mietdauer die Gefahr der zufälligen Verschlechterung oder des zufälligen Untergangs der Mietgegenstände. Zufällige Schäden sind solche, die ohne Verschulden einer Partei eintreten, z. B. durch Naturgewalten wie Blitzschlag, Sturm, Überschwemmung, Wasser, Feuer oder ähnliche unvorhersehbare Ereignisse.",
            "(2) Soweit für den Mieter oder den Vermieter eine Versicherung möglich und marktüblich ist, verpflichtet sich der Mieter, für die Dauer der Mietzeit eine angemessene Versicherung gegen zufällige Schäden (z. B. pauschale Sachversicherung) abzuschließen und dem Vermieter auf Verlangen den Nachweis hierüber vorzulegen. Eine solche Versicherung soll insbesondere Risiken wie Diebstahl, Feuer, Wasser, Blitzschlag oder Transport-/Bewegungsschäden abdecken, soweit sie versicherbar sind.",
            "(3) Kommt der Mieter dieser Versicherungspflicht trotz Aufforderung nicht nach, verbleibt die Gefahr der zufälligen Schäden beim Mieter, ohne Rückgriff auf den Vermieter. Der Vermieter haftet nicht für zufällige Schäden an den Mietgegenständen, es sei denn, der Vermieter hat diese durch vorsätzliches oder grob fahrlässiges Verhalten verursacht.",
            "(4) Leistungen einer bestehenden Versicherung werden auf mögliche Ersatzansprüche des Vermieters angerechnet; übersteigende Leistungen stehen dem Mieter zu, soweit sie ihm vertraglich oder gesetzlich zustehen.")),

        ("§8 Reparatur und Ersatz",
            ("Der Vermieter ist im Schadensfall berechtigt, nach eigener Wahl eine fachgerechte Reparatur durchführen zu lassen oder Ersatz zum Wiederbeschaffungswert zu verlangen, sofern eine Reparatur technisch unmöglich oder wirtschaftlich nicht sinnvoll ist.",
            "Ein Abzug „neu für alt“ bleibt vorbehalten.",
            "Leistungen einer Versicherung werden auf den Ersatzanspruch angerechnet.")),

        ("§9 Haftung des Vermieters",
            ("Der Vermieter haftet für Schäden des Mieters nur bei Vorsatz oder grober Fahrlässigkeit.",
            "Bei einfacher Fahrlässigkeit haftet der Vermieter nur bei Verletzung wesentlicher Vertragspflichten (Kardinalpflichten) und begrenzt auf den vertragstypischen, vorhersehbaren Schaden.",
            "Die Haftung für Schäden aus der Verletzung des Lebens, des Körpers oder der Gesundheit bleibt unberührt.")),

        ("§10 Rechte Dritter",
            ("Alle Gebühren und Ansprüche Dritter, insbesondere GEMA-Forderungen oder Ansprüche aus Urheberrechts- und Lizenzverstößen im Zusammenhang mit der Nutzung, trägt der Mieter.",)),

        ("§11 Rückgabe",
            ("Die Rückgabe hat vollständig, funktionsfähig und in gereinigtem Zustand an den Vermieter oder dessen Beauftragte zu erfolgen.",
            "Eine vorzeitige Rückgabe begründet keinen Anspruch auf Rückerstattung der Miete.")),
        ("§12 Kaution",
            ("Es wird eine Kaution in Höhe von __________ Euro vereinbart. Diese ist vor Übergabe zu leisten und wird nach ordnungsgemäßer Rückgabe erstattet, sofern keine Schäden oder offenen Forderungen bestehen.",)),

        ("§13 Kündigung",
            ("Verstößt der Mieter wesentlich gegen seine Vertragspflichten, ist der Vermieter zur fristlosen Kündigung berechtigt.",)),

        ("§14 Salvatorische Klausel",
            ("Sollten einzelne Bestimmungen dieses Vertrags unwirksam sein oder werden, bleibt die Wirksamkeit der übrigen Regelungen unberührt.",)),

        ("§15 Gerichtsstand",
            ("Gerichtsstand für alle Streitigkeiten aus diesem Vertrag ist – soweit gesetzlich zulässig – Waiblingen.",))
    ]


    for head, body in sections:
        story.append(Paragraph(head, normal_small))
        story.append(Paragraph("<br/>".join(body), very_small))

    story.append(Spacer(1, 30))

    # Signature lines on page 2 appear at the end of the right column in the source.
    # (In two-column flow, they'll naturally end up in the right column once the text flows.)
    story.append(HLine(width=70 * mm, thickness=0.9, space_before=0, space_after=2))
    story.append(Paragraph("Ort, Datum Nutzer", small))
    story.append(Spacer(1, 20))
    story.append(HLine(width=70 * mm, thickness=0.9, space_before=0, space_after=2))
    story.append(Paragraph("Unterschrift Nutzer", small))
    story.append(Spacer(1, 30))
    story.append(HLine(width=70 * mm, thickness=0.9, space_before=0, space_after=2))
    story.append(Paragraph("Ort, Datum Überlasser", small))
    story.append(Spacer(1, 20))
    story.append(HLine(width=70 * mm, thickness=0.9, space_before=0, space_after=2))
    story.append(Paragraph("Unterschrift Überlasser", small))

    # Build
    doc.build(story)

    return buf.getvalue()


def file():
    pdf_bytes = _build_pdf_bytes(consignor_info=[
        "Firma XYZ GmbH",
        "Musterstraße 1",
        "12345 Musterstadt",
        "Telefon: 01234 567890",
        "E-Mail: info@firma-xyz.de",
    ], timeframe_str="01.01.2024 - 31.01.2024", items=[
        (1, "Lichtanlage ABC Model XLichtanlage ABC Model XLichtanlage ABC Model XLichtanlage ABC Model XLichtanlage ABC Model X"),
        (1, "Tonanlage DEF Model Y"),
        (1, "Mikrofon GHI Model Z"),
        (1, "Nebelmaschine JKL Model W"),
        (1, "Stromverteiler MNO Model V"),
    ])
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name="ueberlassungsbestaetigung.pdf",
        max_age=0,
    )


if __name__ == "__main__":
    from flask import Flask, send_file
    app = Flask(__name__)
    app.add_url_rule("/", "file", file)
    # pip install flask reportlab
    app.run(debug=True, host="0.0.0.0", port=5001)