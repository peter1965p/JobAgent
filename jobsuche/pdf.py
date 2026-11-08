"""Gemeinsame PDF-Bausteine. Die Schrift kommt aus matplotlib und läuft damit auf jedem System."""

from datetime import date
from pathlib import Path

import matplotlib
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]
INK = colors.HexColor("#1f2937")
BLAU = colors.HexColor("#1d4ed8")
GRAU = colors.HexColor("#6b7280")


def stand() -> str:
    heute = date.today()
    return f"Stand {MONATE[heute.month - 1]} {heute.year}"


def stile() -> dict:
    schriften = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    pdfmetrics.registerFont(TTFont("Sans", str(schriften / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("Sans-Bold", str(schriften / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")
    p = ParagraphStyle("p", fontName="Sans", fontSize=9.5, leading=14, textColor=INK, spaceAfter=6)
    return {
        "text": p,
        "titel": ParagraphStyle("titel", fontName="Sans-Bold", fontSize=22, leading=27, textColor=INK),
        "unter": ParagraphStyle("unter", fontName="Sans", fontSize=10, leading=14, textColor=GRAU, spaceAfter=6),
        "kopf": ParagraphStyle("kopf", fontName="Sans-Bold", fontSize=12.5, leading=16, textColor=BLAU,
                               spaceBefore=11, spaceAfter=5),
        "punkt": ParagraphStyle("punkt", parent=p, leftIndent=12, bulletIndent=2, spaceAfter=3,
                                bulletFontName="Sans", bulletFontSize=9.5),
        "station": ParagraphStyle("station", parent=p, fontName="Sans-Bold", spaceAfter=0, spaceBefore=5),
        "ort": ParagraphStyle("ort", parent=p, textColor=GRAU, fontSize=8.8, spaceAfter=2),
    }


def fusszeile(text: str):
    def zeichne(canvas, doc):
        canvas.saveState()
        canvas.setFont("Sans", 8)
        canvas.setFillColor(GRAU)
        canvas.drawString(20 * mm, 12 * mm, text)
        canvas.drawRightString(190 * mm, 12 * mm, f"Seite {doc.page}")
        canvas.restoreState()
    return zeichne
