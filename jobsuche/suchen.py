"""Gespeicherte Suchen und Ergebnisse.

Eine gespeicherte Suche hält alle Angaben der Suchseite unter einem Namen (Begriffe, Ort, Gebiet, Quellen, Filter,
Stichwörter, Ausschlüsse …) und ihr letztes Ergebnis. Treffer, die beim vorigen Lauf derselben Suche noch nicht da waren,
tragen `neu`. Ohne gespeicherte Suche merkt sich die App das Ergebnis der letzten Suche („zuletzt“), damit die Trefferliste
einen Neustart übersteht. Suchen mit `nachts` laufen im Nachtlauf mit.

Alles liegt in den Einstellungen der Person bzw. des Mandanten (SQLite und PostgreSQL gleich).
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime

from . import db, regeln

# Angaben der Suchseite (Schlüssel „suche.<name>“), die eine gespeicherte Suche ausmachen
ANGABEN = ("begriffe", "ort", "land", "umkreis", "bundesland", "branche", "quellen", "searxng", "arbeitszeit", "nur_ki",
           "max_tage", "aufgefrischte", "remote", "max_treffer", "stichwoerter", "ausschluss")
LISTE = "suchen.liste"
ZULETZT = "zuletzt"
MAX_SUCHEN = 30
MAX_TREFFER_GESPEICHERT = 500


class SuchenFehler(Exception):
    pass


def _jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def liste(conn) -> list[dict]:
    try:
        werte = json.loads(db.einstellung(conn, LISTE, "[]") or "[]")
    except ValueError:
        return []
    return [s for s in werte if isinstance(s, dict) and s.get("id")]


def _schreibe(conn, suchen: list[dict]) -> None:
    db.setze_einstellung(conn, LISTE, json.dumps(suchen, ensure_ascii=False))


def hole(conn, sid: str) -> dict | None:
    return next((s for s in liste(conn) if s["id"] == sid), None)


def aktuelle_angaben(conn) -> dict:
    return {n: db.einstellung(conn, f"suche.{n}", "") for n in ANGABEN}


def speichern(conn, name: str, angaben: dict | None = None, nachts: bool | None = None) -> dict:
    """Speichert die aktuellen Angaben (oder `angaben`) unter `name`. Gleicher Name: überschreibt die Angaben."""
    name = " ".join(str(name).split())[:60]
    if not name:
        raise SuchenFehler("Bitte einen Namen für die Suche angeben.")
    werte = {n: str((angaben or {}).get(n, "")) for n in ANGABEN} if angaben is not None else aktuelle_angaben(conn)
    suchen = liste(conn)
    vorhanden = next((s for s in suchen if s["name"].lower() == name.lower()), None)
    if vorhanden:
        vorhanden.update(name=name, angaben=werte, geaendert=_jetzt())
        if nachts is not None:
            vorhanden["nachts"] = bool(nachts)
        eintrag = vorhanden
    else:
        if len(suchen) >= MAX_SUCHEN:
            raise SuchenFehler(f"Höchstens {MAX_SUCHEN} gespeicherte Suchen. Bitte erst eine löschen.")
        eintrag = {"id": secrets.token_hex(6), "name": name, "angaben": werte, "nachts": bool(nachts),
                   "erstellt": _jetzt(), "geaendert": _jetzt(), "zuletzt": "", "anzahl": None, "neu": None}
        suchen.append(eintrag)
    _schreibe(conn, suchen)
    db.protokolliere(conn, "Suche gespeichert", name)
    return eintrag


def laden(conn, sid: str) -> dict:
    """Übernimmt die Angaben der Suche auf die Suchseite (und damit auch für den Nachtlauf ohne gespeicherte Suchen)."""
    s = hole(conn, sid)
    if not s:
        raise SuchenFehler("Diese Suche gibt es nicht mehr.")
    angaben_setzen(conn, s["angaben"])
    db.setze_einstellung(conn, "suchen.aktiv", sid)
    return s


# Zahlen und Schalter: leer heißt „nie gesetzt“ – dann gilt weiter die Vorgabe der App statt eines leeren Werts
_MIT_VORGABE = {"land", "umkreis", "quellen", "nur_ki", "max_tage", "aufgefrischte", "max_treffer"}


def angaben_setzen(conn, werte: dict) -> None:
    for n in ANGABEN:
        wert = str(werte.get(n, "") or "")
        if wert or n not in _MIT_VORGABE:
            db.setze_einstellung(conn, f"suche.{n}", wert)


def aktiv(conn) -> str:
    """Id der zuletzt geladenen/gespeicherten Suche, solange sie noch existiert – sonst „zuletzt“."""
    sid = db.einstellung(conn, "suchen.aktiv", "")
    return sid if sid and hole(conn, sid) else ZULETZT


def setze_aktiv(conn, sid: str) -> None:
    db.setze_einstellung(conn, "suchen.aktiv", sid if sid != ZULETZT else "")


def aendern(conn, sid: str, name: str | None = None, nachts: bool | None = None) -> dict:
    suchen = liste(conn)
    s = next((x for x in suchen if x["id"] == sid), None)
    if not s:
        raise SuchenFehler("Diese Suche gibt es nicht mehr.")
    if name is not None:
        name = " ".join(name.split())[:60]
        if not name:
            raise SuchenFehler("Bitte einen Namen angeben.")
        if any(x["id"] != sid and x["name"].lower() == name.lower() for x in suchen):
            raise SuchenFehler(f"„{name}“ gibt es schon.")
        s["name"] = name
    if nachts is not None:
        s["nachts"] = bool(nachts)
    _schreibe(conn, suchen)
    return s


def loeschen(conn, sid: str) -> None:
    suchen = liste(conn)
    rest = [s for s in suchen if s["id"] != sid]
    if len(rest) == len(suchen):
        raise SuchenFehler("Diese Suche gibt es nicht mehr.")
    _schreibe(conn, rest)
    db.setze_einstellung(conn, f"suchen.ergebnis.{sid}", "")
    if db.einstellung(conn, "suchen.aktiv", "") == sid:
        db.setze_einstellung(conn, "suchen.aktiv", "")


def _schluessel(t: dict) -> str:
    return f"{regeln.kurzname(t.get('firma', '')).lower()}|{str(t.get('titel') or t.get('beruf') or '').lower().strip()}"


def ergebnis_merken(conn, sid: str, ergebnis: dict) -> dict:
    """Speichert das Ergebnis der Suche `sid` (oder „zuletzt“), markiert neue Treffer und gibt das Ergebnis zurück."""
    vorher = letztes_ergebnis(conn, sid)
    alt = {_schluessel(t) for t in (vorher or {}).get("treffer", [])}
    treffer = []
    for t in ergebnis.get("treffer", [])[:MAX_TREFFER_GESPEICHERT]:
        t = dict(t)
        t["neu"] = bool(vorher) and _schluessel(t) not in alt
        treffer.append(t)
    neu = sum(t["neu"] for t in treffer)
    gespeichert = {"zeit": _jetzt(), "treffer": treffer, "hinweise": list(ergebnis.get("hinweise", [])),
                   "zeiten_text": ergebnis.get("zeiten_text", ""), "begriffe": ergebnis.get("begriffe"), "ort": ergebnis.get("ort")}
    db.setze_einstellung(conn, f"suchen.ergebnis.{sid}", json.dumps(gespeichert, ensure_ascii=False, default=str))
    if sid != ZULETZT:
        suchen = liste(conn)
        for s in suchen:
            if s["id"] == sid:
                s.update(zuletzt=gespeichert["zeit"], anzahl=len(treffer), neu=neu if vorher else None)
        _schreibe(conn, suchen)
    return {**ergebnis, "treffer": treffer, "neu": neu if vorher else None, "gespeichert": gespeichert["zeit"]}


def merke(conn, ergebnis: dict) -> dict:
    """Nach einer Suche: Ergebnis unter der aktiven gespeicherten Suche ablegen – passen die Angaben nicht mehr zu ihr
    (inzwischen geändert), unter „zuletzt“."""
    sid = aktiv(conn)
    s = hole(conn, sid)
    if s and s["angaben"] != aktuelle_angaben(conn):
        sid = ZULETZT
        setze_aktiv(conn, ZULETZT)
    return ergebnis_merken(conn, sid, ergebnis)


def letztes_ergebnis(conn, sid: str) -> dict | None:
    roh = db.einstellung(conn, f"suchen.ergebnis.{sid}", "")
    if not roh:
        return None
    try:
        return json.loads(roh)
    except ValueError:
        return None


def nachts(conn) -> list[dict]:
    return [s for s in liste(conn) if s.get("nachts")]
