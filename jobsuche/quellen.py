"""Firmenquellen neben der Jobbörse: OpenStreetMap und eine selbst betriebene SearXNG-Instanz.

Beide liefern Firmen mit Website. Inhaber und Mailadresse liest danach `firmen.lade_seiten` aus dem Impressum.
Nach außen gehen nur Gebiet, Branche und Suchbegriffe, nie Fakten oder Namen der suchenden Person.
"""

import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import config, regeln

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Bei Überlastung fällt die Suche auf den nächsten Server zurück. Der letzte wird von VK (Russland) betrieben.
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"
MAX_VERZEICHNIS = 300
OVERPASS_PAUSE = 10  # Dieselben Server antworten Sekunden später oft wieder.
NOMINATIM_PAUSE = 1.1  # Die Nutzungsregeln von Nominatim erlauben höchstens eine Anfrage pro Sekunde.
_letzte_ortsanfrage = 0.0
LAENDER = {"de": "Deutschland", "at": "Österreich", "ch": "Schweiz", "li": "Liechtenstein", "lu": "Luxemburg", "nl": "Niederlande"}
SPRACHE = {"de": "de-DE", "at": "de-AT", "ch": "de-CH", "li": "de-LI", "lu": "de-LU", "nl": "nl-NL"}
BRANCHEN = {
    "it": ("IT und Software", '["office"="it"]'),
    "technik": ("Technik und Ingenieure", '["office"~"^(engineer|research|architect)$"]'),
    "handwerk": ("Handwerk", '["craft"]'),
    "beratung": ("Beratung und Büro", '["office"~"^(consulting|accountant|tax_advisor|lawyer|insurance|financial|advertising_agency)$"]'),
    "gesundheit": ("Gesundheit und Pflege", '["healthcare"]'),
    "handel": ("Handel", '["shop"]'),
    "alle": ("Alle Firmen", '["office"="company"]'),
}
# Verzeichnisse und Portale sind keine Firmen, ihr Impressum führt nur zum Betreiber des Portals.
PORTALE = re.compile(
    r"(?:^|\.)(?:indeed|stepstone|monster|xing|linkedin|kununu|glassdoor|northdata|gelbeseiten|dasoertliche|11880|wlw|"
    r"facebook|instagram|youtube|twitter|x|tiktok|wikipedia|arbeitsagentur|meinestadt|jobware|yelp|firmenwissen|"
    r"online-handelsregister|handelsregister|unternehmensregister|creditreform|google|bing|duckduckgo|companyhouse|webvalid|"
    r"firmenabc|firmenregister|implisense|dnb|kompany|wer-zu-wem|bundesanzeiger|landtag|bundestag|reddit|github|amazon|ebay|"
    r"cylex|golocal|hotfrog|infobel|yasni|werkenntdenbesten|stadtbranchenbuch|branchenbuch\w*|\w*oeffnungszeiten\w*|"
    r"\w*branchenbuch\w*|\w*verzeichnis\w*|firmen(?:suche|info|liste|auskunft)\w*|stepstone|jooble|adzuna|kimeta|"
    r"jobs?(?:fuer|in|boerse|portal|suche|scout)\w*)\.[a-z.]+$", re.I)
LOKALE_INSTANZ = "http://127.0.0.1:18888"


class SucheFehler(Exception):
    pass


@dataclass(frozen=True)
class Gebiet:
    ort: str
    land: str
    lat: float
    lon: float
    osm_typ: str = ""
    osm_id: int = 0
    bbox: tuple[float, float, float, float] | None = None  # Süd, Nord, West, Ost


def _client(client: httpx.Client | None, timeout: int = 90) -> httpx.Client:
    return client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": config.USER_AGENT})


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def ist_portal(url: str) -> bool:
    """Auch Dokumente sind keine Firmenseiten, Suchmaschinen liefern gern PDFs von Behörden."""
    return bool(PORTALE.search(_host(url))) or urlsplit(url).path.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx"))


def finde_lokale_instanz() -> str:
    """Die App trägt die eigene SearXNG-Instanz selbst ein, wenn sie läuft. Geprüft wird nur der eigene Rechner."""
    try:
        antwort = httpx.get(f"{LOKALE_INSTANZ}/healthz", timeout=0.3)
    except httpx.HTTPError:
        return ""
    return LOKALE_INSTANZ if antwort.status_code == 200 and antwort.text.strip() == "OK" else ""


def _startseite(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    teile = urlsplit(url)
    return f"{teile.scheme}://{teile.netloc}/" if teile.netloc else ""


def finde_gebiet(ort: str, land: str, client: httpx.Client | None = None) -> Gebiet:
    """Ohne Ort gilt das ganze Land."""
    if land not in LAENDER:
        raise SucheFehler(f"Unbekanntes Land: {land}")
    anfrage = ort.strip() or LAENDER[land]
    try:
        antwort = _frage_nominatim(_client(client), {"q": anfrage, "countrycodes": land, "format": "jsonv2", "limit": 1})
    except httpx.HTTPError as fehler:
        raise SucheFehler(f"Der Ortsdienst ist nicht erreichbar: {fehler}") from fehler
    if antwort.status_code != 200:
        raise SucheFehler(f"Der Ortsdienst meldet {antwort.status_code}.")
    treffer = antwort.json()
    if not treffer:
        raise SucheFehler(f"„{anfrage}“ habe ich in {LAENDER[land]} nicht gefunden.")
    t = treffer[0]
    bbox = tuple(map(float, t["boundingbox"])) if len(t.get("boundingbox") or []) == 4 else None
    return Gebiet(anfrage, land, float(t["lat"]), float(t["lon"]), t.get("osm_type", ""), int(t.get("osm_id", 0)), bbox)


def _frage_nominatim(client: httpx.Client, params: dict) -> httpx.Response:
    global _letzte_ortsanfrage
    time.sleep(max(0, _letzte_ortsanfrage + NOMINATIM_PAUSE - time.monotonic()))
    _letzte_ortsanfrage = time.monotonic()
    return client.get(NOMINATIM, params=params)


def _km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> int:
    breite = math.radians(b_lat - a_lat)
    laenge = math.radians(b_lon - a_lon)
    h = math.sin(breite / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(laenge / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)))


def liegt_im_gebiet(gebiet: Gebiet, umkreis: int, lat: float, lon: float) -> bool:
    """Eine Region gilt über ihr umschließendes Rechteck, das ist am Rand ungenau."""
    if not umkreis and gebiet.bbox and gebiet.osm_typ in ("relation", "way"):
        sued, nord, west, ost = gebiet.bbox
        return sued <= lat <= nord and west <= lon <= ost
    return _km(gebiet.lat, gebiet.lon, lat, lon) <= (umkreis or 25)


def koordinaten_der_plz(plz: str, land: str, client: httpx.Client | None = None) -> tuple[float, float] | None:
    """None heißt: Diese Postleitzahl gibt es dort nicht. Ein Ausfall des Dienstes ist etwas anderes und wirft `SucheFehler`."""
    try:
        antwort = _frage_nominatim(_client(client), {"postalcode": plz, "countrycodes": land, "format": "jsonv2", "limit": 1})
    except httpx.HTTPError as fehler:
        raise SucheFehler(f"Der Ortsdienst ist nicht erreichbar: {fehler}") from fehler
    if antwort.status_code != 200:
        raise SucheFehler(f"Der Ortsdienst meldet {antwort.status_code}.")
    treffer = antwort.json()
    return (float(treffer[0]["lat"]), float(treffer[0]["lon"])) if treffer else None


_PLZ = {"de": r"\d{5}", "at": r"\d{4}", "ch": r"\d{4}", "li": r"\d{4}", "lu": r"\d{4}", "nl": r"\d{4}[ \t]?[A-Z]{2}"}
_ORT = (r"(?:(?:Bad|Neu|Schloß|Schloss|Sankt|St\.|Groß|Klein|Alt)[ \t]+)?[A-ZÄÖÜ][\wäöüß.-]+"
        r"(?:[ \t]+(?:an der|in der|am|an|a\.|im|in|bei|ob|vor)[ \t]+[\wäöüß.-]+)?")
# Register- und Telefonnummern sehen aus wie Postleitzahlen.
_KEINE_PLZ = re.compile(r"(?i)(?:\b(?:hr[ab]|\w*register\w*|ust\S*|steuer\S*|tel\w*|fax|mobil|\w*nummer|nr)\b|\+\d)[^\n]{0,25}$")
_KEIN_ORT = {"amtsgericht", "registergericht", "handelsregister", "telefon", "tel", "tel.", "telefax", "fax", "mobil", "e-mail", "email", "mail",
             "geschäftsführer", "geschäftsführung", "inhaber", "vertreten", "ust", "hrb", "hra", "deutschland", "germany", "österreich", "schweiz", "internet"}


def sitz_aus_text(text: str, land: str) -> tuple[str, str]:
    """Erste Adresse im Text als (PLZ, Ort). Das Impressum nennt die eigene Firma zuerst."""
    for treffer in re.finditer(rf"(?<![\d.,/])({_PLZ[land]})\s+({_ORT})", text):
        if not _KEINE_PLZ.search(text[:treffer.start()]) and treffer.group(2).split()[0].lower() not in _KEIN_ORT:
            return treffer.group(1), treffer.group(2).strip(" .")
    return "", ""


def _abfrage(gebiet: Gebiet, umkreis: int, branche: str) -> str:
    filter_ = BRANCHEN[branche][1]
    vorlauf = ""
    if not umkreis and gebiet.osm_typ in ("relation", "way"):
        basis = 3_600_000_000 if gebiet.osm_typ == "relation" else 2_400_000_000
        vorlauf = f"area({basis + gebiet.osm_id})->.gebiet;"
        auswahl = "(area.gebiet)"
    else:
        # Kleine Orte sind in OSM oft nur ein Punkt ohne Fläche, dann hilft nur ein Radius.
        auswahl = f"(around:{(umkreis or 25) * 1000},{gebiet.lat},{gebiet.lon})"
    return (f"[out:json][timeout:80];{vorlauf}(nwr{filter_}[\"website\"]{auswahl};"
            f"nwr{filter_}[\"contact:website\"]{auswahl};);out tags center {MAX_VERZEICHNIS};")


def _frage_overpass(abfrage: str, client: httpx.Client) -> list[dict]:
    letzter = "keine Antwort"
    for runde in range(2):
        if runde:
            time.sleep(OVERPASS_PAUSE)
        for server in OVERPASS:
            try:
                antwort = client.post(server, data={"data": abfrage})
            except httpx.HTTPError as fehler:
                letzter = type(fehler).__name__
                continue
            if antwort.status_code != 200:
                letzter = f"Fehler {antwort.status_code}"
                continue
            daten = antwort.json()
            # Overpass meldet eine abgebrochene Abfrage mit Status 200 und einem Hinweis.
            if daten.get("remark") and not daten.get("elements"):
                letzter = "Abfrage zu groß"
                continue
            return daten.get("elements", [])
    raise SucheFehler(f"Das Firmenverzeichnis antwortet nicht ({letzter}). Verkleinere das Gebiet oder versuche es später.")


def verzeichnis(gebiet: Gebiet, umkreis: int, branche: str, client: httpx.Client | None = None) -> list[dict]:
    """Firmen mit Website aus OpenStreetMap."""
    if branche not in BRANCHEN:
        raise SucheFehler(f"Unbekannte Branche: {branche}")
    treffer: dict[str, dict] = {}
    for element in _frage_overpass(_abfrage(gebiet, umkreis, branche), _client(client)):
        tags = element.get("tags", {})
        name = tags.get("name", "").strip()
        website = _startseite(tags.get("website") or tags.get("contact:website") or "")
        if not name or not website or ist_portal(website) or _host(website) in treffer:
            continue
        mitte = element.get("center") or element
        entfernung = _km(gebiet.lat, gebiet.lon, mitte["lat"], mitte["lon"]) if "lat" in mitte else None
        treffer[_host(website)] = {
            "referenz": f"osm:{element.get('type')}/{element.get('id')}",
            "firma": name,
            "titel": tags.get("description", "").strip() or BRANCHEN[branche][0],
            "beruf": "",
            "ort": tags.get("addr:city", "").strip(),
            "entfernung": entfernung,
            "url": website,
            "website": website,
            "quelle": "Verzeichnis",
        }
    return list(treffer.values())


def _passt_zeit(stelle: dict, arbeitszeit: str) -> bool:
    """Ohne Angabe bleibt die Stelle drin: Viele Anzeigen nennen die Arbeitszeit nur im Text."""
    arten = " ".join(map(str, stelle.get("job_types") or [])).lower()
    gesamt = f"{arten} {stelle.get('title', '')}".lower()
    voll = bool(re.search(r"full\s*-?time|vollzeit", gesamt))
    teil = bool(re.search(r"part\s*-?time|teilzeit", gesamt))
    if arbeitszeit == "vollzeit":
        return voll or not teil
    if arbeitszeit == "teilzeit":
        return teil or not voll
    return True


def stellenportal(begriffe: list[str], ort: str, client: httpx.Client | None = None, arbeitszeit: str = "") -> list[dict]:
    """Arbeitnow: freie öffentliche Schnittstelle mit Stellen vor allem aus Deutschland. Ein Abruf je Suche genügt."""
    try:
        antwort = _client(client).get(ARBEITNOW)
    except httpx.HTTPError as fehler:
        raise SucheFehler(f"Arbeitnow ist nicht erreichbar: {fehler}") from fehler
    if antwort.status_code != 200:
        raise SucheFehler(f"Arbeitnow meldet {antwort.status_code}.")
    woerter = [b.lower() for b in begriffe]
    treffer = []
    for stelle in antwort.json().get("data", []):
        titel = str(stelle.get("title", "")).strip()
        firma = str(stelle.get("company_name", "")).strip()
        lage = str(stelle.get("location", ""))
        text = f"{titel} {' '.join(map(str, stelle.get('tags') or []))}".lower()
        if not firma or not any(w in text for w in woerter) or not _passt_zeit(stelle, arbeitszeit):
            continue
        if ort and ort.lower() not in lage.lower() and not stelle.get("remote"):
            continue
        treffer.append({
            "referenz": f"arbeitnow:{stelle.get('slug', '')}", "firma": firma, "titel": titel, "beruf": "", "ort": lage,
            "entfernung": None, "url": str(stelle.get("url", "")), "website": "", "quelle": "Arbeitnow",
            "beschreibung": str(stelle.get("description", ""))[:20000],
        })
    return treffer


def _firmenname(titel: str, url: str) -> str:
    """Seitentitel enthalten meist den Namen. Sonst bleibt die Domain, korrigieren lässt sich beides."""
    teile = [t.strip() for t in re.split(r"\s+[|–—-]\s+", titel) if t.strip()]
    namen = [t for t in teile if not re.fullmatch(r"(?i)impressum|startseite|home|kontakt", t)]
    mit_rechtsform = [t for t in namen if regeln.RECHTSFORM.search(" " + t)]
    return (mit_rechtsform or namen or [_host(url)])[0][:80]


def internet(begriffe: list[str], ort: str, land: str, searxng: str, client: httpx.Client | None = None) -> list[dict]:
    """Sucht über die eigene SearXNG-Instanz gezielt Firmenseiten und Impressen statt Stellenanzeigen."""
    basis = searxng.strip().rstrip("/")
    if not basis:
        raise SucheFehler("Für die Internetsuche fehlt die Adresse deiner SearXNG-Instanz.")
    client = _client(client)
    treffer: dict[str, dict] = {}
    for begriff in begriffe[:3]:
        try:
            antwort = client.get(f"{basis}/search", params={
                "q": f"{begriff} {ort or LAENDER[land]} Impressum Geschäftsführer", "format": "json", "language": SPRACHE[land]})
        except httpx.HTTPError as fehler:
            raise SucheFehler(f"SearXNG ist nicht erreichbar: {fehler}") from fehler
        if antwort.status_code == 403:
            raise SucheFehler("SearXNG lehnt ab: In dessen settings.yml muss unter search.formats auch json stehen.")
        if antwort.status_code != 200:
            raise SucheFehler(f"SearXNG meldet {antwort.status_code}.")
        for ergebnis in antwort.json().get("results", []):
            url = ergebnis.get("url", "")
            host = _host(url)
            if not host or host in treffer or ist_portal(url):
                continue
            treffer[host] = {
                "referenz": f"web:{host}",
                "firma": _firmenname(str(ergebnis.get("title", "")), url),
                "titel": str(ergebnis.get("content", "")).strip()[:160] or begriff,
                "beruf": "",
                "ort": "",
                "entfernung": None,
                "url": url,
                "website": _startseite(url),
                "quelle": "Internet",
            }
    return list(treffer.values())


def website_aus_verzeichnis(firma: str, ort: str, land: str = "de", client: httpx.Client | None = None) -> str:
    """Sucht zu einem Firmennamen den OSM-Eintrag. Passt der Name nicht sicher, bleibt das Ergebnis leer."""
    try:
        antwort = _frage_nominatim(_client(client), {
            "q": f"{firma}, {ort}" if ort else firma, "countrycodes": land, "format": "jsonv2", "extratags": 1, "limit": 1})
    except httpx.HTTPError:
        return ""
    if antwort.status_code != 200 or not antwort.json():
        return ""
    treffer = antwort.json()[0]
    kurz = regeln.kurzname(firma).lower()
    if not kurz or kurz not in str(treffer.get("display_name", "")).lower():
        return ""
    tags = treffer.get("extratags") or {}
    website = _startseite(tags.get("website") or tags.get("contact:website") or "")
    return "" if not website or ist_portal(website) else website


def website_aus_internet(firma: str, ort: str, land: str, searxng: str, client: httpx.Client | None = None) -> str:
    """Erster Treffer, dessen Domain oder Titel den Firmennamen enthält."""
    try:
        antwort = _client(client).get(f"{searxng.strip().rstrip('/')}/search", params={
            "q": f"{firma} {ort} Impressum", "format": "json", "language": SPRACHE.get(land, "de-DE")})
    except httpx.HTTPError:
        return ""
    if antwort.status_code != 200:
        return ""
    # Nicht nur das erste Wort: Bei „B. & K. Beispielnetz“ wäre das „b.“, und das steht in jedem Titel.
    for ergebnis in antwort.json().get("results", []):
        url = ergebnis.get("url", "")
        if url and not ist_portal(url) and name_passt(firma, f"{_host(url)} {ergebnis.get('title', '')}"):
            return _startseite(url)
    return ""


_FUELLWOERTER = {"und", "der", "die", "das", "für", "von", "the", "and", "gruppe", "group", "deutschland", "germany"}


def _slug(text: str) -> str:
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("&", "und")):
        text = text.replace(alt, neu)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def domain_kandidaten(firma: str) -> list[str]:
    """Naheliegende eigene Domains einer Firma, aus ihrem Namen abgeleitet."""
    woerter = [_slug(w) for w in re.split(r"[\s/,]+", regeln.kurzname(firma).lower()) if _slug(w) and _slug(w) not in _FUELLWOERTER]
    if not woerter:
        return []
    namen = ["".join(woerter[:2]), "-".join(woerter[:2]), woerter[0]] if len(woerter) > 1 else [woerter[0]]
    eindeutig = list(dict.fromkeys(n for n in namen if len(n) >= 3))
    return [f"https://www.{n}.{tld}/" for n in eindeutig for tld in ("de", "com")]


def name_passt(firma: str, seitentext: str) -> bool:
    """Das erste Namenswort muss auf der Seite stehen, bei längeren Namen auch ein zweites. So fällt die Namensvetterin auf."""
    text = seitentext.lower()
    woerter = [w for w in re.split(r"[\s/,&-]+", regeln.kurzname(firma).lower()) if len(w) >= 3 and w not in _FUELLWOERTER]
    if not woerter or woerter[0] not in text:
        return False
    return len(woerter) == 1 or any(w in text for w in woerter[1:])
