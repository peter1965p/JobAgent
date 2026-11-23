"""Hybride Suche: Vektor (sqlite-vec bzw. pgvector) und Stichwort (FTS5 bzw. tsvector), per Reciprocal Rank Fusion vereint.

Die Filter stehen vor dem Ranking im SQL. Was ein Aufrufer nicht sehen darf, kann so
nicht in die Treffer rutschen. Mailentwürfe nutzen nur `fakten` und `firmentext`.
"""

import re
import sqlite3
import threading

import sqlite_vec

from . import config, db

_modell = None
_lock = threading.Lock()


def embedde(texte: list[str]) -> list[bytes]:
    global _modell
    with _lock:
        if _modell is None:
            from fastembed import TextEmbedding

            config.MODELLE.mkdir(parents=True, exist_ok=True)
            _modell = TextEmbedding(config.EMBEDDING_MODELL, cache_dir=str(config.MODELLE))
        return [sqlite_vec.serialize_float32(v.tolist()) for v in _modell.embed(texte)]


def zerlege(text: str, groesse: int = 700, ueberlappung: int = 100) -> list[str]:
    """Teilt Text an Absätzen; zu lange Absätze werden mit Überlappung geschnitten."""
    stuecke: list[str] = []
    aktuell = ""
    for absatz in re.split(r"\n\s*\n", text):
        absatz = re.sub(r"[ \t]+", " ", absatz).strip()
        if not absatz:
            continue
        if len(aktuell) + len(absatz) + 2 <= groesse:
            aktuell = f"{aktuell}\n\n{absatz}" if aktuell else absatz
            continue
        if aktuell:
            stuecke.append(aktuell)
        while len(absatz) > groesse:
            stuecke.append(absatz[:groesse])
            absatz = absatz[groesse - ueberlappung:]
        aktuell = absatz
    if aktuell:
        stuecke.append(aktuell)
    return stuecke


def speichere(conn: sqlite3.Connection, art: str, texte: list[str], *, dokument_id=None, firma_id=None,
              fakt_id=None, quellen: list[str] | None = None, abruf: str | None = None) -> None:
    if not texte:
        return
    quellen = quellen or [""] * len(texte)
    for text, quelle, vektor in zip(texte, quellen, embedde(texte)):
        conn.execute(
            "INSERT INTO chunks (art, dokument_id, firma_id, fakt_id, text, quelle, abruf, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (art, dokument_id, firma_id, fakt_id, text, quelle, abruf, vektor),
        )
    conn.commit()


def _fts_anfrage(frage: str) -> str:
    woerter = re.findall(r"[\wÄÖÜäöüß.-]{3,}", frage)
    return " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(woerter))


def _suche(conn: sqlite3.Connection, frage: str, von_wo: str, parameter: tuple, k: int,
           modus: str = "hybrid") -> list[sqlite3.Row]:
    """`von_wo` ist Teil des Quellcodes (FROM ... WHERE ...), nie Eingabe von außen."""
    vektor_ids: list[int] = []
    if modus in ("hybrid", "vektor"):
        vektor = embedde([frage])[0]
        vektor_ids = [z["id"] for z in conn.execute(
            f"SELECT c.id {von_wo} ORDER BY vec_distance_cosine(c.embedding, ?) LIMIT ?",
            (*parameter, vektor, k * 3),
        )]
    wort_ids: list[int] = []
    anfrage = _fts_anfrage(frage)
    if anfrage and modus in ("hybrid", "wort") and db.ist_postgres(conn):
        # PostgreSQL: Spalte tsv (deutsches Wörterbuch); websearch versteht dieselbe Anfrage „"a" OR "b"“
        wort_ids = [z["id"] for z in conn.execute(
            f"SELECT c.id {von_wo} AND c.tsv @@ websearch_to_tsquery('german', ?) "
            "ORDER BY ts_rank(c.tsv, websearch_to_tsquery('german', ?)) DESC, c.id LIMIT ?",
            (*parameter, anfrage, anfrage, k * 3),
        )]
    elif anfrage and modus in ("hybrid", "wort"):
        wort_ids = [z["id"] for z in conn.execute(
            f"SELECT c.id {von_wo} AND c.id IN (SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?) "
            "ORDER BY (SELECT rank FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid = c.id) LIMIT ?",
            (*parameter, anfrage, anfrage, k * 3),
        )]
    punkte: dict[int, float] = {}
    for liste in (vektor_ids, wort_ids):
        for rang, chunk_id in enumerate(liste):
            punkte[chunk_id] = punkte.get(chunk_id, 0) + 1 / (60 + rang)
    beste = sorted(punkte, key=punkte.get, reverse=True)[:k]
    if not beste:
        return []
    marken = ",".join("?" * len(beste))
    zeilen = {z["id"]: z for z in conn.execute(f"SELECT * FROM chunks WHERE id IN ({marken})", beste)}
    return [zeilen[i] for i in beste]


def fakten(conn: sqlite3.Connection, frage: str, k: int = 12) -> list[sqlite3.Row]:
    """Nur bestätigte Fakten. Das ist die einzige Quelle für Aussagen über die Person."""
    return _suche(
        conn, frage,
        "FROM chunks c JOIN fakten f ON f.id = c.fakt_id WHERE c.art = 'fakt' AND f.status = 'bestaetigt'",
        (), k,
    )


def firmentext(conn: sqlite3.Connection, firma_id: int, frage: str, k: int = 4) -> list[sqlite3.Row]:
    return _suche(conn, frage, "FROM chunks c WHERE c.art = 'firma' AND c.firma_id = ?", (firma_id,), k)


def belegstellen(conn: sqlite3.Connection, frage: str, k: int = 3) -> list[sqlite3.Row]:
    """Interne Unterlagen. Nur zur Anzeige in der App und für die lokale Extraktion."""
    return _suche(conn, frage, "FROM chunks c WHERE c.art = 'dokument'", (), k)


def unterlagen(conn: sqlite3.Connection, frage: str, k: int = 6) -> list[sqlite3.Row]:
    """Lebenslauf und Zeugnis, ohne sonstige Dokumente: Dort können Gehalt oder Notizen stehen. Nur für lokale Prüfungen."""
    return _suche(conn, frage, "FROM chunks c JOIN dokumente d ON d.id = c.dokument_id "
                  "WHERE c.art = 'dokument' AND d.art IN ('lebenslauf', 'zeugnis')", (), k)


def messe_fakten(conn: sqlite3.Connection, frage: str, k: int, modus: str) -> list[sqlite3.Row]:
    """Nur für die Analyse: sucht auch in unbestätigten Fakten, damit sich die Suche früh messen lässt."""
    return _suche(conn, frage, "FROM chunks c WHERE c.art = 'fakt'", (), k, modus)


def setze_faktindex(conn: sqlite3.Connection, fakt_id: int, text: str, quelle: str) -> None:
    conn.execute("DELETE FROM chunks WHERE fakt_id = ?", (fakt_id,))
    conn.commit()
    speichere(conn, "fakt", [text], fakt_id=fakt_id, quellen=[quelle], abruf=db.jetzt())
