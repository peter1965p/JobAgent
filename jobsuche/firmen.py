"""Firmenliste und Abruf öffentlicher Firmenseiten."""

import re
import sqlite3
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import config, db, rag

MAX_SEITEN = 6
SEITEN_PAUSE = 0.5  # Kleine Firmen hosten oft schwach: Der Abruf soll niemandem zur Last fallen.
MAX_BYTES = 3_000_000  # Baukasten-Seiten sind oft über 1 MB, Footer mit Adresse und Karriere-Link stehen am Ende.
IMPRESSUM = re.compile(r"impressum|imprint|legal|rechtlich|anbieterkennzeichnung", re.I)
# Manche Seiten bauen ihre Navigation erst per JavaScript auf, das Impressum ist aber trotzdem da.
IMPRESSUM_PFADE = ("/impressum", "/impressum/", "/de/impressum", "/imprint", "/legal-notice", "/impressum.html")
RELEVANT = re.compile(r"impressum|imprint|legal|rechtlich|anbieterkennzeichnung|kontakt|contact|stellen|career|leistung|service|loesung|lösung|kompetenz|ki\b|ai\b|kuenstlich|künstlich"
                      r"|karriere|jobs?\b|ueber|über|about|team|unternehmen", re.I)


def _host(url: str) -> str:
    return urlsplit(url).netloc.removeprefix("www.")


def lege_an(conn: sqlite3.Connection, name: str, personen: str = "", email: str = "", website: str = "",
            sitz: str = "", warum: str = "", quelle: str = "von dir eingetragen") -> int:
    name, email, website = name.strip(), email.strip(), website.strip()
    if not name:
        raise ValueError("Der Firmenname fehlt.")
    if email and "@" not in email:
        raise ValueError("Die Mailadresse ist ungültig.")
    if not website and email:
        website = "https://www." + email.split("@")[-1]
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    cursor = conn.execute(
        "INSERT INTO firmen (name, sitz, personen, email, website, warum, quelle) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        (name, sitz.strip(), personen.strip(), email, website, warum.strip(), quelle),
    )
    conn.commit()
    if not cursor.rowcount:
        raise ValueError(f"{name} steht schon in der Liste.")
    db.protokolliere(conn, "Firma angelegt", name, cursor.lastrowid)
    return cursor.lastrowid


def entferne(conn: sqlite3.Connection, firma_id: int) -> None:
    gesendet = conn.execute(
        "SELECT 1 FROM entwuerfe WHERE firma_id = ? AND status IN ('gesendet', 'antwort', 'absage', 'keine_antwort')",
        (firma_id,),
    ).fetchone()
    if gesendet:
        raise ValueError("An diese Firma ging schon eine Mail. Sie bleibt für die Auswertung erhalten.")
    conn.execute("DELETE FROM firmen WHERE id = ?", (firma_id,))
    conn.commit()


def aendere(conn: sqlite3.Connection, firma_id: int, **werte) -> None:
    """Ändert Kontaktdaten. Ein neues Ziel gilt erst nach erneutem Laden als geprüft."""
    werte = {k: str(v).strip() for k, v in werte.items() if k in ("name", "personen", "email", "website", "sitz", "warum")}
    if "name" in werte and not werte["name"]:
        raise ValueError("Der Firmenname fehlt.")
    if werte.get("email") and "@" not in werte["email"]:
        raise ValueError("Die Mailadresse ist ungültig.")
    if werte.get("website") and not werte["website"].startswith(("http://", "https://")):
        werte["website"] = "https://" + werte["website"]
    for spalte, wert in werte.items():
        conn.execute(f"UPDATE firmen SET {spalte} = ? WHERE id = ?", (wert, firma_id))  # Spalten stammen aus der Liste oben.
    conn.commit()


# Großschreibung trennt hier Namen von Text, deshalb gilt die Groß-/Kleinschreibung nur für die Stichwörter nicht.
NAMENSTEIL = r"[A-ZÄÖÜ][\wäöüßéèáà-]+"
PERSON = rf"((?:(?:Dr|Prof)\.[ \t]+)*{NAMENSTEIL}(?:[ \t]+(?:(?:von|van|de|der|zu|große|grosse)[ \t]+)?{NAMENSTEIL}){{1,2}})"
ANREDE = r"(?:(?:Herr|Frau|Herrn|Dipl\.?[- ]?\w*\.?|Ing\.?|Inf\.?|Kfm\.?|Kffr\.?|M\.?\s?Sc\.?|B\.?\s?Sc\.?)[ \t]+)*"
GF = re.compile(rf"(?i:geschäftsführ\w*|vertreten[ \t]+durch|vertretungsberechtigt\w*|inhaber\w*|vorstand)"
                rf"[ \t]*(?:\([^)\n]*\))?[ \t]*:?\s*(?i:(?:den|die)[ \t]+)?(?i:geschäftsführ\w*(?:[ \t]+gesellschafter\w*)?[ \t]*:?\s*)?{ANREDE}{PERSON}")
MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
WEDER_NOCH_NAME = {"registergericht", "handelsregister", "amtsgericht", "registernummer", "telefon", "mail", "kontakt",
                   "umsatzsteuer", "sitz", "geschäftsführung", "gesellschafter", "impressum", "die", "der", "unsere", "unser", "unserer",
                   "social", "media", "kanäle", "team", "experten", "expert", "expertinnen", "ansprechpartner", "ihre", "ihr", "weitere",
                   "datenschutz", "unternehmen", "firma", "angaben", "inhaltlich", "verantwortlich", "alle", "das", "und", "gmbh", "bank"}
# Solche Adressen erreichen selten die Geschäftsführung. Sie sind nur die letzte Wahl.
NEBENADRESSE = re.compile(r"^(impressum|datenschutz|privacy|abuse|webmaster|noreply|no-reply|dsb|dpo|jugendschutz\w*|compliance|presse|press|investor\w*|ir|support|hotline|service)@", re.I)


# Auf Teamseiten steht die Funktion meist hinter dem Namen.
GF_DAHINTER = re.compile(rf"{PERSON}[ \t]*(?:[,–—|-][ \t]*|\n[ \t]*)(?i:geschäftsführ\w*|inhaber\w*|ceo\b|managing[ \t]+director)")
VERSTECKT = re.compile(r"\s*(?:\(at\)|\[at\]|\{at\}|\(ät\)|\[ät\]|\s+at\s+(?=[\w-]+\s*(?:\.|\(dot\)|\[dot\])\s*[a-z]{2,}\b))\s*", re.I)
PUNKT = re.compile(r"\s*(?:\(dot\)|\[dot\]|\(punkt\)|\[punkt\])\s*", re.I)


def _nachname(personen: str) -> str:
    teile = re.split(r",| und ", personen)[0].split()
    return teile[-1].lower() if teile else ""


def _ascii(text: str) -> str:
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(alt, neu)
    return text


def ist_persoenlich(email: str, personen: str) -> bool:
    nachname = _nachname(personen)
    lokal = email.split("@")[0].lower()
    return bool(nachname) and (nachname in lokal or _ascii(nachname) in lokal)


def adressart(email: str, personen: str) -> str:
    if not email:
        return ""
    return "persönlich" if ist_persoenlich(email, personen) else "allgemein"


def _mails(text: str) -> list[str]:
    """Findet auch Schreibweisen wie „info (at) firma (dot) de“. Das bleibt eine veröffentlichte Adresse."""
    return MAIL.findall(PUNKT.sub(".", VERSTECKT.sub("@", text)))


def lies_kontakt(text: str, domain: str = "") -> dict:
    """Sucht Ansprechperson und Mailadresse in einem Impressum. Beides kann falsch sein und bleibt zu prüfen."""
    ergebnis = {"personen": "", "email": ""}
    for treffer in GF.finditer(text):
        name = re.split(r"[ \t]+(?:Registergericht|Handelsregister|Amtsgericht|Telefon|Tel|E-?Mail|Sitz|Kontakt|Geschäftsführer\w*)\b",
                        treffer.group(1))[0].strip()
        if len(name.split()) >= 2 and not ({w.lower() for w in name.split()} & WEDER_NOCH_NAME):
            ergebnis["personen"] = name
            break
    mails = _mails(text)
    passend = [m for m in mails if domain and m.lower().endswith(domain.lower())] or mails
    if passend:
        ergebnis["email"] = sorted(passend, key=lambda m: (not ist_persoenlich(m, ergebnis["personen"]), bool(NEBENADRESSE.match(m))))[0]
    return ergebnis


def _name_aus_teamseite(text: str) -> str:
    for treffer in GF_DAHINTER.finditer(text):
        name = treffer.group(1).strip()
        if len(name.split()) >= 2 and not ({w.lower() for w in name.split()} & WEDER_NOCH_NAME):
            return name
    return ""


def kontakt_aus_texten(impressum: str, alles: str, domain: str, bekannte_person: str = "") -> dict:
    """Inhaber und Adresse aus den Firmenseiten. Die Adresse des Inhabers geht vor info@, auch von der Teamseite."""
    if impressum:
        gefunden = lies_kontakt(impressum, domain)
    else:
        # Ohne Impressum zählt nur eine Adresse der eigenen Domain.
        gefunden = {"personen": "", "email": lies_kontakt(alles, domain)["email"]}
        if not gefunden["email"].lower().endswith(domain.lower() or "\0"):
            gefunden["email"] = ""
    gefunden["personen"] = gefunden["personen"] or bekannte_person or _name_aus_teamseite(alles)
    domain = domain.lower()
    persoenlich = [m for m in _mails(alles) if ist_persoenlich(m, gefunden["personen"]) and domain and m.lower().endswith(domain)]
    if persoenlich:
        gefunden["email"] = persoenlich[0]
    elif not gefunden["email"] and domain:
        # Steht im Impressum keine Adresse, hilft die der Karriere- oder Kontaktseite, aber nur von der eigenen Domain.
        eigene = [m for m in _mails(alles) if m.lower().endswith(domain) and not NEBENADRESSE.match(m)]
        eigene.sort(key=lambda m: not KARRIERE_ADRESSE.match(m))
        gefunden["email"] = eigene[0] if eigene else ""
    return gefunden


KARRIERE_ADRESSE = re.compile(r"^(bewerbung\w*|jobs?|karriere|career\w*|personal|hr|recruiting)@", re.I)
RECHTSFORM_KONZERN = re.compile(r"(?<![\w.])(AG|SE|KGaA|S\.A\.|N\.V\.|plc|Inc\.?|Corp\.?)(?![\w])")
NAME_KONZERN = re.compile(r"(?i)\b(gruppe|group|holding|konzern)\b")
IMPRESSUM_KONZERN = (
    (re.compile(r"(?i)\baufsichtsrat"), "Aufsichtsrat im Impressum"),
    (re.compile(r"(?i)\b(?:unternehmen|tochter\w*|mitglied|teil) der [\w .&-]{2,40}?(?:gruppe|group)\b"), "gehört zu einer Gruppe"),
    (re.compile(r"(?i)\bkonzern\w*\b"), "Konzern im Impressum"),
)
VIELE_ANZEIGEN = 10


def konzern_merkmale(name: str, impressum: str = "", anzeigen: int = 0) -> list[str]:
    """Hinweise auf einen Konzern. Eine Größenangabe liefert keine Quelle, das bleibt eine Schätzung."""
    merkmale = []
    form = RECHTSFORM_KONZERN.search(name)
    if form:
        merkmale.append(f"Rechtsform {form.group(1)}")
    if NAME_KONZERN.search(name):
        merkmale.append("Gruppe oder Holding im Namen")
    merkmale += [text for muster, text in IMPRESSUM_KONZERN if muster.search(impressum)]
    if anzeigen >= VIELE_ANZEIGEN:
        merkmale.append(f"{anzeigen} laufende Anzeigen")
    return list(dict.fromkeys(merkmale))


def impressum_text(conn: sqlite3.Connection, firma_id: int) -> str:
    zeilen = conn.execute("SELECT text, quelle FROM chunks WHERE art = 'firma' AND firma_id = ? ORDER BY id", (firma_id,)).fetchall()
    return "\n".join(z["text"] for z in zeilen if IMPRESSUM.search(z["quelle"]))


def finde_kontakt(conn: sqlite3.Connection, firma_id: int) -> dict:
    """Ergänzt fehlende Kontaktdaten aus dem geladenen Impressum. Vorhandene Einträge bleiben stehen."""
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    zeilen = conn.execute("SELECT text, quelle FROM chunks WHERE art = 'firma' AND firma_id = ? ORDER BY id", (firma_id,)).fetchall()
    gefunden = kontakt_aus_texten("\n".join(z["text"] for z in zeilen if IMPRESSUM.search(z["quelle"])),
                                  "\n".join(z["text"] for z in zeilen), _host(firma["website"]), firma["personen"])
    neu = {k: v for k, v in gefunden.items() if v and not firma[k]}
    if neu:
        aendere(conn, firma_id, **neu)
        db.protokolliere(conn, "Kontakt aus Impressum", ", ".join(neu), firma_id)
    return neu


STELLE = re.compile(r"[^\n.!?]{3,80}?\(?\b[mwfdx]\s*/\s*[mwfdx]\s*(?:/\s*[mwfdx*]\s*)?\)?", re.I)


def stellen_aus_text(text: str) -> list[str]:
    stellen: list[str] = []
    for treffer in STELLE.finditer(text):
        titel = re.sub(r"\s+", " ", treffer.group(0)).strip(" -–:|,")
        if titel.lower() not in map(str.lower, stellen):
            stellen.append(titel)
    return stellen[:10]


def kurztext(html: str, maximal: int = 300) -> str:
    """Was die Firma über sich sagt: ihre Seitenbeschreibung, sonst der erste längere Absatz."""
    suppe = BeautifulSoup(html, "html.parser")
    for auswahl in ('meta[name="description"]', 'meta[property="og:description"]'):
        tag = suppe.select_one(auswahl)
        if tag and tag.get("content", "").strip():
            return re.sub(r"\s+", " ", tag["content"]).strip()[:maximal]
    for absatz in text_aus_html(html).splitlines():
        if len(absatz) >= 60:
            return absatz[:maximal]
    return ""


def offene_stellen(conn: sqlite3.Connection, firma_id: int) -> list[str]:
    """Stellentitel von den eigenen Seiten der Firma. Das Kürzel (m/w/d) ist dort das sicherste Zeichen."""
    return stellen_aus_text("\n".join(zeile[0] for zeile in conn.execute(
        "SELECT text FROM chunks WHERE art = 'firma' AND firma_id = ? AND quelle NOT LIKE '%arbeitsagentur%' "
        "AND quelle NOT LIKE '%arbeitnow%' ORDER BY id", (firma_id,))))


def inhaltsnaehe(conn: sqlite3.Connection, firma_id: int) -> int | None:
    """Wie nah die Texte der Firma an deinen Fakten liegen, aus den drei besten Abschnitten. Das ersetzt keine Bewertung."""
    zeilen = conn.execute(
        "SELECT min(vec_distance_cosine(a.embedding, b.embedding)) AS abstand FROM chunks a "
        "JOIN chunks b ON b.art = 'fakt' JOIN fakten f ON f.id = b.fakt_id AND f.status = 'bestaetigt' "
        "WHERE a.art = 'firma' AND a.firma_id = ? GROUP BY a.id ORDER BY abstand LIMIT 3", (firma_id,)).fetchall()
    if not zeilen:
        return None
    return max(0, round(100 * (1 - sum(z[0] for z in zeilen) / len(zeilen))))


def text_aus_html(html: str) -> str:
    suppe = BeautifulSoup(html, "html.parser")
    for tag in suppe(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    # Unsichtbarer Text ist ein typisches Versteck für Prompt Injection.
    for tag in suppe.select('[hidden], [aria-hidden="true"], [style*="display:none"], [style*="display: none"]'):
        tag.decompose()
    text = suppe.get_text("\n", strip=True)
    # Oft steht die Adresse nur im Link, der sichtbare Text sagt bloß „E-Mail“.
    mails = [a["href"][7:].split("?")[0].strip() for a in suppe.select('a[href^="mailto:"]')]
    return re.sub(r"\n{2,}", "\n", "\n".join([text, *mails]))


# Die Stellenseite auf der Website der Firma zählt wie eine Anzeige. Die Marke hängt an ihrer Adresse in `chunks.quelle`.
STELLENSEITE = "#stellenanzeige"
KARRIERE_SEITE = re.compile(r"karriere|career|jobs?\b|stellen|vakanz|bewerb", re.I)
MAX_STELLENSEITEN = 6


def _links(html: str, basis: str, muster: re.Pattern | None = None) -> list[str]:
    host = urlsplit(basis).netloc
    muster = muster or RELEVANT
    gefunden, impressum = [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(basis, a["href"]).split("#")[0]
        teile = urlsplit(url)
        if teile.scheme in ("http", "https") and teile.netloc == host and url not in gefunden and url != basis:
            if muster.search(teile.path) or muster.search(a.get_text()):
                gefunden.append(url)
                if IMPRESSUM.search(a.get_text()):
                    impressum.add(url)
    # Das Impressum zuerst: Dort stehen die Ansprechpartner. Sein Link heißt oft anders als seine Adresse.
    return sorted(gefunden, key=lambda u: 0 if u in impressum or IMPRESSUM.search(u) else 1 if re.search("karriere|career|jobs?\\b|stellen|kontakt|contact|team", u, re.I) else 2)


# Bewerbermanagement-Systeme, auf denen Firmen ihre Stellen veröffentlichen. Gilt nur für Links von der eigenen Website,
# dort ist es die Stellenseite der Firma und kein Portal.
BEWERBERSYSTEM = re.compile(r"(?:^|\.)(?:softgarden\.(?:io|de)|personio\.(?:de|com)|join\.com|recruitee\.com|greenhouse\.io|lever\.co"
                            r"|smartrecruiters\.com|myworkdayjobs\.com|successfactors\.(?:eu|com)|dvinci(?:-hr)?\.(?:com|de)|onlyfy\.jobs"
                            r"|prescreen\.io|rexx-systems\.com|concludis\.de|(?:my-)?job-shop\.com|umantis\.com|jobbase\.io|kenjo\.io"
                            r"|teamtailor\.com|workable\.com|breezy\.hr|bamboohr\.com|ashbyhq\.com|heyrecruit\.de|jobylon\.com"
                            r"|hr4you\.org|coveto\.de|talention\.(?:de|com)|ventari\.de|jobs\.b-ite\.com)$", re.I)
STELLEN_LINK = re.compile(r"karriere|career|jobs?|stelle|position|offer|vacanc|vakanz|bewerb|ausschreibung|\d{4,}", re.I)


def _schwester(url: str, domain: str) -> bool:
    """Eigene Karriere-Domain der Firma: firma.de verlinkt auf firma-karriere.de, der Domainname muss im Host stehen. Oder die
    Stellenseite der Firma bei einem Bewerbermanagement-System, aber nicht dessen Werbelink „powered by“ auf die Startseite."""
    teile = urlsplit(url)
    host, kern = _host(url), domain.split(".")[0]
    if not host or host == domain or "." not in host:   # Links wie http://intranet/ haben keinen Domainnamen
        return False
    if BEWERBERSYSTEM.search(host):
        basis = BEWERBERSYSTEM.search(host).group(0).lstrip(".")
        return host != basis or teile.path.strip("/") != ""
    return len(kern) >= 3 and kern in host.split(".")[-2] and bool(KARRIERE_SEITE.search(url))


MAX_KARRIERE_DOMAINS = 2


def karriere_links(html: str, basis: str, domain: str) -> list[str]:
    gefunden, hosts = [], set()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(basis, a["href"]).split("#")[0]
        if urlsplit(url).scheme in ("http", "https") and _schwester(url, domain) and _host(url) not in hosts:
            gefunden.append(url)
            hosts.add(_host(url))
    return gefunden[:MAX_KARRIERE_DOMAINS]


def _hole(client: httpx.Client, url: str) -> tuple[str, str] | None:
    """Liefert (finale URL, HTML). Abgebrochen wird bei fremdem Host oder Nicht-HTML."""
    with client.stream("GET", url) as antwort:
        if antwort.status_code != 200 or "html" not in antwort.headers.get("content-type", ""):
            return None
        rohdaten = bytearray()
        for stueck in antwort.iter_bytes():
            rohdaten += stueck
            if len(rohdaten) > MAX_BYTES:
                break
        return str(antwort.url), bytes(rohdaten).decode(antwort.encoding or "utf-8", errors="replace")


def _robots(client: httpx.Client, basis: str) -> RobotFileParser:
    parser = RobotFileParser()
    try:
        antwort = client.get(urljoin(basis, "/robots.txt"))
        parser.parse(antwort.text.splitlines() if antwort.status_code == 200 else [])
    except httpx.HTTPError:
        parser.parse([])
    return parser


def teste_domains(firma: str, kandidaten: list[str], client: httpx.Client, name_passt) -> str:
    """Ruft mögliche Domains der Firma auf. Gültig ist nur eine Seite, auf der ihr Name steht."""
    for url in kandidaten:
        try:
            treffer = _hole(client, url)
        except httpx.HTTPError:
            continue
        if treffer and name_passt(firma, text_aus_html(treffer[1])):
            teile = urlsplit(treffer[0])
            return f"{teile.scheme}://{teile.netloc}/"
    return ""


def hole_seiten(website: str, client: httpx.Client, max_seiten: int = MAX_SEITEN) -> dict[str, str]:
    """Holt Start-, Impressums- und Leistungsseiten der eigenen Domain. Der erste Eintrag ist die Startseite."""
    domain = _host(website)
    start = None
    for kandidat in (website, f"https://{domain}"):
        try:
            start = _hole(client, kandidat)
        except httpx.HTTPError:
            start = None
        if start:
            break
    if not start or not urlsplit(start[0]).netloc.removeprefix("www.").endswith(domain):
        raise RuntimeError(f"{website} ist nicht erreichbar oder leitet auf eine fremde Domain um.")

    basis = start[0]
    robots = _robots(client, basis)
    seiten = {basis: start[1]}
    for url in _links(start[1], basis):
        if len(seiten) >= max_seiten:
            break
        if url in seiten or not robots.can_fetch(config.USER_AGENT, url):
            continue
        time.sleep(SEITEN_PAUSE)
        try:
            treffer = _hole(client, url)
        except httpx.HTTPError:
            continue
        if treffer and urlsplit(treffer[0]).netloc == urlsplit(basis).netloc:
            seiten[treffer[0]] = treffer[1]
    # Die Karriereseite liegt manchmal auf einer eigenen Domain der Firma.
    # Der Link dorthin steht auf der Startseite oder erst auf der Karriereseite der eigenen Domain.
    ziele = list(dict.fromkeys(u for html in list(seiten.values()) for u in karriere_links(html, basis, domain)))
    for url in ziele[:MAX_KARRIERE_DOMAINS]:
        teile = urlsplit(url)
        fremd = _robots(client, f"{teile.scheme}://{teile.netloc}/")
        if url in seiten or not fremd.can_fetch(config.USER_AGENT, url):
            continue
        time.sleep(SEITEN_PAUSE)
        try:
            treffer = _hole(client, url)
        except httpx.HTTPError:
            continue
        if treffer and _schwester(treffer[0], domain):
            seiten[treffer[0]] = treffer[1]
    # Eine Ebene tiefer nur auf der Karriereseite: Dort hängen die einzelnen Stellen mit Ansprechperson.
    for url, html in list(seiten.items()):
        if not (_schwester(url, domain) or KARRIERE_SEITE.search(urlsplit(url).path)):
            continue
        regeln_ = robots if urlsplit(url).netloc == urlsplit(basis).netloc else _robots(client, urljoin(url, "/"))
        for unter in _links(html, url, STELLEN_LINK if _schwester(url, domain) else KARRIERE_SEITE):
            if len(seiten) >= max_seiten + MAX_STELLENSEITEN:
                break
            if unter in seiten or not regeln_.can_fetch(config.USER_AGENT, unter):
                continue
            time.sleep(SEITEN_PAUSE)
            try:
                treffer = _hole(client, unter)
            except httpx.HTTPError:
                continue
            if treffer and urlsplit(treffer[0]).netloc == urlsplit(url).netloc:
                seiten[treffer[0]] = treffer[1]
    if not any(IMPRESSUM.search(u) for u in seiten):
        for pfad in IMPRESSUM_PFADE:
            url = urljoin(basis, pfad)
            if not robots.can_fetch(config.USER_AGENT, url):
                continue
            try:
                treffer = _hole(client, url)
            except httpx.HTTPError:
                continue
            if treffer and urlsplit(treffer[0]).netloc == urlsplit(basis).netloc and IMPRESSUM.search(treffer[0]):
                seiten[treffer[0]] = treffer[1]
                break
    return seiten


def lade_seiten(conn: sqlite3.Connection, firma_id: int, client: httpx.Client | None = None) -> int:
    """Lädt Start-, Impressums- und Leistungsseiten der eigenen Domain in den Index."""
    firma = conn.execute("SELECT * FROM firmen WHERE id = ?", (firma_id,)).fetchone()
    client = client or httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": config.USER_AGENT})
    if not firma["website"]:
        raise RuntimeError(f"Für {firma['name']} ist keine Website eingetragen.")
    try:
        seiten = hole_seiten(firma["website"], client)
    except RuntimeError:
        db.protokolliere(conn, "Seiten nicht erreichbar", firma["website"], firma_id)
        raise
    basis = next(iter(seiten))

    texte, quellen = [], []
    for url, html in seiten.items():
        for stueck in rag.zerlege(text_aus_html(html)):
            texte.append(stueck)
            quellen.append(url)
    # Eine gemerkte Stellenanzeige bleibt erhalten, nur die Webseiten werden ersetzt.
    conn.execute("DELETE FROM chunks WHERE art = 'firma' AND firma_id = ? AND quelle NOT LIKE '%arbeitsagentur%' AND quelle NOT LIKE '%arbeitnow%' "
                 f"AND quelle NOT LIKE '%{STELLENSEITE}'", (firma_id,))
    jetzt = db.jetzt()
    rag.speichere(conn, "firma", texte, firma_id=firma_id, quellen=quellen, abruf=jetzt)
    conn.execute("UPDATE firmen SET seiten_geladen = ?, website = ? WHERE id = ?", (jetzt, basis, firma_id))
    conn.commit()
    db.protokolliere(conn, "Seiten geladen", f"{len(seiten)} Seiten, {len(texte)} Abschnitte", firma_id)
    finde_kontakt(conn, firma_id)
    return len(seiten)
