"""Angaben, die die Suche braucht: aus den Unterlagen übernommen, sonst bei der Person erfragt.

Interne Angaben (Gehalt, Verfügbarkeit) dienen nur der lokalen Bewertung und erreichen keinen Mailentwurf.
Alle anderen Antworten werden zu Fakten und dürfen in Mails stehen.
"""

import re
import sqlite3
from dataclasses import dataclass

from . import db, profil, regeln

UEBERSPRUNGEN = "übersprungen"


@dataclass(frozen=True)
class Frage:
    schluessel: str
    titel: str
    frage: str
    beispiel: str
    intern: bool
    muster: str = ""
    nur_agenda: bool = False


FRAGEN = [
    Frage("gehalt", "Gehalt", "Welches Bruttogehalt stellst du dir vor?", "Zum Beispiel 60.000 € brutto", True,
          r"\d{2,3}\.\d{3}\s*(?:bis|–|-)\s*\d{2,3}\.\d{3}\s*€\s*brutto"),
    Frage("verfuegbar", "Verfügbarkeit", "Ab wann könntest du anfangen?", "Zum Beispiel ab sofort", True,
          r"verfügbar ab[^.\n]{0,40}|kündigungsfrist[^.\n]{0,40}"),
    Frage("fuehrerschein", "Führerschein", "Welchen Führerschein hast du?", "Zum Beispiel Klasse B, eigenes Auto", False,
          r"[^.\n]{0,40}führerschein[^.\n]{0,40}"),
    Frage("reisen", "Reisebereitschaft", "Wie weit und wie oft kannst du für Einsätze reisen?",
          "Zum Beispiel bundesweit, mehrere Tage am Stück", False, r"[^.\n]{0,40}reisebereit[^.\n]{0,40}"),
    Frage("koerperlich", "Praktisches Können", "Was kannst du körperlich und handwerklich leisten?",
          "Zum Beispiel Server und Hardware tragen, Montage, Verkabelung. Bitte ohne Gesundheitsangaben", False),
    Frage("einstieg", "Einstieg", "Wie soll der Einstieg bei einer Firma aussehen?",
          "Zum Beispiel Festanstellung mit Probezeit, die ersten Wochen als Probeprojekt", True),
    Frage("eigene_projekte", "Eigene Projekte", "Welche eigenen Projekte bleiben dein Eigentum?",
          "Zum Beispiel vom Arbeitsverhältnis ausgenommene Projekte", True, nur_agenda=True),
    Frage("ziel_6m", "Ziel in 6 Monaten", "Wo willst du in 6 Monaten stehen?",
          "Zum Beispiel Umzug abgeschlossen, feste Rolle im Team", True, nur_agenda=True),
    Frage("ziel_5j", "Ziel in 5 Jahren", "Wo willst du in 5 Jahren stehen?",
          "Zum Beispiel Verantwortung für den Betrieb von KI im Unternehmen", True, nur_agenda=True),
    Frage("von_firma", "Was du von der Firma brauchst", "Was brauchst du von einer Firma, um dich zu entscheiden?",
          "Zum Beispiel Zeitplan, erste Projekte, Einschätzung zum Gehalt", True, nur_agenda=True),
]


def _unterlagen(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Nur Lebenslauf und Zeugnis zählen. Sonstiges wie Notizen liefert keine verbindlichen Angaben."""
    return [(titel, re.sub(r"\s+", " ", text)) for titel, text in conn.execute(
        "SELECT d.titel, group_concat(c.text, ' ') FROM chunks c JOIN dokumente d ON d.id = c.dokument_id "
        "WHERE d.art != 'sonstiges' GROUP BY d.id ORDER BY d.id")]


def _speichere(conn: sqlite3.Connection, frage: Frage, wert: str, quelle: str) -> None:
    conn.execute(
        "INSERT INTO angaben (schluessel, wert, quelle, zeit) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(schluessel) DO UPDATE SET wert = excluded.wert, quelle = excluded.quelle, zeit = excluded.zeit",
        (frage.schluessel, wert, quelle, db.jetzt()),
    )
    conn.commit()
    # Interne Werte stehen nicht im Klartext im Protokoll.
    db.protokolliere(conn, "Angabe gespeichert", f"{frage.titel} ({quelle})")


def uebernimm_aus_unterlagen(conn: sqlite3.Connection) -> int:
    """Übernimmt, was in CV oder Zeugnis steht. Deine eigenen Antworten bleiben unangetastet."""
    vorhanden = {z[0] for z in conn.execute("SELECT schluessel FROM angaben")}
    neu = 0
    unterlagen = _unterlagen(conn)
    for frage in FRAGEN:
        if frage.schluessel in vorhanden or not frage.muster:
            continue
        for titel, text in unterlagen:
            treffer = re.search(frage.muster, text, re.I)
            if not treffer:
                continue
            wert = treffer.group(0).strip()
            if not frage.intern:
                if regeln.ist_sensibel(wert):
                    continue
                # Ein Textausschnitt aus einem PDF kann unsauber sein: Er bleibt ein Vorschlag.
                profil.lege_fakt_an(conn, "praktisch", wert, titel)
            _speichere(conn, frage, wert, titel)
            neu += 1
            break
    return neu


def markiere_erledigt(conn: sqlite3.Connection, schluessel: str, wert: str, quelle: str) -> None:
    """Für Antworten, die anderswo schon gegeben wurden, etwa im CV-Assistenten."""
    _speichere(conn, next(f for f in FRAGEN if f.schluessel == schluessel), wert, quelle)


def offen(conn: sqlite3.Connection) -> list[Frage]:
    vorhanden = {z[0] for z in conn.execute("SELECT schluessel FROM angaben")}
    return [f for f in FRAGEN if f.schluessel not in vorhanden]


def beantworte(conn: sqlite3.Connection, schluessel: str, wert: str) -> None:
    frage = next(f for f in FRAGEN if f.schluessel == schluessel)
    wert = wert.strip()
    if not wert:
        raise ValueError("Die Antwort ist leer.")
    if regeln.GESUNDHEIT.search(wert):
        raise ValueError(profil.SENSIBEL_MELDUNG)
    if not frage.intern:
        # Wirft einen Fehler bei Gesundheitsangaben: Dann wird auch nichts gespeichert.
        profil.lege_eigenen_fakt_an(conn, "praktisch", f"{frage.titel}: {wert}")
    _speichere(conn, frage, wert, "von dir")


def ueberspringe(conn: sqlite3.Connection, schluessel: str) -> None:
    frage = next(f for f in FRAGEN if f.schluessel == schluessel)
    _speichere(conn, frage, "", UEBERSPRUNGEN)


def hat_uebersprungene(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM angaben WHERE quelle = ?", (UEBERSPRUNGEN,)).fetchone() is not None


def frage_erneut(conn: sqlite3.Connection) -> None:
    """Stellt übersprungene Fragen wieder."""
    conn.execute("DELETE FROM angaben WHERE quelle = ?", (UEBERSPRUNGEN,))
    conn.commit()


def interne_kriterien(conn: sqlite3.Connection) -> str:
    """Nur für die lokale Bewertung. `ki.erstelle_entwurf` ruft das bewusst nicht auf."""
    werte = dict(tuple(z) for z in conn.execute("SELECT schluessel, wert FROM angaben WHERE wert != ''"))
    return "\n".join(f"{f.titel}: {werte[f.schluessel]}" for f in FRAGEN
                     if f.intern and not f.nur_agenda and f.schluessel in werte)


def uebersicht(conn: sqlite3.Connection) -> list[dict]:
    """Interne Angaben zum Ansehen und Ändern. Die übrigen Antworten stehen unter den Fakten."""
    werte = {z["schluessel"]: z for z in conn.execute("SELECT * FROM angaben")}
    return [{"schluessel": f.schluessel, "titel": f.titel, "wert": werte[f.schluessel]["wert"],
             "quelle": werte[f.schluessel]["quelle"], "frage": f.frage}
            for f in FRAGEN if f.intern and f.schluessel in werte and werte[f.schluessel]["wert"]]
