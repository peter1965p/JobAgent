"""Versand und Firmensuche. Ohne echtes Netz, ohne echte Mails, ohne echten Schlüsselbund."""

import smtplib
from email import message_from_bytes
from email import policy

import json
import re

import httpx
import pytest

from jobsuche import db, firmen, ki, mailkonten, person, profil, suche, versand
from jobsuche.mailkonten import Konto
from jobsuche import nacht, quellen, rag

KONTO = Konto("Test", "ich@example.org", "smtp.example.org", 587, "starttls", "ich@example.org")
EIN_SATZ = {"betreff": "Bewerbung", "text": "Ich suche eine neue Stelle."}


class FakeServer:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.schritte, self.nachrichten = [], []
        FakeServer.letzter = self

    def ehlo(self):
        self.schritte.append("ehlo")

    def starttls(self, context=None):
        self.schritte.append("starttls")
        self.tls_kontext = context

    def login(self, benutzer, passwort):
        self.schritte.append(("login", benutzer, passwort))

    def send_message(self, nachricht, from_addr=None, to_addrs=None):
        self.nachrichten.append((nachricht, from_addr, to_addrs))

    def quit(self):
        self.schritte.append("quit")


@pytest.fixture
def entwurf(conn, tmp_path):
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Zehn Jahre Support")
    datei = tmp_path / "cv.pdf"
    datei.write_bytes(b"%PDF-Testinhalt")
    profil.fuege_unterlage_hinzu(conn, datei, "lebenslauf", True)
    fid = firmen.lege_an(conn, "X GmbH", personen="Kim Test", email="info@x.de")
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=lambda *a, **k: EIN_SATZ)
    versand.speichere_konto(conn, KONTO)
    return eid


def status(conn, eid):
    return conn.execute("SELECT status FROM entwuerfe WHERE id = ?", (eid,)).fetchone()[0]


# --- Versand ---

def test_entwurf_ohne_freigabe_geht_nicht_raus(conn, entwurf):
    with pytest.raises(versand.VersandFehler, match="freigegebene"):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)


def test_senden_wirklich_nur_einmal(conn, entwurf):
    ki.gib_frei(conn, entwurf)
    versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)
    assert status(conn, entwurf) == "gesendet"
    with pytest.raises(versand.VersandFehler, match="nur einmal"):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)


def test_nachricht_hat_absender_empfaenger_anhang_und_bcc_nur_im_umschlag(conn, entwurf):
    ki.gib_frei(conn, entwurf)
    versand.sende_entwurf(conn, entwurf, "geheim", smtp_klar=FakeServer)
    server = FakeServer.letzter
    assert server.schritte[:4] == ["ehlo", "starttls", "ehlo", ("login", "ich@example.org", "geheim")]
    assert server.tls_kontext.check_hostname  # Das Zertifikat wird geprüft.
    nachricht, von, an = server.nachrichten[0]
    assert von == "ich@example.org" and an == ["info@x.de", "ich@example.org"]
    geparst = message_from_bytes(nachricht.as_bytes(), policy=policy.default)
    assert geparst["To"] == "info@x.de" and "Test Person" in geparst["From"] and geparst["Bcc"] is None
    assert geparst["Subject"].startswith("z. Hd. Kim Test") and geparst["Message-ID"]
    anhang = next(geparst.iter_attachments())
    assert anhang.get_filename() == "cv.pdf" and anhang.get_content() == b"%PDF-Testinhalt"
    assert "Guten Tag Kim Test" in geparst.get_body().get_content()


def test_passwort_steht_nirgends_in_der_datenbank(conn, entwurf):
    ki.gib_frei(conn, entwurf)
    versand.sende_entwurf(conn, entwurf, "GEHEIM-PW-123", smtp_klar=FakeServer)
    for tabelle in ("einstellungen", "ereignisse"):
        assert "GEHEIM-PW-123" not in " ".join(str(tuple(z)) for z in conn.execute(f"SELECT * FROM {tabelle}"))


def test_nach_freigabe_verbotener_text_wird_beim_senden_erneut_geprueft(conn, entwurf):
    ki.gib_frei(conn, entwurf)
    conn.execute("UPDATE entwuerfe SET text = 'Mein Gehalt soll hoch sein.' WHERE id = ?", (entwurf,))
    with pytest.raises(versand.VersandFehler, match="Gesperrt"):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)
    assert status(conn, entwurf) == "freigegeben"


def test_fehlender_anhang_stoppt_den_versand(conn, entwurf, tmp_path):
    ki.gib_frei(conn, entwurf)
    (tmp_path / "cv.pdf").unlink()
    with pytest.raises(versand.VersandFehler, match="Anhang fehlt"):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)


@pytest.mark.parametrize("fehler,meldung", [
    (smtplib.SMTPAuthenticationError(535, b"nein"), "abgelehnt"),
    (OSError("kein Netz"), "nicht erreichbar"),
    (smtplib.SMTPException("kaputt"), "Fehler"),
])
def test_anmeldefehler_lassen_die_mail_freigegeben(conn, entwurf, fehler, meldung):
    ki.gib_frei(conn, entwurf)

    class Kaputt(FakeServer):
        def login(self, *a):
            raise fehler

    with pytest.raises(versand.VersandFehler, match=meldung):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=Kaputt)
    assert status(conn, entwurf) == "freigegeben"


def test_abbruch_beim_senden_markiert_nicht_als_gesendet(conn, entwurf):
    ki.gib_frei(conn, entwurf)

    class Bricht(FakeServer):
        def send_message(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("weg")

    with pytest.raises(versand.VersandFehler, match="nicht gesendet"):
        versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=Bricht)
    assert status(conn, entwurf) == "freigegeben"


def test_ssl_konto_nutzt_ssl_verbindung(conn, entwurf):
    versand.speichere_konto(conn, Konto("T", "ich@example.org", "smtp.example.org", 465, "ssl", "ich@example.org"))
    ki.gib_frei(conn, entwurf)
    versand.sende_entwurf(conn, entwurf, "pw", smtp_ssl=FakeServer, smtp_klar=None)
    assert FakeServer.letzter.kwargs["context"].check_hostname and "starttls" not in FakeServer.letzter.schritte


def test_konto_ohne_verschluesselung_wird_abgewiesen(conn):
    with pytest.raises(ValueError, match="Verschlüsselung"):
        versand.speichere_konto(conn, Konto("T", "a@b.de", "h", 25, "keine", "a@b.de"))
    assert versand.lade_konto(conn) is None


def test_testmail_geht_nur_an_die_eigene_adresse(conn, entwurf):
    assert versand.sende_test(conn, "pw", smtp_klar=FakeServer) == "ich@example.org"
    _, _, an = FakeServer.letzter.nachrichten[0]
    assert an == ["ich@example.org"] and status(conn, entwurf) == "entwurf"


def test_betreff_mit_zeilenumbruch_kann_keine_kopfzeile_einschleusen():
    with pytest.raises(ValueError):
        versand.baue_nachricht("A", "a@b.de", "c@d.de", "x\nBcc: boese@evil.example", "t", [])


# --- Kontoerkennung ---

def test_thunderbird_liest_konten_und_keine_passwoerter(tmp_path):
    profilordner = tmp_path / ".thunderbird/abc.default"
    profilordner.mkdir(parents=True)
    (profilordner / "prefs.js").write_text("\n".join([
        'user_pref("mail.identity.id1.smtpServer", "smtp1");', 'user_pref("mail.identity.id1.useremail", "ich@x.de");',
        'user_pref("mail.smtpserver.smtp1.hostname", "mail.x.de");', 'user_pref("mail.smtpserver.smtp1.port", 465);',
        'user_pref("mail.smtpserver.smtp1.try_ssl", 3);', 'user_pref("mail.smtpserver.smtp1.username", "ich");',
        'user_pref("mail.identity.id2.smtpServer", "smtp2");', 'user_pref("mail.identity.id2.useremail", "ox@gmail.com");',
        'user_pref("mail.smtpserver.smtp2.hostname", "smtp.gmail.com");', 'user_pref("mail.smtpserver.smtp2.try_ssl", 2);',
        'user_pref("mail.smtpserver.smtp2.authMethod", 10);',
        'user_pref("mail.identity.id3.smtpServer", "gibtsnicht");', 'user_pref("mail.identity.id3.useremail", "weg@x.de");',
    ]), encoding="utf-8")
    konten = {k.email: k for k in mailkonten.thunderbird(tmp_path)}
    assert set(konten) == {"ich@x.de", "ox@gmail.com"}
    k = konten["ich@x.de"]
    assert (k.host, k.port, k.sicherheit, k.benutzer, k.oauth) == ("mail.x.de", 465, "ssl", "ich", False)
    assert konten["ox@gmail.com"].oauth and konten["ox@gmail.com"].port == 587


def test_evolution(tmp_path):
    ordner = tmp_path / ".config/evolution/sources"
    ordner.mkdir(parents=True)
    (ordner / "t1.source").write_text("[Data Source]\nEnabled=true\n[Mail Transport]\nBackendName=smtp\n[Authentication]\n"
                                      "Host=mail.x.de\nPort=587\nUser=ich\n[Security]\nMethod=starttls-on-standard-port\n", encoding="utf-8")
    (ordner / "a1.source").write_text("[Mail Identity]\nAddress=ich@x.de\n[Mail Submission]\nTransportUid=t1\n", encoding="utf-8")
    k = mailkonten.evolution(tmp_path)[0]
    assert (k.email, k.host, k.port, k.sicherheit, k.benutzer) == ("ich@x.de", "mail.x.de", 587, "starttls", "ich")


def test_geary(tmp_path):
    ordner = tmp_path / ".config/geary/account_01"
    ordner.mkdir(parents=True)
    (ordner / "geary.ini").write_text("[Account]\nprimary_email=ich@x.de\n[Outgoing]\nhost=mail.x.de\nport=465\ntransport_security=tls\n", encoding="utf-8")
    assert mailkonten.geary(tmp_path)[0].sicherheit == "ssl"


def test_kmail_claws_msmtp(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config/mailtransports").write_text("[General]\nx=1\n[Transport 1]\nhost=m.x.de\nport=465\nuser=ich@x.de\nencryption=SSL\n", encoding="utf-8")
    assert mailkonten.kmail(tmp_path)[0].email == "ich@x.de"
    (tmp_path / ".claws-mail").mkdir()
    (tmp_path / ".claws-mail/accountrc").write_text("[Account: 1]\naddress=c@x.de\nsmtp_server=m.x.de\nssl_smtp=2\n", encoding="utf-8")
    assert mailkonten.claws(tmp_path)[0].sicherheit == "starttls"
    (tmp_path / ".msmtprc").write_text("defaults\ntls on\naccount a\nhost m.x.de\nport 465\nfrom m@x.de\nuser m\ntls on\ntls_starttls off\npassword geheim\n", encoding="utf-8")
    k = mailkonten.msmtp(tmp_path)[0]
    assert (k.email, k.sicherheit, k.benutzer) == ("m@x.de", "ssl", "m") and "geheim" not in str(k)


def test_outlook_werte_aus_der_registry_als_utf16():
    def t(s):
        return s.encode("utf-16-le") + b"\x00\x00"
    k = mailkonten.outlook_aus_werten([{"SMTP Server": t("smtp.office365.com"), "Email": t("ich@x.de"), "SMTP Use SSL": 2, "SMTP Port": 587}, {"Email": t("nur@adresse.de")}])
    assert len(k) == 1 and (k[0].host, k[0].email, k[0].sicherheit) == ("smtp.office365.com", "ich@x.de", "starttls")


def test_eigene_adresse_steht_vorn_und_defektes_programm_stoppt_nichts(tmp_path):
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config/mailtransports").write_text("kaputt ohne abschnitt", encoding="utf-8")
    (tmp_path / ".claws-mail").mkdir()
    (tmp_path / ".claws-mail/accountrc").write_text("[A]\naddress=andere@x.de\nsmtp_server=m\nssl_smtp=1\n[B]\naddress=ich@x.de\nsmtp_server=m\nssl_smtp=1\n", encoding="utf-8")
    konten = mailkonten.finde_alle(tmp_path, "ICH@x.de")
    assert [k.email for k in konten] == ["ich@x.de", "andere@x.de"]


def test_nichts_gefunden_ist_kein_fehler(tmp_path):
    assert mailkonten.finde_alle(tmp_path) == []


# --- Passwort ---

def test_passwort_nur_ueber_den_schluesselbund(monkeypatch):
    speicher = {}
    monkeypatch.setattr(versand.keyring, "set_password", lambda d, n, p: speicher.__setitem__((d, n), p))
    monkeypatch.setattr(versand.keyring, "get_password", lambda d, n: speicher.get((d, n)))
    monkeypatch.setattr(versand.keyring, "delete_password", lambda d, n: speicher.pop((d, n)))
    assert versand.gespeichertes_passwort(KONTO) is None
    versand.speichere_passwort(KONTO, "pw")
    assert versand.gespeichertes_passwort(KONTO) == "pw" and list(speicher) == [("jobsuche", "ich@example.org@smtp.example.org")]
    versand.loesche_passwort(KONTO)
    assert versand.gespeichertes_passwort(KONTO) is None


def test_fehlender_schluesselbund_gibt_klare_meldung(monkeypatch):
    def kaputt(*a):
        raise versand.KeyringError("kein Backend")
    monkeypatch.setattr(versand.keyring, "set_password", kaputt)
    monkeypatch.setattr(versand.keyring, "get_password", kaputt)
    assert versand.gespeichertes_passwort(KONTO) is None
    with pytest.raises(versand.VersandFehler, match="Schlüsselbund"):
        versand.speichere_passwort(KONTO, "pw")


# --- Suche ---

ANTWORT = {"ergebnisliste": [
    {"referenznummer": "R1", "firma": "NewTec GmbH", "stellenangebotsTitel": "KI-Entwickler", "hauptberuf": "Softwareentwickler/in",
     "entfernung": 9, "stellenlokationen": [{"adresse": {"ort": "Bremen"}}]},
    {"referenznummer": "R2", "firma": "Bäckerei Korn", "stellenangebotsTitel": "Bäcker", "hauptberuf": "Bäcker/in"},
    {"referenznummer": "R3", "stellenangebotsTitel": "Ohne Firma"},
    {"firma": "Ohne Referenz"},
]}


def _api(antworten=None, code=200):
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        return httpx.Response(code, json=antworten or ANTWORT)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, anfragen


def test_suche_liest_treffer_und_ueberspringt_unvollstaendige():
    client, anfragen = _api()
    treffer = suche.suche("KI, Admin", "Bremen", 50, client=client)
    assert [t["referenz"] for t in treffer] == ["R1", "R2"]  # R1 und R2 aus beiden Anfragen nur einmal.
    assert treffer[0]["ort"] == "Bremen" and treffer[0]["url"].endswith("/R1")
    assert len(anfragen) == 2 and anfragen[0].url.params["wo"] == "Bremen" and anfragen[0].headers["X-API-Key"]
    assert anfragen[0].url.params["pav"] == "false" and anfragen[0].url.params["zeitarbeit"] == "false"


def test_vermittler_fallen_raus():
    antwort = {"ergebnisliste": [
        {"referenznummer": "1", "firma": "Griepenstroh Personalvermittlung GmbH", "stellenangebotsTitel": "Admin"},
        {"referenznummer": "2", "firma": "Muster Zeitarbeit KG", "stellenangebotsTitel": "Admin"},
        {"referenznummer": "3", "firma": "Personal Service Nord GmbH", "stellenangebotsTitel": "Admin"},
        {"referenznummer": "4", "firma": "Stadtwerke Bremen", "stellenangebotsTitel": "Admin"},
    ]}
    assert [t["firma"] for t in suche.suche("x", "y", client=_api(antwort)[0])] == ["Stadtwerke Bremen"]


@pytest.mark.parametrize("was,wo", [("", "Bremen"), ("KI", " "), (" , ", "Bremen")])
def test_suche_ohne_eingabe(was, wo):
    with pytest.raises(suche.SucheFehler):
        suche.suche(was, wo, client=_api()[0])


def test_geaenderte_schnittstelle_gibt_klare_meldung():
    with pytest.raises(suche.SucheFehler, match="403"):
        suche.suche("KI", "Bremen", client=_api(code=403)[0])


def test_naehe_sortiert_bekannte_firmen_nach_hinten(conn):
    profil.lege_eigenen_fakt_an(conn, "technik", "KI Entwickler Softwareentwickler")
    firmen.lege_an(conn, "Bäckerei Korn GmbH")
    treffer = suche.bewerte_naehe(conn, suche.suche("x", "y", client=_api()[0]))
    assert [t["firma"] for t in treffer] == ["NewTec GmbH", "Bäckerei Korn"]
    assert treffer[1]["bekannt"] and treffer[0]["naehe"] > 0


def test_naehe_ohne_bestaetigte_fakten(conn):
    assert suche.bewerte_naehe(conn, suche.suche("x", "y", client=_api()[0]))[0]["naehe"] is None
    assert suche.bewerte_naehe(conn, []) == []


def test_uebernehmen_legt_firma_und_anzeige_an_und_laden_behaelt_sie(conn):
    stelle = suche.suche("x", "y", client=_api()[0])[0]
    fid = suche.uebernimm(conn, stelle)
    f = conn.execute("SELECT * FROM firmen WHERE id = ?", (fid,)).fetchone()
    assert (f["name"], f["sitz"], f["email"]) == ("NewTec GmbH", "Bremen", "")
    with pytest.raises(ValueError, match="schon"):
        suche.uebernimm(conn, stelle)

    firmen.aendere(conn, fid, website="newtec.example")
    client = httpx.Client(follow_redirects=True, transport=httpx.MockTransport(lambda r: (
        httpx.Response(200, html='<a href="/impressum">Impressum</a>Start', request=r) if r.url.path == "/" else
        httpx.Response(200, html="Geschäftsführer: Max Muster<br>E-Mail: info@newtec.example", request=r) if r.url.path == "/impressum" else
        httpx.Response(404, request=r))))
    firmen.lade_seiten(conn, fid, client=client)
    f = conn.execute("SELECT * FROM firmen WHERE id = ?", (fid,)).fetchone()
    assert (f["personen"], f["email"]) == ("Max Muster", "info@newtec.example")
    texte = " ".join(z[0] for z in conn.execute("SELECT text FROM chunks WHERE firma_id = ?", (fid,)))
    assert "Stellenangebot: KI-Entwickler" in texte  # Die Anzeige überlebt das Laden der Webseiten.


@pytest.mark.parametrize("text,person,mail", [
    ("Geschäftsführer: Fabio Martin\nHandelsregister: HRB 1", "Fabio Martin", ""),
    ("Vertreten durch: Dr. Lars Röwekamp und Jens Schumann", "Dr. Lars Röwekamp", ""),
    ("Geschäftsführung Björn Groenewold Registergericht Leer", "Björn Groenewold", ""),
    ("Kontakt: info@firma.de oder presse@andere.com", "", "info@firma.de"),
    ("Keine Angaben hier", "", ""),
    ("Geschäftsführung: Registergericht Bremen", "", ""),
    ("Geschäftsführer: Philip Kraatz\nKontakt\nTelefon 123", "Philip Kraatz", ""),
    ("Geschäftsführer: Herr Dipl. Inf. Björn Groenewold\nSitz Leer", "Björn Groenewold", ""),
    ("vertreten durch ihre Gesellschafter Dennis Peters", "", ""),
    ("Geschäftsführer: Marc große Austing, Dominik Heuer", "Marc große Austing", ""),
    ("Geschäftsführer Prof. Dr. Maria Muster", "Prof. Dr. Maria Muster", ""),
    ("Impressum: impressum@firma.de, team@firma.de", "", "team@firma.de"),
    ("Nur impressum@firma.de", "", "impressum@firma.de"),
])
def test_impressum_lesen(text, person, mail):
    k = firmen.lies_kontakt(text, "firma.de")
    assert k["personen"] == person and k["email"] == mail


def test_vorhandene_kontaktdaten_werden_nicht_ueberschrieben(conn):
    from jobsuche import rag
    fid = firmen.lege_an(conn, "X", personen="Kim Test", email="a@x.de", website="https://x.de")
    rag.speichere(conn, "firma", ["Geschäftsführer: Max Muster, info@x.de"], firma_id=fid, quellen=["https://x.de/impressum"])
    assert firmen.finde_kontakt(conn, fid) == {}
    f = conn.execute("SELECT personen, email FROM firmen WHERE id = ?", (fid,)).fetchone()
    assert (f["personen"], f["email"]) == ("Kim Test", "a@x.de")


def test_ohne_impressum_nur_mail_der_eigenen_domain(conn):
    from jobsuche import rag
    a = firmen.lege_an(conn, "A", website="https://a.de")
    rag.speichere(conn, "firma", ["Geschäftsführer Max Muster schreibt: info@a.de"], firma_id=a, quellen=["https://a.de/team"])
    assert firmen.finde_kontakt(conn, a) == {"email": "info@a.de"}
    b = firmen.lege_an(conn, "B", website="https://b.de")
    rag.speichere(conn, "firma", ["Kontakt: fremd@andere.de"], firma_id=b, quellen=["https://b.de/x"])
    assert firmen.finde_kontakt(conn, b) == {}


# --- Passende Firmen aus den Fakten ---

class Berufe:
    def __init__(self, antwort):
        self.antwort, self.aufrufe, self.prompts = antwort, 0, []

    def __call__(self, modell, system, nutzer, schema, temperatur=0.3):
        self.aufrufe += 1
        self.prompts.append(nutzer)
        return {"berufe": self.antwort, "branche": "it"}


def test_begriffe_kommen_aus_bestaetigten_fakten_und_werden_gemerkt(conn):
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Zehn Jahre Linux-Server betreut")
    profil.lege_eigenen_fakt_an(conn, "suche", "Sucht im Raum Bremen")
    profil.lege_fakt_an(conn, "projekt", "Unbestätigter Vorschlag", "KI-Vorschlag")
    llm = Berufe(["Systemadministrator (m/w/d)", "Linux Administrator"])
    assert suche.leite_begriffe_ab(conn, "m", llm) == ["Systemadministrator", "Linux Administrator"]
    assert "Linux-Server" in llm.prompts[0] and "Unbestätigter" not in llm.prompts[0] and "Raum Bremen" not in llm.prompts[0]
    assert suche.leite_begriffe_ab(conn, "m", llm) == ["Systemadministrator", "Linux Administrator"] and llm.aufrufe == 1
    profil.lege_eigenen_fakt_an(conn, "technik", "Neu: Ansible")
    suche.leite_begriffe_ab(conn, "m", llm)
    assert llm.aufrufe == 2  # Neue Fakten, neue Ableitung.


@pytest.mark.parametrize("roh,erwartet", [
    (["A", "a", "B"], ["A", "B"]),
    (["Ein viel zu langer Satz über irgendwas Schönes"], []),
    (["Gehalt Manager", "Admin"], ["Admin"]),
    ([str(i) for i in range(10)], [str(i) for i in range(6)]),
    ("kein Array", []),
])
def test_begriffe_bereinigen(roh, erwartet):
    assert suche.bereinige_begriffe(roh) == erwartet


def test_ohne_fakten_oder_brauchbare_antwort_klare_meldung(conn):
    with pytest.raises(suche.SucheFehler, match="bestätigten Fakten"):
        suche.leite_begriffe_ab(conn, "m", Berufe(["A"]))
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Etwas")
    with pytest.raises(suche.SucheFehler, match="brauchbaren"):
        suche.leite_begriffe_ab(conn, "m", Berufe([]))


def test_suchwunsch_geht_vor_wohnort(conn):
    from jobsuche import person
    person.speichere(conn, name="T", email="t@x.de", ort="Daun (Vulkaneifel)")
    assert suche.suchort(conn) == "Daun"
    profil.lege_eigenen_fakt_an(conn, "suche", "Sucht eine Festanstellung im Raum Bremen/Niedersachsen, Umzug geplant.")
    assert suche.suchort(conn) == "Bremen"


def test_ganzer_weg_in_einem_schritt(conn):
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "KI Entwickler Softwareentwickler")
    profil.lege_eigenen_fakt_an(conn, "suche", "Sucht im Raum Bremen")
    client, anfragen = _api()
    ergebnis = suche.finde_passende(conn, "m", suche.Auftrag(quellen=("jobboerse",)), chat=Berufe(["KI Entwickler"]), client=client)
    assert ergebnis["begriffe"] == ["KI Entwickler"] and ergebnis["ort"] == "Bremen"
    assert anfragen[0].url.params["was"] == "KI Entwickler" and anfragen[0].url.params["wo"] == "Bremen"
    assert ergebnis["treffer"][0]["firma"] == "NewTec GmbH"


# --- Mehrere Quellen, Inhaber, komplette Mail ---

def _firmen_netz(seiten=None, overpass=None, nominatim=None, overpass_code=200):
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        url = str(anfrage.url)
        if "nominatim" in url:
            return httpx.Response(200, json=nominatim if nominatim is not None else [
                {"lat": "53.07", "lon": "8.80", "osm_type": "relation", "osm_id": 62559, "display_name": "Bremen"}])
        if "interpreter" in url:
            return httpx.Response(overpass_code, json=overpass or {"elements": []})
        if "/search" in url:
            return httpx.Response(200, json={"results": [
                {"url": "https://www.indeed.com/x", "title": "Portal"},
                {"url": "https://www.firma-a.de/impressum", "title": "Impressum - Firma A GmbH", "content": "Software"},
            ]})
        for pfad, html in (seiten or {}).items():
            if url.rstrip("/").endswith(pfad):
                return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), anfragen


OSM = {"elements": [
    {"type": "node", "id": 1, "lat": 53.1, "lon": 8.8, "tags": {"name": "Plutex GmbH", "office": "it", "website": "plutex.de/x", "addr:city": "Bremen"}},
    {"type": "way", "id": 2, "center": {"lat": 53.2, "lon": 8.8}, "tags": {"name": "Zweiter Eintrag", "contact:website": "https://plutex.de"}},
    {"type": "node", "id": 3, "lat": 53.1, "lon": 8.8, "tags": {"name": "Nur Facebook", "website": "https://www.facebook.com/x"}},
    {"type": "node", "id": 4, "lat": 53.1, "lon": 8.8, "tags": {"website": "https://ohne-name.de"}},
]}


def test_verzeichnis_liefert_firmen_mit_website_ohne_portale_und_doppelte():
    client, anfragen = _firmen_netz(overpass=OSM)
    gebiet = quellen.finde_gebiet("Bremen", "de", client)
    treffer = quellen.verzeichnis(gebiet, 25, "it", client)
    assert [(t["firma"], t["website"], t["quelle"]) for t in treffer] == [("Plutex GmbH", "https://plutex.de/", "Verzeichnis")]
    assert treffer[0]["entfernung"] == 3
    assert anfragen[0].url.params["countrycodes"] == "de"
    assert b"around%3A25000" in anfragen[1].content


def test_ganze_region_fragt_die_flaeche_statt_eines_radius():
    client, anfragen = _firmen_netz()
    quellen.verzeichnis(quellen.Gebiet("Bremen", "de", 53, 8, "relation", 62559), 0, "it", client)
    assert b"3600062559" in anfragen[0].content and b"around" not in anfragen[0].content


def test_verzeichnis_faellt_auf_naechsten_server_zurueck_und_meldet_totalausfall():
    aufrufe = []

    def handler(anfrage):
        aufrufe.append(str(anfrage.url))
        return httpx.Response(502) if len(aufrufe) == 1 else httpx.Response(200, json=OSM)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert quellen.verzeichnis(quellen.Gebiet("B", "de", 53, 8), 25, "it", client) and len(aufrufe) == 2
    client, _ = _firmen_netz(overpass_code=502)
    with pytest.raises(suche.SucheFehler, match="antwortet nicht"):
        quellen.verzeichnis(quellen.Gebiet("B", "de", 53, 8), 25, "it", client)


def test_internetsuche_filtert_portale_und_zielt_aufs_impressum():
    client, anfragen = _firmen_netz()
    treffer = quellen.internet(["KI Entwickler"], "Bremen", "de", "http://127.0.0.1:8888/", client)
    assert [(t["firma"], t["website"]) for t in treffer] == [("Firma A GmbH", "https://www.firma-a.de/")]
    assert "Impressum" in anfragen[0].url.params["q"] and anfragen[0].url.params["format"] == "json"
    with pytest.raises(suche.SucheFehler, match="SearXNG"):
        quellen.internet(["x"], "Bremen", "de", "")


def test_ausfall_einer_quelle_stoppt_die_anderen_nicht(conn):
    client, _ = _firmen_netz({"firma-a.de": "<html><body>Musterweg 1<br>28195 Bremen</body></html>"}, overpass_code=502)
    ergebnis = suche.suche_ueberall(conn, suche.Auftrag("KI", "Bremen", quellen=("verzeichnis", "internet"), searxng="http://s"), client=client)
    assert any("Firmenverzeichnis" in h for h in ergebnis["hinweise"])
    assert [t["firma"] for t in ergebnis["treffer"]] == ["Firma A GmbH"]


def test_jobboerse_gilt_nur_fuer_deutschland(conn):
    client, anfragen = _api()
    ergebnis = suche.suche_ueberall(conn, suche.Auftrag("KI", "Wien", "at", quellen=("jobboerse",)), client=client)
    assert ergebnis["treffer"] == [] and "nur Deutschland" in ergebnis["hinweise"][0] and not anfragen


def test_dieselbe_firma_aus_zwei_quellen_zaehlt_einmal():
    a = {"firma": "Plutex GmbH", "quelle": "Jobbörse", "website": ""}
    b = {"firma": "PLUTEX", "quelle": "Verzeichnis", "website": "https://plutex.de/"}
    ergebnis = suche._vereine([[a], [b]])
    assert len(ergebnis) == 1 and ergebnis[0]["website"] == "https://plutex.de/" and ergebnis[0]["quelle"] == "Jobbörse, Verzeichnis"


def test_keine_fakten_oder_person_verlassen_den_rechner(conn):
    person.speichere(conn, name="Geheime Person", email="geheim@example.org", ort="Geheimstadt")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Geheimer Fakt zur Erfahrung")
    client, anfragen = _firmen_netz(overpass=OSM)
    suche.suche_ueberall(conn, suche.Auftrag("KI", "Bremen", quellen=("verzeichnis", "internet"), searxng="http://s"), client=client)
    for a in anfragen:
        roh = str(a.url) + a.content.decode()
        assert "Geheim" not in roh


IMPRESSUM = """<html><body><h1>Impressum</h1>Geschäftsführer: Kim Müller<br>E-Mail: info (at) firma-a (dot) de</body></html>"""
TEAM = """<html><body><p>Kim Müller, Geschäftsführer</p><a href="mailto:k.mueller@firma-a.de">Schreiben</a></body></html>"""
START = """<html><body><a href="/impressum">Impressum</a><a href="/team">Team</a></body></html>"""


def _seiten():
    return {"firma-a.de": START, "/impressum": IMPRESSUM, "/team": TEAM}


def test_inhaber_und_persoenliche_adresse_gehen_vor_info(conn):
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    client, _ = _firmen_netz(_seiten())
    ergebnis = suche.hole_inhaber(conn, fid, client=client)
    assert ergebnis["personen"] == "Kim Müller" and ergebnis["email"] == "k.mueller@firma-a.de" and ergebnis["art"] == "persönlich"


def test_verschleierte_adresse_und_keine_geratene(conn):
    assert firmen.lies_kontakt("Mail: info (at) firma-a (dot) de", "firma-a.de")["email"] == "info@firma-a.de"
    assert firmen.lies_kontakt("Wir treffen uns at home. Kim Müller", "x.de")["email"] == ""
    fid = firmen.lege_an(conn, "Ohne Mail GmbH", website="https://firma-a.de/")
    client, _ = _firmen_netz({"firma-a.de": "<html><body>Geschäftsführer: Kim Müller</body></html>"})
    with pytest.raises(ki.NichtMoeglich, match="keine Mailadresse"):
        suche.schlage_mail_vor(conn, fid, "m", client=client, chat=Berufe(["egal"]))
    assert conn.execute("SELECT email FROM firmen WHERE id = ?", (fid,)).fetchone()[0] == ""


def test_teamseite_liefert_inhaber_wenn_das_impressum_schweigt(conn):
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    client, _ = _firmen_netz({"firma-a.de": START, "/team": TEAM})
    assert suche.hole_inhaber(conn, fid, client=client)["personen"] == "Kim Müller"


def test_jobboersen_treffer_bekommt_website_nur_bei_passendem_namen(conn):
    fid = firmen.lege_an(conn, "Firma A GmbH", sitz="Bremen")
    passt = [{"display_name": "Firma A, Bremen", "extratags": {"website": "https://firma-a.de"}}]
    client, _ = _firmen_netz(_seiten(), nominatim=passt)
    assert suche.hole_inhaber(conn, fid, client=client)["website"].startswith("https://firma-a.de")
    fid2 = firmen.lege_an(conn, "Andere GmbH", sitz="Bremen")
    client, _ = _firmen_netz(_seiten(), nominatim=passt)
    with pytest.raises(suche.SucheFehler, match="keine Website"):
        suche.hole_inhaber(conn, fid2, client=client)


def test_uebernehmen_behaelt_die_firma_wenn_die_seite_ausfaellt(conn):
    stelle = {"referenz": "osm:node/1", "firma": "Tot GmbH", "titel": "IT", "ort": "Bremen", "website": "https://tot.de/", "quelle": "Verzeichnis"}
    client, _ = _firmen_netz()
    ergebnis = suche.uebernimm_und_finde(conn, stelle, client=client)
    assert "nicht erreichbar" in ergebnis["fehler"]
    zeile = conn.execute("SELECT quelle FROM firmen WHERE name = 'Tot GmbH'").fetchone()
    assert zeile[0] == "Verzeichnis"
    assert conn.execute("SELECT count(*) FROM chunks WHERE text LIKE 'Stellenangebot%'").fetchone()[0] == 0


class _Mail:
    def __init__(self):
        self.prompts = []

    def __call__(self, modell, system, nutzer, schema, temperatur=0.3):
        self.prompts.append(system + nutzer)
        return {"betreff": "Initiativbewerbung", "text": "Ihre Seiten passen. Haben Sie 15 Minuten?"}


def test_komplette_mail_ohne_tippen_empfaenger_vom_programm_bleibt_unfreigegeben(conn):
    person.speichere(conn, name="Test Person", email="ich@example.org")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Baut Software")
    db.setze_einstellung(conn, "vorlage", "Mein Stil.")
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    client, _ = _firmen_netz(_seiten())
    llm = _Mail()
    eid = suche.schlage_mail_vor(conn, fid, "m", chat=llm, client=client)
    e = conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (eid,)).fetchone()
    assert e["empfaenger"] == "k.mueller@firma-a.de" and e["status"] == "entwurf"
    assert e["text"].startswith("Guten Tag Kim Müller,") and "Ihre Seiten passen" in e["text"]
    assert "<stilbeispiel>" in llm.prompts[0] and "Baut Software" in llm.prompts[0]


def test_impressum_wird_auch_ohne_link_und_unter_anderem_namen_gefunden(conn):
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    client, _ = _firmen_netz({"firma-a.de": "<html><body>Nur JavaScript</body></html>", "/impressum": IMPRESSUM})
    assert suche.hole_inhaber(conn, fid, client=client)["personen"] == "Kim Müller"
    assert firmen._links('<a href="/x">Team</a><a href="/rechtliches">Imprint</a>', "https://f.de/")[0].endswith("/rechtliches")


def test_geschaeftsfuehrende_gesellschafter_und_titel():
    text = "Vertreten durch die geschäftsführenden Gesellschafter:\nJustus Meyer, Jarne Habermann"
    assert firmen.lies_kontakt(text)["personen"] == "Justus Meyer"


# --- Nachtlauf ---

class _Nacht:
    def __init__(self, scores):
        self.scores, self.prompts = list(scores), []

    def __call__(self, modell, system, nutzer, schema, temperatur=0.3):
        self.prompts.append(nutzer)
        if "score" in schema["properties"]:
            return {"score": self.scores.pop(0), "passt": ["x"], "passt_nicht": [], "belege": []}
        return {"betreff": "Initiativbewerbung", "text": "Haben Sie 15 Minuten?"}


def _netz_nacht(firmen_namen, ausfaelle=0):
    aufrufe = {"overpass": 0}

    def handler(anfrage):
        url = str(anfrage.url)
        if "nominatim" in url:
            return httpx.Response(200, json=[{"lat": "53", "lon": "8", "osm_type": "relation", "osm_id": 1}])
        if "interpreter" in url:
            aufrufe["overpass"] += 1
            if aufrufe["overpass"] <= ausfaelle:
                return httpx.Response(502)
            return httpx.Response(200, json={"elements": [
                {"type": "node", "id": i, "lat": 53, "lon": 8, "tags": {"name": n, "website": f"https://{n.lower()}.de/"}}
                for i, n in enumerate(firmen_namen)]})
        if "ohnemail" in url:
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>Nichts</body></html>")
        if url.rstrip("/").endswith(".de"):
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text=f"<html><body>Geschäftsführer: Kim Müller<br>info@{anfrage.url.host}</body></html>")
        if "impressum" in url and "ohnemail" not in url:
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text=f"<html><body>Geschäftsführer: Kim Müller<br>info@{anfrage.url.host}</body></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), aufrufe


def _bereit(conn):
    person.speichere(conn, name="Test Person", email="ich@example.org")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Baut Software")
    db.setze_einstellung(conn, "suche.begriffe", "Entwickler")
    db.setze_einstellung(conn, "suche.ort", "Bremen")
    db.setze_einstellung(conn, "suche.quellen", "verzeichnis")


def test_nachtlauf_sortiert_aus_berichtet_und_sendet_nichts(conn, monkeypatch):
    monkeypatch.setattr(versand, "sende_entwurf", lambda *a, **k: pytest.fail("Der Nachtlauf darf nicht senden"))
    _bereit(conn)
    client, _ = _netz_nacht(["Gut", "Schwach", "Ohnemail"])
    bericht = nacht.lauf(conn, "m", chat=_Nacht([80, 20]), client=client)
    assert not bericht["fehler"] and bericht["geprueft"] == 3 and bericht["ohne_adresse"] == 1 and bericht["zu_niedrig"] == 1
    assert [f["name"] for f in bericht["firmen"]] == ["Gut"] and bericht["firmen"][0]["entwurf_id"]
    assert [z[0] for z in conn.execute("SELECT name FROM firmen")] == ["Gut"]
    assert {z[0] for z in conn.execute("SELECT status FROM entwuerfe")} == {"entwurf"}
    assert nacht.letzter_bericht(conn)["firmen"][0]["name"] == "Gut"


def test_nachtlauf_prueft_aussortierte_nicht_noch_einmal(conn):
    _bereit(conn)
    client, _ = _netz_nacht(["Schwach"])
    nacht.lauf(conn, "m", chat=_Nacht([20]), client=client)
    client, _ = _netz_nacht(["Schwach"])
    assert nacht.lauf(conn, "m", chat=_Nacht([]), client=client)["geprueft"] == 0


def test_nachtlauf_wiederholt_bei_serverausfall(conn):
    _bereit(conn)
    client, _ = _netz_nacht(["Gut"], ausfaelle=6)
    pausen = []
    bericht = nacht.lauf(conn, "m", chat=_Nacht([80]), client=client, warte=pausen.append)
    assert pausen == [nacht.PAUSE] and [f["name"] for f in bericht["firmen"]] == ["Gut"]


def test_nachtlauf_schreibt_fehler_in_den_bericht(conn, monkeypatch):
    _bereit(conn)
    monkeypatch.setattr(suche, "suche_ueberall", lambda *a, **k: 1 / 0)
    assert "ZeroDivisionError" in nacht.lauf(conn, "m", client=_netz_nacht([])[0])["fehler"]
    assert "ZeroDivisionError" in nacht.letzter_bericht(conn)["fehler"]


def test_timer_nur_im_eigenen_ordner_und_mit_nachholen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    befehle = []
    nacht.einschalten(tmp_path / "meine db.sqlite", "03:00", run=lambda b, **k: befehle.append(b))
    timer = (tmp_path / "systemd/user/tueroeffner-nacht.timer").read_text()
    dienst = (tmp_path / "systemd/user/tueroeffner-nacht.service").read_text()
    assert "03:00:00" in timer and "Persistent=true" in timer and "'" in dienst and "--db" in dienst
    assert all(b[:2] == ["systemctl", "--user"] for b in befehle) and nacht.ist_eingeschaltet()
    nacht.ausschalten(run=lambda b, **k: befehle.append(b))
    assert not nacht.ist_eingeschaltet()


def test_demo_hat_keinen_nachtlauf(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(nacht.config, "DEMO", True)
    with pytest.raises(RuntimeError):
        nacht.einschalten("x.db", run=lambda *a, **k: pytest.fail("kein Aufruf"))


def test_arbeitnow_filtert_nach_begriff_und_ort():
    daten = {"data": [
        {"slug": "a", "company_name": "A GmbH", "title": "Softwareentwickler", "location": "Bremen", "tags": [], "url": "u"},
        {"slug": "b", "company_name": "B GmbH", "title": "Softwareentwickler", "location": "München", "remote": False, "tags": []},
        {"slug": "c", "company_name": "C GmbH", "title": "Koch", "location": "Bremen", "tags": []},
        {"slug": "d", "company_name": "D GmbH", "title": "Softwareentwickler", "location": "Köln", "remote": True, "tags": []},
    ]}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=daten)))
    assert [t["firma"] for t in quellen.stellenportal(["softwareentwickler"], "Bremen", client)] == ["A GmbH", "D GmbH"]


# --- Arbeitszeit ---

def test_jobboerse_filtert_und_zeigt_die_arbeitszeit(conn):
    antwort = {"ergebnisliste": [{"referenznummer": "R", "firma": "A GmbH", "stellenangebotsTitel": "Admin",
                                  "arbeitszeitVollzeit": True, "arbeitszeitTeilzeitFlexibel": True}]}
    client, anfragen = _api(antwort)
    treffer = suche.suche_ueberall(conn, suche.Auftrag("Admin", "Bremen", arbeitszeit="teilzeit", quellen=("jobboerse",)), client=client)["treffer"]
    assert anfragen[0].url.params["arbeitszeit"] == "tz" and treffer[0]["titel"] == "Admin · Vollzeit, Teilzeit"
    client, anfragen = _api(antwort)
    suche.suche("Admin", "Bremen", client=client)
    assert "arbeitszeit" not in anfragen[0].url.params


@pytest.mark.parametrize("zeit, erwartet", [("", ["V", "T", "O"]), ("vollzeit", ["V", "O"]), ("teilzeit", ["T", "O"])])
def test_arbeitnow_filtert_die_arbeitszeit_und_behaelt_anzeigen_ohne_angabe(zeit, erwartet):
    daten = {"data": [
        {"company_name": "V", "title": "Entwickler", "location": "Bremen", "job_types": ["Full Time"]},
        {"company_name": "T", "title": "Entwickler (Teilzeit)", "location": "Bremen", "job_types": []},
        {"company_name": "O", "title": "Entwickler", "location": "Bremen", "job_types": ["Experienced"]},
    ]}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=daten)))
    assert [t["firma"] for t in quellen.stellenportal(["entwickler"], "Bremen", client, zeit)] == erwartet


def test_firmenquellen_melden_dass_der_arbeitszeitfilter_dort_nicht_gilt(conn):
    client, _ = _firmen_netz()
    hinweise = suche.suche_ueberall(conn, suche.Auftrag("KI", "Bremen", arbeitszeit="vollzeit", quellen=("verzeichnis",)), client=client)["hinweise"]
    assert any("Arbeitszeit" in h for h in hinweise)


# --- Firmenseiten statt Stellenbörsen ---

def test_nachtlauf_nimmt_firmen_ohne_stellenboerse_zuerst(conn):
    _bereit(conn)
    db.setze_einstellung(conn, "suche.quellen", "verzeichnis,jobboerse")
    monkey = suche.quellen
    client, _ = _netz_nacht(["Direkt"])
    alt = nacht.MAX_PRUEFEN
    nacht.MAX_PRUEFEN = 1
    try:
        bericht = nacht.lauf(conn, "m", chat=_Nacht([80]), client=client)
    finally:
        nacht.MAX_PRUEFEN = alt
    assert monkey and bericht["geprueft"] == 1 and bericht["firmen"][0]["name"] == "Direkt" and bericht["firmen"][0]["direkt"]


def test_nachtlauf_laesst_nicht_bewertete_firmen_fuer_die_naechste_nacht_liegen(conn):
    _bereit(conn)
    alt = nacht.MAX_BEWERTEN
    nacht.MAX_BEWERTEN = 1
    try:
        client, _ = _netz_nacht(["Eins", "Zwei"])
        nacht.lauf(conn, "m", chat=_Nacht([80]), client=client)
        assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 1
        client, _ = _netz_nacht(["Eins", "Zwei"])
        assert nacht.lauf(conn, "m", chat=_Nacht([80]), client=client)["geprueft"] == 1
    finally:
        nacht.MAX_BEWERTEN = alt
    assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 2


def test_offene_stellen_von_der_eigenen_seite_und_inhaltsnaehe(conn):
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Softwareentwickler Python Linux")
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    assert firmen.inhaltsnaehe(conn, fid) is None
    rag.speichere(conn, "firma", ["Wir suchen:\nSoftwareentwickler Python (m/w/d)\nVertrieb Innendienst (w/m/d)\nWir sind ein Team."],
                  firma_id=fid, quellen=["https://firma-a.de/karriere"], abruf=db.jetzt())
    assert firmen.offene_stellen(conn, fid) == ["Softwareentwickler Python (m/w/d)", "Vertrieb Innendienst (w/m/d)"]
    assert firmen.inhaltsnaehe(conn, fid) > 0


def test_stellenanzeige_aus_der_boerse_zaehlt_nicht_als_eigene_seite(conn):
    fid = firmen.lege_an(conn, "Firma A GmbH")
    rag.speichere(conn, "firma", ["Admin (m/w/d)"], firma_id=fid, quellen=["https://www.arbeitsagentur.de/x"], abruf=db.jetzt())
    assert firmen.offene_stellen(conn, fid) == []


# --- Ausfälle ohne Fehlermeldung ---

def test_verzeichnis_kommt_aus_dem_speicher_und_uebersteht_den_ausfall(conn, monkeypatch):
    auftrag = lambda: suche.Auftrag("KI", "Bremen", quellen=("verzeichnis",))
    client, anfragen = _firmen_netz(overpass=OSM)
    assert suche.suche_ueberall(conn, auftrag(), client=client)["treffer"]
    zahl = len(anfragen)
    assert suche.suche_ueberall(conn, auftrag(), client=client)["treffer"][0]["firma"] == "Plutex GmbH" and len(anfragen) == zahl
    monkeypatch.setattr(suche, "VERZEICHNIS_FRISCH", suche.timedelta(0))
    client, _ = _firmen_netz(overpass_code=502)
    ergebnis = suche.suche_ueberall(conn, auftrag(), client=client)
    assert ergebnis["treffer"][0]["firma"] == "Plutex GmbH" and "überlastet" in ergebnis["hinweise"][0]


def test_zweite_runde_rettet_wenn_alle_server_kurz_ausfallen():
    aufrufe = []

    def handler(anfrage):
        aufrufe.append(1)
        return httpx.Response(504) if len(aufrufe) <= len(quellen.OVERPASS) else httpx.Response(200, json=OSM)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert quellen.verzeichnis(quellen.Gebiet("B", "de", 53, 8), 25, "it", client)


def test_internet_ohne_adresse_und_leere_berufe_sind_kein_fehler(conn):
    client, anfragen = _firmen_netz()
    ergebnis = suche.suche_ueberall(conn, suche.Auftrag("", "Bremen", quellen=("internet", "jobboerse", "arbeitnow")), client=client)
    assert ergebnis["treffer"] == [] and not anfragen
    assert len(ergebnis["hinweise"]) == 1 and "Passende Firmen finden" in ergebnis["hinweise"][0]
    db.setze_einstellung(conn, "suche.abgeleitet", "Entwickler")
    client, anfragen = _api()
    suche.suche_ueberall(conn, suche.Auftrag("", "Bremen", quellen=("jobboerse",)), client=client)
    assert anfragen[0].url.params["was"] == "Entwickler"


# --- Ansprechperson aus der Stellenanzeige ---

ANZEIGE_TEXT = ("# IT-Servicetechniker\n\nWir freuen uns auf Deine Bewerbung! Für Fragen steht Dir Jolyne Donkor unter "
                "jolyne.donkor@wiit.cloud gern zur Verfügung.")


def _netz_anzeige(text=ANZEIGE_TEXT, seiten=None):
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        url = str(anfrage.url)
        if "jobdetails" in url:
            return httpx.Response(200, json={"stellenangebotsBeschreibung": text}) if text is not None else httpx.Response(500)
        if "nominatim" in url:
            return httpx.Response(200, json=[])
        for pfad, html in (seiten or {}).items():
            if url.rstrip("/").endswith(pfad):
                return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), anfragen


STELLE_BA = {"referenz": "10001-1003900685-S", "firma": "myLoc managed IT AG", "titel": "IT-Servicetechniker", "beruf": "",
             "ort": "Nordwalde", "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003900685-S", "website": "",
             "quelle": "Jobbörse"}


def test_anzeigentext_wird_geladen_und_gespeichert(conn):
    client, anfragen = _netz_anzeige()
    fid = suche.uebernimm(conn, STELLE_BA, client)
    assert anfragen[0].url.path.endswith("/jobdetails/MTAwMDEtMTAwMzkwMDY4NS1T")
    assert anfragen[0].headers["X-API-Key"] == "jobboerse-jobsuche"
    texte = " ".join(z[0] for z in conn.execute("SELECT text FROM chunks WHERE firma_id = ?", (fid,)))
    assert "jolyne.donkor@wiit.cloud" in texte and "IT-Servicetechniker" in texte


def test_ausfall_der_anzeige_laesst_nur_den_titel(conn):
    client, _ = _netz_anzeige(text=None)
    fid = suche.uebernimm(conn, STELLE_BA, client)
    assert [z[0][:15] for z in conn.execute("SELECT text FROM chunks WHERE firma_id = ?", (fid,))] == ["Stellenangebot:"]


def test_ansprechperson_aus_anzeige_wenn_die_website_keine_adresse_nennt(conn):
    impressum = "<html><body><h1>Impressum</h1>Vorstand: Alessandro Cozzi<br>E-Mail: siehe Kontaktformular</body></html>"
    start = '<html><body><a href="/impressum">Impressum</a></body></html>'
    client, _ = _netz_anzeige(seiten={"myloc.de": start, "/impressum": impressum})
    ergebnis = suche.uebernimm_und_finde(conn, {**STELLE_BA, "website": "https://myloc.de/"}, client=client)
    assert ergebnis["email"] == "jolyne.donkor@wiit.cloud" and ergebnis["art"] == "persönlich"
    assert ergebnis["personen"].startswith("Jolyne Donkor")


def test_ansprechperson_aus_anzeige_ohne_website(conn):
    client, _ = _netz_anzeige()
    ergebnis = suche.uebernimm_und_finde(conn, STELLE_BA, client=client)
    assert ergebnis["email"] == "jolyne.donkor@wiit.cloud" and "fehler" not in ergebnis


def test_aeltere_firma_laedt_ihre_anzeige_beim_inhaber_finden_nach(conn):
    fid = firmen.lege_an(conn, "myLoc managed IT AG", quelle="Jobbörse 10001-1003900685-S")
    client, _ = _netz_anzeige()
    assert suche.hole_inhaber(conn, fid, client=client)["email"] == "jolyne.donkor@wiit.cloud"
    client, anfragen = _netz_anzeige()
    assert not suche.lade_anzeige_nach(conn, fid, client) and not anfragen


def test_persoenliche_adresse_der_geschaeftsfuehrung_geht_vor(conn):
    client, _ = _netz_anzeige()
    fid = suche.uebernimm(conn, STELLE_BA, client)
    firmen.aendere(conn, fid, email="a.cozzi@myloc.de", personen="Alessandro Cozzi")
    assert not suche.kontakt_aus_anzeige(conn, fid)
    assert conn.execute("SELECT email FROM firmen WHERE id = ?", (fid,)).fetchone()[0] == "a.cozzi@myloc.de"


def test_allgemeine_adresse_aus_der_anzeige_bekommt_keinen_namen(conn):
    client, _ = _netz_anzeige("Bewerbungen an jobs@wiit.cloud, Ansprechpartner Kim Test.")
    fid = suche.uebernimm(conn, STELLE_BA, client)
    firmen.aendere(conn, fid, personen="Alessandro Cozzi")
    assert suche.kontakt_aus_anzeige(conn, fid)
    assert tuple(conn.execute("SELECT email, personen FROM firmen WHERE id = ?", (fid,)).fetchone()) == ("jobs@wiit.cloud", "Alessandro Cozzi")
    firmen.aendere(conn, fid, email="info@myloc.de")
    assert not suche.kontakt_aus_anzeige(conn, fid)


def test_arbeitnow_beschreibung_wird_gespeichert(conn):
    stelle = {"referenz": "arbeitnow:x", "firma": "A GmbH", "titel": "Dev", "url": "https://www.arbeitnow.com/jobs/x", "website": "",
              "quelle": "Arbeitnow", "beschreibung": "&lt;p&gt;Fragen an Kim Test: kim.test@a.de&lt;/p&gt;"}
    fid = suche.uebernimm(conn, stelle, httpx.Client(transport=httpx.MockTransport(lambda a: pytest.fail("kein Abruf"))))
    assert suche.kontakt_aus_anzeige(conn, fid)
    assert conn.execute("SELECT personen FROM firmen WHERE id = ?", (fid,)).fetchone()[0] == "Kim Test"


# --- Umkreis der Internetsuche ---

def test_internettreffer_zeigt_beschreibung_der_startseite_statt_impressum(conn):
    client, _ = _netz_mit_sitz('<meta name="description" content="Weniger IT-Aufwand für Ihr Geschäft.">Weg 1<br>28195 Bremen',
                               {"28195": (53.08, 8.81)})
    assert suche.suche_ueberall(conn, _internet(), client=client)["treffer"][0]["titel"] == "Weniger IT-Aufwand für Ihr Geschäft."
    client, _ = _netz_mit_sitz("")
    assert suche.suche_ueberall(conn, _internet(), client=client)["treffer"][0]["titel"] == "Weniger IT-Aufwand für Ihr Geschäft."


def _netz_mit_sitz(seite_a, plz=None):
    """Bremen ist der Suchort. Firma A steht auf ihrer Seite mit `seite_a`, `plz` liefert die Koordinaten dazu."""
    anfragen = []
    bremen = {"lat": "53.07", "lon": "8.80", "osm_type": "relation", "osm_id": 62559, "boundingbox": ["53.0", "53.6", "8.4", "9.0"]}

    def handler(anfrage):
        anfragen.append(anfrage)
        url = str(anfrage.url)
        if "nominatim" in url:
            code = anfrage.url.params.get("postalcode")
            if code:
                return httpx.Response(200, json=[{"lat": str(plz[code][0]), "lon": str(plz[code][1])}] if code in (plz or {}) else [])
            return httpx.Response(200, json=[bremen])
        if "/search" in url:
            return httpx.Response(200, json={"results": [{"url": "https://www.firma-a.de/impressum", "title": "Firma A GmbH", "content": "Software"}]})
        if url.rstrip("/").endswith("firma-a.de") and seite_a:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=f"<html><body>{seite_a}</body></html>")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), anfragen


def _internet(umkreis=50, ort="Bremen"):
    return suche.Auftrag("KI", ort, umkreis=umkreis, quellen=("internet",), searxng="http://s")


def test_internettreffer_ausserhalb_des_umkreises_fliegt_raus(conn):
    client, _ = _netz_mit_sitz("Musterweg 1<br>70173 Stuttgart", {"70173": (48.78, 9.18)})
    ergebnis = suche.suche_ueberall(conn, _internet(), client=client)
    assert ergebnis["treffer"] == [] and "1 außerhalb des Gebiets" in ergebnis["hinweise"][0]


def test_internettreffer_im_umkreis_bekommt_ort_und_entfernung(conn):
    client, _ = _netz_mit_sitz("Musterweg 1<br>28195 Bremen", {"28195": (53.08, 8.81)})
    ergebnis = suche.suche_ueberall(conn, _internet(), client=client)
    assert [(t["firma"], t["ort"], t["entfernung"]) for t in ergebnis["treffer"]] == [("Firma A GmbH", "Bremen", 1)]
    assert ergebnis["hinweise"] == []


def test_internettreffer_ohne_lesbare_adresse_fliegt_raus(conn):
    for seite in ("Keine Adresse hier", ""):
        client, _ = _netz_mit_sitz(seite)
        ergebnis = suche.suche_ueberall(conn, _internet(), client=client)
        assert ergebnis["treffer"] == [] and "1 ohne lesbare Adresse" in ergebnis["hinweise"][0]


def test_region_gilt_ueber_ihr_rechteck_und_ohne_ort_wird_nicht_gefiltert(conn):
    client, _ = _netz_mit_sitz("Musterweg 1<br>28790 Schwanewede", {"28790": (53.23, 8.6)})
    assert suche.suche_ueberall(conn, _internet(umkreis=0), client=client)["treffer"]
    client, _ = _netz_mit_sitz("Musterweg 1<br>70173 Stuttgart", {"70173": (48.78, 9.18)})
    conn.execute("DELETE FROM einstellungen WHERE key LIKE 'cache.%'")
    assert not suche.suche_ueberall(conn, _internet(umkreis=0), client=client)["treffer"]
    client, anfragen = _netz_mit_sitz("")
    assert suche.suche_ueberall(conn, _internet(ort=""), client=client)["treffer"]
    assert not any("firma-a.de" in str(a.url) for a in anfragen)


def test_ausfall_des_ortsdienstes_zeigt_keine_ungeprueften_treffer(conn):
    client = httpx.Client(transport=httpx.MockTransport(lambda anfrage: httpx.Response(503) if "nominatim" in str(anfrage.url) else httpx.Response(
        200, json={"results": [{"url": "https://firma-a.de/", "title": "Firma A GmbH"}]})))
    ergebnis = suche.suche_ueberall(conn, _internet(), client=client)
    assert ergebnis["treffer"] == [] and "Ortsdienst" in ergebnis["hinweise"][0]


def test_sitz_wird_gemerkt_aber_eine_ausgefallene_seite_nicht(conn):
    client, anfragen = _netz_mit_sitz("Musterweg 1<br>28195 Bremen", {"28195": (53.08, 8.81)})
    suche.suche_ueberall(conn, _internet(), client=client)
    client, anfragen = _netz_mit_sitz("Musterweg 1<br>28195 Bremen", {"28195": (53.08, 8.81)})
    assert suche.suche_ueberall(conn, _internet(), client=client)["treffer"]
    assert not any("firma-a.de" in str(a.url) or "postalcode" in str(a.url) for a in anfragen)
    conn.execute("DELETE FROM einstellungen WHERE key LIKE 'cache.%'")
    client, _ = _netz_mit_sitz("")
    suche.suche_ueberall(conn, _internet(), client=client)
    assert db.einstellung(conn, "cache.sitz.de.firma-a.de", "") == ""


@pytest.mark.parametrize("text,land,erwartet", [
    ("Musterstr. 5\n41372 Niederkrüchten", "de", ("41372", "Niederkrüchten")),
    ("Musterstr. 5, D-41372 Niederkrüchten", "de", ("41372", "Niederkrüchten")),
    ("60311 Frankfurt am Main", "de", ("60311", "Frankfurt am Main")),
    ("33758 Schloß Holte-Stukenbrock", "de", ("33758", "Schloß Holte-Stukenbrock")),
    ("76829 Landau in der Pfalz", "de", ("76829", "Landau in der Pfalz")),
    ("HRB 12345\nAmtsgericht Düsseldorf\nWeg 1\n40210 Düsseldorf", "de", ("40210", "Düsseldorf")),
    ("Handelsregister: 12345 Köln", "de", ("", "")),
    ("Tel.: 02163 12345 Mönchengladbach", "de", ("", "")),
    ("Seit 2015 Marktführer", "de", ("", "")),
    ("Musterweg 1, 1010 Wien", "at", ("1010", "Wien")),
    ("Weg 1, 5914 AB Venlo", "nl", ("5914 AB", "Venlo")),
])
def test_sitz_aus_impressum(text, land, erwartet):
    assert quellen.sitz_aus_text(text, land) == erwartet


# --- Vorschau ---

def test_vorschau_zeigt_firma_ohne_sie_anzulegen(conn):
    html_start = '<html><head><meta name="description" content="Wir bauen Software in Bremen."></head><body>' \
                 '<a href="/impressum">Impressum</a><a href="/karriere">Karriere</a></body></html>'
    client, _ = _firmen_netz({"firma-a.de": html_start, "/impressum": IMPRESSUM,
                              "/karriere": "<html><body>Softwareentwickler Python (m/w/d)</body></html>"})
    treffer = {"firma": "Firma A GmbH", "ort": "Bremen", "website": "https://firma-a.de/"}
    v = suche.vorschau(treffer, client=client)
    assert v["personen"] == "Kim Müller" and v["email"] == "info@firma-a.de" and v["art"] == "allgemein"
    assert v["kurztext"] == "Wir bauen Software in Bremen." and v["stellen"] == ["Softwareentwickler Python (m/w/d)"]
    assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 0


def test_vorschau_meldet_ausfall_statt_fehler():
    client, _ = _firmen_netz()
    assert "nicht erreichbar" in suche.vorschau({"firma": "Tot", "ort": "", "website": "https://tot.de/"}, client=client)["problem"]
    client, _ = _firmen_netz(nominatim=[])
    assert suche.vorschau({"firma": "Unbekannt GmbH", "ort": "", "website": ""}, client=client)["problem"] == "Keine Website gefunden."


# --- Website aus dem Firmennamen ---

def test_domainkandidaten_aus_dem_namen():
    assert quellen.domain_kandidaten("EPLAN GmbH & Co. KG") == ["https://www.eplan.de/", "https://www.eplan.com/"]
    kandidaten = quellen.domain_kandidaten("TETA Automation GmbH")
    assert "https://www.tetaautomation.de/" in kandidaten and "https://www.teta-automation.de/" in kandidaten and "https://www.teta.de/" in kandidaten
    assert quellen.domain_kandidaten("Müller Bau") [0] == "https://www.muellerbau.de/"


def test_name_muss_auf_der_seite_stehen():
    assert quellen.name_passt("EPLAN GmbH", "Willkommen bei Eplan")
    assert not quellen.name_passt("EPLAN GmbH", "Diese Domain steht zum Verkauf")
    assert quellen.name_passt("SysTec Systemtechnik", "SysTec Systemtechnik und mehr")
    assert not quellen.name_passt("SysTec Systemtechnik", "Systec Elektronik und Software")


def test_vorschau_findet_website_ueber_den_namen_nur_wenn_er_dort_steht():
    def handler(anfrage):
        url = str(anfrage.url)
        if "nominatim" in url:
            return httpx.Response(200, json=[])
        if anfrage.url.host == "www.acme.de" and anfrage.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>Die Acme GmbH. Geschäftsführer: Kim Müller, info@acme.de</body></html>")
        if anfrage.url.host == "www.anders.de":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>Domain zu verkaufen</body></html>")
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    v = suche.vorschau({"firma": "Acme GmbH", "ort": ""}, client=client)
    assert v["website"] == "https://www.acme.de/" and v["geraten"] and v["email"] == "info@acme.de"
    assert suche.vorschau({"firma": "Anders GmbH", "ort": ""}, client=client)["problem"] == "Keine Website gefunden."


@pytest.mark.parametrize("text", ["Social Media Kanäle\nGeschäftsführer", "Unsere Expert\nGeschäftsführung"])
def test_floskeln_gelten_nicht_als_inhaber(text):
    assert firmen._name_aus_teamseite(text) == ""


def test_nebenadressen_kommen_zuletzt():
    text = "jugendschutz@firma.de presse@firma.de kontakt@firma.de"
    assert firmen.lies_kontakt(text, "firma.de")["email"] == "kontakt@firma.de"


def test_register_und_dokumente_sind_keine_firmen():
    for url in ("https://www.companyhouse.de/x", "https://www.landtag.nrw.de/portal/a", "https://firma.de/flyer.PDF"):
        assert quellen.ist_portal(url)
    assert not quellen.ist_portal("https://www.ki-werke-bremen.de/de/impressum/")


def test_vermittler_fliegen_auch_aus_den_anderen_quellen():
    treffer = {"firma": "Headhunter für KI", "quelle": "Internet", "website": ""}
    assert suche._vereine([[treffer]]) == []


# --- Anzeigen der Firma, Karriereadresse, Mittelstand ---

def _netz_firmenanzeigen(liste, details=ANZEIGE_TEXT, seiten=None):
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        url = str(anfrage.url)
        if "firmenanzeigen" in url:
            return httpx.Response(200, json={"ergebnisliste": liste})
        if "jobdetails" in url:
            return httpx.Response(200, json={"stellenangebotsBeschreibung": details})
        if "nominatim" in url:
            return httpx.Response(200, json=[])
        for pfad, html in (seiten or {}).items():
            if url.rstrip("/").endswith(pfad):
                return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), anfragen


def test_anzeigen_der_firma_prueft_den_arbeitgeber_selbst():
    liste = [{"referenznummer": "1", "firma": "myLoc managed IT AG"}, {"referenznummer": "2", "firma": "Andere GmbH"}]
    client, anfragen = _netz_firmenanzeigen(liste)
    assert suche.anzeigen_der_firma("myLoc managed IT AG", client) == (["1"], 1)
    assert anfragen[0].url.params["was"] == "myLoc managed IT"


def test_inhaber_finden_holt_ansprechperson_aus_laufender_anzeige(conn):
    start = '<html><body><a href="/impressum">Impressum</a></body></html>'
    impressum = "<html><body><h1>Impressum</h1>Vorstand: Alessandro Cozzi</body></html>"
    client, anfragen = _netz_firmenanzeigen([{"referenznummer": f"R{i}", "firma": "myLoc managed IT AG"} for i in range(5)],
                                            seiten={"myloc.de": start, "/impressum": impressum})
    fid = firmen.lege_an(conn, "myLoc managed IT AG", website="https://myloc.de/")
    ergebnis = suche.hole_inhaber(conn, fid, client=client)
    assert ergebnis["email"] == "jolyne.donkor@wiit.cloud" and ergebnis["personen"].startswith("Jolyne Donkor")
    assert sum("jobdetails" in str(a.url) for a in anfragen) == suche.MAX_ANZEIGEN_LADEN
    assert "Rechtsform AG" in ergebnis["konzern"]


def test_persoenliche_adresse_spart_die_anzeigensuche(conn):
    impressum = "<html><body><h1>Impressum</h1>Geschäftsführer: Kim Müller<br>k.mueller@firma-a.de</body></html>"
    client, anfragen = _netz_firmenanzeigen([], seiten={"firma-a.de": '<a href="/impressum">Impressum</a>', "/impressum": impressum})
    fid = firmen.lege_an(conn, "Firma A GmbH", website="https://firma-a.de/")
    assert suche.hole_inhaber(conn, fid, client=client)["email"] == "k.mueller@firma-a.de"
    assert not any("firmenanzeigen" in str(a.url) for a in anfragen)


def test_karriereadresse_der_eigenen_domain_wenn_das_impressum_keine_nennt():
    impressum = "Impressum\nGeschäftsführer: Kim Müller"
    alles = impressum + "\nKarriere: jobs@firma-a.de\nDatenschutz: datenschutz@firma-a.de\nPartner: info@fremd.de"
    assert firmen.kontakt_aus_texten(impressum, alles, "firma-a.de")["email"] == "jobs@firma-a.de"
    assert firmen.kontakt_aus_texten(impressum, impressum + "\ninfo@fremd.de", "firma-a.de")["email"] == ""


@pytest.mark.parametrize("name,impressum,anzeigen,erwartet", [
    ("Siegers IT GmbH", "Geschäftsführer: Max Siegers", 1, []),
    ("myLoc managed IT AG", "", 0, ["Rechtsform AG"]),
    ("Muster GmbH", "Die Muster GmbH ist ein Unternehmen der Beispiel Gruppe.", 0, ["gehört zu einer Gruppe"]),
    ("Muster GmbH", "Vorsitzender des Aufsichtsrats: Kim Test", 0, ["Aufsichtsrat im Impressum"]),
    ("Muster Holding GmbH", "", 12, ["Gruppe oder Holding im Namen", "12 laufende Anzeigen"]),
    ("Ageras GmbH", "", 0, []),
])
def test_konzernmerkmale(name, impressum, anzeigen, erwartet):
    assert firmen.konzern_merkmale(name, impressum, anzeigen) == erwartet


def test_mittelstand_hat_vorrang_konzerne_bleiben_in_der_liste(conn):
    stellen = [{"referenznummer": f"K{i}", "firma": "Gross AG", "stellenangebotsTitel": "Entwickler"} for i in range(2)]
    stellen.append({"referenznummer": "M1", "firma": "Klein GmbH", "stellenangebotsTitel": "Entwickler"})
    client = httpx.Client(transport=httpx.MockTransport(lambda a: httpx.Response(200, json={"ergebnisliste": stellen})))
    ergebnis = suche.suche_ueberall(conn, suche.Auftrag("Entwickler", "Bremen", quellen=("jobboerse",)), client=client)
    assert [(t["firma"], bool(t["konzern"])) for t in ergebnis["treffer"]] == [("Klein GmbH", False), ("Gross AG", True)]


def test_viele_anzeigen_in_einer_suche_deuten_auf_konzern():
    treffer = [{"firma": "Viel GmbH", "quelle": "Jobbörse", "website": ""} for _ in range(3)]
    assert suche._vereine([treffer])[0]["konzern"] == "3 Anzeigen in dieser Suche"


def test_nachtlauf_nimmt_mittelstand_vor_konzern(conn):
    _bereit(conn)
    elemente = [{"type": "node", "id": i, "lat": 53, "lon": 8, "tags": {"name": n, "website": f"https://{n.lower()}.de/"}}
                for i, n in enumerate(["Tochter", "Klein"])]

    def handler(anfrage):
        url = str(anfrage.url)
        if "nominatim" in url:
            return httpx.Response(200, json=[{"lat": "53", "lon": "8", "osm_type": "relation", "osm_id": 1}])
        if "interpreter" in url:
            return httpx.Response(200, json={"elements": elemente})
        if url.rstrip("/").endswith(".de"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<a href="/impressum">Impressum</a>')
        if "impressum" in url:
            zusatz = "Aufsichtsrat: Kim Test" if "tochter" in url else ""
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text=f"<html><body>Geschäftsführer: Kim Müller<br>info@{anfrage.url.host}<br>{zusatz}</body></html>")
        return httpx.Response(404)
    alt = nacht.MAX_BEWERTEN
    nacht.MAX_BEWERTEN = 1
    try:
        bericht = nacht.lauf(conn, "m", chat=_Nacht([80]), client=httpx.Client(transport=httpx.MockTransport(handler)))
    finally:
        nacht.MAX_BEWERTEN = alt
    assert [f["name"] for f in bericht["firmen"]] == ["Klein"]


# --- Eignung und Sofort bewerben ---

class _Eignung:
    """Bewertet jede Anforderung nach einer festen Tabelle {Anforderung: (Bewertung, Beleg)}, Unbekanntes fehlt.
    Für Anzeigen ohne Aufzählung liefert es `liste`, auf den Entwurf eine kurze Mail."""
    def __init__(self, bewertungen, liste=()):
        self.bewertungen, self.liste, self.prompts = bewertungen, list(liste), []

    def __call__(self, modell, system, nutzer, schema, temperatur=0.3):
        self.prompts.append((system, nutzer))
        if "bewertung" in schema["properties"]:
            punkt = re.search(r"<anforderung>\n(.*)\n</anforderung>", nutzer, re.S).group(1)
            bewertung, beleg, *zitat = self.bewertungen.get(punkt, ("fehlt", 0))
            return {"bewertung": bewertung, "beleg": beleg, "zitat": zitat[0] if zitat else ""}
        if "anforderungen" in schema["properties"]:
            return {"anforderungen": self.liste}
        return {"betreff": "Anfrage", "text": "Haben Sie 15 Minuten für ein Gespräch?"}


def _mit_unterlagen(conn, tmp_path):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF")
    (tmp_path / "notiz.pdf").write_bytes(b"%PDF")
    cv = profil.fuege_unterlage_hinzu(conn, tmp_path / "cv.pdf", "lebenslauf", True)
    rag.speichere(conn, "dokument", ["Linux-Administration und Python-Entwicklung seit 2015"], dokument_id=cv, quellen=["cv.pdf"])
    sonst = profil.fuege_unterlage_hinzu(conn, tmp_path / "notiz.pdf", "sonstiges", False)
    rag.speichere(conn, "dokument", ["Gehaltsvorstellung Linux Python 99.000"], dokument_id=sonst, quellen=["notiz.pdf"])
    return conn.execute("SELECT id FROM chunks WHERE dokument_id = ?", (cv,)).fetchone()[0]


def test_eignung_zaehlt_nur_belegtes_und_rechnet_selbst(conn, tmp_path):
    beleg = _mit_unterlagen(conn, tmp_path)
    client, _ = _netz_anzeige("## Deine Aufgaben\n- Server betreuen\n\n## Dein Profil\n- Linux\n- Python\n- SAP\n"
                              "- Docker von Vorteil\n- Ansible\n- Reisebereitschaft und Führerschein Klasse B.\n\n"
                              "## Wir bieten\n- Obstkorb")
    fid = suche.uebernimm(conn, STELLE_BA, client)
    chat = _Eignung({"Linux": ("erfuellt", beleg), "Python": ("teilweise", beleg), "SAP": ("erfuellt", 0),
                     "Ansible": ("erfuellt", 99999)})
    eignung = ki.pruefe_eignung(conn, fid, "m", chat)
    assert eignung["erfuellt"] == ["Linux"] and eignung["teilweise"] == ["Python"]
    assert eignung["fehlt"] == ["SAP", "Docker von Vorteil", "Ansible"]  # ohne gültigen Beleg zählt nichts
    assert eignung["selbst"] == ["Reisebereitschaft und Führerschein Klasse B."]
    assert eignung["score"] == 33  # (1 + 0,5) von 4,5: „von Vorteil“ zählt halb, Führerschein gar nicht
    assert len(chat.prompts) == 5 and not any("Obstkorb" in n or "Server betreuen" in n for _, n in chat.prompts)
    assert not any("Gehalt" in n or "99.000" in n for _, n in chat.prompts)
    assert any("„SAP“ ist nicht belegt" in v for v in eignung["cv"]) and not any("Docker" in v for v in eignung["cv"])
    assert json.loads(conn.execute("SELECT ki_eignung FROM firmen WHERE id = ?", (fid,)).fetchone()[0])["score"] == 33


def test_eignung_schlaegt_zeugnis_inhalte_fuer_den_cv_vor(conn, tmp_path):
    (tmp_path / "z.pdf").write_bytes(b"%PDF")
    z = profil.fuege_unterlage_hinzu(conn, tmp_path / "z.pdf", "zeugnis", True)
    rag.speichere(conn, "dokument", ["Field Service Technician, Deskside Support"], dokument_id=z, quellen=["z.pdf"])
    beleg = conn.execute("SELECT id FROM chunks WHERE dokument_id = ?", (z,)).fetchone()[0]
    client, _ = _netz_anzeige("Dein Profil:\n• Erfahrung im Field Service")
    fid = suche.uebernimm(conn, STELLE_BA, client)
    eignung = ki.pruefe_eignung(conn, fid, "m", _Eignung({"Erfahrung im Field Service": ("erfuellt", beleg)}))
    assert eignung["score"] == 100 and "steht im Zeugnis" in eignung["cv"][0]
    assert "Für diese Stelle im CV" in ki.eignung_text(eignung)


def test_lange_anzeige_kommt_ohne_doppelte_ueberlappung_zurueck(conn, tmp_path):
    punkte = [f"- Anforderung Nummer {i} mit etwas mehr Text, damit der Absatz lang wird" for i in range(20)]
    client, _ = _netz_anzeige("## Dein Profil\n" + "\n".join(punkte) + "\n\n## Wir bieten\n- Obstkorb")
    fid = suche.uebernimm(conn, STELLE_BA, client)
    text = ki.anzeige_der_firma(conn, fid)[0]
    assert all(text.count(p[2:]) == 1 for p in punkte)
    assert len(ki.anforderungen_aus_liste(ki._anforderungsteil(text))) == ki.MAX_ANFORDERUNGEN


def test_eignung_ohne_aufzaehlung_fragt_das_modell_nach_den_anforderungen(conn, tmp_path):
    beleg = _mit_unterlagen(conn, tmp_path)
    client, _ = _netz_anzeige("Wir suchen jemanden mit Linux-Erfahrung und Freude am Lernen.")
    fid = suche.uebernimm(conn, STELLE_BA, client)
    eignung = ki.pruefe_eignung(conn, fid, "m", _Eignung({"Linux": ("erfuellt", beleg)}, liste=["Linux", "Linux", "Neugier"]))
    assert eignung["erfuellt"] == ["Linux"] and eignung["fehlt"] == ["Neugier"] and eignung["score"] == 50


def test_eignung_ohne_anzeige_oder_unterlagen_ist_nicht_moeglich(conn, tmp_path):
    fid = firmen.lege_an(conn, "Ohne Anzeige GmbH")
    with pytest.raises(ki.NichtMoeglich, match="keine Stellenanzeige"):
        ki.pruefe_eignung(conn, fid, "m", _Eignung({}))
    client, _ = _netz_anzeige()
    fid = suche.uebernimm(conn, STELLE_BA, client)
    with pytest.raises(ki.NichtMoeglich, match="Lebenslauf oder Zeugnis"):
        ki.pruefe_eignung(conn, fid, "m", _Eignung({}))


def _bereit_fuer_mail(conn, tmp_path):
    person.speichere(conn, name="Test Person", email="ich@example.org")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Linux und Python seit 2015")
    return _mit_unterlagen(conn, tmp_path)


def test_sofort_bewerben_legt_entwurf_an_und_sendet_nichts(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(versand, "sende_entwurf", lambda *a, **k: pytest.fail("Sofort bewerben darf nicht senden"))
    beleg = _bereit_fuer_mail(conn, tmp_path)
    client, _ = _netz_anzeige()
    ergebnis = suche.sofort_bewerben(conn, STELLE_BA, "m", chat=_Eignung({"Linux": ("erfuellt", beleg)}, liste=["Linux"]),
                                     client=client)
    assert ergebnis["eignung"]["score"] == 100 and ergebnis["entwurf_id"] and ergebnis["anhaenge"] == ["cv.pdf"]
    e = conn.execute("SELECT empfaenger, status FROM entwuerfe WHERE id = ?", (ergebnis["entwurf_id"],)).fetchone()
    assert tuple(e) == ("jolyne.donkor@wiit.cloud", "entwurf")


def test_sofort_bewerben_schreibt_bei_geringer_eignung_keine_mail(conn, tmp_path):
    _bereit_fuer_mail(conn, tmp_path)
    client, _ = _netz_anzeige()
    ergebnis = suche.sofort_bewerben(conn, STELLE_BA, "m", chat=_Eignung({}, liste=["SAP"]),
                                     client=client)
    assert ergebnis["eignung"]["score"] == 0 and not ergebnis["entwurf_id"] and "keine Mail" in ergebnis["hinweis"]
    assert not conn.execute("SELECT count(*) FROM entwuerfe").fetchone()[0]


def test_sofort_bewerben_ohne_adresse_bricht_ab_und_ohne_anzeige_wird_initiativ(conn, tmp_path):
    _bereit_fuer_mail(conn, tmp_path)
    client, _ = _netz_anzeige("Bewerbung bitte über unser Portal.")
    with pytest.raises(ki.NichtMoeglich, match="Keine veröffentlichte Mailadresse"):
        suche.sofort_bewerben(conn, STELLE_BA, "m", chat=_Eignung({}), client=client)
    fid = firmen.lege_an(conn, "Initiativ GmbH", email="info@initiativ.de")
    firmen.aendere(conn, fid, seiten_geladen=db.jetzt())
    treffer = {"referenz": "web:initiativ.de", "firma": "Initiativ GmbH", "titel": "IT", "website": "", "quelle": "Internet"}
    ergebnis = suche.sofort_bewerben(conn, treffer, "m", chat=_Eignung({}), client=_netz_anzeige()[0])
    assert ergebnis["eignung"] is None and "keine Stellenanzeige" in ergebnis["hinweis"] and ergebnis["entwurf_id"]


def test_eignung_laedt_die_anzeige_einer_aelteren_firma_nach(conn, tmp_path):
    beleg = _mit_unterlagen(conn, tmp_path)
    fid = firmen.lege_an(conn, "myLoc managed IT AG", quelle="Jobbörse 10001-1003900685-S")
    client, _ = _netz_anzeige("## Dein Profil\n- Linux")
    eignung = suche.pruefe_eignung(conn, fid, "m", _Eignung({"Linux": ("erfuellt", beleg)}), client)
    assert eignung["score"] == 100
    fid2 = firmen.lege_an(conn, "Andere AG")
    client, _ = _netz_firmenanzeigen([{"referenznummer": "X1", "firma": "Andere AG"}], details="## Profil\n- Linux")
    assert suche.pruefe_eignung(conn, fid2, "m", _Eignung({"Linux": ("erfuellt", beleg)}), client)["score"] == 100


# --- Lebenslauf für die Stelle ---

def _echtes_pdf(pfad, text):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(pfad))
    for seite in text.split("|"):
        c.drawString(100, 750, seite)
        c.showPage()
    c.save()
    return pfad


def test_zitat_zaehlt_nur_wenn_es_woertlich_in_der_unterlage_steht():
    quelle = "Herr Muster war als Field Service Tech\xad nician tätig und übernahm End\xadUser Support sowie Deskside Support."
    assert ki.echtes_zitat("„Field Service Technician tätig“", quelle) == "Field Service Technician tätig"
    assert ki.echtes_zitat("End-User Support sowie Deskside", quelle) == "End-User Support sowie Deskside"
    assert ki.echtes_zitat("Endkunden-Support sowie Deskside", quelle) == ""  # umformuliert
    assert ki.echtes_zitat("Support", quelle) == ""  # zu kurz
    assert ki.echtes_zitat("zehn Jahre Kubernetes im Rechenzentrum", quelle) == ""  # erfunden
    assert ki.echtes_zitat("geboren am 1. Mai 1970", "Herr Muster, geboren am 1. Mai 1970, war") == ""  # sensibel


def test_sofort_bewerben_haengt_lebenslauf_fuer_die_stelle_und_zeugnis_an(conn, tmp_path):
    import hashlib
    from pypdf import PdfReader
    person.speichere(conn, name="Test Person", email="ich@example.org")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Linux und Python seit 2015")
    cv = _echtes_pdf(tmp_path / "cv.pdf", "Lebenslauf Seite 1|Lebenslauf Seite 2")
    vorher = hashlib.sha256(cv.read_bytes()).hexdigest()
    zeugnis = _echtes_pdf(tmp_path / "zeugnis.pdf", "Zeugnis")
    profil.fuege_unterlage_hinzu(conn, cv, "lebenslauf", True)
    z = profil.fuege_unterlage_hinzu(conn, zeugnis, "zeugnis", True)
    rag.speichere(conn, "dokument", ["Er war als Field Service Technician im Deskside Support tätig."], dokument_id=z, quellen=["z"])
    beleg = conn.execute("SELECT id FROM chunks WHERE dokument_id = ?", (z,)).fetchone()[0]
    client, _ = _netz_anzeige(ANZEIGE_TEXT + "\n\n## Dein Profil\n- Erfahrung im Field Service\n- Erfahrung mit SAP")
    chat = _Eignung({"Erfahrung im Field Service": ("erfuellt", beleg, "Field Service Technician im Deskside Support"),
                     "Erfahrung mit SAP": ("erfuellt", beleg, "zehn Jahre SAP-Beratung")})
    ergebnis = suche.sofort_bewerben(conn, STELLE_BA, "m", chat=chat, client=client)
    assert ergebnis["eignung"]["nachweise"] == [{"anforderung": "Erfahrung im Field Service",
                                                 "zitat": "Field Service Technician im Deskside Support", "art": "zeugnis"}]
    anhaenge = profil.anhaenge_des_entwurfs(conn, ergebnis["entwurf_id"])
    assert [p.name for p in anhaenge] == ergebnis["anhaenge"] == ["Lebenslauf_myLoc_managed_IT_AG.pdf", "zeugnis.pdf"]
    neu = PdfReader(str(anhaenge[0]))
    assert len(neu.pages) == 3
    vorn = neu.pages[0].extract_text()
    assert "Qualifikationsprofil" in vorn and "Field Service Technician im Deskside Support" in vorn
    assert "SAP-Beratung" not in vorn  # erfundenes Zitat fliegt raus
    assert "„IT-Servicetechniker“ bei myLoc managed IT AG" in vorn.replace("\n", " ")
    assert "Lebenslauf Seite 1" in neu.pages[1].extract_text()
    assert hashlib.sha256(cv.read_bytes()).hexdigest() == vorher  # das Original bleibt unverändert
    assert conn.execute("SELECT count(*) FROM entwuerfe").fetchone()[0] == 1


def test_versand_nimmt_die_anhaenge_des_entwurfs(conn, entwurf, tmp_path):
    eigen = _echtes_pdf(tmp_path / "Lebenslauf_X.pdf", "Profil")
    conn.execute("UPDATE entwuerfe SET anhaenge = ? WHERE id = ?", (json.dumps([str(eigen)]), entwurf))
    ki.gib_frei(conn, entwurf)
    versand.sende_entwurf(conn, entwurf, "pw", smtp_klar=FakeServer)
    nachricht = FakeServer.letzter.nachrichten[0][0]
    assert [t.get_filename() for t in nachricht.iter_attachments()] == ["Lebenslauf_X.pdf"]


def test_ohne_nachweise_bleibt_es_bei_den_allgemeinen_anhaengen(conn, entwurf):
    assert [p.name for p in profil.anhaenge_des_entwurfs(conn, entwurf)] == ["cv.pdf"]
    assert conn.execute("SELECT anhaenge FROM entwuerfe WHERE id = ?", (entwurf,)).fetchone()[0] is None
