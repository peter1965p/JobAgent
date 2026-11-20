"""Unterlagen einlesen und Fakten verwalten."""

import hashlib
import json
import sqlite3
from pathlib import Path

from docx import Document
from pypdf import PdfReader

from . import db, llm, rag, regeln

UNTERLAGEN_ARTEN = {"lebenslauf": "Lebenslauf", "zeugnis": "Zeugnis", "sonstiges": "Sonstiges (nur Hintergrund)"}
KATEGORIEN = ["positionierung", "erfahrung", "projekt", "technik", "praktisch", "suche"]


def lies_datei(pfad: Path) -> str:
    endung = pfad.suffix.lower()
    if endung == ".pdf":
        return "\n\n".join(seite.extract_text() or "" for seite in PdfReader(str(pfad)).pages)
    if endung == ".docx":
        return "\n\n".join(p.text for p in Document(str(pfad)).paragraphs)
    return pfad.read_text(encoding="utf-8")


def fuege_unterlage_hinzu(conn: sqlite3.Connection, pfad: Path, art: str, anhang: bool, titel: str = "") -> int:
    if art not in UNTERLAGEN_ARTEN:
        raise ValueError("Unbekannte Art der Unterlage.")
    if not Path(pfad).exists():
        raise ValueError(f"Datei nicht gefunden: {pfad}")
    cursor = conn.execute(
        "INSERT INTO dokumente (pfad, titel, sha256, eingelesen, art, anhang) VALUES (?, ?, '', ?, ?, ?) "
        "ON CONFLICT(pfad) DO UPDATE SET art = excluded.art, anhang = excluded.anhang "
        "RETURNING id",
        (str(pfad), titel or Path(pfad).stem, db.jetzt(), art, int(anhang)),
    )
    dokument_id = cursor.fetchone()[0]
    conn.commit()
    return dokument_id


def entferne_unterlage(conn: sqlite3.Connection, dokument_id: int) -> None:
    """Nimmt die Unterlage samt Index und daraus übernommenen Angaben heraus. Die Datei bleibt liegen."""
    zeile = conn.execute("SELECT titel FROM dokumente WHERE id = ?", (dokument_id,)).fetchone()
    conn.execute("DELETE FROM dokumente WHERE id = ?", (dokument_id,))
    conn.execute("DELETE FROM angaben WHERE quelle = ?", (zeile["titel"],))
    conn.commit()
    db.protokolliere(conn, "Unterlage entfernt", zeile["titel"])


def setze_anhang(conn: sqlite3.Connection, dokument_id: int, anhang: bool) -> None:
    conn.execute("UPDATE dokumente SET anhang = ? WHERE id = ?", (int(anhang), dokument_id))
    conn.commit()


def anhaenge(conn: sqlite3.Connection) -> list[Path]:
    return [Path(z[0]) for z in conn.execute("SELECT pfad FROM dokumente WHERE anhang = 1 ORDER BY id")]


def anhaenge_des_entwurfs(conn: sqlite3.Connection, entwurf_id: int) -> list[Path]:
    """Eigene Anhänge des Entwurfs, etwa der Lebenslauf für die Stelle, sonst die allgemeinen."""
    zeile = conn.execute("SELECT anhaenge FROM entwuerfe WHERE id = ?", (entwurf_id,)).fetchone()
    if zeile and zeile[0] is not None:
        return [Path(p) for p in json.loads(zeile[0])]
    return anhaenge(conn)


def setze_anhaenge_des_entwurfs(conn: sqlite3.Connection, entwurf_id: int, pfade: list[Path]) -> None:
    """Legt die Anhänge dieser einen Mail fest. Wie eine Textänderung kostet das die Freigabe."""
    e = conn.execute("SELECT status FROM entwuerfe WHERE id = ?", (entwurf_id,)).fetchone()
    if not e or e[0] not in ("entwurf", "freigegeben"):
        raise ValueError("Bei gesendeten Mails lassen sich die Anhänge nicht mehr ändern.")
    eindeutig = list(dict.fromkeys(str(Path(p)) for p in pfade))
    conn.execute("UPDATE entwuerfe SET anhaenge = ?, status = 'entwurf', freigegeben_am = NULL WHERE id = ?",
                 (json.dumps(eindeutig), entwurf_id))
    conn.commit()


def fuege_anhang_zum_entwurf(conn: sqlite3.Connection, entwurf_id: int, pfad: Path) -> None:
    pfad = Path(pfad)
    if not pfad.is_file():
        raise ValueError(f"Datei nicht gefunden: {pfad.name}")
    setze_anhaenge_des_entwurfs(conn, entwurf_id, anhaenge_des_entwurfs(conn, entwurf_id) + [pfad])


def entferne_anhang_vom_entwurf(conn: sqlite3.Connection, entwurf_id: int, pfad: Path) -> None:
    bleiben = [p for p in anhaenge_des_entwurfs(conn, entwurf_id) if str(p) != str(Path(pfad))]
    setze_anhaenge_des_entwurfs(conn, entwurf_id, bleiben)


def lies_unterlagen(conn: sqlite3.Connection) -> list[str]:
    """Liest geänderte Unterlagen ein und gibt Probleme als Klartext zurück."""
    probleme = []
    for zeile in conn.execute("SELECT * FROM dokumente ORDER BY id").fetchall():
        pfad, titel = Path(zeile["pfad"]), zeile["titel"]
        if not pfad.exists():
            probleme.append(f"{titel}: Datei fehlt ({pfad})")
            continue
        summe = hashlib.sha256(pfad.read_bytes()).hexdigest()
        if zeile["sha256"] == summe:
            continue
        try:
            text = lies_datei(pfad)
        except Exception as fehler:  # pypdf und python-docx werfen je nach Datei sehr verschiedene Fehler.
            probleme.append(f"{titel}: Datei nicht lesbar ({fehler})")
            continue
        if len(text.strip()) < 100:
            probleme.append(f"{titel}: Kein Text lesbar, vermutlich ein Scan. OCR ist nicht eingebaut.")
            continue
        conn.execute("DELETE FROM chunks WHERE dokument_id = ?", (zeile["id"],))
        conn.execute("UPDATE dokumente SET sha256 = ?, eingelesen = ? WHERE id = ?", (summe, db.jetzt(), zeile["id"]))
        stuecke = rag.zerlege(text)
        rag.speichere(conn, "dokument", stuecke, dokument_id=zeile["id"], quellen=[titel] * len(stuecke))
        db.protokolliere(conn, "Unterlage eingelesen", f"{titel}: {len(stuecke)} Abschnitte")
    return probleme


SENSIBEL_MELDUNG = "Interne Angabe (z. B. Gehalt oder Gesundheit): Das kann kein Fakt für Mails werden."


def lege_fakt_an(conn: sqlite3.Connection, kategorie: str, text: str, quelle: str) -> int | None:
    text = text.strip()
    if kategorie not in KATEGORIEN or not text:
        raise ValueError("Kategorie oder Text ungültig.")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO fakten (kategorie, text, quelle, erstellt) VALUES (?, ?, ?, ?)",
        (kategorie, text, quelle, db.jetzt()),
    )
    conn.commit()
    if not cursor.rowcount:
        return None
    rag.setze_faktindex(conn, cursor.lastrowid, text, quelle)
    return cursor.lastrowid


def lege_eigenen_fakt_an(conn: sqlite3.Connection, kategorie: str, text: str, quelle: str = "von dir ergänzt") -> None:
    """Was die Person selbst eingibt, gilt sofort. Nur interne Angaben werden abgewiesen."""
    if regeln.ist_sensibel(text):
        raise ValueError(SENSIBEL_MELDUNG)
    fakt_id = lege_fakt_an(conn, kategorie, text, quelle)
    if fakt_id:
        bestaetige(conn, fakt_id)


def bestaetige(conn: sqlite3.Connection, fakt_id: int) -> None:
    fakt = conn.execute("SELECT text FROM fakten WHERE id = ?", (fakt_id,)).fetchone()
    if regeln.ist_sensibel(fakt["text"]):
        raise ValueError(SENSIBEL_MELDUNG)
    conn.execute("UPDATE fakten SET status = 'bestaetigt', bestaetigt_am = ? WHERE id = ?", (db.jetzt(), fakt_id))
    conn.commit()
    db.protokolliere(conn, "Fakt bestätigt", fakt["text"][:80])


def bestaetige_ki_vorschlaege(conn: sqlite3.Connection) -> int:
    """Bestätigt alle KI-Vorschläge auf einmal. Interne Angaben bleiben offen."""
    anzahl = 0
    for z in conn.execute("SELECT id, text FROM fakten WHERE status = 'vorgeschlagen' AND quelle LIKE 'KI-Vorschlag%'").fetchall():
        if not regeln.ist_sensibel(z["text"]):
            bestaetige(conn, z["id"])
            anzahl += 1
    return anzahl


def lehne_ab(conn: sqlite3.Connection, fakt_id: int) -> None:
    conn.execute("UPDATE fakten SET status = 'abgelehnt', bestaetigt_am = NULL WHERE id = ?", (fakt_id,))
    conn.commit()


def aendere(conn: sqlite3.Connection, fakt_id: int, text: str) -> None:
    """Die eigene Änderung gilt als geprüft, auch bei einem KI-Vorschlag."""
    if regeln.ist_sensibel(text):
        raise ValueError(SENSIBEL_MELDUNG)
    fakt = conn.execute("SELECT quelle FROM fakten WHERE id = ?", (fakt_id,)).fetchone()
    conn.execute("UPDATE fakten SET text = ?, status = 'bestaetigt', bestaetigt_am = ? WHERE id = ?",
                 (text.strip(), db.jetzt(), fakt_id))
    conn.commit()
    rag.setze_faktindex(conn, fakt_id, text.strip(), fakt["quelle"])


EXTRAKTION_SCHEMA = {
    "type": "object",
    "properties": {"fakten": {"type": "array", "items": {
        "type": "object",
        "properties": {"kategorie": {"type": "string", "enum": KATEGORIEN}, "text": {"type": "string"}},
        "required": ["kategorie", "text"],
    }}},
    "required": ["fakten"],
}


def extrahiere(conn: sqlite3.Connection, modell: str, chat=llm.chat, fortschritt=lambda _: None) -> int:
    """Schlägt aus Lebenslauf und Zeugnis lokal neue Fakten vor. Bestätigen muss sie die Person."""
    system = (
        "Du extrahierst belegbare Fakten über einen Bewerber aus seinen Unterlagen. Nur was wörtlich dort steht. "
        "Keine Gehälter, keine Gesundheit, kein Alter, keine Kundennamen. Kurze deutsche Sätze. "
        "Kategorie „praktisch“ steht für handwerkliches und physisches Können."
    )
    neu = 0
    zeilen = conn.execute(
        "SELECT c.text, c.quelle FROM chunks c JOIN dokumente d ON d.id = c.dokument_id "
        "WHERE d.art != 'sonstiges' ORDER BY c.id"
    ).fetchall()
    for nummer, zeile in enumerate(zeilen, 1):
        fortschritt(f"Extraktion {nummer}/{len(zeilen)}")
        antwort = chat(modell, system, zeile["text"], EXTRAKTION_SCHEMA, 0.1)
        for f in antwort.get("fakten", []):
            text = str(f.get("text", "")).strip()
            if len(text) < 10 or regeln.ist_sensibel(text) or f.get("kategorie") not in KATEGORIEN:
                continue
            if lege_fakt_an(conn, f["kategorie"], text, f"KI-Vorschlag aus: {zeile['quelle']}"):
                neu += 1
    db.protokolliere(conn, "Fakten extrahiert", f"{neu} neue Vorschläge, Modell {modell}")
    return neu
