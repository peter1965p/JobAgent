"""Anmeldung, getrennte Datenbanken und Demo. Ohne echten Mailserver."""

import smtplib
import time

import pytest
from PySide6.QtWidgets import QApplication

from jobsuche import anmeldung, config, db, demo, ki, mailkonten, person, profil, versand
from jobsuche.ui import anmelden, fenster

KONTO = mailkonten.Konto("Test", "ich@x.de", "m.x.de", 587, "starttls", "ich@x.de")


class Server:
    passwort = "richtig"
    erreichbar = True
    gesendet = 0

    def __init__(self, *a, **k):
        if not Server.erreichbar:
            raise OSError("kein Netz")

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, benutzer, passwort):
        if passwort != Server.passwort:
            raise smtplib.SMTPAuthenticationError(535, b"nein")

    def send_message(self, *a, **k):
        Server.gesendet += 1

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def ablage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATEN", tmp_path)
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "unbenutzt.db")
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(Server, "erreichbar", True)
    monkeypatch.setattr(versand.keyring, "get_password", lambda d, n: None)
    yield
    demo.aufraeumen()


def pruefe(email="ich@x.de", passwort="richtig", konto=KONTO):
    return anmeldung.pruefe(email, passwort, konto, smtp_klar=Server, smtp_ssl=Server)


def test_falsches_passwort_wird_abgelehnt_und_legt_nichts_an(tmp_path):
    with pytest.raises(versand.AnmeldeFehler):
        pruefe(passwort="falsch")
    assert not list((tmp_path / "personen").glob("*")) if (tmp_path / "personen").exists() else True


@pytest.mark.parametrize("email,pw", [("", "x"), ("ohne-at", "x"), ("a@b.de", "")])
def test_unvollstaendige_eingaben(email, pw):
    with pytest.raises(versand.AnmeldeFehler):
        pruefe(email, pw)


def test_jede_person_bekommt_eine_eigene_datenbank():
    a, b = pruefe("ich@x.de"), pruefe("du@x.de", konto=mailkonten.Konto("T", "du@x.de", "m.x.de", 587, "starttls", "du@x.de"))
    assert a.pfad != b.pfad
    anmeldung.oeffne(a, "ich@x.de", "richtig")
    conn = db.verbinden(a.pfad)
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Geheimer Fakt von ich")
    conn.close()
    anmeldung.oeffne(b, "du@x.de", "richtig")
    conn = db.verbinden(b.pfad)
    assert conn.execute("SELECT count(*) FROM fakten").fetchone()[0] == 0
    assert person.lade(conn)["email"] == "du@x.de" and versand.lade_konto(conn).email == "du@x.de"
    conn.close()


def test_grossschreibung_fuehrt_zur_selben_datenbank():
    assert pruefe("ICH@X.de").pfad == pruefe("ich@x.de").pfad


def test_bestehende_datenbank_gehoert_ihrer_besitzerin(tmp_path):
    alt = tmp_path / "jobsuche.db"
    conn = db.verbinden(alt)
    person.speichere(conn, name="Alte Person", email="alt@x.de")
    profil.lege_eigenen_fakt_an(conn, "erfahrung", "Alter Fakt")
    conn.close()
    assert anmeldung.datenbank_fuer("ALT@x.de") == alt
    fremd = anmeldung.datenbank_fuer("neu@x.de")
    assert fremd != alt and fremd.parent.parent == tmp_path / "personen"


def test_konto_wird_in_mailprogrammen_gesucht(tmp_path):
    (tmp_path / ".msmtprc").write_text("account a\nhost m.x.de\nfrom ich@x.de\nuser ich\ntls on\n", encoding="utf-8")
    ergebnis = anmeldung.pruefe("ICH@x.de", "richtig", home=tmp_path, smtp_klar=Server, smtp_ssl=Server)
    assert ergebnis.konto.host == "m.x.de" and ergebnis.konto.quelle == "msmtp"
    with pytest.raises(anmeldung.KontoFehlt):
        anmeldung.pruefe("fremd@x.de", "richtig", home=tmp_path, smtp_klar=Server, smtp_ssl=Server)


def test_unverschluesseltes_konto_kommt_nicht_rein(tmp_path):
    with pytest.raises(versand.AnmeldeFehler, match="Verschlüsselung"):
        pruefe(konto=mailkonten.Konto("T", "ich@x.de", "m", 25, "keine", "ich@x.de"))


def test_ohne_netz_gilt_die_letzte_erfolgreiche_anmeldung():
    ergebnis = pruefe()
    anmeldung.oeffne(ergebnis, "ich@x.de", "richtig")
    Server.erreichbar = False
    offline = pruefe(konto=None)
    assert offline.offline and offline.pfad == ergebnis.pfad
    with pytest.raises(versand.NichtErreichbar):
        pruefe(passwort="falsch", konto=None)
    with pytest.raises(versand.NichtErreichbar):
        pruefe("neu@x.de")  # Wer noch nie angemeldet war, braucht das Netz.


def test_passwort_steht_nicht_im_klartext_in_der_datenbank():
    ergebnis = pruefe(passwort="richtig")
    anmeldung.oeffne(ergebnis, "ich@x.de", "richtig")
    conn = db.verbinden(ergebnis.pfad)
    alles = " ".join(str(tuple(z)) for t in ("einstellungen", "ereignisse", "person") for z in conn.execute(f"SELECT * FROM {t}"))
    conn.close()
    assert "richtig" not in alles and "login.pruefwert" in alles


def test_merken_nur_wenn_gewuenscht(monkeypatch):
    speicher = {}
    monkeypatch.setattr(versand.keyring, "set_password", lambda d, n, p: speicher.__setitem__(n, p))
    monkeypatch.setattr(versand.keyring, "get_password", lambda d, n: speicher.get(n))
    monkeypatch.setattr(versand.keyring, "delete_password", lambda d, n: speicher.pop(n))
    anmeldung.merke_passwort("ich@x.de", "pw", False)
    assert speicher == {}
    anmeldung.merke_passwort("ICH@x.de", "pw", True)
    assert anmeldung.gemerktes_passwort("ich@x.de") == "pw"
    anmeldung.merke_passwort("ich@x.de", "pw", False)
    assert speicher == {}


# --- Demo ---

def _app():
    return QApplication.instance() or QApplication([])


def warte(app, w, sekunden=15):
    ende = time.time() + sekunden
    while w.warteschlange and time.time() < ende:
        app.processEvents()
        time.sleep(0.01)
    assert not w.warteschlange
    app.processEvents()


def test_demo_liegt_im_wegwerf_ordner_und_sendet_nicht(tmp_path):
    app = _app()
    echte = tmp_path / "jobsuche.db"
    conn = db.verbinden(echte)
    person.speichere(conn, name="Echte Person", email="echt@x.de")
    conn.close()
    demo.vorbereiten()
    assert config.DEMO and tmp_path not in config.DB_PFAD.parents
    ordner = demo.ORDNER
    w = fenster.Hauptfenster()
    w.melde = lambda t: None
    warte(app, w)
    assert person.name(w.conn) == "Kim Beispiel" and "Demo" in w.pill.text() and w.abmelden_knopf.text() == "Demo beenden"
    assert w.conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 3
    entwurf = w.conn.execute("SELECT * FROM entwuerfe").fetchone()
    assert entwurf["empfaenger"].endswith(".example") and demo.FIRMENSATZ in entwurf["text"]
    assert not ki.regeln.gesperrt(ki.pruefe_entwurf(w.conn, entwurf["id"]))

    ki.gib_frei(w.conn, entwurf["id"])
    versand.speichere_konto(w.conn, KONTO)
    for senden in (lambda: versand.sende_entwurf(w.conn, entwurf["id"], "pw", smtp_klar=Server),
                   lambda: versand.sende_test(w.conn, "pw", smtp_klar=Server)):
        with pytest.raises(versand.VersandFehler, match="Demo"):
            senden()
    assert Server.gesendet == 0

    w.zeige_seite(fenster.MailkontoSeite)
    assert w.stapel.currentWidget().konten == []  # Echte Mailprogramme bleiben außen vor.
    w.conn.close()
    demo.aufraeumen()
    assert not ordner.exists()
    prüfung = db.verbinden(echte)
    assert person.name(prüfung) == "Echte Person"
    prüfung.close()


def test_kein_personenbezug_in_der_demo():
    text = str(demo.FAKTEN) + str(demo.FIRMEN) + demo.MAILTEXT
    assert ".example" in text and "@" in text and "gmail" not in text


# --- Oberfläche ---

@pytest.fixture
def dialog(monkeypatch):
    app = _app()
    from PySide6.QtCore import QSettings
    QSettings("jobsuche", "jobsuche").remove("letzte_adresse")
    d = anmelden.AnmeldeFenster()
    d.show()
    d.warte = lambda: _warte_dialog(app, d)
    yield d
    d.pool.waitForDone(2000)
    d.close()


def _warte_dialog(app, d):
    ende = time.time() + 5
    while not d.anmelden_knopf.isEnabled() and time.time() < ende:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_anmeldefenster_erfolg(dialog, monkeypatch):
    monkeypatch.setattr(anmeldung, "pruefe", lambda e, p, k: anmeldung.Ergebnis(config.DATEN / "a.db", KONTO))
    dialog.email.setText("ich@x.de")
    dialog.pw.setText("richtig")
    dialog.anmelden()
    dialog.warte()
    assert dialog.ergebnis.konto == KONTO and dialog.passwort == "richtig" and not dialog.isVisible()


def test_anmeldefenster_fehler_leert_passwort(dialog, monkeypatch):
    def scheitert(e, p, k):
        raise versand.AnmeldeFehler("Der Server hat die Anmeldung abgelehnt.")
    monkeypatch.setattr(anmeldung, "pruefe", scheitert)
    dialog.email.setText("ich@x.de")
    dialog.pw.setText("falsch")
    dialog.anmelden()
    dialog.warte()
    assert "abgelehnt" in dialog.meldung.text() and dialog.pw.text() == "" and dialog.ergebnis is None and dialog.isVisible()


def test_anmeldefenster_fragt_nach_server_wenn_keiner_bekannt(dialog, monkeypatch):
    aufrufe = []

    def pruefung(e, p, k):
        aufrufe.append(k)
        if k is None:
            raise anmeldung.KontoFehlt("Kein Server gefunden.")
        return anmeldung.Ergebnis(config.DATEN / "a.db", k)

    monkeypatch.setattr(anmeldung, "pruefe", pruefung)
    dialog.email.setText("ich@x.de")
    dialog.pw.setText("pw")
    assert not dialog.server.isVisible()
    dialog.anmelden()
    dialog.warte()
    assert dialog.server.isVisible() and "Kein Server" in dialog.meldung.text()
    dialog.anmelden()  # Server fehlt noch.
    assert "Postausgangsserver" in dialog.meldung.text()
    dialog.host.setText("smtp.x.de")
    dialog.port.setText("abc")
    dialog.anmelden()
    assert "Zahl" in dialog.meldung.text()
    dialog.port.setText("")
    dialog.anmelden()
    dialog.warte()
    assert aufrufe[-1].host == "smtp.x.de" and aufrufe[-1].port == 587 and aufrufe[-1].benutzer == "ich@x.de"


def test_demo_knopf(dialog):
    dialog.demo_starten()
    assert dialog.demo and not dialog.isVisible()


def test_sitzungspasswort_ersetzt_nachfrage_und_nur_fuer_dieses_konto(tmp_path):
    app = _app()
    config.DB_PFAD = tmp_path / "s.db"
    w = fenster.Hauptfenster(einrichten=False, sitzung_konto=KONTO, sitzung_passwort="aus-anmeldung")
    w.melde = lambda t: None
    w.frage = lambda t: pytest.fail("Darf das Speichern nicht erneut anbieten")
    w.frage_passwort = lambda k: pytest.fail("Darf nicht fragen")
    benutzt = []
    w.mit_passwort(KONTO, "T", lambda c, f, pw: benutzt.append(pw))
    warte(app, w)
    assert benutzt == ["aus-anmeldung"]
    anderes = mailkonten.Konto("T", "b@x.de", "m", 587, "starttls", "b@x.de")
    w.frage_passwort = lambda k: None
    w.mit_passwort(anderes, "T", lambda c, f, pw: benutzt.append(pw))
    warte(app, w)
    assert benutzt == ["aus-anmeldung"]  # Ein anderes Konto bekommt das Passwort nicht.
    w.conn.close()


def test_abmelden_vergisst_passwort_und_ruft_anmeldung(tmp_path):
    app = _app()
    config.DB_PFAD = tmp_path / "s.db"
    ereignisse = []
    w = fenster.Hauptfenster(einrichten=False, sitzung_konto=KONTO, sitzung_passwort="pw",
                             beim_schliessen=lambda: ereignisse.append("ende"), beim_abmelden=lambda: ereignisse.append("ab"))
    w.melde = lambda t: ereignisse.append(t)
    w.warteschlange = 1
    w.abmelden()
    assert ereignisse == ["Es läuft noch eine Arbeit. Warte kurz, bis sie fertig ist."]
    w.warteschlange = 0
    w.abmelden()
    assert ereignisse[-1] == "ab" and w.sitzung_passwort is None
    w.conn.close()
    w2 = fenster.Hauptfenster(einrichten=False, beim_schliessen=lambda: ereignisse.append("ende"), beim_abmelden=lambda: None)
    w2.close()
    assert ereignisse[-1] == "ende"
    w2.conn.close()
    app.processEvents()


def test_neue_person_ohne_namen_landet_beim_profil(tmp_path):
    app = _app()
    ergebnis = pruefe()
    anmeldung.oeffne(ergebnis, "ich@x.de", "richtig")
    w = fenster.Hauptfenster(einrichten=False)
    assert "Willkommen" in w.seiten[0].schritt_text.text()
    w.zeige_seite(fenster.ProfilSeite)
    assert w.stapel.currentWidget().felder["email"].text() == "ich@x.de"
    w.fuss_text.click()
    assert w.titel.text() == "Profil"
    w.conn.close()
    app.processEvents()
