"""Das Lernen der lokalen KI: Rückmeldungen werden gespeichert und in spätere Prompts eingespielt.

Die Modellgewichte ändern sich dabei nicht. Lektionen stammen nur aus den eigenen Aktionen der Person,
nie aus Webseiten. Firmen- und Personennamen werden vor dem Speichern anonymisiert.
"""

import re
import sqlite3

from . import db, rag, regeln

RANG_BONUS = {"antwort": -0.3, "absage": 0.1, "keine_antwort": 0.15}


def anonymisiere(text: str, firma: sqlite3.Row) -> str:
    namen = [firma["name"], regeln.kurzname(firma["name"])]
    for person in re.split(r",| und ", firma["personen"]):
        person = person.strip()
        if person:
            namen.extend([person, person.split()[-1]])
    for name in sorted(set(namen), key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[Firma]" if name in namen[:2] else "[Name]", text)
    return text


def merke_regel(conn: sqlite3.Connection, text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("Die Regel ist leer.")
    if regeln.ist_sensibel(text):
        raise ValueError("Interne Angaben gehören nicht in eine Schreibregel.")
    conn.execute("INSERT INTO lektionen (art, nachher, erstellt) VALUES ('regel', ?, ?)", (text, db.jetzt()))
    conn.commit()
    db.protokolliere(conn, "Regel gelernt", text[:80])


def merke_korrektur(conn: sqlite3.Connection, entwurf_id: int) -> None:
    e = conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (entwurf_id,)).fetchone()
    if e["text"].strip() == e["ki_text"].strip():
        conn.execute("DELETE FROM lektionen WHERE entwurf_id = ? AND art = 'korrektur'", (entwurf_id,))
        conn.commit()
        return
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (e["firma_id"],)).fetchone()
    vorher, nachher = anonymisiere(e["ki_text"], firma), anonymisiere(e["text"], firma)
    vektor = rag.embedde([f"{firma['warum']} {firma['sitz']}"])[0]
    conn.execute(
        "INSERT INTO lektionen (art, entwurf_id, firma_id, vorher, nachher, embedding, erstellt) "
        "VALUES ('korrektur', ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entwurf_id) DO UPDATE SET art = 'korrektur', vorher = excluded.vorher, nachher = excluded.nachher",
        (entwurf_id, e["firma_id"], vorher, nachher, vektor, db.jetzt()),
    )
    conn.commit()


def merke_bewertung(conn: sqlite3.Connection, firma_id: int, score: int, notiz: str) -> None:
    if not 0 <= score <= 100:
        raise ValueError("Die Bewertung muss zwischen 0 und 100 liegen.")
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    conn.execute("UPDATE firmen SET mein_score = ?, meine_notiz = ? WHERE id = ?", (score, notiz.strip(), firma_id))
    conn.execute("DELETE FROM lektionen WHERE art = 'bewertung' AND firma_id = ?", (firma_id,))
    vektor = rag.embedde([f"{firma['warum']} {firma['sitz']}"])[0]
    conn.execute(
        "INSERT INTO lektionen (art, firma_id, vorher, nachher, notiz, embedding, erstellt) "
        "VALUES ('bewertung', ?, ?, ?, ?, ?, ?)",
        (firma_id, anonymisiere(firma["warum"], firma), str(score), anonymisiere(notiz.strip(), firma), vektor, db.jetzt()),
    )
    conn.commit()
    db.protokolliere(conn, "Bewertung korrigiert", f"{score}: {notiz[:60]}", firma_id)


def merke_ergebnis(conn: sqlite3.Connection, entwurf_id: int, ergebnis: str) -> None:
    """Hält das Ergebnis fest. Eine Mail mit Antwort wird zum Vorbild für spätere Entwürfe."""
    e = conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (entwurf_id,)).fetchone()
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (e["firma_id"],)).fetchone()
    vorhanden = conn.execute("SELECT id FROM lektionen WHERE entwurf_id = ?", (entwurf_id,)).fetchone()
    if not vorhanden and ergebnis == "antwort":
        vektor = rag.embedde([f"{firma['warum']} {firma['sitz']}"])[0]
        conn.execute(
            "INSERT INTO lektionen (art, entwurf_id, firma_id, nachher, ergebnis, embedding, erstellt) "
            "VALUES ('vorbild', ?, ?, ?, ?, ?, ?)",
            (entwurf_id, e["firma_id"], anonymisiere(e["text"], firma), ergebnis, vektor, db.jetzt()),
        )
    else:
        conn.execute("UPDATE lektionen SET ergebnis = ? WHERE entwurf_id = ?", (ergebnis, entwurf_id))
    conn.commit()


def regeln_liste(conn: sqlite3.Connection) -> list[str]:
    return [z["nachher"] for z in conn.execute(
        "SELECT nachher FROM lektionen WHERE art = 'regel' ORDER BY id DESC LIMIT 10")]


def _aehnlich(conn: sqlite3.Connection, arten: tuple[str, ...], frage: str, k: int) -> list[sqlite3.Row]:
    vektor = rag.embedde([frage])[0]
    marken = ",".join("?" * len(arten))
    zeilen = conn.execute(
        f"SELECT *, vec_distance_cosine(embedding, ?) AS abstand FROM lektionen "
        f"WHERE art IN ({marken}) AND embedding IS NOT NULL",
        (vektor, *arten),
    ).fetchall()
    return sorted(zeilen, key=lambda z: z["abstand"] + RANG_BONUS.get(z["ergebnis"], 0))[:k]


def fuer_entwurf(conn: sqlite3.Connection, frage: str, k: int = 2) -> str:
    teile = [f"- {r}" for r in regeln_liste(conn)]
    for z in _aehnlich(conn, ("korrektur", "vorbild"), frage, k):
        erfolg = " (darauf kam eine Antwort)" if z["ergebnis"] == "antwort" else ""
        teile.append(f"Beispiel, wie der Bewerber schreibt{erfolg}:\n{z['nachher']}")
    return "\n\n".join(teile)


def fuer_bewertung(conn: sqlite3.Connection, frage: str, k: int = 3) -> str:
    teile = [f"- {r}" for r in regeln_liste(conn)]
    for z in _aehnlich(conn, ("bewertung",), frage, k):
        teile.append(f"Der Bewerber bewertete eine Firma („{z['vorher']}“) mit {z['nachher']}/100: {z['notiz']}")
    return "\n".join(teile)


def statistik(conn: sqlite3.Connection) -> dict:
    zaehle = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "regeln": zaehle("SELECT count(*) FROM lektionen WHERE art = 'regel'"),
        "korrekturen": zaehle("SELECT count(*) FROM lektionen WHERE art IN ('korrektur', 'vorbild')"),
        "bewertungen": zaehle("SELECT count(*) FROM lektionen WHERE art = 'bewertung'"),
        "gesendet": zaehle("SELECT count(*) FROM entwuerfe WHERE status IN ('gesendet', 'antwort', 'absage', 'keine_antwort')"),
        "antworten": zaehle("SELECT count(*) FROM entwuerfe WHERE status = 'antwort'"),
        "absagen": zaehle("SELECT count(*) FROM entwuerfe WHERE status = 'absage'"),
    }
