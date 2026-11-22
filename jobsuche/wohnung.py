"""Wohnungssuche für den Umzug: Angebote aus den Suchauftrags-Mails von Immowelt, ImmoScout24, meinestadt und
Kleinanzeigen. Die Portale selbst sperren Abfragen von Programmen, ihre Mails nicht.

Jede Mail enthält eine oder mehrere Karten (Titel, Preis, Fläche, Zimmer, Ort). Die Portale ordnen die Zeilen
verschieden an, gemeinsam ist: genau eine Flächenangabe je Karte. Von ihr aus wird die Karte im HTML gesucht."""

from __future__ import annotations

import email
import email.policy
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from hashlib import sha1

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from . import db


@dataclass(frozen=True)
class Portal:
    name: str
    absender: str  # Domain (oder ganze Adresse) für die IMAP-Suche


PORTALE = (
    Portal("Immowelt", "immowelt.de"),
    Portal("ImmoScout24", "immobilienscout24.de"),
    Portal("meinestadt", "meinestadt.de"),
    Portal("Kleinanzeigen", "kleinanzeigen.de"),
)

FLAECHE = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*m²")
PREIS = re.compile(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+)\s*(?:€|EUR\b)")
ZIMMER = re.compile(r"(\d{1,2}(?:[.,]5)?)\s*(?:Zi\b\.?|Zimmer\b|-?Zi(?:mmer)?-?Wohnung|ZKB|ZKBB|ZKBK|ZK\b)", re.I)
PLZ = re.compile(r"\b(\d{5})\b")
# Zeilen, die nie Titel sind
KNOPF = re.compile(
    r"(?i)^(?:mehr informationen|alle angebote|zum angebot|zur anzeige|anzeige ansehen|exposé ansehen|details|jetzt .*|"
    r"suchauftrag .*|abmelden|datenschutz.*|impressum|hilfe.*|kontakt|app .*|hol dir die app|mietpreis|wohnfläche|"
    r"zimmer|kaltmiete|warmmiete|kaufpreis|nebenkosten|provisionsfrei|neu|top|anzeigen von unserem partner)$")
MIETART = re.compile(r"(?i)^(kaltmiete|warmmiete|mietpreis|kaufpreis|gesamtmiete|miete)$")

# Stichworte im Titel. „Tausch“ und „WBS“ sind für einen Umzug in der Regel nicht zu haben: Sie stehen als Warnung dabei.
MERKMALE = {
    "balkon": re.compile(r"(?i)balkon|loggia"),
    "terrasse": re.compile(r"(?i)terrasse"),
    "garten": re.compile(r"(?i)garten|gartenanteil"),
    "haustiere": re.compile(r"(?i)haustier|hund|katze|tiere erlaubt"),
    "einbaukueche": re.compile(r"(?i)einbauküche|\bebk\b"),
    "tausch": re.compile(r"(?i)tausch"),
    "wbs": re.compile(r"(?i)\bwbs\b|wohnberechtigungsschein"),
}
WARNUNG = ("tausch", "wbs")


def portal_der_mail(absender: str) -> Portal | None:
    adresse = parseaddr(absender)[1].lower()
    domain = adresse.rpartition("@")[2]
    return next((p for p in PORTALE if adresse == p.absender or domain == p.absender or domain.endswith("." + p.absender)), None)


def _zahl(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def _zeilen(knoten: Tag) -> list[tuple[str, str]]:
    """(Text, Link) je sichtbarer Textzeile, Kommentare (Outlook-Weichen) und Stile ausgenommen."""
    aus = []
    for text in knoten.find_all(string=True):
        if isinstance(text, Comment) or not isinstance(text, NavigableString) or text.parent.name in ("style", "script", "head", "title"):
            continue
        zeile = " ".join(re.sub(r"[​-‏⁠﻿­͏]", "", str(text)).split())
        if zeile:
            anker = text.find_parent("a", href=True)
            aus.append((zeile, anker["href"] if anker else ""))
    return aus


def _ist_flaeche(text) -> bool:
    """Eine Flächenzeile („62,36 m²“, „2 Zimmer · 65 m²“), nicht ein Titel, der zufällig „62 m²“ enthält."""
    return not isinstance(text, Comment) and bool(FLAECHE.search(str(text))) and len(" ".join(str(text).split())) <= 30


def _karten(soup: BeautifulSoup) -> list[Tag]:
    """Je Flächenzeile der größte umgebende Block, der nur diese eine Flächenzeile enthält und kurz bleibt."""
    karten: list[Tag] = []
    for text in soup.find_all(string=_ist_flaeche):
        knoten, beste = text.parent, None
        while isinstance(knoten, Tag) and knoten.name not in ("body", "html", "[document]"):
            if len(knoten.find_all(string=_ist_flaeche)) > 1 or len(knoten.get_text(" ")) > 900:
                break
            beste = knoten
            knoten = knoten.parent
        if beste is not None and not any(beste is k or beste in k.descendants for k in karten):
            karten = [k for k in karten if k not in beste.descendants]
            karten.append(beste)
    return karten


def _kennung(portal: Portal, url: str, titel: str, ort: str, flaeche: float | None) -> tuple[str, str]:
    """(Adresse zum Öffnen, Kennung). ImmoScout24 hat eine feste Exposé-Nummer, sonst zählt das Angebot selbst."""
    treffer = re.search(r"(?:immobilienscout24\.de|is24\.de)/.*?expose/(\d{6,})", url)
    if treffer:
        return f"https://www.immobilienscout24.de/expose/{treffer.group(1)}", f"is24:{treffer.group(1)}"
    treffer = re.search(r"kleinanzeigen\.de/s-anzeige/[^?\s]*?/(\d{6,})", url)
    if treffer:
        return url.split("?")[0], f"ka:{treffer.group(1)}"
    schluessel = f"{re.sub(r'\W+', ' ', titel.lower()).strip()}|{ort.lower()}|{flaeche or ''}"
    return url, f"{portal.name.lower()}:{sha1(schluessel.encode()).hexdigest()[:16]}"


def angebot_aus_karte(karte: Tag, portal: Portal) -> dict | None:
    zeilen = _zeilen(karte)
    texte = [z for z, _ in zeilen]
    voll = " · ".join(texte)
    flaeche = next((FLAECHE.search(z) for z in texte if _ist_flaeche(z)), None)
    if not flaeche:
        return None
    # Preise mit ihrer Art: steht „Kaltmiete“ in derselben oder der nächsten Zeile, gilt der Preis als kalt
    kalt = warm = None
    for i, z in enumerate(texte):
        preis = PREIS.search(z)
        if not preis or "m²" in z and not z.strip().startswith(preis.group(0)):
            continue
        art = (MIETART.search(z.replace(preis.group(0), "").strip(" ·")) or
               (MIETART.match(texte[i + 1]) if i + 1 < len(texte) else None))
        wert = _zahl(preis.group(1))
        if wert < 50:
            continue
        if art and art.group(1).lower() in ("warmmiete", "gesamtmiete"):
            warm = warm or wert
        else:
            kalt = kalt or wert
    if not (kalt or warm):
        return None  # Kopf der Mail („Wohnung • bis 65 m²“) oder Werbung, kein Angebot
    zimmer = ZIMMER.search(voll)
    anzahl = _zahl(zimmer.group(1)) if zimmer else None
    if anzahl is None:  # meinestadt: „2“ und darunter „Zimmer“
        for i, z in enumerate(texte[:-1]):
            if re.fullmatch(r"\d{1,2}(?:,5)?", z) and texte[i + 1].lower() == "zimmer":
                anzahl = _zahl(z)
                break
    # Ort: „Hiltrup, Münster (48165)“, „67063 Ludwigshafen am Rhein“ oder „Straße 6, Oggersheim, Ludwigshafen am Rhein“
    ort = next((z for z in texte if PLZ.search(z) and not PREIS.search(z) and "m²" not in z), "")
    if not ort:
        ort = next((z for z in reversed(texte) if "," in z and not PREIS.search(z) and "m²" not in z and len(z) < 90), "")
    # Titel: der längste verlinkte Text, der kein Knopf, Preis, Ort und keine Fläche ist
    def ist_titel(z: str) -> bool:
        return (len(z) >= 8 and not KNOPF.match(z) and not PREIS.fullmatch(z.strip()) and not _ist_flaeche(z)
                and z != ort and not re.fullmatch(r"[\d\s.,€]+", z))
    kandidaten = [(z, h) for z, h in zeilen if h and ist_titel(z)] or [(z, h) for z, h in zeilen if ist_titel(z)]
    if not kandidaten:
        return None
    titel, link = max(kandidaten, key=lambda zh: len(zh[0]))
    link = link or next((h for _, h in zeilen if h), "")
    url, kennung = _kennung(portal, link, titel, ort, _zahl(flaeche.group(1)))
    plz = PLZ.search(ort)
    return {
        "kennung": kennung, "portal": portal.name, "titel": titel[:200], "ort": ort[:120], "plz": plz.group(1) if plz else "",
        "kalt": kalt, "warm": warm, "flaeche": _zahl(flaeche.group(1)), "zimmer": anzahl, "url": url,
        "merkmale": [m for m, muster in MERKMALE.items() if muster.search(re.sub(r"(?i)\bohne\s+\w+", "", titel))],
    }


def angebote_aus_mail(roh: bytes) -> list[dict]:
    nachricht = email.message_from_bytes(roh, policy=email.policy.default)
    portal = portal_der_mail(str(nachricht.get("From", "")))
    if not portal:
        return []
    teil = nachricht.get_body(("html",))
    if teil is None:
        return []
    try:
        datum = parsedate_to_datetime(str(nachricht.get("Date"))).date().isoformat()
    except (TypeError, ValueError):
        datum = date.today().isoformat()
    soup = BeautifulSoup(teil.get_content(), "html.parser")
    angebote, gesehen = [], set()
    for karte in _karten(soup):
        a = angebot_aus_karte(karte, portal)
        if a and a["kennung"] not in gesehen:
            gesehen.add(a["kennung"])
            angebote.append({**a, "datum": datum})
    return angebote


# --- Speicher -------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS wohnungen (
    kennung TEXT PRIMARY KEY,
    portal TEXT NOT NULL,
    titel TEXT NOT NULL,
    ort TEXT NOT NULL DEFAULT '',
    plz TEXT NOT NULL DEFAULT '',
    kalt REAL,
    warm REAL,
    flaeche REAL,
    zimmer REAL,
    merkmale TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL DEFAULT '',
    datum TEXT NOT NULL,              -- Datum der jüngsten Mail mit diesem Angebot
    zuerst TEXT NOT NULL,             -- wann die App es zum ersten Mal sah
    status TEXT NOT NULL DEFAULT 'neu',  -- neu, gemerkt, verworfen
    lat REAL,
    lon REAL,
    bundesland TEXT NOT NULL DEFAULT '',
    km INTEGER,                       -- Fahrstrecke zum nächsten Arbeitsort
    km_ziel TEXT NOT NULL DEFAULT ''
);
"""

TAGE = 30  # Wohnungen sind schneller weg als Stellen; älter lohnt nicht
# NRW bis in den Norden: die Vorauswahl für den Umzug
BUNDESLAENDER = ("Nordrhein-Westfalen", "Niedersachsen", "Bremen", "Hamburg", "Schleswig-Holstein", "Mecklenburg-Vorpommern",
                 "Sachsen-Anhalt", "Brandenburg", "Berlin", "Hessen", "Thüringen", "Sachsen", "Rheinland-Pfalz", "Saarland",
                 "Baden-Württemberg", "Bayern")
STANDARD_LAENDER = BUNDESLAENDER[:5]


def tabelle_anlegen(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def speichere(conn: sqlite3.Connection, angebote: list[dict]) -> int:
    """Neue Angebote anlegen, bekannte auffrischen (Preis, Datum). Status, Ort und Entfernung bleiben. Gibt die Zahl neuer zurück."""
    tabelle_anlegen(conn)
    neu = 0
    for a in angebote:
        alt = conn.execute("SELECT datum FROM wohnungen WHERE kennung = ?", (a["kennung"],)).fetchone()
        if alt is None:
            neu += 1
            conn.execute(
                "INSERT INTO wohnungen (kennung, portal, titel, ort, plz, kalt, warm, flaeche, zimmer, merkmale, url, datum, zuerst) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a["kennung"], a["portal"], a["titel"], a["ort"], a["plz"], a["kalt"], a["warm"], a["flaeche"], a["zimmer"],
                 json.dumps(a["merkmale"]), a["url"], a["datum"], db.jetzt()))
        elif a["datum"] >= alt["datum"]:
            conn.execute("UPDATE wohnungen SET kalt = ?, warm = ?, datum = ?, url = ? WHERE kennung = ?",
                         (a["kalt"], a["warm"], a["datum"], a["url"] or "", a["kennung"]))
    conn.commit()
    return neu


def setze_status(conn: sqlite3.Connection, kennung: str, status: str) -> None:
    tabelle_anlegen(conn)
    conn.execute("UPDATE wohnungen SET status = ? WHERE kennung = ?", (status, kennung))
    conn.commit()


# --- Einstellungen ----------------------------------------------------------------------------------------------------

@dataclass
class Filter:
    max_miete: int = 0          # 0 = egal; verglichen wird die Warmmiete, sonst Kaltmiete + geschätzte Nebenkosten
    min_zimmer: float = 0
    min_flaeche: int = 0
    laender: tuple[str, ...] = STANDARD_LAENDER
    max_km: int = 0             # 0 = egal
    merkmale: tuple[str, ...] = ()  # alle davon müssen im Titel stehen
    ohne_tausch_wbs: bool = True
    verworfene: bool = False


def lade_filter(conn: sqlite3.Connection) -> Filter:
    werte = json.loads(db.einstellung(conn, "wohnung.filter", "{}") or "{}")
    f = Filter()
    for k, v in werte.items():
        if hasattr(f, k):
            setattr(f, k, tuple(v) if isinstance(v, list) else v)
    return f


def merke_filter(conn: sqlite3.Connection, f: Filter) -> None:
    db.setze_einstellung(conn, "wohnung.filter", json.dumps(f.__dict__, ensure_ascii=False))


def arbeitsorte(conn: sqlite3.Connection) -> list[str]:
    return [o.strip() for o in db.einstellung(conn, "wohnung.arbeitsorte", "").split(";") if o.strip()]


def setze_arbeitsorte(conn: sqlite3.Connection, orte: list[str]) -> None:
    db.setze_einstellung(conn, "wohnung.arbeitsorte", "; ".join(o.strip() for o in orte if o.strip()))
    tabelle_anlegen(conn)
    conn.execute("UPDATE wohnungen SET km = NULL, km_ziel = ''")  # neu rechnen
    conn.commit()


NEBENKOSTEN_JE_M2 = 2.8  # grobe Schätzung (Betriebskosten + Heizung) für Angebote ohne Warmmiete


def warmmiete(zeile) -> float | None:
    if zeile["warm"]:
        return zeile["warm"]
    if zeile["kalt"] and zeile["flaeche"]:
        return round(zeile["kalt"] + zeile["flaeche"] * NEBENKOSTEN_JE_M2)
    return zeile["kalt"]


def passt(zeile, f: Filter) -> tuple[bool, list[str]]:
    """(passt, Gründe dagegen). Fehlende Angaben zählen nicht gegen das Angebot."""
    gruende = []
    merkmale = json.loads(zeile["merkmale"] or "[]")
    miete = warmmiete(zeile)
    if f.max_miete and miete and miete > f.max_miete:
        gruende.append(f"{miete:.0f} € warm")
    if f.min_zimmer and zeile["zimmer"] and zeile["zimmer"] < f.min_zimmer:
        gruende.append(f"{zeile['zimmer']:g} Zimmer")
    if f.min_flaeche and zeile["flaeche"] and zeile["flaeche"] < f.min_flaeche:
        gruende.append(f"{zeile['flaeche']:g} m²")
    if f.laender and zeile["bundesland"] and zeile["bundesland"] not in f.laender:
        gruende.append(zeile["bundesland"])
    if f.max_km and zeile["km"] is not None and zeile["km"] > f.max_km:
        gruende.append(f"{zeile['km']} km zur Arbeit")
    for m in f.merkmale:
        if m not in merkmale:
            gruende.append(f"ohne {m}")
    if f.ohne_tausch_wbs:
        gruende += [m.upper() if m == "wbs" else "Tausch" for m in merkmale if m in WARNUNG]
    return not gruende, gruende


def liste(conn: sqlite3.Connection, f: Filter | None = None) -> list[tuple[sqlite3.Row, bool, list[str]]]:
    """Alle Angebote der letzten Wochen mit Bewertung, passende zuerst, darin die neuesten zuerst."""
    tabelle_anlegen(conn)
    f = f or lade_filter(conn)
    grenze = (date.today() - timedelta(days=TAGE)).isoformat()
    zeilen = conn.execute("SELECT * FROM wohnungen WHERE datum >= ? ORDER BY datum DESC, zuerst DESC", (grenze,)).fetchall()
    aus = []
    for z in zeilen:
        if z["status"] == "verworfen" and not f.verworfene:
            continue
        ok, gruende = passt(z, f)
        aus.append((z, ok, gruende))
    return sorted(aus, key=lambda e: (e[0]["status"] != "gemerkt", not e[1]))


# --- Ort, Bundesland, Entfernung --------------------------------------------------------------------------------------

def _ortsanfrage(ort: str) -> str:
    """„Mannheimer Straße 6, Oggersheim, Ludwigshafen am Rhein“ → Straße + Stadt; „Hiltrup, Münster (48165)“ → „48165 Münster“."""
    plz = PLZ.search(ort)
    ohne = re.sub(r"\(?\b\d{5}\b\)?", "", ort).strip(" ,")
    teile = [t.strip() for t in ohne.split(",") if t.strip()]
    if plz:
        return f"{plz.group(1)} {teile[-1] if teile else ''}".strip()
    if len(teile) >= 3:
        return f"{teile[0]}, {teile[-1]}"
    return ", ".join(teile)


def verorte(conn: sqlite3.Connection, client=None, melde=lambda _: None, hoechstens: int = 60) -> int:
    """Koordinaten und Bundesland für Angebote ohne Ort (Nominatim, gedrosselt, je Anfrage zwischengespeichert)."""
    from . import quellen
    tabelle_anlegen(conn)
    offen = conn.execute("SELECT kennung, ort FROM wohnungen WHERE lat IS NULL AND ort != '' ORDER BY zuerst DESC LIMIT ?",
                         (hoechstens,)).fetchall()
    erledigt = 0
    netz = quellen._client(client)
    for i, z in enumerate(offen):
        anfrage = _ortsanfrage(z["ort"])
        schluessel = f"cache.wohnort.{anfrage.lower()}"
        gemerkt = db.einstellung(conn, schluessel, "")
        if gemerkt:
            lat, lon, land = json.loads(gemerkt)
        else:
            melde(f"Verorte Wohnung {i + 1}/{len(offen)}: {anfrage}")
            try:
                antwort = quellen._frage_nominatim(netz, {"q": anfrage, "countrycodes": "de", "format": "jsonv2", "limit": 1,
                                                           "addressdetails": 1})
                treffer = antwort.json() if antwort.status_code == 200 else []
            except Exception:  # Dienst weg: beim nächsten Mal
                break
            if not treffer:
                lat = lon = 0.0
                land = ""
            else:
                lat, lon = float(treffer[0]["lat"]), float(treffer[0]["lon"])
                land = treffer[0].get("address", {}).get("state", "")
            db.setze_einstellung(conn, schluessel, json.dumps([lat, lon, land]))
        conn.execute("UPDATE wohnungen SET lat = ?, lon = ?, bundesland = ? WHERE kennung = ?", (lat, lon, land, z["kennung"]))
        erledigt += 1
    conn.commit()
    return erledigt


def entfernungen(conn: sqlite3.Connection, client=None, melde=lambda _: None) -> int:
    """Fahrstrecke (Auto, km) von jeder verorteten Wohnung zum nächsten Arbeitsort."""
    from . import quellen
    orte = arbeitsorte(conn)
    if not orte:
        return 0
    ziele = []
    for ort in orte:
        schluessel = f"cache.wohnort.{_ortsanfrage(ort).lower()}"
        gemerkt = db.einstellung(conn, schluessel, "")
        if not gemerkt:
            try:
                antwort = quellen._frage_nominatim(quellen._client(client), {"q": ort, "countrycodes": "de", "format": "jsonv2", "limit": 1})
                treffer = antwort.json() if antwort.status_code == 200 else []
            except Exception:
                return 0
            gemerkt = json.dumps([float(treffer[0]["lat"]), float(treffer[0]["lon"]), ""] if treffer else [0.0, 0.0, ""])
            db.setze_einstellung(conn, schluessel, gemerkt)
        lat, lon, _ = json.loads(gemerkt)
        if lat:
            ziele.append((ort, (lat, lon)))
    if not ziele:
        return 0
    offen = conn.execute("SELECT kennung, lat, lon FROM wohnungen WHERE km IS NULL AND lat IS NOT NULL AND lat != 0").fetchall()
    for i, z in enumerate(offen):
        melde(f"Fahrstrecke {i + 1}/{len(offen)}")
        km = quellen.fahrstrecken((z["lat"], z["lon"]), [p for _, p in ziele], client)
        beste = min(((k, name) for k, (name, _) in zip(km, ziele) if k is not None), default=None)
        if beste:
            conn.execute("UPDATE wohnungen SET km = ?, km_ziel = ? WHERE kennung = ?", (beste[0], beste[1], z["kennung"]))
    conn.commit()
    return len(offen)


BREITBANDATLAS = "https://gigabitgrundbuch.bund.de/GIGA/DE/Breitbandatlas/Vollbild/start.html"


def netz_adresse(zeile) -> str:
    """Was im Breitbandatlas (Glasfaser, Kabel, 5G der Bundesnetzagentur) ins Suchfeld gehört."""
    return _ortsanfrage(zeile["ort"]) or zeile["ort"]


# --- Postfächer -------------------------------------------------------------------------------------------------------

def konten(conn: sqlite3.Connection):
    """Postfächer für die Wohnungssuche; ohne eigene Wahl die der Job-Alerts."""
    from . import postfach
    gewaehlt = json.loads(db.einstellung(conn, "wohnung.konten", "") or "null")
    if gewaehlt is None:
        return postfach.gewaehlte(conn)
    return [postfach.Postfach(**p) for p in gewaehlt]


def waehle_konten(conn: sqlite3.Connection, postfaecher) -> None:
    db.setze_einstellung(conn, "wohnung.konten", json.dumps([p.__dict__ for p in postfaecher]))


def lies(conn: sqlite3.Connection, melde=lambda _: None, imap_klasse=None, client=None) -> tuple[int, list[str]]:
    """Neue Suchauftrags-Mails aus allen Wohnungs-Postfächern lesen, speichern, verorten. (neue Angebote, Hinweise)"""
    from . import postfach
    from .quellen import SucheFehler
    tabelle_anlegen(conn)
    hinweise, neu = [], 0
    postfaecher = konten(conn)
    if not postfaecher:
        return 0, ["Wohnungen: Noch kein Postfach gewählt."]
    for p in postfaecher:
        kennwort = postfach.passwort(conn, p)
        if not kennwort:
            hinweise.append(f"Wohnungen: Für {p.name} ist kein Passwort gespeichert.")
            continue
        schluessel = f"cache.wohnpostfach.{p.benutzer}@{p.host}"
        gemerkt = json.loads(db.einstellung(conn, schluessel, "") or "{}")
        melde(f"Lese Wohnungsangebote aus {p.name}")
        try:
            validity, mails = postfach._hole(p, kennwort, gemerkt.get("uidvalidity", ""), int(gemerkt.get("letzte_uid", 0)),
                                             imap_klasse, absender=[q.absender for q in PORTALE], tage=TAGE)
        except SucheFehler as fehler:
            hinweise.append(f"Wohnungen: {fehler}")
            continue
        angebote = [a for _, roh in mails for a in angebote_aus_mail(roh)]
        neu += speichere(conn, angebote)
        letzte = max([u for u, _ in mails] + [int(gemerkt.get("letzte_uid", 0)) if validity == gemerkt.get("uidvalidity") else 0])
        db.setze_einstellung(conn, schluessel, json.dumps({"uidvalidity": validity, "letzte_uid": letzte,
                                                           "zeit": datetime.now().isoformat(timespec="seconds")}))
        if mails:
            db.protokolliere(conn, "Wohnungen gelesen", f"{p.name}: {len(mails)} Mails, {len(angebote)} Angebote")
    verorte(conn, client, melde)
    entfernungen(conn, client, melde)
    return neu, hinweise
