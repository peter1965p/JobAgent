"""Job-Alerts aus dem eigenen Postfach. LinkedIn, Indeed, StepStone und andere Portale schicken passende Stellen per Mail.

Die App liest diese Mails nur. Sie öffnet den Posteingang schreibgeschützt (EXAMINE) und holt die Mails mit BODY.PEEK,
dadurch bleiben ungelesene Mails ungelesen. Links aus den Mails ruft die App nie auf: Das sind Klick-Zähler der
Portale, und ihre Seiten sperren Bots. Öffnen kann sie nur die Person selbst im Browser.
"""

import email
import email.policy
import imaplib
import json
import re
import socket
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from hashlib import sha1
from urllib.parse import parse_qs, urlsplit

import keyring
from bs4 import BeautifulSoup, NavigableString
from keyring.errors import KeyringError

from . import db, mailkonten, versand
from .quellen import SucheFehler

TAGE = 14  # Ältere Alerts sind meist schon vergeben oder ausgelaufen.
MAX_MAILS = 300  # Beim ersten Abruf nur die jüngsten, danach kommen nur neue dazu.
STUECK = 25  # Mails je FETCH-Befehl
MONATE = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # IMAP verlangt Englisch


@dataclass(frozen=True)
class Portal:
    name: str
    absender: str  # Domain für die IMAP-Suche
    link: re.Pattern


PORTALE = (
    Portal("Indeed", "indeed.com", re.compile(r"indeed\.com/(?:pagead|rc)/clk|cts\.indeed\.com/|indeed\.com/viewjob")),
    Portal("Indeed", "indeedemail.com", re.compile(r"indeed\.com/(?:pagead|rc)/clk|cts\.indeed\.com/|indeed\.com/viewjob")),
    Portal("LinkedIn", "linkedin.com", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/")),
    Portal("StepStone", "stepstone.de", re.compile(r"click\.stepstone\.de/|stepstone\.de/stellenangebote--")),
    Portal("Jobs für Niedersachsen", "jobsfuerniedersachsen.de",
           re.compile(r"mandrillapp\.com/track/click/\d+/www\.jobsfuerniedersachsen\.de|jobsfuerniedersachsen\.de/(?:stellenanzeige|job/)")),
    Portal("XING", "xing.com", re.compile(r"xing\.com/(?:m/)?jobs/[\w%-]+-\d+")),
    Portal("Interamt", "interamt.de", re.compile(r"interamt\.de/koop/app/stelle")),
    Portal("Karriereportal Niedersachsen", "niedersachsen.de", re.compile(r"karriere\.niedersachsen\.de/.*(?:stelle|job)", re.I)),
    Portal("Jobbörse", "arbeitsagentur.de", re.compile(r"arbeitsagentur\.de/jobsuche/jobdetail/")),
    Portal("heise jobs", "heise.de", re.compile(r"jobs\.heise\.de/")),
)

# Navigation, Knöpfe und Fußzeilen. Solche Links führen zwar oft zur selben Adresse, sind aber keine Stelle.
NAVIGATION = re.compile(
    r"(?i)^(?:job anzeigen|jobs? ansehen|zum job|passt nicht|mehr erfahren|mehr|more|nein|ja|anmelden|jobs finden|weitere jobs.*|"
    r"alle (?:jobs )?anzeigen|abbestellen|abmelden|profil bearbeiten|hilfebereich|hilfe|datenschutz.*|nutzungsbedingungen|impressum|"
    r"diese e-mails pausieren|upload cv|einfach bewerben|jetzt bewerben|bewerben|schnellbewerbung|job merken|speichern|details|"
    r"apply now|view job|see more jobs|indeed|linkedin|stepstone|xing|jobs in .*|\d+ neue jobs.*)$")
# Zeilen ohne Aussage über Firma oder Ort
RAUSCHEN = re.compile(
    r"(?i)^(?:\||vor \d+ \w+|\d+ (?:day|days|hour|hours) ago|heute|gestern|neu|new|aktiver arbeitgeber|employees|mitarbeitende|"
    r"\d[\d.\s-]*|.*€.*|.*\bapplicants?\b.*|.*bewerbungen?\b.*|feste anstellung|vollzeit.*|teilzeit.*|befristet.*|"
    r"top-?job|gesponsert|sponsored|promoted|anzeige)$")
UNSICHTBAR = re.compile(r"[​-‏⁠﻿­]")
DATUM = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}$")


# --- Mails auswerten ---------------------------------------------------------------------------------------------

def portal_der_mail(absender: str) -> Portal | None:
    domain = parseaddr(absender)[1].rpartition("@")[2].lower()
    return next((p for p in PORTALE if domain == p.absender or domain.endswith("." + p.absender)), None)


def _zeile(text: str) -> str:
    return " ".join(UNSICHTBAR.sub("", text).split())


def _blöcke(soup: BeautifulSoup, portal: Portal) -> list[tuple[str, list[str]]]:
    """Die Mail als Folge von Stellen: Link zur Stelle plus die Textzeilen bis zur nächsten Stelle."""
    bloecke: list[tuple[str, list[str]]] = []
    letzter_link = None
    for text in soup.find_all(string=True):
        if not isinstance(text, NavigableString) or text.parent.name in ("style", "script", "head", "title"):
            continue
        zeile = _zeile(str(text))
        if not zeile:
            continue
        anker = text.find_parent("a", href=True)
        ist_stelle = anker is not None and portal.link.search(anker["href"]) and not NAVIGATION.match(_zeile(anker.get_text(" ")))
        if ist_stelle and anker is not letzter_link:
            letzter_link = anker
            bloecke.append((anker["href"], [zeile]))
        elif bloecke and not NAVIGATION.match(zeile):
            bloecke[-1][1].append(zeile)
    return bloecke


def _ort(text: str) -> str:
    """„| Bremen | 08.11.2026“ und „Bremen“ werden beide zu „Bremen“."""
    teile = [t.strip() for t in text.split("|") if t.strip() and not DATUM.match(t.strip())]
    return teile[0] if teile else ""


def _url(href: str, portal: Portal) -> tuple[str, str]:
    """(Adresse zum Öffnen, Kennung). Wo die Kennung im Link steht, fallen die Zählerparameter weg."""
    treffer = re.search(r"linkedin\.com/(?:comm/)?jobs/view/[\w%-]*?(\d{6,})", href)
    if treffer:
        return f"https://www.linkedin.com/jobs/view/{treffer.group(1)}/", f"linkedin:{treffer.group(1)}"
    jk = parse_qs(urlsplit(href).query).get("jk")
    if "indeed." in href and jk:
        return f"https://de.indeed.com/viewjob?jk={jk[0]}", f"indeed:{jk[0]}"
    treffer = re.search(r"arbeitsagentur\.de/jobsuche/jobdetail/([\w-]+)", href)
    if treffer:
        return f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{treffer.group(1)}", treffer.group(1)
    return href, f"alert:{sha1(href.encode()).hexdigest()[:16]}"


def stellen_aus_html(html: str, portal: Portal) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    stellen, gesehen = [], set()
    for href, zeilen in _blöcke(soup, portal):
        titel, rest = zeilen[0], [z for z in zeilen[1:] if not RAUSCHEN.match(z)]
        if len(titel) < 4 or not rest:
            continue
        if " · " in rest[0]:  # LinkedIn: „Firma · Ort“
            firma, ort = (t.strip() for t in rest[0].split(" · ", 1))
        else:
            firma, ort = rest[0], _ort(rest[1]) if len(rest) > 1 else ""
        if len(firma) < 2 or NAVIGATION.match(firma) or (firma.lower(), titel.lower()) in gesehen:
            continue
        gesehen.add((firma.lower(), titel.lower()))
        url, kennung = _url(href, portal)
        stellen.append({
            "referenz": kennung, "firma": firma[:120], "titel": titel[:200], "beruf": "", "ort": ort[:80], "entfernung": None,
            "url": url, "website": "", "quelle": portal.name if portal.name == "Jobbörse" else f"{portal.name} (Alert)",
            "beschreibung": "",
        })
    return stellen


def stellen_aus_mail(roh: bytes) -> list[dict]:
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
    return [{**s, "datum": datum} for s in stellen_aus_html(teil.get_content(), portal)]


# --- Postfächer ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Postfach:
    host: str
    port: int
    benutzer: str
    oauth: bool = False  # Bei OAuth-Konten (Gmail in Thunderbird) braucht es ein App-Passwort.
    sicherheit: str = "ssl"  # "ssl" oder "starttls". Ohne Verschlüsselung meldet sich die App nicht an.

    @property
    def name(self) -> str:
        return f"{self.benutzer} auf {self.host}"


def vorschlaege(conn: sqlite3.Connection, home=None) -> list[Postfach]:
    """Posteingänge aus Thunderbird, dazu der zum Versandkonto passende Server."""
    gefunden = [Postfach(k.host, k.port, k.benutzer, k.oauth, k.sicherheit) for k in mailkonten.thunderbird_eingang(home)]
    konto = versand.lade_konto(conn)
    if konto and not any(p.benutzer == konto.benutzer for p in gefunden):
        host = re.sub(r"^(?:smtps?|mail\.out|out|mx)\.", "imap.", konto.host) if konto.host.split(".")[0] != "imap" else konto.host
        gefunden.append(Postfach(host if host != konto.host else "imap." + konto.host.split(".", 1)[-1], 993, konto.benutzer))
    return list(dict.fromkeys(gefunden))


def gewaehlte(conn: sqlite3.Connection) -> list[Postfach]:
    try:
        return [Postfach(**p) for p in json.loads(db.einstellung(conn, "postfach.konten", "[]"))]
    except (TypeError, ValueError):
        return []


def waehle(conn: sqlite3.Connection, postfaecher: list[Postfach]) -> None:
    db.setze_einstellung(conn, "postfach.konten", json.dumps([p.__dict__ for p in postfaecher]))
    db.protokolliere(conn, "Postfächer für Job-Alerts", ", ".join(p.name for p in postfaecher) or "keins")


def _schluessel(p: Postfach) -> str:
    return f"imap:{p.benutzer}@{p.host}"


def passwort(conn: sqlite3.Connection, p: Postfach) -> str:
    """Eigenes Passwort im Schlüsselbund, sonst das des Versandkontos, wenn es derselbe Benutzer ist."""
    try:
        eigenes = keyring.get_password(versand.DIENST, _schluessel(p))
    except KeyringError:
        eigenes = None
    if eigenes:
        return eigenes
    konto = versand.lade_konto(conn)
    if konto and konto.benutzer == p.benutzer:
        return versand.gespeichertes_passwort(konto) or ""
    return ""


def merke_passwort(p: Postfach, wert: str) -> None:
    try:
        keyring.set_password(versand.DIENST, _schluessel(p), wert)
    except KeyringError as fehler:
        raise SucheFehler(f"Der Schlüsselbund ist nicht verfügbar: {fehler}") from fehler


def _seit(tage: int) -> str:
    tag = date.today() - timedelta(days=tage)
    return f"{tag.day:02d}-{MONATE[tag.month - 1]}-{tag.year}"


def _verbinde(p: Postfach, imap_klasse):
    """`imap_klasse` ersetzt in Tests den Server. Sonst SSL oder STARTTLS, unverschlüsselt nie: Das Passwort ginge im Klartext."""
    if imap_klasse is not None:
        return imap_klasse(p.host, p.port)
    kontext = ssl.create_default_context()
    if p.sicherheit == "ssl":
        return imaplib.IMAP4_SSL(p.host, p.port, ssl_context=kontext, timeout=30)
    if p.sicherheit == "starttls":
        verbindung = imaplib.IMAP4(p.host, p.port, timeout=30)
        verbindung.starttls(kontext)
        return verbindung
    raise SucheFehler(f"Postfach {p.name}: Der Server ist ohne Verschlüsselung eingetragen, dort meldet sich die App nicht an.")


def _hole(p: Postfach, kennwort: str, uidvalidity: str, letzte_uid: int, imap_klasse) -> tuple[str, list[tuple[int, bytes]]]:
    try:
        verbindung = _verbinde(p, imap_klasse)
    except (OSError, socket.timeout, imaplib.IMAP4.error) as fehler:
        raise SucheFehler(f"Postfach {p.host}:{p.port} ist nicht erreichbar: {fehler}") from fehler
    try:
        try:
            verbindung.login(p.benutzer, kennwort)
        except imaplib.IMAP4.error as fehler:
            hinweis = " Für dieses Konto braucht es ein App-Passwort des Anbieters." if p.oauth or "gmail" in p.host else ""
            raise SucheFehler(f"Postfach {p.name}: Anmeldung abgelehnt.{hinweis}") from fehler
        typ, _ = verbindung.select("INBOX", readonly=True)
        if typ != "OK":
            raise SucheFehler(f"Postfach {p.name}: Der Posteingang lässt sich nicht öffnen.")
        neu_validity = str((verbindung.response("UIDVALIDITY")[1] or [b""])[0] or b"", "ascii", "replace")
        if neu_validity != uidvalidity:
            letzte_uid = 0  # Der Server hat die Nummern neu vergeben, gemerkte gelten nicht mehr.
        uids: set[int] = set()
        for portal in {q.absender for q in PORTALE}:
            typ, daten = verbindung.uid("SEARCH", None, "SINCE", _seit(TAGE), "FROM", f'"{portal}"')
            if typ == "OK" and daten and daten[0]:
                uids.update(int(u) for u in daten[0].split())
        offen = sorted(u for u in uids if u > letzte_uid)[-MAX_MAILS:]
        mails = []
        for i in range(0, len(offen), STUECK):
            typ, daten = verbindung.uid("FETCH", ",".join(map(str, offen[i:i + STUECK])), "(BODY.PEEK[])")
            for teil in daten if typ == "OK" else []:
                if isinstance(teil, tuple):
                    uid = re.search(rb"UID (\d+)", teil[0])
                    if uid:
                        mails.append((int(uid.group(1)), teil[1]))
        return neu_validity, mails
    except (imaplib.IMAP4.error, OSError) as fehler:
        raise SucheFehler(f"Postfach {p.name}: {fehler}") from fehler
    finally:
        try:
            verbindung.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def lies_alerts(conn: sqlite3.Connection, melde=lambda _: None, imap_klasse=None) -> tuple[list[dict], list[str]]:
    """Stellen der letzten Tage aus allen gewählten Postfächern und Hinweise zu Ausfällen. Schon gelesene Mails liegen
    ausgewertet in der Datenbank, abgerufen werden nur neue."""
    stellen, hinweise = [], []
    grenze = (date.today() - timedelta(days=TAGE)).isoformat()
    postfaecher = gewaehlte(conn)
    if not postfaecher:
        return [], ["Job-Alerts: Noch kein Postfach gewählt."]
    for p in postfaecher:
        kennwort = passwort(conn, p)
        if not kennwort:
            hinweise.append(f"Job-Alerts: Für {p.name} ist kein Passwort gespeichert.")
            continue
        schluessel = f"cache.postfach.{p.benutzer}@{p.host}"
        gemerkt = json.loads(db.einstellung(conn, schluessel, "") or "{}")
        melde(f"Lese Job-Alerts aus {p.name}")
        try:
            validity, mails = _hole(p, kennwort, gemerkt.get("uidvalidity", ""), int(gemerkt.get("letzte_uid", 0)), imap_klasse)
        except SucheFehler as fehler:
            hinweise.append(f"Job-Alerts: {fehler}")
            stellen += [s for s in gemerkt.get("stellen", []) if s["datum"] >= grenze]
            continue
        alt = gemerkt.get("stellen", []) if validity == gemerkt.get("uidvalidity") else []
        neu = [s for _, roh in mails for s in stellen_aus_mail(roh)]
        behalten = [s for s in alt + neu if s["datum"] >= grenze]
        letzte = max([u for u, _ in mails] + [int(gemerkt.get("letzte_uid", 0)) if alt else 0])
        db.setze_einstellung(conn, schluessel, json.dumps({"uidvalidity": validity, "letzte_uid": letzte, "stellen": behalten,
                                                           "zeit": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False))
        if mails:
            db.protokolliere(conn, "Job-Alerts gelesen", f"{p.name}: {len(mails)} Mails, {len(neu)} Stellen")
        stellen += behalten
    # Dieselbe Stelle kommt oft aus mehreren Alerts, die jüngste zählt.
    eindeutig: dict[tuple[str, str], dict] = {}
    for s in sorted(stellen, key=lambda s: s["datum"]):
        eindeutig[(s["firma"].lower(), s["titel"].lower())] = s
    return list(eindeutig.values()), hinweise
