"""Stellen aus Job-Alerts, Portalen über SearXNG und den Aggregatoren Adzuna und Jooble. Kein Test erreicht echte Server."""

import json
from datetime import date
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime

import httpx
import pytest

from jobsuche import db, mailkonten, nacht, postfach, quellen, suche

HEUTE = format_datetime(datetime.now().astimezone())


def _alert(absender: str, karten: list[tuple[str, str, str, str]]) -> bytes:
    """Alert-Mail im Stil von LinkedIn: Link mit Titel, darunter „Firma · Ort“."""
    teile = "".join(f"<tr><td><a href='https://www.linkedin.com/comm/jobs/view/{nummer}/?trk=alert'>{titel}</a>"
                    f"<p>{firma} · {ort}</p><a href='https://www.linkedin.com/comm/jobs/view/{nummer}/'>Job anzeigen</a></td></tr>"
                    for nummer, titel, firma, ort in karten)
    nachricht = EmailMessage()
    nachricht["From"] = absender
    nachricht["To"] = "person@example.org"
    nachricht["Subject"] = "Neue Jobs für dich"
    nachricht["Date"] = HEUTE
    nachricht.set_content("Nur HTML")
    nachricht.add_alternative(f"<html><body><table>{teile}</table><a href='https://www.linkedin.com/x'>Abmelden</a></body></html>",
                              subtype="html")
    return nachricht.as_bytes()


class FakeImap:
    """Ein Posteingang mit festen Mails. Merkt sich, ob schreibgeschützt geöffnet und nur mit PEEK gelesen wurde."""
    mails: dict[int, bytes] = {}
    protokoll: list = []
    passwort = "richtig"

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.untagged_responses = {}

    def login(self, benutzer, kennwort):
        if kennwort != self.passwort:
            import imaplib
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"ok"]

    def select(self, postfach, readonly=False):
        FakeImap.protokoll.append(("select", postfach, readonly))
        return "OK", [str(len(self.mails)).encode()]

    def response(self, name):
        return name, [b"4711"]

    def uid(self, befehl, *argumente):
        FakeImap.protokoll.append((befehl, argumente))
        if befehl == "SEARCH":
            absender = argumente[-1].strip('"')
            treffer = [u for u, roh in self.mails.items() if absender.encode() in roh.split(b"\n", 3)[0] + roh[:400]]
            return "OK", [" ".join(map(str, treffer)).encode()]
        if befehl == "FETCH":
            daten = []
            for u in argumente[0].split(","):
                daten.append((f"{u} (UID {u} BODY[] {{1}}".encode(), self.mails[int(u)]))
                daten.append(b")")
            return "OK", daten
        return "NO", []

    def logout(self):
        return "BYE", []


@pytest.fixture
def imap(monkeypatch):
    FakeImap.mails = {
        11: _alert("LinkedIn <jobalerts-noreply@linkedin.com>", [
            ("1111111111", "IT-Administrator (m/w/d)", "Muster Systemhaus GmbH", "Kassel, Hessen, Deutschland"),
            ("2222222222", "Erzieher (m/w/d)", "Kita Sonnenschein", "Kassel"),
            ("3333333333", "IT Support Engineer", "Fernfirma AG", "Deutschland (Remote)"),
            ("4444444444", "Software-Entwickler (m/w/d)", "Weitweg GmbH", "München"),
        ]),
        12: b"From: freund@example.org\r\nSubject: Hallo\r\n\r\nKein Alert",
    }
    FakeImap.protokoll = []
    monkeypatch.setattr(postfach, "passwort", lambda conn, p: "richtig")
    return FakeImap


def _ortsdienst(orte: dict[str, tuple[float, float]]):
    def handler(anfrage):
        url = str(anfrage.url)
        if "nominatim" in url:
            q = anfrage.url.params.get("q", "")
            if q in orte:
                lat, lon = orte[q]
                return httpx.Response(200, json=[{"lat": str(lat), "lon": str(lon), "osm_type": "node", "osm_id": 1,
                                                  "boundingbox": [str(lat - .1), str(lat + .1), str(lon - .1), str(lon + .1)]}])
            return httpx.Response(200, json=[])
        return httpx.Response(404)
    return handler


ORTE = {"Kassel": (51.31, 9.48), "München": (48.14, 11.58), "Göttingen": (51.54, 9.93)}


def test_thunderbird_eingang_liest_imap_server_ohne_passwoerter(tmp_path):
    profil = tmp_path / ".thunderbird" / "abc.default"
    profil.mkdir(parents=True)
    (profil / "prefs.js").write_text("\n".join([
        'user_pref("mail.server.server1.type", "imap");', 'user_pref("mail.server.server1.hostname", "imap.example.org");',
        'user_pref("mail.server.server1.socketType", 2);', 'user_pref("mail.server.server1.userName", "p@example.org");',
        'user_pref("mail.server.server2.type", "imap");', 'user_pref("mail.server.server2.hostname", "imap.gmail.com");',
        'user_pref("mail.server.server2.port", 993);', 'user_pref("mail.server.server2.socketType", 3);',
        'user_pref("mail.server.server2.authMethod", 10);', 'user_pref("mail.server.server2.userName", "p@gmail.com");',
        'user_pref("mail.server.server3.type", "none");', 'user_pref("mail.server.server3.hostname", "Local Folders");',
        'user_pref("mail.server.server4.type", "pop3");', 'user_pref("mail.server.server4.hostname", "pop.example.org");',
        'user_pref("mail.server.server4.userName", "p@example.org");',
    ]))
    konten = mailkonten.thunderbird_eingang(tmp_path)
    assert [(k.host, k.port, k.sicherheit, k.oauth) for k in konten] == [
        ("imap.example.org", 143, "starttls", False), ("imap.gmail.com", 993, "ssl", True)]


def test_alerts_werden_schreibgeschuetzt_und_nur_einmal_gelesen(conn, imap):
    postfach.waehle(conn, [postfach.Postfach("imap.example.org", 993, "p@example.org")])
    stellen, hinweise = postfach.lies_alerts(conn, imap_klasse=FakeImap)
    assert hinweise == []
    assert {s["firma"] for s in stellen} == {"Muster Systemhaus GmbH", "Kita Sonnenschein", "Fernfirma AG", "Weitweg GmbH"}
    erste = next(s for s in stellen if s["firma"] == "Muster Systemhaus GmbH")
    assert erste["url"] == "https://www.linkedin.com/jobs/view/1111111111/" and erste["referenz"] == "linkedin:1111111111"
    assert erste["quelle"] == "LinkedIn (Alert)" and erste["datum"] == date.today().isoformat()
    assert ("select", "INBOX", True) in FakeImap.protokoll
    abrufe = [a for a in FakeImap.protokoll if a[0] == "FETCH"]
    assert abrufe and all(a[1][-1] == "(BODY.PEEK[])" for a in abrufe)
    # Zweiter Lauf: nichts Neues abzurufen, die Stellen kommen aus dem Speicher.
    FakeImap.protokoll = []
    wieder, _ = postfach.lies_alerts(conn, imap_klasse=FakeImap)
    assert len(wieder) == 4 and not [a for a in FakeImap.protokoll if a[0] == "FETCH"]


def test_falsches_passwort_ist_ein_hinweis_kein_absturz(conn, imap, monkeypatch):
    monkeypatch.setattr(postfach, "passwort", lambda conn, p: "falsch")
    postfach.waehle(conn, [postfach.Postfach("imap.gmail.com", 993, "p@gmail.com", oauth=True)])
    stellen, hinweise = postfach.lies_alerts(conn, imap_klasse=FakeImap)
    assert stellen == [] and "App-Passwort" in hinweise[0]


def test_unverschluesseltes_postfach_wird_nicht_angemeldet():
    with pytest.raises(quellen.SucheFehler, match="ohne Verschlüsselung"):
        postfach._verbinde(postfach.Postfach("imap.example.org", 143, "p", sicherheit="keine"), None)


def test_suche_mit_alerts_filtert_beruf_und_ort(conn, imap):
    postfach.waehle(conn, [postfach.Postfach("imap.example.org", 993, "p@example.org")])
    client = httpx.Client(transport=httpx.MockTransport(_ortsdienst(ORTE)))
    auftrag = suche.Auftrag("IT Support, Administrator, Entwickler", "Kassel", "de", 50, "it", ("postfach",))
    ergebnis = suche.suche_ueberall(conn, auftrag, client=client, imap_klasse=FakeImap)
    firmen_ = {t["firma"] for t in ergebnis["treffer"]}
    assert firmen_ == {"Muster Systemhaus GmbH", "Fernfirma AG"}  # Erzieher: anderer Beruf, München: zu weit, Remote bleibt
    assert any("1 mit anderem Beruf" in h and "1 außerhalb" in h for h in ergebnis["hinweise"])
    assert not nacht.ist_direkt(ergebnis["treffer"][0])
    # Orte sind gemerkt, der zweite Lauf fragt den Ortsdienst nicht erneut.
    assert json.loads(db.einstellung(conn, "cache.ort.de.münchen", "")) == list(ORTE["München"])


def test_stichwoerter_eigene_vor_abgeleiteten_vor_begriffen(conn):
    assert "Support" in suche.stichwoerter(conn, ["IT Support"]) and "IT" in suche.stichwoerter(conn, ["IT Support"])
    db.setze_einstellung(conn, "suche.abgeleitet_stichwoerter", "Admin, DevOps")
    assert suche.stichwoerter(conn, ["IT Support"]) == ["Admin", "DevOps"]
    db.setze_einstellung(conn, "suche.stichwoerter", "Linux")
    assert suche.stichwoerter(conn, ["IT Support"]) == ["Linux"]
    assert suche.passt_stichwort("IT-Administrator (m/w/d)", ["IT"])
    assert not suche.passt_stichwort("Mitarbeiter Kommissionierung", ["IT"])  # „it“ in „Mitarbeiter“ zählt nicht
    assert suche.passt_stichwort("Linux-Systemadministrator", ["Administrator"])


def test_it_berufe_landen_nicht_unter_technik(conn):
    from jobsuche import profil
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Zwanzig Jahre IT-Support und Administration")
    antwort = {"berufe": ["IT-Berater", "Systemadministrator", "Software-Entwickler"], "branche": "technik",
               "stichwoerter": ["Administrator", "IT", "Support"]}
    suche.leite_begriffe_ab(conn, "m", lambda *a, **k: antwort)
    assert db.einstellung(conn, "suche.abgeleitet_branche", "") == "it"
    assert db.einstellung(conn, "suche.abgeleitet_stichwoerter", "") == "Administrator, IT, Support"


SEARX = [
    {"url": "https://de.linkedin.com/jobs/view/it-support-mitarbeiter-m-w-d-at-muster-gruppe-4481870394",
     "title": "IT-Support Mitarbeiter (m/w/d) bei Muster Gruppe — Kassel - LinkedIn Jobs", "content": "x"},
    {"url": "https://de.linkedin.com/jobs/view/admin-at-beispiel-4385683874?position=31",
     "title": "Beispiel AG sucht Systemadministrator (m/w/d) in Göttingen | LinkedIn", "content": "y"},
    {"url": "https://de.linkedin.com/jobs/view/arbeitsvorbereitung-at-probe-4",
     "title": "Mitarbeiter Arbeitsvorbereitung & IT-Support",
     "content": "Bewerben Sie sich für die Stelle als Mitarbeiter Arbeitsvorbereitung &amp; IT-Support bei Probe Glas GmbH in Kassel. Position"},
    {"url": "https://www.stepstone.de/stellenangebote--IT-Support-Kassel-Muster-GmbH--12345678-inline.html", "title": "x",
     "content": "Stellenangebot als IT-Support (m/w/d) in 34117 Kassel bei der Firma Stepmuster GmbH. Jetzt bewerben"},
    {"url": "https://www.linkedin.com/company/muster", "title": "Muster bei LinkedIn — Kassel", "content": ""},
]


def test_portale_werden_nur_aus_der_trefferliste_gelesen(conn):
    anfragen = []

    def handler(anfrage):
        anfragen.append(str(anfrage.url))
        if anfrage.url.host == "searx.test":
            site = anfrage.url.params["q"].split()[0]
            passend = [t for t in SEARX if site.removeprefix("site:").split("/")[0] in t["url"]]
            return httpx.Response(200, json={"results": passend, "unresponsive_engines": [["brave", "Suspended: too many requests"]]})
        return _ortsdienst(ORTE)(anfrage)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    auftrag = suche.Auftrag("IT Support, Administrator", "Kassel", "de", 50, "it", ("portale",), "http://searx.test")
    ergebnis = suche.suche_ueberall(conn, auftrag, client=client)
    gefunden = {(t["firma"], t["quelle"]) for t in ergebnis["treffer"]}
    assert gefunden == {("Muster Gruppe", "LinkedIn"), ("Beispiel AG", "LinkedIn"), ("Probe Glas GmbH", "LinkedIn"),
                        ("Stepmuster GmbH", "StepStone")}
    assert not any("linkedin.com" in a.split("?")[0] or "stepstone.de" in a.split("?")[0] for a in anfragen)
    assert any("brave" in h for h in ergebnis["hinweise"])
    muster = next(t for t in ergebnis["treffer"] if t["firma"] == "Muster Gruppe")
    assert muster["url"] == "https://www.linkedin.com/jobs/view/4481870394/" and muster["referenz"] == "linkedin:4481870394"


def test_uebernahme_eines_portaltreffers_ruft_weder_portal_noch_jobboerse(conn):
    anfragen = []

    def handler(anfrage):
        anfragen.append(str(anfrage.url))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stelle = {"referenz": "linkedin:4481870394", "firma": "Muster Gruppe", "titel": "IT-Support", "beruf": "", "ort": "Kassel",
              "entfernung": 3, "url": "https://www.linkedin.com/jobs/view/4481870394/", "website": "", "quelle": "LinkedIn",
              "beschreibung": ""}
    fid = suche.uebernimm(conn, stelle, client)
    assert anfragen == []
    assert conn.execute("SELECT count(*) FROM chunks WHERE firma_id = ?", (fid,)).fetchone()[0] == 1
    adzuna = {**stelle, "referenz": "adzuna:77", "firma": "Adzuna Muster GmbH", "url": "https://www.adzuna.de/land/ad/77",
              "quelle": "Adzuna", "beschreibung": "Wir suchen dich für unsere IT-Abteilung in Kassel."}
    fid = suche.uebernimm(conn, adzuna, client)
    assert anfragen == []
    assert "IT-Abteilung" in " ".join(z[0] for z in conn.execute("SELECT text FROM chunks WHERE firma_id = ?", (fid,)))


def test_get_in_it_ueber_searxng_mit_ort_datum_und_website_von_der_anzeige():
    seite = ('<html><script type="application/ld+json">' + json.dumps({
        "@context": "https://schema.org", "@type": "JobPosting", "title": "Softwareentwickler KI (m/w/d)",
        "datePosted": "2026-11-20T00:00:00+01:00", "description": "<p>Mit Claude Code und RAG</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Muster IT GmbH", "url": "https://www.muster-it.de/"},
        "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Bremen"}}]}) + "</script></html>")
    anfragen = []

    def handler(anfrage):
        anfragen.append(str(anfrage.url))
        if anfrage.url.host == "searx.test":
            if "get-in-it" not in anfrage.url.params["q"]:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={"results": [
                {"url": "https://www.get-in-it.de/jobsuche/p314999?utm=x",
                 "title": "Softwareentwickler KI (m/w/d) | Muster IT GmbH | get in IT", "content": ""},
                {"url": "https://www.get-in-it.de/unternehmen/muster", "title": "Muster IT GmbH | get in IT", "content": ""}]})
        if anfrage.url.path == "/jobsuche/p314999":
            return httpx.Response(200, text=seite)
        return httpx.Response(404)

    stellen, _ = quellen.portale(["KI"], "Bremen", "de", "http://searx.test", httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(stellen) == 1
    s = stellen[0]
    assert (s["firma"], s["titel"], s["quelle"], s["referenz"]) == ("Muster IT GmbH", "Softwareentwickler KI (m/w/d)", "get in IT", "getinit:314999")
    assert s["url"] == "https://www.get-in-it.de/jobsuche/p314999"
    assert (s["ort"], s["datum"], s["website"]) == ("Bremen", "2026-11-20", "https://www.muster-it.de/")
    assert "Claude Code" in s["beschreibung"] and "<p>" not in s["beschreibung"]
    assert quellen.ist_portal("https://www.get-in-it.de/unternehmen/muster")


def test_mehrere_standorte_von_get_in_it_waehlen_den_naechsten(conn, monkeypatch):
    seite = ('<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "datePosted": "2026-07-06",
             "hiringOrganization": {"name": "M GmbH", "url": "https://m.example/"},
             "jobLocation": {"@type": "Place", "address": [{"addressLocality": "Berlin"}, {"addressLocality": "Hannover"}]}})
             + "</script>")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=seite)))
    stelle = {"url": "https://www.get-in-it.de/jobsuche/p1", "ort": "", "beschreibung": ""}
    quellen.ergaenze_aus_anzeige(stelle, client)
    assert stelle["ort"] == "Berlin" and stelle["ortsnamen"] == ["Berlin", "Hannover"]
    orte = {"Berlin": (52.52, 13.40), "Hannover": (52.37, 9.73), "Bremen": (53.08, 8.80)}
    monkeypatch.setattr(suche, "gemerktes_gebiet", lambda c, name, land, client=None: quellen.Gebiet(name, "de", *orte[name]))
    monkeypatch.setattr(quellen, "fahrstrecken", lambda start, ziele, client=None: [None] * len(ziele))
    t = dict(stelle, firma="M GmbH", titel="x", quelle="get in IT", entfernung=None)
    suche.mit_fahrstrecke(conn, [t], suche.Auftrag("x", "Bremen", umkreis=300), [], client=client)
    assert t["ort"].startswith("Hannover (+1 weiterer Standort)")
