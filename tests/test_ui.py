import time

import pytest
from PySide6.QtWidgets import QApplication

from jobsuche import config, db, person, profil
from jobsuche.ui import fenster


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def f(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "t.db")
    w = fenster.Hauptfenster(einrichten=False)
    w.meldungen = []
    w.melde = w.meldungen.append
    w.frage = lambda _: True
    person.speichere(w.conn, name="Test Person", email="test@example.org")
    yield w
    warte(app, w)
    w.conn.close()


def warte(app, w, sekunden=10):
    ende = time.time() + sekunden
    while w.warteschlange and time.time() < ende:
        app.processEvents()
        time.sleep(0.01)
    assert w.warteschlange == 0, "Aufgabe wurde nicht als fertig gemeldet"
    app.processEvents()


def test_aufgabe_meldet_ergebnis_und_fehler_an_die_oberflaeche(app, f):
    ergebnisse = []
    f.starte("Test", lambda conn, fort: 42, nach=ergebnisse.append)
    warte(app, f)
    assert ergebnisse == [42]

    def boom(conn, fort):
        raise ValueError("kaputt")

    f.starte("Test", boom)
    warte(app, f)
    assert f.meldungen == ["kaputt"]
    assert f.pill.objectName() == "pill"  # Die Anzeige bleibt nicht auf „Läuft“ stehen.


def test_alle_seiten_laufen_ohne_daten(app, f):
    for i in range(len(f.seiten)):
        f.zeige(i)
        app.processEvents()
    assert f.titel.text() == "Protokoll"


def test_thema_umschalten(app, f):
    vorher = f.hell
    f.thema_wechseln()
    assert f.hell != vorher
    f.thema_wechseln()


def test_fakten_bestaetigen_ueber_die_oberflaeche(app, f):
    fid = profil.lege_fakt_an(f.conn, "praktisch", "Führerschein Klasse B", "von dir ergänzt")
    f.zeige_seite(fenster.FaktenSeite)
    seite = f.stapel.currentWidget()
    seite.tab.selectRow(0)
    warte(app, f)
    seite.bestaetigen()
    warte(app, f)
    assert f.conn.execute("SELECT status FROM fakten WHERE id = ?", (fid,)).fetchone()[0] == "bestaetigt"


def test_sensibler_fakt_zeigt_meldung_statt_absturz(app, f):
    profil.lege_fakt_an(f.conn, "suche", "Gehalt 58.000 brutto", "Test")
    f.zeige_seite(fenster.FaktenSeite)
    seite = f.stapel.currentWidget()
    seite.tab.selectRow(0)
    warte(app, f)
    seite.bestaetigen()
    warte(app, f)
    assert any("Interne Angabe" in m for m in f.meldungen)


def test_kein_sendeknopf_nur_kopieren_nach_freigabe(app, f):
    fid = profil.lege_fakt_an(f.conn, "erfahrung", "Team von rund 20 Personen geleitet", "Test")
    profil.bestaetige(f.conn, fid)
    firma = f.conn.execute(
        "INSERT INTO firmen (name, personen, email, warum) VALUES ('X GmbH', 'Kim Test', 'a@x.de', 'KI')").lastrowid
    f.conn.execute(
        "INSERT INTO entwuerfe (firma_id, empfaenger, betreff, text, ki_text, modell, erstellt) "
        "VALUES (?, 'a@x.de', 'b', 'Hallo', 'Hallo', 'm', ?)", (firma, db.jetzt()))
    f.conn.commit()
    f.zeige_seite(fenster.EntwurfSeite)
    seite = f.stapel.currentWidget()
    assert not seite.kopie.isEnabled() and seite.freigabe.isEnabled()
    seite.freigeben()
    warte(app, f)
    assert seite.kopie.isEnabled()
    QApplication.clipboard().clear()
    seite.kopieren()
    assert "a@x.de" in QApplication.clipboard().text()


def test_eigene_bewertung_ueber_die_oberflaeche(app, f):
    fid = f.conn.execute("INSERT INTO firmen (name, email, warum) VALUES ('X GmbH', 'a@x.de', 'KI')").lastrowid
    f.conn.commit()
    f.zeige_seite(fenster.FirmenSeite)
    seite = f.stapel.currentWidget()
    seite.tab.selectRow(0)
    app.processEvents()
    seite.bewertung_speichern()
    assert f.meldungen == ["Trage eine Bewertung von 0 bis 100 ein."]
    seite.mein_score.setText("73")
    seite.notiz.setText("passt gut")
    seite.bewertung_speichern()
    warte(app, f)
    assert f.conn.execute("SELECT mein_score FROM firmen WHERE id = ?", (fid,)).fetchone()[0] == 73
    assert seite.mein_score.text() == "73"


def test_frage_ueber_die_oberflaeche_beantworten_und_ueberspringen(app, f):
    f.zeige_seite(fenster.FaktenSeite)
    seite = f.stapel.currentWidget()
    assert seite.aktuelle_frage.schluessel == "gehalt"
    seite.ueberspringen()
    assert seite.aktuelle_frage.schluessel == "verfuegbar" and seite.erneut_knopf.isVisibleTo(seite)
    seite.antwort.setText("ab sofort")
    seite.antworten()
    warte(app, f)
    assert seite.aktuelle_frage.schluessel == "fuehrerschein" and seite.antwort.text() == ""
    seite.erneut()
    assert seite.aktuelle_frage.schluessel == "gehalt"


# --- Neue Installation ---

@pytest.fixture
def neu(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "neu.db")
    monkeypatch.setattr(config, "DATEN", tmp_path)
    w = fenster.Hauptfenster(einrichten=False)
    w.meldungen = []
    w.melde = w.meldungen.append
    w.frage = lambda _: True
    yield w
    warte(app, w)
    w.conn.close()


def test_leere_installation_fuehrt_zum_profil(app, neu):
    start = neu.stapel.currentWidget()
    assert "Willkommen" in start.schritt_text.text() and start.ziel_seite is fenster.ProfilSeite
    assert "Noch kein Profil" in neu.fuss_text.text() and neu.avatar.text() == "?"
    for i in range(len(neu.seiten)):
        neu.zeige(i)
        app.processEvents()
    assert neu.meldungen == []


def test_profil_und_regel_ueber_die_oberflaeche(app, neu):
    neu.zeige_seite(fenster.ProfilSeite)
    seite = neu.stapel.currentWidget()
    seite.speichern()
    assert neu.meldungen == ["Der Name fehlt."]
    seite.felder["name"].setText("Kim Test")
    seite.felder["email"].setText("kim@example.org")
    seite.speichern()
    assert neu.avatar.text() == "KT" and "Kim Test" in neu.fuss_text.text()
    seite.wert.setText("(kaputt")
    seite.art.setCurrentIndex(seite.art.findData("muster"))
    seite.anlegen()
    assert "ungültig" in neu.meldungen[-1]
    seite.wert.setText("Blockchain")
    seite.art.setCurrentIndex(seite.art.findData("verboten"))
    seite.anlegen()
    assert seite.tab.rowCount() == 1
    seite.tab.selectRow(0)
    seite.loeschen()
    assert seite.tab.rowCount() == 0
    seite.vorlage.setPlainText("Mein Gehalt ist hoch")
    seite.vorlage_speichern()
    assert db.einstellung(neu.conn, "vorlage", "") == ""


def test_cv_assistent_von_vorn_bis_hinten(app, neu, tmp_path):
    neu.zeige_seite(fenster.CVSeite)
    s = neu.stapel.currentWidget()
    assert s.stapel.currentIndex() == 0
    s.kontakt["name"].setText("Kim Test")
    s.kontakt["email"].setText("kim@example.org")
    s.kontakt_speichern()
    assert s.stapel.currentIndex() == 1 and "letzte Stelle" in s.frage_text.text()

    s.station_speichern()
    assert neu.meldungen == ["Die Bezeichnung fehlt."]
    s.station["titel"].setText("Admin")
    s.station["organisation"].setText("Muster AG")
    s.station_details.setPlainText("50 Server betreut")
    s.station_speichern()
    assert "davor" in s.frage_text.text() and s.station["titel"].text() == "" and s.tab.rowCount() == 1
    s.station["titel"].setText("Techniker")  # „Das war alles“ mit Eingabe speichert sie mit.
    s.station_fertig()
    assert s.schritt.schluessel == "ausbildung" and s.tab.rowCount() == 2
    s.station_fertig()
    assert s.stapel.currentIndex() == 2

    s.text_speichern()
    assert "Schreib eine Antwort" in neu.meldungen[-1]
    s.antwort.setPlainText("Admin mit Erfahrung")
    s.text_speichern()
    s.antwort.setPlainText("Wegen Krankheit nur Teilzeit")
    s.text_speichern()
    assert "Interne Angabe" in neu.meldungen[-1] and s.antwort.toPlainText()  # Der Text bleibt zum Korrigieren stehen.
    s.text_ueberspringen()
    s.text_ueberspringen()
    s.antwort.setPlainText("Führerschein B")
    s.text_speichern()
    assert s.stapel.currentIndex() == 3 and not s.uebernehmen_knopf.isVisibleTo(s)

    s.erstellen()
    assert s.pfad.exists() and s.uebernehmen_knopf.isVisibleTo(s)
    s.uebernehmen()
    warte(app, neu)
    assert neu.conn.execute("SELECT count(*) FROM fakten WHERE status = 'bestaetigt'").fetchone()[0] >= 3
    assert s.pfad in fenster.profil.anhaenge(neu.conn)


def test_firma_und_unterlage_ueber_die_oberflaeche(app, neu, tmp_path):
    neu.zeige_seite(fenster.FirmenSeite)
    seite = neu.stapel.currentWidget()
    seite.speichere_firma({"name": "", "email": ""})
    assert neu.meldungen == ["Der Firmenname fehlt."]
    seite.speichere_firma({"name": "X GmbH", "email": "a@x.de", "personen": "Kim", "website": "", "sitz": "", "warum": ""})
    assert seite.tab.rowCount() == 1
    seite.tab.selectRow(0)
    seite.entfernen()
    assert seite.tab.rowCount() == 0

    datei = tmp_path / "cv.md"
    datei.write_text("Mein Lebenslauf. " * 20, encoding="utf-8")
    neu.zeige_seite(fenster.UnterlagenSeite)
    u = neu.stapel.currentWidget()
    u.fuege_hinzu(str(datei), "lebenslauf")
    warte(app, neu)
    assert u.tab.rowCount() == 1 and u.tab.item(0, 2).text() == "ja"
    u.tab.selectRow(0)
    u.anhang_umschalten()
    assert u.tab.item(0, 2).text() == "nein"
    u.tab.selectRow(0)
    u.entfernen()
    assert u.tab.rowCount() == 0 and datei.exists()


# --- Versand und Suche in der Oberfläche ---

def test_senden_fragt_nach_und_speichert_passwort_nur_nach_erfolg(app, f, tmp_path, monkeypatch):
    from jobsuche import firmen, ki, mailkonten, versand
    gesendet, gespeichert, fragen = [], [], []
    monkeypatch.setattr(versand, "sende_entwurf", lambda conn, eid, pw: gesendet.append(pw))
    monkeypatch.setattr(versand, "gespeichertes_passwort", lambda k: None)
    monkeypatch.setattr(versand, "speichere_passwort", lambda k, p: gespeichert.append(p))
    profil.lege_eigenen_fakt_an(f.conn, "erfahrung", "Zehn Jahre Support")
    fid = firmen.lege_an(f.conn, "X GmbH", email="a@x.de")
    eid = ki.erstelle_entwurf(f.conn, fid, "m", chat=lambda *a, **k: {"betreff": "b", "text": "Text"})
    f.zeige_seite(fenster.EntwurfSeite)
    seite = f.stapel.currentWidget()
    assert not seite.senden_knopf.isEnabled()
    seite.freigeben()
    assert seite.senden_knopf.isEnabled()

    seite.senden()  # Ohne Konto führt der Weg zur Kontoseite.
    assert f.titel.text() == "Mailkonto" and gesendet == []
    versand.speichere_konto(f.conn, mailkonten.Konto("T", "ich@x.de", "m.x.de", 587, "starttls", "ich"))
    f.zeige_seite(fenster.EntwurfSeite)

    f.frage = lambda t: (fragen.append(t), False)[1]
    seite.senden()  # Nein bei der Sicherheitsfrage: Es passiert nichts.
    assert gesendet == [] and "a@x.de" in fragen[0] and "ich@x.de" in fragen[0]

    f.frage = lambda t: "wirklich senden" in t
    f.frage_passwort = lambda k: None
    seite.senden()  # Abbruch beim Passwort: Es passiert nichts.
    warte(app, f)
    assert gesendet == []

    f.frage_passwort = lambda k: "pw1"
    seite.senden()  # Speichern abgelehnt.
    warte(app, f)
    assert gesendet == ["pw1"] and gespeichert == []

    f.frage = lambda t: True
    seite.senden()  # Speichern angenommen.
    warte(app, f)
    assert gespeichert == ["pw1"]


def test_fehlgeschlagener_versand_bietet_kein_speichern_an(app, f, monkeypatch):
    from jobsuche import mailkonten, versand
    gespeichert = []
    monkeypatch.setattr(versand, "gespeichertes_passwort", lambda k: None)
    monkeypatch.setattr(versand, "speichere_passwort", lambda k, p: gespeichert.append(p))
    f.frage_passwort = lambda k: "falsch"

    def scheitert(conn, fort, pw):
        raise versand.VersandFehler("abgelehnt")

    f.mit_passwort(mailkonten.Konto("T", "i@x.de", "m", 587, "starttls", "i"), "Test", scheitert)
    warte(app, f)
    assert f.meldungen == ["abgelehnt"] and gespeichert == []


def test_gespeichertes_passwort_wird_ohne_nachfrage_genutzt(app, f, monkeypatch):
    from jobsuche import mailkonten, versand
    monkeypatch.setattr(versand, "gespeichertes_passwort", lambda k: "aus-schluesselbund")
    f.frage_passwort = lambda k: pytest.fail("darf nicht fragen")
    benutzt = []
    f.mit_passwort(mailkonten.Konto("T", "i@x.de", "m", 587, "starttls", "i"), "Test", lambda c, fort, pw: benutzt.append(pw))
    warte(app, f)
    assert benutzt == ["aus-schluesselbund"]


def test_mailkonto_seite(app, f, monkeypatch):
    from jobsuche import mailkonten, versand
    konto = mailkonten.Konto("Thunderbird", "ich@x.de", "m.x.de", 465, "ssl", "ich")
    monkeypatch.setattr(mailkonten, "finde_alle", lambda **k: [konto])
    monkeypatch.setattr(versand, "gespeichertes_passwort", lambda k: None)
    f.zeige_seite(fenster.MailkontoSeite)
    s = f.stapel.currentWidget()
    assert s.tab.rowCount() == 1 and versand.lade_konto(f.conn) is None  # Gefunden heißt nicht gewählt.
    s.verwenden()
    assert "Wähle zuerst" in f.meldungen[-1]
    s.tab.selectRow(0)
    s.verwenden()
    assert versand.lade_konto(f.conn) == konto and "ich@x.de" in s.aktuell.text()
    s.felder["email"].setText("h@x.de")
    s.felder["host"].setText("m.x.de")
    s.felder["port"].setText("abc")
    s.von_hand()
    assert "Zahl" in f.meldungen[-1]
    s.felder["port"].setText("")
    s.von_hand()
    k = versand.lade_konto(f.conn)
    assert (k.email, k.port, k.sicherheit, k.benutzer) == ("h@x.de", 587, "starttls", "h@x.de")


def test_suche_seite_zeigt_treffer_und_uebernimmt(app, f, monkeypatch):
    from jobsuche import suche
    stelle = {"referenz": "R1", "firma": "NewTec GmbH", "titel": "KI-Entwickler", "beruf": "x", "ort": "Bremen",
              "entfernung": 9, "url": "https://example.org/R1"}
    monkeypatch.setattr(suche, "suche", lambda was, wo, km, client=None, arbeitszeit="": [stelle])
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.was.setText("KI")
    s.wo.setText("Bremen")
    s.q_verzeichnis.setChecked(False)
    s.q_arbeitnow.setChecked(False)
    s.suchen()
    warte(app, f)
    assert s.tab.rowCount() == 1 and s.tab.item(0, 1).text() == "NewTec GmbH"
    s.tab.selectRow(0)
    s.uebernehmen()
    warte(app, f)
    assert f.conn.execute("SELECT count(*) FROM firmen WHERE name = 'NewTec GmbH'").fetchone()[0] == 1
    f.neu_laden()
    assert s.tab.item(0, 6).text() == "in Liste"
    s2 = s.was.text()
    assert s2 == "KI" and fenster.db.einstellung(f.conn, "suche.ort", "") == "Bremen"


def test_passende_firmen_mit_einem_klick_ohne_tippen(app, f, monkeypatch):
    from jobsuche import suche
    monkeypatch.setattr(suche, "finde_passende", lambda conn, modell, auftrag, fort: {
        "begriffe": ["Systemadministrator", "DevOps Engineer"], "ort": "Bremen",
        "treffer": [{"referenz": "R", "firma": "X GmbH", "titel": "Admin", "beruf": "", "ort": "Bremen", "entfernung": 1, "url": "u", "naehe": 60, "bekannt": False}]})
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    assert s.was.text() == ""
    s.finden_knopf.click()
    warte(app, f)
    assert s.was.text() == "Systemadministrator, DevOps Engineer" and s.wo.text() == "Bremen" and s.tab.rowCount() == 1


def test_morgenbericht_zeigt_den_letzten_lauf(app, f, monkeypatch, tmp_path):
    import json
    from jobsuche import nacht
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    f.conn.execute("INSERT INTO berichte (datum, inhalt) VALUES (?, ?)", ("2026-11-07T01:30:00", json.dumps({
        "datum": "2026-11-07T01:30:00", "gefunden": 40, "geprueft": 20, "ohne_adresse": 5, "zu_niedrig": 3, "hinweise": [], "fehler": "",
        "firmen": [{"firma_id": 9, "name": "Gut GmbH", "score": 80, "personen": "Kim Test", "email": "kim.test@gut.de",
                    "art": "persönlich", "website": "u", "quelle": "Verzeichnis", "begruendung": "Passt: x", "entwurf_id": 1}]})))
    f.conn.commit()
    f.zeige_seite(fenster.BerichtSeite)
    s = f.stapel.currentWidget()
    s.aktualisieren()
    assert s.tab.rowCount() == 1 and s.tab.item(0, 1).text() == "Gut GmbH" and "40 gefunden" in s.stand.text()
    assert s.schalter.text() == "Nachtlauf einschalten"
    s.tab.selectRow(0)
    assert s.grund.text() == "Passt: x"


def test_schalter_verhaelt_sich_wie_ein_knopf_und_internet_braucht_eine_adresse(app, f):
    from jobsuche.ui.schalter import Schalter
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    assert isinstance(s.q_internet, Schalter) and s.q_internet.isEnabled() and not s.q_internet.isChecked()
    s.q_internet.click()  # Ohne Adresse springt er zurück und erklärt, was fehlt.
    assert not s.q_internet.isChecked() and "SearXNG" in s.ausfaelle.text()
    s.searxng.setText("http://localhost:8888")
    s.q_internet.click()
    assert s.q_internet.isChecked() and "internet" in s._auftrag().quellen
    s.searxng.clear()
    assert not s.q_internet.isChecked()
    assert s.q_verzeichnis.sizeHint().width() > 60
    assert not fenster.Schalter("x").isChecked()


def test_leeres_berufefeld_nimmt_die_abgeleiteten_berufe(app, f, monkeypatch):
    from jobsuche import suche
    fenster.db.setze_einstellung(f.conn, "suche.abgeleitet", "Entwickler")
    gesehen = []
    monkeypatch.setattr(suche, "suche_ueberall", lambda conn, auftrag, fort: gesehen.append(auftrag.begriffe) or {"treffer": [], "hinweise": []})
    f.melde = lambda _: None
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.was.clear()
    s.suchen()
    warte(app, f)
    assert gesehen == ["Entwickler"] and s.was.text() == "Entwickler"


def test_klick_auf_treffer_zeigt_details_mit_links_und_maskiert_fremden_text(app, f, monkeypatch):
    from jobsuche import suche
    aufrufe = []
    monkeypatch.setattr(suche, "vorschau", lambda t, l, x: aufrufe.append(t["firma"]) or {
        "website": "https://a.de/", "personen": "Kim Test", "email": "kim.test@a.de", "art": "persönlich",
        "kurztext": "<img src=x>Böse", "stellen": ["Admin (m/w/d)"], "problem": ""})
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.verzoegerung.setInterval(0)
    s.gefunden({"treffer": [{"referenz": "R", "firma": "A GmbH", "titel": "Admin", "beruf": "", "ort": "Bremen", "entfernung": 3,
                             "url": "https://anzeige.example/1", "website": "", "quelle": "Jobbörse", "naehe": 50, "bekannt": False}]})
    assert not s.knopf_inhaber.isEnabled()
    s.tab.selectRow(0)
    assert "Stellenanzeige" in s.details.text() and "Lese die Firmenseite" in s.details.text() and s.knopf_inhaber.isEnabled()
    for _ in range(50):
        app.processEvents()
        if aufrufe:
            break
        fenster.QThreadPool.globalInstance().waitForDone(10)
    warte(app, f)
    assert aufrufe == ["A GmbH"]
    text = s.details.text()
    assert "Kim Test" in text and "https://a.de/" in text and "Admin (m/w/d)" in text and "<img" not in text and "&lt;img" in text


def test_internettreffer_verlinkt_impressum_statt_stellenanzeige_und_nennt_initiativanfrage(app, f, monkeypatch):
    from jobsuche import suche
    monkeypatch.setattr(suche, "vorschau", lambda t, l, x: {
        "website": "https://a.de/", "personen": "Kim Test", "email": "info@a.de", "art": "allgemein",
        "kurztext": "", "stellen": [], "problem": ""})
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.verzoegerung.setInterval(0)
    s.gefunden({"treffer": [{"referenz": "web:a.de", "firma": "A IT", "titel": "IT", "beruf": "", "ort": "Nordhorn", "entfernung": 4,
                             "url": "https://a.de/impressum/", "website": "https://a.de/", "quelle": "Internet", "naehe": 50, "bekannt": False}]})
    s.tab.selectRow(0)
    for _ in range(200):
        app.processEvents()
        fenster.QThreadPool.globalInstance().waitForDone(10)
        if "Initiativanfrage" in s.details.text():
            break
    warte(app, f)
    text = s.details.text()
    assert "Impressum öffnen" in text and "Stellenanzeige" not in text and "Initiativanfrage" in text


def test_konzern_steht_als_status_und_sofort_bewerben_wird_aktiv(app, f):
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.verzoegerung.setInterval(10_000)
    s.gefunden({"treffer": [{"referenz": "R", "firma": "Gross AG", "titel": "Dev", "beruf": "", "ort": "", "entfernung": None,
                             "url": "", "website": "", "quelle": "Jobbörse", "naehe": 50, "bekannt": False, "konzern": "Rechtsform AG"}]})
    assert s.tab.item(0, 6).text() == "Konzern" and not s.knopf_sofort.isEnabled()
    s.tab.selectRow(0)
    assert s.knopf_sofort.isEnabled()


def test_anhaenge_des_entwurfs_lassen_sich_vor_dem_senden_oeffnen(app, f, tmp_path, monkeypatch):
    import json
    from PySide6.QtGui import QDesktopServices
    geoeffnet = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: geoeffnet.append(url.toLocalFile()))
    cv = tmp_path / "Lebenslauf_X_GmbH.pdf"
    cv.write_bytes(b"%PDF")
    firma = f.conn.execute("INSERT INTO firmen (name, email) VALUES ('X GmbH', 'a@x.de')").lastrowid
    f.conn.execute(
        "INSERT INTO entwuerfe (firma_id, empfaenger, betreff, text, ki_text, modell, erstellt, anhaenge) "
        "VALUES (?, 'a@x.de', 'b', 'Hallo', 'Hallo', 'm', ?, ?)",
        (firma, db.jetzt(), json.dumps([str(cv), str(tmp_path / "weg.pdf")])))
    f.conn.commit()
    f.zeige_seite(fenster.EntwurfSeite)
    seite = f.stapel.currentWidget()
    text = seite.anhang.text()
    assert f'href="{cv}"' in text and "<s>weg.pdf</s> (fehlt)" in text
    from PySide6.QtCore import Qt
    assert seite.anhang.textInteractionFlags() & Qt.LinksAccessibleByMouse  # wirklich klickbar, nicht nur das Signal
    seite.anhang.linkActivated.emit(str(cv))
    assert geoeffnet == [str(cv)]


def test_eignung_fragt_offenes_nach_und_uebernimmt_die_antwort(app, f, monkeypatch):
    import json
    from jobsuche import ki, suche
    from PySide6.QtWidgets import QLineEdit
    fid = f.conn.execute("INSERT INTO firmen (name, email) VALUES ('X GmbH', 'a@x.de')").lastrowid
    f.conn.commit()
    eignung = {"score": 0, "erfuellt": [], "teilweise": [], "fehlt": ["MS Office"], "selbst": [], "cv": [], "nachweise": [],
               "anzeige": "", "modell": "m", "zeit": ""}

    def pruefe(conn, firma_id, modell, melde=None):
        conn.execute("UPDATE firmen SET ki_eignung = ? WHERE id = ?", (json.dumps(eignung), firma_id))
        conn.commit()
        return eignung
    monkeypatch.setattr(suche, "pruefe_eignung", pruefe)
    gefragt = []
    f.nachfragen = lambda punkte: gefragt.append(punkte) or {"MS Office": "Office seit Version 1, zuletzt Microsoft 365"}
    f.zeige_seite(fenster.FirmenSeite)
    seite = f.stapel.currentWidget()
    seite.firma_id = lambda: fid
    seite.eignung()
    warte(app, f)
    assert gefragt == [["MS Office"]]
    neu = json.loads(f.conn.execute("SELECT ki_eignung FROM firmen WHERE id = ?", (fid,)).fetchone()[0])
    assert neu["score"] == 100 and neu["erfuellt"] == ["MS Office"]
    assert any("Eignung 100 %" in m for m in f.meldungen)
    dialog = fenster.NachfrageDialog(["A", "B"])
    felder = dialog.findChildren(QLineEdit)
    felder[1].setText(" ja, seit 2010 ")
    assert dialog.antworten() == {"B": "ja, seit 2010"}


def test_claude_wird_einmal_angeboten_und_nur_mit_zustimmung_gewaehlt(app, f, monkeypatch):
    from jobsuche import llm
    monkeypatch.setattr(llm, "claude_status", lambda: "")
    fragen = []
    f.frage = lambda text: fragen.append(text) or False
    f.biete_claude_an()
    assert len(fragen) == 1 and "Anthropic" in fragen[0] and not llm.ist_claude(f.modell())
    f.biete_claude_an()
    assert len(fragen) == 1  # nur einmal pro Person
    db.setze_einstellung(f.conn, "claude.gefragt", "")
    f.frage = lambda text: True
    f.biete_claude_an()
    assert f.modell() == llm.CLAUDE_MODELLE[0] == "claude:claude-haiku-5-5"
    f.zeige_seite(fenster.SystemSeite)
    seite = f.stapel.currentWidget()
    seite.aktualisieren()
    assert seite.auswahl.findText("claude:claude-haiku-5-5") >= 0 and "Anthropic" in seite.status.text()


def test_gesendet_seite_zeigt_falsche_adresse_und_kopiert(app, f):
    firma = f.conn.execute("INSERT INTO firmen (name, email, warum) VALUES ('X GmbH', 'jobs@x.de', 'KI')").lastrowid
    f.conn.execute("INSERT INTO entwuerfe (firma_id, empfaenger, betreff, text, ki_text, modell, erstellt, status, gesendet_am) "
                   "VALUES (?, 'info@verzeichnis.de', 'b', 'Hallo', 'Hallo', 'm', ?, 'gesendet', ?)", (firma, db.jetzt(), db.jetzt()))
    f.conn.commit()
    f.zeige_seite(fenster.EntwurfSeite)
    assert f.stapel.currentWidget().liste.count() == 0  # Gesendetes steht nicht mehr bei den Entwürfen
    f.zeige_seite(fenster.GesendetSeite)
    seite = f.stapel.currentWidget()
    assert seite.tab.rowCount() == 1 and seite.tab.item(0, 2).text() == "info@verzeichnis.de"
    assert "jobs@x.de" in seite.kopf.text()
    seite.neu_entwurf()
    assert isinstance(f.stapel.currentWidget(), fenster.EntwurfSeite)
    assert f.conn.execute("SELECT empfaenger FROM entwuerfe WHERE status = 'entwurf'").fetchone()[0] == "jobs@x.de"
