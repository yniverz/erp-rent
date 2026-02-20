"""
Shared base utilities for PDF generation (reportlab).

Provides common styles, header/footer drawing, and helper flowables.
"""
from __future__ import annotations

import os
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Flowable,
)


# ─── Colour palette ──────────────────────────────────────────────
CLR_BLACK = colors.black
CLR_GREY_LIGHT = colors.HexColor("#f5f5f5")
CLR_GREY_MID = colors.HexColor("#d9d9d9")
CLR_GREY_DARK = colors.HexColor("#666666")
CLR_TABLE_HEADER_BG = colors.HexColor("#e8e8e8")

# ─── Page metrics ─────────────────────────────────────────────────
PAGE_W, PAGE_H = A4
MARGIN_LEFT = 20 * mm
MARGIN_RIGHT = 20 * mm
MARGIN_TOP = 5 * mm
MARGIN_BOTTOM = 30 * mm

CONTENT_W = PAGE_W - MARGIN_LEFT - MARGIN_RIGHT

HEADER_HEIGHT = 70 * mm   # space reserved for header (address blocks, etc.)
FOOTER_HEIGHT = MARGIN_BOTTOM


# ─── Reusable style factory ──────────────────────────────────────
def _base_styles():
    """Return a dict of ParagraphStyles used across all doc types."""
    ss = getSampleStyleSheet()
    base = ParagraphStyle("Base", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=9, leading=11, spaceAfter=0)
    return {
        "base": base,
        "title": ParagraphStyle("DocTitle", parent=base, fontName="Helvetica-Bold",
                                fontSize=16, leading=19, spaceAfter=4),
        "subtitle": ParagraphStyle("SubTitle", parent=base, fontName="Helvetica-Bold",
                                   fontSize=11, leading=13, spaceAfter=2),
        "normal": ParagraphStyle("Norm", parent=base, spaceAfter=2),
        "small": ParagraphStyle("Small", parent=base, fontSize=7.5, leading=9.5, spaceAfter=1),
        "bold": ParagraphStyle("Bold", parent=base, fontName="Helvetica-Bold"),
        "right": ParagraphStyle("Right", parent=base, alignment=2),  # TA_RIGHT
        "right_bold": ParagraphStyle("RightBold", parent=base, fontName="Helvetica-Bold", alignment=2),
        "center": ParagraphStyle("Center", parent=base, alignment=1),
        "meta": ParagraphStyle("Meta", parent=base, fontSize=9, leading=11, spaceAfter=0),
        "table_header": ParagraphStyle("TH", parent=base, fontName="Helvetica-Bold",
                                        fontSize=8.5, leading=10),
        "table_cell": ParagraphStyle("TC", parent=base, fontSize=8.5, leading=10),
        "table_cell_right": ParagraphStyle("TCR", parent=base, fontSize=8.5, leading=10, alignment=2),
        "table_cell_bold": ParagraphStyle("TCB", parent=base, fontName="Helvetica-Bold",
                                           fontSize=8.5, leading=10),
        "table_cell_indent": ParagraphStyle("TCI", parent=base, fontSize=8, leading=9.5,
                                             leftIndent=8, textColor=CLR_GREY_DARK),
        "footer": ParagraphStyle("Footer", parent=base, fontSize=7, leading=9,
                                  textColor=CLR_GREY_DARK),
    }


# ─── Helper flowables ────────────────────────────────────────────
class HLine(Flowable):
    """A thin horizontal line with configurable width."""
    def __init__(self, width: float = CONTENT_W, thickness: float = 0.6,
                 color=CLR_BLACK, space_before=4, space_after=4):
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


# ─── Common page callbacks ───────────────────────────────────────
def _draw_header(canvas, doc, *,
                 issuer_name: str,
                 issuer_address: list[str],
                 recipient_lines: list[str],
                 meta_lines: list[tuple[str, str]],
                 logo_path: str | None = None):
    """Draw the standard header block (sender line, recipient, meta, logo)."""
    canvas.saveState()

    # ── Logo (top-right) ──
    if logo_path and os.path.exists(logo_path):
        try:
            max_h = 30 * mm
            max_w = 55 * mm
            ext = os.path.splitext(logo_path)[1].lower()

            if ext == '.svg':
                # SVG: convert via svglib
                from svglib.svglib import svg2rlg
                from reportlab.graphics import renderPDF
                drawing = svg2rlg(logo_path)
                if drawing:
                    iw, ih = drawing.width, drawing.height
                    ratio = min(max_w / iw, max_h / ih, 1)
                    draw_w, draw_h = iw * ratio, ih * ratio
                    drawing.width = draw_w
                    drawing.height = draw_h
                    drawing.scale(ratio, ratio)
                    x = PAGE_W - MARGIN_RIGHT - draw_w
                    y = PAGE_H - MARGIN_TOP - draw_h
                    renderPDF.draw(drawing, canvas, x, y)
            else:
                # Raster image (PNG, JPEG, etc.)
                img = ImageReader(logo_path)
                iw, ih = img.getSize()
                ratio = min(max_w / iw, max_h / ih, 1)
                draw_w, draw_h = iw * ratio, ih * ratio
                x = PAGE_W - MARGIN_RIGHT - draw_w
                y = PAGE_H - MARGIN_TOP - draw_h
                canvas.drawImage(img, x, y, draw_w, draw_h, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass  # silently skip broken logo

    # ── Sender line (small, above recipient) ──
    sender_str = issuer_name
    if issuer_address:
        sender_str += " – " + " – ".join(issuer_address[:2])
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(CLR_GREY_DARK)
    y_sender = PAGE_H - MARGIN_TOP - 35 * mm
    canvas.drawString(MARGIN_LEFT, y_sender, sender_str)

    # ── Recipient block ──
    canvas.setFillColor(CLR_BLACK)
    y_recip = y_sender - 14
    for i, line in enumerate(recipient_lines[:6]):
        if i == 0:
            canvas.setFont("Helvetica-Bold", 10)
        else:
            canvas.setFont("Helvetica", 10)
        canvas.drawString(MARGIN_LEFT, y_recip - i * 13, line)

    # ── Meta block (right side, below logo) ──
    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(CLR_BLACK)
    x_meta_label = PAGE_W - MARGIN_RIGHT - 70 * mm
    x_meta_value = PAGE_W - MARGIN_RIGHT - 32 * mm
    y_meta_start = y_sender - 14
    for i, (label, value) in enumerate(meta_lines[:6]):
        y = y_meta_start - i * 12
        canvas.drawString(x_meta_label, y, label)
        canvas.drawString(x_meta_value, y, value)

    canvas.restoreState()


def _draw_footer(canvas, doc, *,
                 issuer_name: str,
                 issuer_address: list[str],
                 contact_lines: list[str],
                 bank_lines: list[str],
                 tax_number: str | None = None,
                 vat_id: str | None = None):
    """Draw the 3-column footer with business info."""
    canvas.saveState()

    y_line = MARGIN_BOTTOM - 2 * mm
    canvas.setStrokeColor(CLR_GREY_MID)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN_LEFT, y_line, PAGE_W - MARGIN_RIGHT, y_line)

    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(CLR_GREY_DARK)

    col_w = CONTENT_W / 3
    x1 = MARGIN_LEFT
    x2 = MARGIN_LEFT + col_w
    x3 = MARGIN_LEFT + 2 * col_w

    left_lines = [issuer_name] + issuer_address[:3]
    mid_lines = list(contact_lines[:3])
    # Add tax identifiers below contact lines
    if tax_number:
        mid_lines.append(f"St.-Nr.: {tax_number}")
    if vat_id:
        mid_lines.append(f"USt-IdNr: {vat_id}")
    right_lines = bank_lines[:5]

    dy = 8.5
    y_start = y_line - 10
    for i, t in enumerate(left_lines):
        canvas.drawString(x1, y_start - i * dy, t)
    for i, t in enumerate(mid_lines):
        canvas.drawString(x2, y_start - i * dy, t)
    for i, t in enumerate(right_lines):
        canvas.drawString(x3, y_start - i * dy, t)

    # Page number
    canvas.drawRightString(PAGE_W - MARGIN_RIGHT, 8 * mm,
                           f"Seite {canvas.getPageNumber()}")

    canvas.restoreState()


# ─── Document builder helper ─────────────────────────────────────
def build_base_doc(buf: BytesIO, title: str, author: str,
                   on_page_callback, *, extra_top_space: float = 0):
    """Create a BaseDocTemplate with a single-column frame and the given on_page callback.
    Returns (doc, frame_width) so the caller can build the story.
    """
    frame_top = MARGIN_TOP + HEADER_HEIGHT + extra_top_space
    frame_height = PAGE_H - frame_top - MARGIN_BOTTOM

    frame = Frame(
        MARGIN_LEFT, MARGIN_BOTTOM,
        CONTENT_W, frame_height,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="main",
    )

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN_LEFT, rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
        title=title, author=author,
        pageTemplates=[PageTemplate(id="default", frames=[frame], onPage=on_page_callback)],
    )
    return doc, CONTENT_W


# ─── Price formatting helpers ────────────────────────────────────
def fmt_eur(value: float) -> str:
    """Format a float as Euro string (German style)."""
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_percent(value: float) -> str:
    return f"{value:,.2f} %".replace(",", "X").replace(".", ",").replace("X", ".")
