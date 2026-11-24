"""Erzeugt die Agenda als PDF aus bestätigten Fakten und den Antworten der Person. Ohne LLM, also ohne Erfindungen.

Die Agenda enthält das Gehalt. Sie ist für die Zeit nach dem ersten Gespräch gedacht und kein Mailanhang.
"""

import sqlite3
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate

from . import angaben, config, db, pdf, person


def _fakten(conn: sqlite3.Connection, kategorie: str) -> list[str]:
    return [z[0] for z in conn.execute(
        "SELECT text FROM fakten WHERE status = 'bestaetigt' AND kategorie = ? ORDER BY id", (kategorie,))]


def abschnitte(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """Der Inhalt der Agenda. Übersprungene Angaben und leere Abschnitte fallen weg."""
    werte = {z["schluessel"]: z["wert"] for z in conn.execute("SELECT schluessel, wert FROM angaben WHERE wert != ''")}
    suche = _fakten(conn, "suche")
    if "gehalt" in werte:
        suche.append(f"Gehaltsvorstellung: {werte['gehalt']}")
    if "einstieg" in werte:
        suche.append(f"Einstieg: {werte['einstieg']}")
    if "verfuegbar" in werte:
        suche.append(f"Verfügbar: {werte['verfuegbar']}")
    if "eigene_projekte" in werte:
        suche.append(f"Eigene Projekte: {werte['eigene_projekte']}")
    ziele = []
    if "ziel_6m" in werte:
        ziele.append(f"In 6 Monaten: {werte['ziel_6m']}")
    if "ziel_5j" in werte:
        ziele.append(f"In 5 Jahren: {werte['ziel_5j']}")
    if "perspektive" in werte:
        ziele.append(f"Langfristig: {werte['perspektive']}")
    inhalt = [
        ("Wer ich bin", _fakten(conn, "erfahrung")),
        ("Was ich gebaut habe", _fakten(conn, "projekt")),
        ("Plattformen und Kenntnisse", _fakten(conn, "technik")),
        ("Praktisches Können", _fakten(conn, "praktisch")),
        ("Was ich suche", suche),
        ("Wohin ich will", ziele),
        ("Was ich von Ihnen brauche", [werte["von_firma"]] if "von_firma" in werte else []),
    ]
    return [(titel, punkte) for titel, punkte in inhalt if punkte]


def erstelle(conn: sqlite3.Connection, ziel: Path | None = None) -> Path:
    offen = angaben.offen(conn)
    if offen:
        raise ValueError(f"Es sind noch {len(offen)} Fragen offen. Beantworte oder überspringe sie unter „Fakten“.")
    inhalt = abschnitte(conn)
    if not inhalt:
        raise ValueError("Für eine Agenda fehlen noch Fakten und Angaben.")

    name = person.name(conn)
    if not name:
        raise ValueError("Trage zuerst deinen Namen unter „Profil“ ein.")
    stand = pdf.stand()
    stile = pdf.stile()
    position = _fakten(conn, "positionierung")
    unterzeile = " · ".join([name, *position, stand])
    story = [Paragraph("Agenda", stile["titel"]), Paragraph(escape(unterzeile), stile["unter"])]
    for nummer, (titel, punkte) in enumerate(inhalt, 1):
        kopf = Paragraph(f"{nummer}. {escape(titel)}", stile["kopf"])
        absaetze = [Paragraph(escape(p), stile["punkt"], bulletText="•") for p in punkte]
        # Überschrift und erster Punkt bleiben zusammen, lange Abschnitte dürfen umbrechen.
        story.append(KeepTogether([kopf, absaetze[0]]))
        story.extend(absaetze[1:])

    # Eigener Ordner: Vorhandene Dateien an anderen Orten werden nie überschrieben.
    ziel = ziel or db.datenordner() / "agenda" / f"Agenda_{date.today().isoformat()}.pdf"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fuss = pdf.fusszeile(f"{name} · Agenda · {stand}")
    SimpleDocTemplate(
        str(ziel), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
        title=f"Agenda {name}", author=name,
    ).build(story, onFirstPage=fuss, onLaterPages=fuss)
    db.protokolliere(conn, "Agenda erstellt", ziel.name)
    return ziel
