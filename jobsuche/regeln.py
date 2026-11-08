"""Feste Prüfungen. Sie laufen im Code, damit kein Modell und keine Webseite sie umgehen kann.

Allgemeine Regeln gelten für alle. Eigene Regeln legt jede Person in ihrem Profil an.
"""

import re
import sqlite3
from dataclasses import dataclass

from . import db

GESUNDHEIT = re.compile(r"gesundheit|krank|behinder|diagnose|\brücken|bandscheibe", re.IGNORECASE)
SENSIBEL = re.compile(
    r"gehalt(?!en)|brutto|netto|€|\beuro\b|absage|hinhalt|lebensalter|\balter\s*[:=]|\balter\s+von\s+\d|mein(?:em)?\s+alter\b|jahre\s+alt\b|jahrgang|geboren|" + GESUNDHEIT.pattern,
    re.IGNORECASE,
)
PLATZHALTER = re.compile(r"Herr/Frau|\[[^\]]*\]|…|\.\.\.\s*$", re.M)
ZAHL = re.compile(r"\d+(?:[.,]\d+)*")
ADRESSE = re.compile(r"https?://[^\s,;)]+|www\.[^\s,;)]+|(?<![@\w.-])[\w-]+(?:\.[\w-]+)*\.(?:de|eu|com|net|org|io)\b", re.I)
RECHTSFORM = re.compile(r"\s+(GmbH|GbR|AG|KG|UG|\(.*\))", re.I)
REGEL_ARTEN = {"verboten": "Begriff verboten", "zusammen": "Nur zusammen nennen", "muster": "Suchmuster (Regex)"}


@dataclass(frozen=True)
class Befund:
    stufe: str  # "sperre" oder "hinweis"
    text: str


def ist_sensibel(text: str) -> bool:
    return bool(SENSIBEL.search(text))


def zahlen(text: str) -> set[str]:
    return {z.rstrip(".,") for z in ZAHL.findall(text)}


def adressen(text: str) -> set[str]:
    return {a.rstrip(".").lower().removeprefix("https://").removeprefix("http://") for a in ADRESSE.findall(text)}


def kurzname(firma: str) -> str:
    return RECHTSFORM.split(firma)[0].strip()


def lade_eigene(conn: sqlite3.Connection) -> list[dict]:
    return [dict(z) for z in conn.execute("SELECT * FROM regeln ORDER BY id")]


def lege_regel_an(conn: sqlite3.Connection, art: str, wert: str, zweiter: str = "", meldung: str = "") -> None:
    wert, zweiter, meldung = wert.strip(), zweiter.strip(), meldung.strip()
    if art not in REGEL_ARTEN or not wert:
        raise ValueError("Art oder Inhalt der Regel fehlt.")
    if art == "zusammen" and not zweiter:
        raise ValueError("Diese Regel braucht zwei Angaben.")
    if art == "muster":
        try:
            re.compile(wert)
        except re.error as fehler:
            raise ValueError(f"Das Suchmuster ist ungültig: {fehler}") from fehler
    conn.execute("INSERT INTO regeln (art, wert, zweiter, meldung, erstellt) VALUES (?, ?, ?, ?, ?)",
                 (art, wert, zweiter, meldung, db.jetzt()))
    conn.commit()
    db.protokolliere(conn, "Regel angelegt", f"{REGEL_ARTEN[art]}: {wert[:60]}")


def loesche_regel(conn: sqlite3.Connection, regel_id: int) -> None:
    conn.execute("DELETE FROM regeln WHERE id = ?", (regel_id,))
    conn.commit()


def _meldung(regel: dict) -> str:
    if regel.get("meldung"):
        return regel["meldung"]
    if regel["art"] == "zusammen":
        return f"„{regel['wert']}“ nur zusammen mit „{regel['zweiter']}“ nennen."
    return f"Nicht erlaubt: {regel['wert']}."


def verstoesse(regel: dict, text: str) -> bool:
    if regel["art"] == "verboten":
        return bool(re.search(rf"(?<!\w){re.escape(regel['wert'])}(?!\w)", text, re.I))
    if regel["art"] == "zusammen":
        return regel["wert"].lower() in text.lower() and regel["zweiter"].lower() not in text.lower()
    return bool(re.search(regel["wert"], text, re.I))


def fuer_prompt(eigene: list[dict]) -> str:
    return " ".join(_meldung(r) for r in eigene)


def pruefe(betreff: str, text: str, empfaenger: str, erlaubte_quellen: list[str], andere_firmen: list[str],
           eigene_domain: str = "", eigene: list[dict] = ()) -> list[Befund]:
    """`erlaubte_quellen`: bestätigte Fakten, Firmentexte und die Stilvorlage."""
    befunde: list[Befund] = []
    gesamt = f"{betreff}\n{text}"

    if not empfaenger or "@" not in empfaenger:
        befunde.append(Befund("sperre", "Keine gültige Empfängeradresse."))
    treffer = SENSIBEL.search(gesamt)
    if treffer:
        befunde.append(Befund("sperre", f"Interne Angabe im Text: „{treffer.group(0)}“."))
    if PLATZHALTER.search(gesamt):
        befunde.append(Befund("sperre", "Platzhalter im Text."))
    for regel in eigene:
        if verstoesse(regel, gesamt):
            befunde.append(Befund("sperre", _meldung(regel)))

    belegt = " ".join(erlaubte_quellen)
    fehlend = sorted(zahlen(gesamt) - zahlen(belegt))
    if fehlend:
        befunde.append(Befund("sperre", "Zahl ohne Beleg in den Fakten: " + ", ".join(fehlend)))

    erlaubte_adressen = adressen(belegt) | ({eigene_domain.lower()} if eigene_domain else set())
    fremd = sorted(a for a in adressen(gesamt)
                   if not any(a == e or a.endswith("." + e) or a.startswith(e + "/") for e in erlaubte_adressen))
    if fremd:
        befunde.append(Befund("sperre", "Adresse ohne Beleg im Text: " + ", ".join(fremd)))

    for firma in andere_firmen:
        name = kurzname(firma)
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", gesamt, re.I):
            befunde.append(Befund("sperre", f"Text nennt eine andere Firma: {name}."))

    if re.search(r"\b(Herr|Frau)\s+[A-ZÄÖÜ]", text):
        befunde.append(Befund("hinweis", "Anrede mit Herr/Frau: Bitte selbst prüfen, ob sie stimmt."))
    woerter = len(text.split())
    if woerter > 180:
        befunde.append(Befund("hinweis", f"Mit {woerter} Wörtern lang für eine Erstmail."))
    return befunde


def gesperrt(befunde: list[Befund]) -> bool:
    return any(b.stufe == "sperre" for b in befunde)
