"""Auswertungen für das Notebook `analyse.ipynb`. Alles liest nur aus der lokalen Datenbank."""

import difflib
import json
import sqlite3
from pathlib import Path

import pandas as pd

from . import rag

GESENDET = ("gesendet", "antwort", "absage", "keine_antwort")
BAENDER = [0, 40, 60, 80, 101]
BAND_NAMEN = ["0–39", "40–59", "60–79", "80–100"]


def _df(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    zeilen = conn.execute(sql).fetchall()
    spalten = [b[0] for b in conn.execute(sql).description] if not zeilen else list(zeilen[0].keys())
    return pd.DataFrame([tuple(z) for z in zeilen], columns=spalten)


def trichter(conn: sqlite3.Connection) -> pd.DataFrame:
    z = lambda sql: conn.execute(sql).fetchone()[0]
    marken = ",".join(f"'{s}'" for s in GESENDET)
    return pd.DataFrame([
        ("Firmen", z("SELECT count(*) FROM firmen")),
        ("Seiten geladen", z("SELECT count(*) FROM firmen WHERE seiten_geladen IS NOT NULL")),
        ("Bewertet", z("SELECT count(*) FROM firmen WHERE ki_score IS NOT NULL")),
        ("Entwurf erstellt", z("SELECT count(DISTINCT firma_id) FROM entwuerfe")),
        ("Freigegeben", z("SELECT count(DISTINCT firma_id) FROM entwuerfe WHERE freigegeben_am IS NOT NULL")),
        ("Gesendet", z(f"SELECT count(DISTINCT firma_id) FROM entwuerfe WHERE status IN ({marken})")),
        ("Antwort", z("SELECT count(DISTINCT firma_id) FROM entwuerfe WHERE status = 'antwort'")),
    ], columns=["Schritt", "Anzahl"])


def bewertungen(conn: sqlite3.Connection) -> pd.DataFrame:
    df = _df(conn, "SELECT name, passung, ki_score, mein_score, meine_notiz FROM firmen ORDER BY ki_score DESC")
    df["ki_score"] = pd.to_numeric(df["ki_score"])
    df["mein_score"] = pd.to_numeric(df["mein_score"])
    df["abweichung"] = df["ki_score"] - df["mein_score"]
    return df


def bewertungsguete(conn: sqlite3.Connection) -> dict:
    """Wie nah liegt die KI an deinem Urteil? Unter 3 Paaren sagt eine Rangkorrelation nichts."""
    paare = bewertungen(conn).dropna(subset=["ki_score", "mein_score"])
    ergebnis = {"paare": len(paare), "mittlere_abweichung": None, "rangkorrelation": None}
    if len(paare):
        ergebnis["mittlere_abweichung"] = round(float(paare["abweichung"].abs().mean()), 1)
    if len(paare) >= 3 and paare["ki_score"].nunique() > 1 and paare["mein_score"].nunique() > 1:
        ergebnis["rangkorrelation"] = round(float(paare["ki_score"].rank().corr(paare["mein_score"].rank())), 2)
    return ergebnis


def korrekturgrad(conn: sqlite3.Connection) -> pd.DataFrame:
    """Anteil, den du an jedem KI-Entwurf ändern musstest. Sinkt er, greift das Lernen."""
    df = _df(conn, "SELECT e.id, e.erstellt, f.name, e.modell, e.ki_text, e.text FROM entwuerfe e "
                   "JOIN firmen f ON f.id = e.firma_id WHERE e.status != 'verworfen' ORDER BY e.id")
    df["aenderung_prozent"] = [
        round(100 * (1 - difflib.SequenceMatcher(None, a.split(), b.split()).ratio()), 1)
        for a, b in zip(df["ki_text"], df["text"])
    ]
    df["woerter"] = df["text"].str.split().str.len()
    return df.drop(columns=["ki_text", "text"])


def ergebnis_nach_score(conn: sqlite3.Connection) -> pd.DataFrame:
    """Trennt der Score Firmen, die antworten, von denen, die nicht antworten?"""
    marken = ",".join(f"'{s}'" for s in GESENDET)
    df = _df(conn, f"SELECT COALESCE(f.mein_score, f.ki_score) AS score, e.status FROM entwuerfe e "
                   f"JOIN firmen f ON f.id = e.firma_id WHERE e.status IN ({marken})")
    df["score"] = pd.to_numeric(df["score"])
    df["band"] = pd.cut(df["score"], BAENDER, right=False, labels=BAND_NAMEN)
    gruppe = df.groupby("band", observed=False)["status"]
    tabelle = pd.DataFrame({
        "gesendet": gruppe.size(),
        "antworten": gruppe.apply(lambda s: int((s == "antwort").sum())),
        "absagen": gruppe.apply(lambda s: int((s == "absage").sum())),
    })
    tabelle["antwortquote_prozent"] = (100 * tabelle["antworten"] / tabelle["gesendet"]).round(0)
    return tabelle.reset_index()


def lade_fragen(pfad: Path) -> list[dict]:
    return json.loads(Path(pfad).read_text(encoding="utf-8"))


def suchqualitaet(conn: sqlite3.Connection, fragen: list[dict], k: int = 5,
                  modi: tuple[str, ...] = ("vektor", "wort", "hybrid")) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trefferquote und MRR je Suchmodus. `erwartet` ist ein Textstück des richtigen Fakts."""
    details = []
    for frage in fragen:
        for modus in modi:
            treffer = rag.messe_fakten(conn, frage["frage"], k, modus)
            rang = next((i for i, c in enumerate(treffer, 1) if frage["erwartet"].lower() in c["text"].lower()), None)
            details.append({"frage": frage["frage"], "modus": modus, "rang": rang})
    d = pd.DataFrame(details)
    d["reziprok"] = d["rang"].rdiv(1).fillna(0)
    uebersicht = d.groupby("modus", sort=False).agg(
        trefferquote=("rang", lambda r: round(float(r.notna().mean()), 2)),
        treffer_auf_1=("rang", lambda r: round(float((r == 1).mean()), 2)),
        mrr=("reziprok", lambda r: round(float(r.mean()), 2)),
    ).reset_index()
    return uebersicht, d


def firmenabdeckung(conn: sqlite3.Connection) -> pd.DataFrame:
    """Wie viel Material hat die Suche pro Firma? Ohne Text ist jeder Score nur eine Vermutung."""
    return _df(conn, "SELECT f.name, count(c.id) AS abschnitte, count(DISTINCT c.quelle) AS seiten, f.seiten_geladen "
                     "FROM firmen f LEFT JOIN chunks c ON c.firma_id = f.id AND c.art = 'firma' "
                     "GROUP BY f.id ORDER BY abschnitte")
