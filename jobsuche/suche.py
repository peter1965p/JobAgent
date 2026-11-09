"""Firmensuche über mehrere Quellen: Firmenverzeichnis, Internet und die Jobbörse der Bundesagentur für Arbeit.

Die Quellen liefern Firmen. Inhaber und Mailadresse findet `hole_inhaber` auf den Firmenseiten selbst.
"""

import base64
import hashlib
import html
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from . import config, db, firmen, ki, llm, profil, quellen, rag, regeln
from .quellen import SucheFehler

API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
# Dieser Schlüssel ist öffentlich und gehört zur Web-Jobsuche der Bundesagentur. Er ist kein Geheimnis.
API_SCHLUESSEL = "jobboerse-jobsuche"
# Personalvermittler und Zeitarbeit taugen nicht für die Direktansprache der Geschäftsführung.
VERMITTLER = re.compile(r"personal\s*(?:vermittlung|dienst|service|beratung|leasing|management)|zeitarbeit|arbeitnehmerüberlassung|"
                        r"personalvermittl|recruiting|headhunt", re.I)
ARBEITSZEIT = {"": "Voll- und Teilzeit", "vollzeit": "Vollzeit", "teilzeit": "Teilzeit"}
ARBEITSZEIT_CODE = {"vollzeit": "vz", "teilzeit": "tz"}
ANZEIGE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{}"
ANZEIGEN_SUCHE = API  # Eigener Name, damit Tests diese Abfragen getrennt umleiten können.
DETAILS = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{}"
MAX_ANZEIGE = 8000


def _arbeitszeit(stelle: dict) -> str:
    teilzeit = any(stelle.get(f"arbeitszeitTeilzeit{art}") for art in ("Abend", "Flexibel", "Nachmittag", "Vormittag"))
    return ", ".join(name for name, gilt in (("Vollzeit", stelle.get("arbeitszeitVollzeit")), ("Teilzeit", teilzeit)) if gilt)


def suche(begriffe: str, wo: str, umkreis: int = 50, client: httpx.Client | None = None, arbeitszeit: str = "") -> list[dict]:
    """`begriffe` darf mehrere Suchwörter enthalten, durch Kommas getrennt."""
    woerter = [b.strip() for b in begriffe.split(",") if b.strip()]
    if not woerter or not wo.strip():
        raise SucheFehler("Gib einen Suchbegriff und einen Ort an.")
    client = client or httpx.Client(timeout=30, headers={"User-Agent": config.USER_AGENT})
    treffer: dict[str, dict] = {}
    filter_zeit = {"arbeitszeit": ARBEITSZEIT_CODE[arbeitszeit]} if arbeitszeit else {}
    for wort in woerter:
        try:
            antwort = client.get(API, headers={"X-API-Key": API_SCHLUESSEL}, params={
                "was": wort, "wo": wo.strip(), "umkreis": umkreis, "size": 50, "page": 1, "angebotsart": 1, "pav": "false", "zeitarbeit": "false",
                **filter_zeit,
            })
        except httpx.HTTPError as fehler:
            raise SucheFehler(f"Die Jobbörse ist nicht erreichbar: {fehler}") from fehler
        if antwort.status_code != 200:
            raise SucheFehler(f"Die Jobbörse meldet {antwort.status_code}. Vielleicht hat sich die Schnittstelle geändert.")
        for stelle in antwort.json().get("ergebnisliste", []):
            referenz = stelle.get("referenznummer", "")
            firma = str(stelle.get("firma", "")).strip()
            if not referenz or not firma or referenz in treffer or VERMITTLER.search(firma):
                continue
            orte = stelle.get("stellenlokationen") or [{}]
            treffer[referenz] = {
                "referenz": referenz,
                "firma": firma,
                "titel": " · ".join(t for t in (str(stelle.get("stellenangebotsTitel", "")).strip(), _arbeitszeit(stelle)) if t),
                "beruf": str(stelle.get("hauptberuf", "")).strip(),
                "ort": (orte[0].get("adresse") or {}).get("ort", ""),
                "entfernung": stelle.get("entfernung"),
                "url": stelle.get("externeUrl") or ANZEIGE.format(referenz),
                "website": "",
                "quelle": "Jobbörse",
            }
    return list(treffer.values())


BEGRIFFE_SCHEMA = {
    "type": "object",
    "properties": {
        "berufe": {"type": "array", "items": {"type": "string"}},
        "branche": {"type": "string", "enum": list(quellen.BRANCHEN)},
        "stichwoerter": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["berufe", "branche", "stichwoerter"],
}
# IT-Berufe landen beim Modell gern unter „technik“. Im Verzeichnis sind das Ingenieurbüros, Systemhäuser fehlen dann.
IT_BERUF = re.compile(r"(?i)(?:^|[\s-])(?:it|edv)(?:$|[\s-])|software|informatik|entwickler|developer|administrator|devops|"
                      r"netzwerk|systembetreu|support|helpdesk|programmier|cloud|daten(?:bank)?")
MAX_BEGRIFFE = 6
VERZEICHNIS_FRISCH = timedelta(days=7)  # Firmeneinträge ändern sich langsam, die OSM-Server sind oft überlastet.
GLEICHZEITIGE_SEITEN = 8  # Jede Website wird nur einmal gleichzeitig abgerufen, belastet wird also keine einzelne.


def _fakten_fuer_suche(conn: sqlite3.Connection) -> list[str]:
    return [z[0] for z in conn.execute(
        "SELECT text FROM fakten WHERE status = 'bestaetigt' AND kategorie != 'suche' ORDER BY kategorie, id")]


def bereinige_begriffe(werte) -> list[str]:
    """Kurze Berufsbezeichnungen ohne Doppelte. Sätze und interne Angaben fallen raus."""
    ergebnis: list[str] = []
    for wert in werte if isinstance(werte, list) else []:
        text = re.sub(r"\s*\(?[mwd]/[mwd](?:/[mwd])?\)?", "", str(wert)).strip(" .,;")
        if text and len(text.split()) <= 4 and not regeln.ist_sensibel(text) and text.lower() not in map(str.lower, ergebnis):
            ergebnis.append(text)
    return ergebnis[:MAX_BEGRIFFE]


def leite_begriffe_ab(conn: sqlite3.Connection, modell: str, chat=llm.chat) -> list[str]:
    """Das lokale Modell liest die bestätigten Fakten und nennt Berufe und Branche. Das Ergebnis bleibt gespeichert,
    bis sich die Fakten ändern: Ein Modelllauf dauert auf der CPU Minuten."""
    fakten = _fakten_fuer_suche(conn)
    if not fakten:
        raise SucheFehler("Noch keine bestätigten Fakten: Erst unter „Fakten“ oder im CV-Assistenten etwas eintragen.")
    pruefsumme = hashlib.sha256("\n".join(fakten).encode()).hexdigest()
    if db.einstellung(conn, "suche.fakten_stand", "") == pruefsumme and db.einstellung(conn, "suche.abgeleitet_branche", ""):
        gespeichert = db.einstellung(conn, "suche.abgeleitet", "")
        if gespeichert:
            return gespeichert.split(", ")
    system = (
        "Du liest die Fakten über einen Bewerber und nennst Berufsbezeichnungen, unter denen er passende Stellen und "
        f"Firmen findet. Höchstens {MAX_BEGRIFFE}, die passendsten zuerst, jeweils höchstens drei Wörter, so wie sie in "
        "echten Stellenanzeigen stehen. Nur was die Fakten belegen, keine Wunschberufe. Wähle unter branche, wo solche "
        "Firmen am ehesten zu finden sind: it für Software, Systemhäuser, Administration und Support, technik nur für "
        "Ingenieur- und Planungsbüros. Unter stichwoerter nennst du bis zu zehn kurze Wörter, an denen man passende "
        "Stellentitel erkennt, zum Beispiel Administrator, Support, Entwickler."
    )
    antwort = chat(modell, system, "\n".join(f"- {f}" for f in fakten), BEGRIFFE_SCHEMA, 0.2)
    begriffe = bereinige_begriffe(antwort.get("berufe"))
    if not begriffe:
        raise SucheFehler("Das Modell hat keine brauchbaren Berufe genannt. Trage Suchbegriffe von Hand ein.")
    db.setze_einstellung(conn, "suche.abgeleitet", ", ".join(begriffe))
    branche = antwort.get("branche")
    if branche == "technik" and sum(bool(IT_BERUF.search(b)) for b in begriffe) * 2 >= len(begriffe):
        branche = "it"
    if branche in quellen.BRANCHEN:
        db.setze_einstellung(conn, "suche.abgeleitet_branche", branche)
    stichwoerter = bereinige_stichwoerter(antwort.get("stichwoerter"))
    if stichwoerter:
        db.setze_einstellung(conn, "suche.abgeleitet_stichwoerter", ", ".join(stichwoerter))
    db.setze_einstellung(conn, "suche.fakten_stand", pruefsumme)
    db.protokolliere(conn, "Suchbegriffe abgeleitet", f"{len(begriffe)} Begriffe, Modell {modell}")
    return begriffe


MAX_STICHWOERTER = 15


def bereinige_stichwoerter(werte) -> list[str]:
    ergebnis: list[str] = []
    for wert in werte if isinstance(werte, list) else []:
        wort = str(wert).strip(" .,;()")
        if 2 <= len(wort) <= 30 and len(wort.split()) <= 2 and wort.lower() not in map(str.lower, ergebnis):
            ergebnis.append(wort)
    return ergebnis[:MAX_STICHWOERTER]


def stichwoerter(conn: sqlite3.Connection, begriffe: list[str]) -> list[str]:
    """Woran die App bei Alerts und Portalen passende Titel erkennt. Eigene Angaben gehen vor denen des Modells, ohne beide
    zählen die Suchbegriffe und ihre Teile. Geändert werden sie auf der Suchseite."""
    for schluessel in ("suche.stichwoerter", "suche.abgeleitet_stichwoerter"):
        eigene = [w.strip() for w in db.einstellung(conn, schluessel, "").split(",") if w.strip()]
        if eigene:
            return eigene
    teile = [t for b in begriffe for t in re.split(r"[\s/-]+", b) if len(t) >= 2]
    return bereinige_stichwoerter(begriffe + teile)


def passt_stichwort(titel: str, woerter: list[str]) -> bool:
    """Kurze Wörter wie IT nur als ganzes Wort, sonst stünde IT in jedem „Mitarbeiter“."""
    klein = titel.lower()
    for wort in (w.lower() for w in woerter):
        if len(wort) >= 5 and wort in klein:
            return True
        if len(wort) < 5 and re.search(rf"(?<![a-zäöüß]){re.escape(wort)}(?![a-zäöüß])", klein):
            return True
    return False


MAX_NEUE_ORTE = 40  # Nominatim erlaubt eine Anfrage pro Sekunde, gemerkte Orte kosten nichts.


def _ort_koordinaten(conn: sqlite3.Connection, ort: str, land: str, client, budget: list[int]) -> tuple[float, float] | None | str:
    """Koordinaten, None für unbekannt oder "offen", wenn das Budget für neue Anfragen aufgebraucht ist."""
    schluessel = f"cache.ort.{land}.{ort.lower()}"
    gemerkt = db.einstellung(conn, schluessel, "")
    if gemerkt:
        return json.loads(gemerkt)
    if budget[0] <= 0:
        return "offen"
    budget[0] -= 1
    punkt = quellen.koordinaten_des_orts(ort, land, client)
    db.setze_einstellung(conn, schluessel, json.dumps(punkt))
    return punkt


def filtere_stellen(conn: sqlite3.Connection, treffer: list[dict], auftrag: Auftrag, begriffe: list[str], name: str,
                    melde=lambda _: None, client=None) -> tuple[list[dict], str]:
    """Alerts, Portale und Aggregatoren liefern auch Fremdes: Stellen anderer Berufe und anderer Orte. Fernarbeit bleibt,
    ein unbekannter Ort auch, beides steht dann im Hinweis."""
    if not treffer:
        return [], ""
    woerter = stichwoerter(conn, begriffe)
    passend = [t for t in treffer if not woerter or passt_stichwort(t["titel"], woerter)]
    fremd_beruf = len(treffer) - len(passend)
    zu_weit = unklar = 0
    bleiben = passend
    if auftrag.ort.strip() and passend:
        try:
            gebiet = quellen.finde_gebiet(auftrag.ort, auftrag.land, client)
        except SucheFehler:
            gebiet = None
        if gebiet:
            melde(f"{name}: prüfe die Orte von {len(passend)} Stellen")
            bleiben, budget = [], [MAX_NEUE_ORTE]
            for t in passend:
                ort = quellen.ortsname(t.get("ort", ""))
                if quellen.FERN.search(t.get("ort", "")) or not ort:
                    unklar += not quellen.FERN.search(t.get("ort", ""))
                    bleiben.append(t)
                    continue
                try:
                    punkt = _ort_koordinaten(conn, ort, auftrag.land, client, budget)
                except SucheFehler:
                    punkt = "offen"
                if punkt is None or punkt == "offen":
                    unklar += 1
                    bleiben.append(t)
                elif quellen.liegt_im_gebiet(gebiet, auftrag.umkreis, *punkt):
                    bleiben.append({**t, "entfernung": t.get("entfernung") or quellen._km(gebiet.lat, gebiet.lon, *punkt)})
                else:
                    zu_weit += 1
    teile = [text for zahl, text in ((fremd_beruf, f"{fremd_beruf} mit anderem Beruf"), (zu_weit, f"{zu_weit} außerhalb des Gebiets"))
             if zahl]
    hinweis = f"{name}: aussortiert {', '.join(teile)}." if teile else ""
    if unklar:
        hinweis = (hinweis + " " if hinweis else f"{name}: ") + f"Bei {unklar} ist der Ort unklar, sie bleiben drin."
    return bleiben, hinweis


def suchort(conn: sqlite3.Connection) -> str:
    """Der Ort aus dem Suchwunsch geht vor dem Wohnort: Wer umziehen will, sucht nicht zu Hause."""
    for zeile in conn.execute("SELECT text FROM fakten WHERE status = 'bestaetigt' AND kategorie = 'suche' ORDER BY id"):
        treffer = re.search(r"\b(?:Raum|Region|Großraum)\s+([A-ZÄÖÜ][\wäöüß-]+)", zeile[0])
        if treffer:
            return treffer.group(1)
    zeile = conn.execute("SELECT ort FROM person WHERE id = 1").fetchone()
    return re.split(r"[\s(,]", zeile[0])[0] if zeile and zeile[0] else ""


@dataclass
class Auftrag:
    """`umkreis` 0 heißt: die ganze Region oder, ohne Ort, das ganze Land. `branche` leer: aus den Fakten."""
    begriffe: str = ""
    ort: str = ""
    land: str = "de"
    umkreis: int = 50
    branche: str = ""
    quellen: tuple[str, ...] = ("verzeichnis", "jobboerse", "arbeitnow")
    searxng: str = ""
    arbeitszeit: str = ""


def gespeicherter_auftrag(conn: sqlite3.Connection) -> Auftrag:
    """Die Angaben der Suchseite gelten auch für den Nachtlauf."""
    lies = lambda name, vorgabe="": db.einstellung(conn, f"suche.{name}", vorgabe)
    gewaehlt = tuple(q for q in lies("quellen", ",".join(Auftrag.quellen)).split(",") if q)
    return Auftrag(lies("begriffe"), lies("ort") or suchort(conn), lies("land", "de"), int(lies("umkreis", "50")),
                   lies("branche"), gewaehlt, lies("searxng"), lies("arbeitszeit"))


def _gleich(treffer: dict) -> str:
    return regeln.kurzname(treffer["firma"]).lower()


def _vereine(listen: list[list[dict]]) -> list[dict]:
    """Dieselbe Firma aus zwei Quellen zählt einmal, die Website bleibt erhalten. Viele Anzeigen einer Firma deuten auf einen Konzern."""
    ergebnis: dict[str, dict] = {}
    anzahl: dict[str, int] = {}
    for treffer in (t for liste in listen for t in liste):
        anzahl[_gleich(treffer)] = anzahl.get(_gleich(treffer), 0) + 1
        # Die Jobbörse filtert Vermittler selbst, die anderen Quellen nicht.
        if VERMITTLER.search(treffer["firma"]):
            continue
        vorhanden = ergebnis.get(_gleich(treffer))
        if not vorhanden:
            ergebnis[_gleich(treffer)] = treffer
        elif treffer["quelle"] not in vorhanden["quelle"]:
            vorhanden["quelle"] += f", {treffer['quelle']}"
            vorhanden["website"] = vorhanden["website"] or treffer["website"]
    for schluessel, treffer in ergebnis.items():
        merkmale = firmen.konzern_merkmale(treffer["firma"]) + ([treffer["konzern"]] if treffer.get("konzern") else [])
        if anzahl[schluessel] >= 3:
            merkmale.append(f"{anzahl[schluessel]} Anzeigen in dieser Suche")
        treffer["konzern"] = ", ".join(dict.fromkeys(merkmale))
    return list(ergebnis.values())


def _sitz_der_website(website: str, land: str, client: httpx.Client) -> tuple[str, str, str, str] | None:
    """(PLZ, Ort, Kurztext der Startseite, Konzernmerkmale aus dem Impressum). Der Schnipsel der Suchmaschine stammt oft vom
    Impressum und sagt nichts über die Firma. None: Die Seite war nicht lesbar, das kann beim nächsten Mal anders sein."""
    try:
        seiten = firmen.hole_seiten(website, client, 2)
    except (RuntimeError, httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None
    kurz = firmen.kurztext(next(iter(seiten.values())), 160)
    texte = {url: firmen.text_aus_html(html) for url, html in seiten.items()}
    impressum = "\n".join(t for u, t in texte.items() if firmen.IMPRESSUM.search(u))
    konzern = ", ".join(firmen.konzern_merkmale("", impressum))
    for url in sorted(texte, key=lambda u: not firmen.IMPRESSUM.search(u)):
        plz, ort = quellen.sitz_aus_text(texte[url], land)
        if plz:
            return plz, ort, kurz, konzern
    return "", "", kurz, konzern


def _sitze(conn: sqlite3.Connection, treffer: list[dict], land: str, client: httpx.Client) -> dict[str, tuple[str, str, str, str] | None]:
    """Sitz je Website, gemerkte Antworten zuerst. Jede Suche soll dieselben kleinen Firmen nicht erneut abrufen."""
    sitze, offen = {}, []
    for t in treffer:
        gemerkt = json.loads(db.einstellung(conn, f"cache.sitz.{land}.{firmen._host(t['website'])}", "") or "null")
        # Einträge ohne Konzernmerkmale stammen aus einer älteren Version und werden neu gelesen.
        if gemerkt and "konzern" in gemerkt and datetime.now() - datetime.fromisoformat(gemerkt["zeit"]) < VERZEICHNIS_FRISCH:
            sitze[t["website"]] = (gemerkt["plz"], gemerkt["ort"], gemerkt["kurz"], gemerkt["konzern"])
        else:
            offen.append(t["website"])
    with ThreadPoolExecutor(GLEICHZEITIGE_SEITEN) as pool:
        gelesen = list(pool.map(lambda w: _sitz_der_website(w, land, client), offen))
    for website, sitz in zip(offen, gelesen):
        sitze[website] = sitz
        if sitz:
            db.setze_einstellung(conn, f"cache.sitz.{land}.{firmen._host(website)}",
                                 json.dumps({"zeit": db.jetzt(), "plz": sitz[0], "ort": sitz[1], "kurz": sitz[2], "konzern": sitz[3]}, ensure_ascii=False))
    return sitze


def _koordinaten(conn: sqlite3.Connection, plz: str, land: str, client) -> tuple[float, float] | None:
    schluessel = f"cache.plz.{land}.{plz}"
    gemerkt = db.einstellung(conn, schluessel, "")
    if gemerkt:
        return json.loads(gemerkt)
    punkt = quellen.koordinaten_der_plz(plz, land, client)
    db.setze_einstellung(conn, schluessel, json.dumps(punkt))
    return punkt


def nur_im_gebiet(conn: sqlite3.Connection, treffer: list[dict], auftrag: Auftrag, melde=lambda _: None, client=None) -> tuple[list[dict], str]:
    """Suchmaschinen kennen keinen Umkreis. Der Sitz aus dem Impressum entscheidet, ohne lesbare Adresse fliegt der Treffer raus."""
    if not auftrag.ort.strip() or not treffer:
        return treffer, ""
    gebiet = quellen.finde_gebiet(auftrag.ort, auftrag.land, client)
    melde(f"Prüfe den Firmensitz von {len(treffer)} Internet-Treffern")
    netz = client or httpx.Client(follow_redirects=True, timeout=10, headers={"User-Agent": config.USER_AGENT})
    sitze = _sitze(conn, treffer, auftrag.land, netz)
    bleiben, zu_weit, unklar = [], 0, 0
    for t in treffer:
        plz, ort, kurz, konzern = sitze[t["website"]] or ("", "", "", "")
        punkt = _koordinaten(conn, plz, auftrag.land, client) if plz else None
        if not punkt:
            unklar += 1
        elif not quellen.liegt_im_gebiet(gebiet, auftrag.umkreis, *punkt):
            zu_weit += 1
        else:
            bleiben.append({**t, "ort": ort, "entfernung": quellen._km(gebiet.lat, gebiet.lon, *punkt), "titel": kurz or t["titel"], "konzern": konzern})
    teile = [text for zahl, text in ((zu_weit, f"{zu_weit} außerhalb des Gebiets"), (unklar, f"{unklar} ohne lesbare Adresse")) if zahl]
    return bleiben, f"Internet: aussortiert, weil der Firmensitz nicht passt: {', '.join(teile)}." if teile else ""


def suche_ueberall(conn: sqlite3.Connection, auftrag: Auftrag, melde=lambda _: None, client=None, imap_klasse=None) -> dict:
    """Fällt eine Quelle aus, liefern die anderen weiter. Der Ausfall steht dann in `hinweise`."""
    auftrag.begriffe = auftrag.begriffe.strip() or db.einstellung(conn, "suche.abgeleitet", "")
    begriffe = [b.strip() for b in auftrag.begriffe.split(",") if b.strip()]
    branche = auftrag.branche or db.einstellung(conn, "suche.abgeleitet_branche", "") or "alle"
    listen, hinweise = [], []

    def quelle(name, arbeit):
        melde(f"Suche: {name}")
        try:
            listen.append(arbeit())
        except SucheFehler as fehler:
            hinweise.append(f"{name}: {fehler}")

    if "verzeichnis" in auftrag.quellen:
        def verzeichnis():
            schluessel = "cache.verzeichnis." + hashlib.sha256(
                f"{auftrag.ort.lower()}|{auftrag.land}|{auftrag.umkreis}|{branche}".encode()).hexdigest()[:24]
            gemerkt = json.loads(db.einstellung(conn, schluessel, "") or "null")
            if gemerkt and datetime.now() - datetime.fromisoformat(gemerkt["zeit"]) < VERZEICHNIS_FRISCH:
                return gemerkt["treffer"]
            try:
                gebiet = quellen.finde_gebiet(auftrag.ort, auftrag.land, client)
                gefunden = quellen.verzeichnis(gebiet, auftrag.umkreis, branche, client)
            except SucheFehler:
                if not gemerkt:
                    raise
                hinweise.append(f"Firmenverzeichnis: Server gerade überlastet, ich zeige den Stand vom {gemerkt['zeit'][:10]}.")
                return gemerkt["treffer"]
            if len(gefunden) >= quellen.MAX_VERZEICHNIS:
                hinweise.append(f"Firmenverzeichnis: Nur die ersten {quellen.MAX_VERZEICHNIS} Einträge, das Gebiet ist groß.")
            db.setze_einstellung(conn, schluessel, json.dumps({"zeit": db.jetzt(), "treffer": gefunden}, ensure_ascii=False))
            return gefunden
        quelle("Firmenverzeichnis", verzeichnis)
    if "internet" in auftrag.quellen and auftrag.searxng.strip():
        def internet():
            passend, hinweis = nur_im_gebiet(conn, quellen.internet(begriffe, auftrag.ort, auftrag.land, auftrag.searxng, client), auftrag, melde, client)
            if hinweis:
                hinweise.append(hinweis)
            return passend
        quelle("Internet", internet)
    if not begriffe and {"jobboerse", "arbeitnow", "portale", "adzuna", "jooble"} & set(auftrag.quellen):
        hinweise.append("Stellenbörsen übersprungen: Es fehlen noch Berufe. „Passende Firmen finden“ leitet sie aus deinen Fakten ab.")
    elif "jobboerse" in auftrag.quellen:
        if auftrag.land != "de":
            hinweise.append("Jobbörse: Sie deckt nur Deutschland ab.")
        else:
            # Die Jobbörse kennt keine Regionen, 200 km ist ihr größter Umkreis.
            quelle("Jobbörse", lambda: suche(auftrag.begriffe, auftrag.ort or "Deutschland", auftrag.umkreis or 200, client, auftrag.arbeitszeit))
    if begriffe and "arbeitnow" in auftrag.quellen:
        quelle("Arbeitnow", lambda: quellen.stellenportal(begriffe, auftrag.ort, client, auftrag.arbeitszeit))

    def gefiltert(name, liste):
        bleiben, hinweis = filtere_stellen(conn, liste, auftrag, begriffe, name, melde, client)
        if hinweis:
            hinweise.append(hinweis)
        return bleiben

    if "postfach" in auftrag.quellen:
        def alerts():
            from . import postfach  # Erst hier: Das Modul braucht den Schlüsselbund.
            stellen, ausfaelle = postfach.lies_alerts(conn, melde, imap_klasse)
            hinweise.extend(ausfaelle)
            return gefiltert("Job-Alerts", stellen)
        quelle("Job-Alerts", alerts)
    if "portale" in auftrag.quellen:
        if not auftrag.searxng.strip():
            hinweise.append("Portale: Dafür braucht es die SearXNG-Adresse.")
        elif begriffe:
            def portale():
                stellen, ausfaelle = quellen.portale(begriffe, auftrag.ort, auftrag.land, auftrag.searxng, client)
                hinweise.extend(ausfaelle)
                return gefiltert("Portale", stellen)
            quelle("Portale", portale)
    for name, schluessel, abruf in (("Adzuna", "adzuna", quellen.adzuna), ("Jooble", "jooble", quellen.jooble)):
        if schluessel not in auftrag.quellen or not begriffe:
            continue
        api = quellen.api_schluessel(schluessel)
        if not api:
            hinweise.append(f"{name}: Es ist noch kein Schlüssel hinterlegt.")
            continue
        quelle(name, lambda abruf=abruf, api=api, name=name: gefiltert(
            name, abruf(begriffe, auftrag.ort, auftrag.umkreis, auftrag.land, api, client)))
    if auftrag.arbeitszeit and {"verzeichnis", "internet"} & set(auftrag.quellen):
        hinweise.append(f"Arbeitszeit: Verzeichnis und Internet liefern Firmen, keine Stellen. Dort gilt „{ARBEITSZEIT[auftrag.arbeitszeit]}“ nicht.")
    # Konzerne bleiben in der Liste, der Mittelstand steht aber vorn.
    return {"treffer": bewerte_naehe(conn, _vereine(listen)), "hinweise": hinweise, "branche": branche}


def finde_passende(conn: sqlite3.Connection, modell: str, auftrag: Auftrag | None = None, melde=lambda _: None,
                   chat=llm.chat, client=None) -> dict:
    """Der ganze Weg in einem Schritt: Fakten, Berufe, Branche, Ort, alle gewählten Quellen, Sortierung."""
    auftrag = auftrag or Auftrag()
    melde("Lese deine Fakten, das kann einige Minuten dauern")
    begriffe = leite_begriffe_ab(conn, modell, chat)
    auftrag.begriffe = ", ".join(begriffe)
    auftrag.ort = auftrag.ort.strip() or suchort(conn)
    if not auftrag.ort and not auftrag.umkreis == 0:
        raise SucheFehler("Ich weiß nicht, wo du suchst. Trage den Ort ein oder wähle „Ganze Region“ für das ganze Land.")
    ergebnis = suche_ueberall(conn, auftrag, melde, client)
    return {**ergebnis, "begriffe": begriffe, "ort": auftrag.ort}


def bekannte_firmen(conn: sqlite3.Connection) -> set[str]:
    return {regeln.kurzname(z[0]).lower() for z in conn.execute("SELECT name FROM firmen")}


def bewerte_naehe(conn: sqlite3.Connection, treffer: list[dict]) -> list[dict]:
    """Sortiert nach der Nähe zu deinen Fakten, schnell und lokal über Embeddings. Das ersetzt keine Bewertung."""
    if not treffer:
        return []
    bekannt = bekannte_firmen(conn)
    vektoren = rag.embedde([f"{t['firma']} {t['titel']} {t['beruf']}" for t in treffer])
    ergebnis = []
    for stelle, vektor in zip(treffer, vektoren):
        zeile = conn.execute(
            "SELECT min(vec_distance_cosine(c.embedding, ?)) FROM chunks c JOIN fakten f ON f.id = c.fakt_id "
            "WHERE c.art = 'fakt' AND f.status = 'bestaetigt'", (vektor,)).fetchone()
        naehe = None if zeile[0] is None else max(0, round(100 * (1 - zeile[0])))
        ergebnis.append({**stelle, "naehe": naehe, "bekannt": regeln.kurzname(stelle["firma"]).lower() in bekannt})
    return sorted(ergebnis, key=lambda s: (s["bekannt"], bool(s.get("konzern")), -(s["naehe"] or 0)))


def anzeigentext(stelle: dict, client=None) -> str:
    """Volltext der Stellenanzeige. Arbeitnow liefert ihn mit der Liste, die Jobbörse auf Nachfrage. Ein Ausfall ist kein Fehler."""
    referenz = str(stelle.get("referenz", ""))
    if stelle.get("beschreibung"):
        return firmen.text_aus_html(html.unescape(stelle["beschreibung"]))[:MAX_ANZEIGE]
    # Nur Jobbörsen-Nummern haben keinen Doppelpunkt. Alerts, Portale und Aggregatoren liefern höchstens einen Schnipsel.
    if not referenz or ":" in referenz:
        return ""
    client = client or httpx.Client(timeout=20, headers={"User-Agent": config.USER_AGENT})
    try:
        antwort = client.get(DETAILS.format(base64.b64encode(referenz.encode()).decode()), headers={"X-API-Key": API_SCHLUESSEL})
        if antwort.status_code != 200:
            return ""
        return str(antwort.json().get("stellenangebotsBeschreibung") or "")[:MAX_ANZEIGE]
    except (httpx.HTTPError, ValueError):
        return ""


def uebernimm(conn: sqlite3.Connection, stelle: dict, client=None) -> int:
    """Legt die Firma an. Bei einer Stellenanzeige bleibt deren Text für Bewertung, Entwurf und Ansprechperson erhalten."""
    if str(stelle.get("referenz", "")).startswith(("osm:", "web:")):
        return firmen.lege_an(conn, stelle["firma"], website=stelle.get("website", ""), sitz=stelle.get("ort", ""),
                              warum=stelle.get("titel", ""), quelle=stelle.get("quelle", "Suche"))
    firma_id = firmen.lege_an(conn, stelle["firma"], website=stelle.get("website", ""), sitz=stelle.get("ort", ""),
                              warum=f"Stelle: {stelle['titel']}", quelle=f"{stelle.get('quelle', 'Jobbörse')} {stelle['referenz']}")
    text = f"Stellenangebot: {stelle['titel']}. Beruf: {stelle.get('beruf', '')}. Ort: {stelle.get('ort', '')}."
    voll = anzeigentext(stelle, client)
    stuecke = [text] + (rag.zerlege(voll) if voll else [])
    referenz = str(stelle.get("referenz", ""))
    # Die Jobbörse nennt manchmal statt ihrer Seite die Bewerbungsseite der Firma. Ihr Text bleibt trotzdem die Jobbörsen-Anzeige.
    adresse = stelle["url"] if referenz.startswith("arbeitnow:") or ":" in referenz else ANZEIGE.format(referenz)
    rag.speichere(conn, "firma", stuecke, firma_id=firma_id, quellen=[adresse] * len(stuecke), abruf=db.jetzt())
    if stelle["url"] != adresse and not quellen.ist_portal(stelle["url"]):
        client = client or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": config.USER_AGENT})
        speichere_stellenseite(conn, firma_id, stelle["url"], GESCHLECHT.sub("", stelle["titel"].split(" · ")[0]).strip(),
                               lambda u: not quellen.ist_portal(u), client)
    return firma_id


def lade_anzeige_nach(conn: sqlite3.Connection, firma_id: int, client=None) -> bool:
    """Firmen aus älteren Versionen haben von ihrer Jobbörsen-Anzeige nur den Titel."""
    firma = conn.execute("SELECT quelle FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    treffer = re.match(r"Jobbörse (\S+)$", firma["quelle"] or "")
    if not treffer:
        return False
    url = ANZEIGE.format(treffer.group(1))
    if conn.execute("SELECT count(*) FROM chunks WHERE firma_id = ? AND quelle = ? AND text NOT LIKE 'Stellenangebot:%'",
                    (firma_id, url)).fetchone()[0]:
        return False
    voll = anzeigentext({"referenz": treffer.group(1)}, client)
    if not voll:
        return False
    stuecke = rag.zerlege(voll)
    rag.speichere(conn, "firma", stuecke, firma_id=firma_id, quellen=[url] * len(stuecke), abruf=db.jetzt())
    return True


MAX_ANZEIGEN_LADEN = 3


def anzeigen_der_firma(name: str, client=None) -> tuple[list[str], int]:
    """Laufende Anzeigen der Firma bei der Jobbörse: (Referenzen, Anzahl). Der Arbeitgeberfilter der Schnittstelle verlangt den
    exakten Namen, darum sucht die App den Kurznamen im Volltext und prüft den Arbeitgeber selbst."""
    kurz = regeln.kurzname(name)
    if len(kurz) < 3:
        return [], 0
    client = client or httpx.Client(timeout=30, headers={"User-Agent": config.USER_AGENT})
    try:
        antwort = client.get(ANZEIGEN_SUCHE, headers={"X-API-Key": API_SCHLUESSEL},
                             params={"was": kurz, "size": 50, "page": 1, "angebotsart": 1, "pav": "false"})
        if antwort.status_code != 200:
            return [], 0
        liste = antwort.json().get("ergebnisliste", [])
    except (httpx.HTTPError, ValueError):
        return [], 0
    passend = [s["referenznummer"] for s in liste if s.get("referenznummer") and quellen.name_passt(name, str(s.get("firma", "")))]
    return passend, len(passend)


def suche_anzeigen(conn: sqlite3.Connection, firma_id: int, client=None) -> int:
    """Lädt bis zu drei laufende Anzeigen der Firma. Dort steht oft die Ansprechperson, die auf der Website fehlt."""
    firma = conn.execute("SELECT name FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    referenzen, anzahl = anzeigen_der_firma(firma["name"], client)
    for referenz in referenzen[:MAX_ANZEIGEN_LADEN]:
        url = ANZEIGE.format(referenz)
        if conn.execute("SELECT 1 FROM chunks WHERE firma_id = ? AND quelle = ?", (firma_id, url)).fetchone():
            continue
        voll = anzeigentext({"referenz": referenz}, client)
        if voll:
            stuecke = rag.zerlege(voll)
            rag.speichere(conn, "firma", stuecke, firma_id=firma_id, quellen=[url] * len(stuecke), abruf=db.jetzt())
    if referenzen:
        db.protokolliere(conn, "Anzeigen der Firma gefunden", f"{anzahl} bei der Jobbörse", firma_id)
    return anzahl


def _name_zur_mail(mail: str, text: str) -> str:
    """Den Namen gibt es nur, wenn Vor- und Nachname aus der Adresse auch im Text stehen. Geraten wird nichts."""
    teile = [t for t in re.split(r"[._-]", mail.split("@")[0]) if len(t) >= 2 and t.isalpha()]
    if len(teile) < 2:
        return ""
    treffer = re.search(rf"(?<!\w){re.escape(teile[0])}\s+(?:\w+\s+)?{re.escape(teile[-1])}(?!\w)", text, re.I)
    return treffer.group(0) if treffer else ""


def kontakt_aus_anzeige(conn: sqlite3.Connection, firma_id: int) -> bool:
    """Die Adresse aus der Anzeige geht vor info@ der Website, auch ohne Namen, aber nicht vor der persönlichen Adresse
    der Geschäftsführung."""
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    if firma["email"] and firmen.ist_persoenlich(firma["email"], firma["personen"]):
        return False
    # Je Anzeige der ganze Text: Die Adresse aus dem Link steht am Ende, der Name oft in einem früheren Stück.
    seiten: dict[str, list[str]] = {}
    for text, quelle in conn.execute("SELECT text, quelle FROM chunks WHERE art = 'firma' AND firma_id = ? AND "
                                     f"{ki.ANZEIGE_QUELLE} ORDER BY id", (firma_id,)):
        seiten.setdefault(quelle, []).append(text)
    stuecke = ["\n".join(teile) for teile in seiten.values()]
    kandidaten = [(mail, _name_zur_mail(mail, stueck)) for stueck in stuecke for mail in firmen._mails(stueck)
                  if not firmen.NEBENADRESSE.match(mail)]
    # Eine Adresse mit Namen ist die Ansprechperson, recruiting@ im Fuß der Stellenseite nur der Sammelkasten.
    for mail, name in sorted(kandidaten, key=lambda k: not k[1]):
        # Wer sich auf diese Anzeige bewirbt, schreibt an die Adresse aus der Anzeige, nicht an info@ der Website.
        if firma["email"].lower() == mail.lower():
            return False
        personen = ", ".join(dict.fromkeys(p for p in [name, *(x.strip() for x in firma["personen"].split(","))] if p))
        firmen.aendere(conn, firma_id, email=mail, personen=personen)
        db.protokolliere(conn, "Kontakt aus Stellenanzeige", mail, firma_id)
        return True
    return False


GESCHLECHT = re.compile(r"\s*\((?:[mwdfix]\s*[/|,]?\s*){2,4}\)|\s*[mwdfix](?:/[mwdfix]){1,3}\b", re.I)


def stellentitel(firma: sqlite3.Row) -> str:
    """Titel der übernommenen Stelle ohne Arbeitszeit und ohne (m/w/d)."""
    warum = firma["warum"] or ""
    if not warum.startswith("Stelle: "):
        return ""
    return GESCHLECHT.sub("", warum.removeprefix("Stelle: ").split(" · ")[0]).strip()


def speichere_stellenseite(conn: sqlite3.Connection, firma_id: int, url: str, titel: str, erlaubt, client) -> bool:
    """Lädt eine Stellenseite und merkt sie als Anzeige, wenn sie nach Weiterleitungen noch erlaubt ist und Titel und Firma nennt."""
    firma = conn.execute("SELECT name FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    try:
        treffer = firmen._hole(client, url)
    except httpx.HTTPError:
        return False
    if not treffer or not erlaubt(treffer[0]):
        return False
    text = firmen.text_aus_html(treffer[1])
    if (titel and titel.lower() not in GESCHLECHT.sub("", text).lower()) or not quellen.name_passt(firma["name"], text):
        return False
    adresse = treffer[0].split("#")[0] + firmen.STELLENSEITE
    conn.execute("DELETE FROM chunks WHERE firma_id = ? AND quelle = ?", (firma_id, adresse))
    stuecke = rag.zerlege(text[:MAX_ANZEIGE])
    rag.speichere(conn, "firma", stuecke, firma_id=firma_id, quellen=[adresse] * len(stuecke), abruf=db.jetzt())
    db.protokolliere(conn, "Stellenseite der Firma geladen", treffer[0], firma_id)
    return True


def lade_stellenseite(conn: sqlite3.Connection, firma_id: int, searxng: str, client=None) -> bool:
    """Sucht die Anzeige auf den eigenen Seiten der Firma, dort steht oft die Ansprechperson mit Adresse. Viele Karriereseiten
    laden ihre Stellenliste per JavaScript, darum fragt die App die eigene SearXNG mit site:. Gültig ist nur eine Seite auf der
    Domain der Firma, die Titel und Firma nennt."""
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    titel = stellentitel(firma)
    if not titel or not firma["website"] or not searxng.strip():
        return False
    if conn.execute("SELECT 1 FROM chunks WHERE firma_id = ? AND quelle LIKE ?", (firma_id, f"%{firmen.STELLENSEITE}")).fetchone():
        return False
    domain = firmen._host(firma["website"])
    hosts = [domain] + [h for h in dict.fromkeys(firmen._host(z[0]) for z in conn.execute(
        "SELECT DISTINCT quelle FROM chunks WHERE firma_id = ? AND art = 'firma'", (firma_id,))) if h != domain and firmen._schwester(f"https://{h}/", domain)]
    client = client or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": config.USER_AGENT})
    klein = titel.lower()
    for host in reversed(hosts):  # Die Karriere-Domain zuerst.
        try:
            antwort = client.get(f"{searxng.strip().rstrip('/')}/search", params={"q": f"site:{host} {titel}", "format": "json"})
            ergebnisse = antwort.json().get("results", []) if antwort.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        for ergebnis in ergebnisse[:5]:
            url = ergebnis.get("url", "").split("#")[0]
            if firmen._host(url) in hosts and klein in GESCHLECHT.sub("", ergebnis.get("title", "")).lower() \
                    and speichere_stellenseite(conn, firma_id, url, titel, lambda u: firmen._host(u) in hosts, client):
                return True
    return False


FREEMAIL = re.compile(r"(?:^|\.)(?:gmail|googlemail|web|gmx|t-online|outlook|hotmail|live|yahoo|icloud|me|posteo|freenet|"
                      r"aol|mail|arcor|online|email|protonmail|proton)\.[a-z.]+$", re.I)


def domains_aus_anzeige(conn: sqlite3.Connection, firma_id: int) -> list[str]:
    """Die Adresse in der Anzeige verrät meist die eigene Domain: jobs@beispielnetz.de gehört zu beispielnetz.de."""
    text = "\n".join(z[0] for z in conn.execute(
        f"SELECT text FROM chunks WHERE art = 'firma' AND firma_id = ? AND {ki.ANZEIGE_QUELLE}", (firma_id,)))
    domains = [m.rpartition("@")[2].lower() for m in firmen._mails(text)]
    return [d for d in dict.fromkeys(domains) if not FREEMAIL.search(d) and not quellen.ist_portal(f"https://{d}/")]


def finde_website(firma: str, ort: str, land: str, searxng: str, client, domains: list[str] | None = None) -> tuple[str, bool]:
    """Liefert die Website und ob sie nur über den Domain-Test gefunden wurde, also besonders zu prüfen ist.
    Domains aus der Stellenanzeige gehen vor, sie stammen von der Firma selbst."""
    if domains:
        netz = client or httpx.Client(follow_redirects=True, timeout=10, headers={"User-Agent": config.USER_AGENT})
        website = firmen.teste_domains(firma, [f"https://{d}/" for d in domains] + [f"https://www.{d}/" for d in domains],
                                       netz, quellen.name_passt)
        if website:
            return website, False
    website = quellen.website_aus_verzeichnis(firma, ort, land, client)
    if not website and searxng:
        website = quellen.website_aus_internet(firma, ort, land, searxng, client)
    if website:
        return website, False
    client = client or httpx.Client(follow_redirects=True, timeout=10, headers={"User-Agent": config.USER_AGENT})
    website = firmen.teste_domains(firma, quellen.domain_kandidaten(firma), client, quellen.name_passt)
    return website, bool(website)


def website_gehoert_firma(conn: sqlite3.Connection, firma_id: int) -> bool:
    """Eine gesuchte Website muss die Firma im Impressum nennen, sonst ist es oft ein Verzeichnis, das über sie schreibt.
    Ohne lesbares Impressum zählt der Text aller geladenen Seiten."""
    firma = conn.execute("SELECT name, website FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    text = firmen.impressum_text(conn, firma_id) or "\n".join(z[0] for z in conn.execute(
        "SELECT text FROM chunks WHERE art = 'firma' AND firma_id = ? AND quelle LIKE ?", (firma_id, f"%{firmen._host(firma['website'])}%")))
    return quellen.name_passt(firma["name"], text)


def verwirf_website(conn: sqlite3.Connection, firma_id: int) -> None:
    """Entfernt die Seiten und die daraus gelesene Adresse. Die Stellenanzeige bleibt."""
    firma = conn.execute("SELECT website, email FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    host = firmen._host(firma["website"])
    conn.execute("DELETE FROM chunks WHERE art = 'firma' AND firma_id = ? AND quelle LIKE ?", (firma_id, f"%{host}%"))
    email = "" if firma["email"].lower().endswith("@" + host) or firma["email"].lower().endswith("." + host) else firma["email"]
    conn.execute("UPDATE firmen SET website = '', email = ?, seiten_geladen = NULL WHERE id = ?", (email, firma_id))
    conn.commit()
    db.protokolliere(conn, "Website verworfen", f"{host}: Firma steht nicht im Impressum", firma_id)


def hole_inhaber(conn: sqlite3.Connection, firma_id: int, land: str = "de", searxng: str = "", client=None) -> dict:
    """Findet nötigenfalls die Website, lädt ihre Seiten und liest Inhaber und veröffentlichte Mailadresse aus."""
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    website, fehler, gesucht = firma["website"], None, False
    if not website:
        lade_anzeige_nach(conn, firma_id, client)  # Die Adresse darin führt oft direkt zur Website.
        website, _ = finde_website(firma["name"], firma["sitz"], land, searxng, client, domains_aus_anzeige(conn, firma_id))
        gesucht = bool(website)
        if website:
            firmen.aendere(conn, firma_id, website=website)
        else:
            fehler = SucheFehler(f"Zu {firma['name']} habe ich keine Website gefunden. Trage sie unter „Firmen“ selbst ein.")
    if website:
        try:
            firmen.lade_seiten(conn, firma_id, client)
        except RuntimeError as ausfall:
            fehler = ausfall
        if gesucht and not fehler and not website_gehoert_firma(conn, firma_id):
            verwirf_website(conn, firma_id)
            fehler = SucheFehler(f"Die gefundene Website {firmen._host(website)} nennt {firma['name']} nicht im Impressum. "
                                 "Ich nutze sie nicht. Trage die richtige unter „Firmen“ selbst ein.")
    # Die Ansprechperson aus der Anzeige hilft auch, wenn die Website fehlt oder ausfällt.
    lade_anzeige_nach(conn, firma_id, client)
    if conn.execute("SELECT seiten_geladen FROM firmen WHERE id = ?", (firma_id,)).fetchone()[0]:
        lade_stellenseite(conn, firma_id, searxng, client)
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    anzeigen = 0
    if not (firma["email"] and firmen.ist_persoenlich(firma["email"], firma["personen"])):
        anzeigen = suche_anzeigen(conn, firma_id, client)
    kontakt_aus_anzeige(conn, firma_id)
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    if fehler and not firma["email"]:
        raise fehler
    konzern = ", ".join(firmen.konzern_merkmale(firma["name"], firmen.impressum_text(conn, firma_id), anzeigen))
    return {"name": firma["name"], "personen": firma["personen"], "email": firma["email"],
            "art": firmen.adressart(firma["email"], firma["personen"]), "website": firma["website"], "konzern": konzern}


def uebernimm_und_finde(conn: sqlite3.Connection, stelle: dict, land: str = "de", searxng: str = "", client=None) -> dict:
    """Die Firma bleibt auch dann in der Liste, wenn ihre Seiten nicht erreichbar sind."""
    firma_id = uebernimm(conn, stelle, client)
    try:
        return hole_inhaber(conn, firma_id, land, searxng, client)
    except (SucheFehler, RuntimeError) as fehler:
        return {"name": stelle["firma"], "personen": "", "email": "", "art": "", "website": "", "konzern": "", "fehler": str(fehler)}


def vorschau(treffer: dict, land: str = "de", searxng: str = "", client=None) -> dict:
    """Zeigt die Firma, ohne sie in die Liste zu übernehmen. Ein Ausfall ist hier kein Fehler, sondern eine Auskunft."""
    client = client or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": config.USER_AGENT})
    ergebnis = {"website": treffer.get("website", ""), "personen": "", "email": "", "art": "", "kurztext": "", "stellen": [], "problem": "",
                "geraten": False, "konzern": treffer.get("konzern", "")}
    if not ergebnis["website"]:
        ergebnis["website"], ergebnis["geraten"] = finde_website(treffer["firma"], treffer.get("ort", ""), land, searxng, client)
    if not ergebnis["website"]:
        ergebnis["problem"] = "Keine Website gefunden."
        return ergebnis
    try:
        seiten = firmen.hole_seiten(ergebnis["website"], client, 4)
    except RuntimeError:
        ergebnis["problem"] = "Die Website ist gerade nicht erreichbar oder leitet auf eine fremde Domain um."
        return ergebnis
    texte = {url: firmen.text_aus_html(html) for url, html in seiten.items()}
    kontakt = firmen.kontakt_aus_texten("\n".join(t for u, t in texte.items() if firmen.IMPRESSUM.search(u)), "\n".join(texte.values()),
                                        firmen._host(ergebnis["website"]))
    impressum = "\n".join(t for u, t in texte.items() if firmen.IMPRESSUM.search(u))
    if not treffer.get("website") and not quellen.name_passt(treffer["firma"], impressum or "\n".join(texte.values())):
        ergebnis.update(website="", problem=f"Gefunden habe ich {firmen._host(ergebnis['website'])}, dort steht {treffer['firma']} "
                                            "aber nicht im Impressum. Die Seite gehört wohl nicht zur Firma.")
        return ergebnis
    merkmale = [m for m in (treffer.get("konzern") or "").split(", ") if m] + firmen.konzern_merkmale(treffer["firma"], impressum)
    ergebnis["konzern"] = ", ".join(dict.fromkeys(merkmale))
    ergebnis.update(kontakt, art=firmen.adressart(kontakt["email"], kontakt["personen"]),
                    kurztext=firmen.kurztext(next(iter(seiten.values()))), stellen=firmen.stellen_aus_text("\n".join(texte.values())))
    return ergebnis


def schlage_mail_vor(conn: sqlite3.Connection, firma_id: int, modell: str, land: str = "de", searxng: str = "",
                     melde=lambda _: None, chat=llm.chat, client=None) -> int:
    """Empfänger und komplette Mail in einem Schritt. Gesendet wird erst nach deiner Freigabe."""
    firma = conn.execute("SELECT seiten_geladen, email FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    if not firma["seiten_geladen"] or not firma["email"]:
        melde("Suche den Inhaber auf den Firmenseiten")
        hole_inhaber(conn, firma_id, land, searxng, client)
    if not conn.execute("SELECT email FROM firmen WHERE id = ?", (firma_id,)).fetchone()[0]:
        raise ki.NichtMoeglich("Auf den Firmenseiten steht keine Mailadresse. Geratene Adressen nutzt die App nicht.")
    melde("Schreibe die Mail, das kann einige Minuten dauern")
    return ki.erstelle_entwurf(conn, firma_id, modell, chat=chat)


def pruefe_eignung(conn: sqlite3.Connection, firma_id: int, modell: str, chat=llm.chat, client=None, melde=lambda _: None) -> dict:
    """Wie `ki.pruefe_eignung`, lädt aber eine fehlende Anzeige vorher nach: aus der eigenen Referenz oder aus den
    laufenden Anzeigen der Firma bei der Jobbörse."""
    if not ki.anzeige_der_firma(conn, firma_id)[0]:
        lade_anzeige_nach(conn, firma_id, client)
    if not ki.anzeige_der_firma(conn, firma_id)[0]:
        suche_anzeigen(conn, firma_id, client)
    return ki.pruefe_eignung(conn, firma_id, modell, chat, melde)


def sofort_bewerben(conn: sqlite3.Connection, treffer: dict, modell: str, land: str = "de", searxng: str = "",
                    melde=lambda _: None, chat=llm.chat, client=None, mit_entwurf: bool = True) -> dict:
    """Übernehmen, Ansprechperson, Eignung und Entwurf in einem Schritt. Gesendet wird nichts, das bleibt dein Klick.
    Bei geringer Eignung entsteht kein Entwurf: Der Modelllauf kostet Minuten und die Mail hätte wenig Aussicht."""
    melde("Übernehme die Firma und suche die Ansprechperson")
    vorhanden = conn.execute("SELECT id FROM firmen WHERE name = ?", (treffer["firma"],)).fetchone()
    if vorhanden:
        try:
            kontakt = hole_inhaber(conn, vorhanden[0], land, searxng, client)
        except (SucheFehler, RuntimeError) as fehler:
            kontakt = {"email": "", "fehler": str(fehler)}
    else:
        kontakt = uebernimm_und_finde(conn, treffer, land, searxng, client)
    firma_id = conn.execute("SELECT id FROM firmen WHERE name = ?", (treffer["firma"],)).fetchone()[0]
    if not kontakt["email"]:
        grund = kontakt.get("fehler") or "Weder Website noch Stellenanzeige nennen eine Adresse."
        raise ki.NichtMoeglich(f"Keine veröffentlichte Mailadresse für {treffer['firma']}. {grund} Geratene Adressen nutzt die App nicht.")
    ergebnis = {"firma_id": firma_id, "kontakt": kontakt, "eignung": None, "entwurf_id": None, "hinweis": "", "anhaenge": []}
    melde("Prüfe deine Eignung anhand von Lebenslauf und Zeugnis, das kann einige Minuten dauern")
    try:
        ergebnis["eignung"] = pruefe_eignung(conn, firma_id, modell, chat, client, melde)
    except ki.NichtMoeglich as grund:
        ergebnis["hinweis"] = f"Eignung nicht geprüft: {grund}"
    if not mit_entwurf:
        return ergebnis  # Die Oberfläche fragt erst Offenes nach und ruft dann `schreibe_bewerbung`.
    ergebnis.update(schreibe_bewerbung(conn, firma_id, modell, ergebnis["eignung"], melde, chat))
    return ergebnis


def schreibe_bewerbung(conn: sqlite3.Connection, firma_id: int, modell: str, eignung: dict | None,
                       melde=lambda _: None, chat=llm.chat) -> dict:
    """Zweiter Teil von „Sofort bewerben“: die Mail samt Lebenslauf für die Stelle, außer die Eignung ist zu gering."""
    if eignung and eignung["score"] < ki.MIN_EIGNUNG:
        return {"entwurf_id": None, "anhaenge": [], "eignung": eignung,
                "hinweis": (f"Eignung {eignung['score']} %, unter {ki.MIN_EIGNUNG} % schreibe ich keine Mail. "
                            "Unter „Firmen“ kannst du sie trotzdem vorschlagen lassen.")}
    melde("Schreibe die Mail, das kann einige Minuten dauern")
    entwurf_id = ki.erstelle_entwurf(conn, firma_id, modell, chat=chat)
    return {"entwurf_id": entwurf_id, "anhaenge": [p.name for p in profil.anhaenge_des_entwurfs(conn, entwurf_id)],
            "eignung": eignung}
