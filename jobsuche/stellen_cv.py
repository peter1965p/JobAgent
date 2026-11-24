"""Lebenslauf für eine bestimmte Stelle: vorn eine Seite „Qualifikationsprofil“, dahinter der unveränderte Lebenslauf.

Die Seite stellt die Anforderungen der Anzeige den wörtlichen Nachweisen aus Lebenslauf und Zeugnis gegenüber.
Das Zitat prüft `ki.pruefe_eignung` gegen den Originaltext, also steht hier nichts Erfundenes. Die Originaldatei
bleibt unangetastet, die neue Datei liegt neben der Datenbank.
"""

import json
import re
import sqlite3
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate

from . import config, db, pdf, person

QUELLEN_NAME = {"zeugnis": "Arbeitszeugnis", "lebenslauf": "Lebenslauf", "angabe": "Eigene Angabe"}


def ordner() -> Path:
    return db.datenordner() / "cv"


def _dateiname(firma: str) -> str:
    return re.sub(r"[^\w-]+", "_", firma).strip("_")[:60] or "Firma"


def stellentitel(conn: sqlite3.Connection, firma_id: int) -> str:
    zeile = conn.execute("SELECT text FROM chunks WHERE art = 'firma' AND firma_id = ? AND text LIKE 'Stellenangebot:%' "
                         "ORDER BY id LIMIT 1", (firma_id,)).fetchone()
    treffer = re.match(r"Stellenangebot:\s*(.+?)\.\s+(Beruf|Ort):", zeile[0]) if zeile else None
    return treffer.group(1).split(" · ")[0].strip() if treffer else ""  # „· Vollzeit“ gehört nicht zum Titel


def _original(conn: sqlite3.Connection) -> Path | None:
    for (pfad,) in conn.execute("SELECT pfad FROM dokumente WHERE art = 'lebenslauf' AND anhang = 1 ORDER BY id"):
        if Path(pfad).suffix.lower() == ".pdf" and Path(pfad).is_file():
            return Path(pfad)
    return None


def _profilseite(conn: sqlite3.Connection, firma: str, titel: str, nachweise: list[dict], ziel: Path) -> None:
    p = person.lade(conn)
    stile = pdf.stile()
    kontakt = " · ".join(t for t in (p["ort"], p["email"], p["telefon"]) if t)
    stelle = f"für die Stelle „{titel}“ bei {firma}" if titel else f"für {firma}"
    story = [Paragraph("Qualifikationsprofil", stile["titel"]),
             Paragraph(escape(f"{p['name']} · {stelle}"), stile["unter"]),
             Paragraph(escape(kontakt), stile["unter"]),
             Paragraph("Ihre Anforderungen und meine Nachweise", stile["kopf"])]
    for n in nachweise:
        story.append(KeepTogether([
            Paragraph(escape(n["anforderung"]), stile["station"]),
            Paragraph(escape(n["zitat"] if n["art"] == "angabe" else f"»{n['zitat']}«"), stile["punkt"], bulletText="✓"),
            Paragraph(escape(QUELLEN_NAME.get(n["art"], "Unterlagen")), stile["ort"]),
        ]))
    hinweis = "Zitate sind wörtliche Auszüge aus Lebenslauf und Arbeitszeugnis, beide liegen bei."
    if any(n["art"] == "angabe" for n in nachweise):
        hinweis += " Eigene Angaben sind als solche gekennzeichnet."
    story.append(Paragraph(hinweis, stile["unter"]))
    fuss = pdf.fusszeile(f"{p['name']} · Qualifikationsprofil · {pdf.stand()}")
    SimpleDocTemplate(str(ziel), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                      bottomMargin=20 * mm, title=f"Qualifikationsprofil {p['name']}", author=p["name"],
                      ).build(story, onFirstPage=fuss, onLaterPages=fuss)


def erstelle(conn: sqlite3.Connection, firma_id: int) -> list[Path]:
    """Die Dateien, die statt des allgemeinen Lebenslaufs mitgehen. Leer, wenn es keine Nachweise gibt.
    Ist der Lebenslauf ein lesbares PDF, wird daraus eine Datei, sonst liegen Profilseite und Lebenslauf getrennt bei."""
    zeile = conn.execute("SELECT name, ki_eignung FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    eignung = json.loads(zeile["ki_eignung"]) if zeile and zeile["ki_eignung"] else {}
    nachweise = eignung.get("nachweise") or []
    if not nachweise:
        return []
    ordner().mkdir(parents=True, exist_ok=True)
    name = _dateiname(zeile["name"])
    seite = ordner() / f"Qualifikationsprofil_{name}.pdf"
    _profilseite(conn, zeile["name"], stellentitel(conn, firma_id), nachweise, seite)
    original = _original(conn)
    if not original:
        return [seite]
    ziel = ordner() / f"Lebenslauf_{name}.pdf"
    try:
        schreiber = PdfWriter()
        for datei in (seite, original):
            for blatt in PdfReader(str(datei)).pages:
                schreiber.add_page(blatt)
        with open(ziel, "wb") as aus:
            schreiber.write(aus)
    except (PyPdfError, OSError, ValueError):
        return [seite, original]
    seite.unlink(missing_ok=True)
    db.protokolliere(conn, "Lebenslauf für die Stelle erstellt", f"{ziel.name}, {len(nachweise)} Nachweise", firma_id)
    return [ziel]


def anhaenge_fuer(conn: sqlite3.Connection, firma_id: int) -> list[Path] | None:
    """Anhänge für eine Mail an diese Firma: Lebenslauf für die Stelle plus alle übrigen Anhänge wie das Zeugnis.
    None heißt: die allgemeinen Anhänge aus „Unterlagen“."""
    neu = erstelle(conn, firma_id)
    if not neu:
        return None
    uebrige = [Path(z[0]) for z in conn.execute(
        "SELECT pfad FROM dokumente WHERE anhang = 1 AND art != 'lebenslauf' ORDER BY id")]
    return neu + [p for p in uebrige if p not in neu]
