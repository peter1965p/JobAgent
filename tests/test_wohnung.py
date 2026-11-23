"""Wohnungssuche: Angebote aus Suchauftrags-Mails (nachgebaut im Aufbau von Immowelt, ImmoScout24 und Kleinanzeigen),
Speichern, Filter, Postfach-Abruf mit Fake-Server und die Seite. Kein Test erreicht echte Server."""

import json
from email.message import EmailMessage

import pytest

from jobsuche import db, postfach, wohnung


def _mail(absender: str, html: str, datum="Sun, 22 Nov 2026 10:00:00 +0000") -> bytes:
    nachricht = EmailMessage()
    nachricht["From"] = absender
    nachricht["To"] = "person@example.org"
    nachricht["Subject"] = "Neue Angebote"
    nachricht["Date"] = datum
    nachricht.set_content("Nur HTML")
    nachricht.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
    return nachricht.as_bytes()


def _immowelt(preis, titel, groesse, ort):
    klick = "https://click.by.immowelt.de/?qs=x"
    return _mail("immowelt Suche <angebot@suchen.immowelt.de>", (
        "<p>1 neues Angebot, passend zu deiner gespeicherten Suche</p><table><tr><td>"
        f"<a href='{klick}1'>{preis}</a><a href='{klick}1'>Kaltmiete</a><a href='{klick}2'>{titel}</a>"
        f"<a href='{klick}3'>{groesse}</a><a href='{klick}4'>{ort}</a><a href='{klick}5'>Mehr Informationen</a>"
        "</td></tr></table><p>Hol dir die App</p><p>Musterweg 1 · 90000 Musterstadt · Deutschland</p>"
        f"<a href='{klick}9'>Suchauftrag bearbeiten</a>"))


IS24 = _mail("ImmoScout24 <myscout@immobilienscout24.de>", (
    "<table><tr><td><a href='https://push.search.is24.de/email/Suche/x'>Wohnung • bis 65 m² mit Wbs</a>"
    "<p>wurde erfolgreich gespeichert.</p></td></tr></table>"
    "<table><tr><td><a href='https://push.search.is24.de/email/expose/111111111?PID=1'>!!! 2-Zimmer !!! WBS für mind. 62 m² !!!</a>"
    "<p>485 €</p><p>62,36 m²</p><p>2 Zi.</p><p>Musterstraße 6, Nord, Musterstadt</p><p>Verwaltung GmbH</p></td></tr></table>"
    "<table><tr><td><a href='https://push.search.is24.de/email/expose/222222222?PID=1'>Helle Wohnung mit Balkon</a>"
    "<p>1.150 €</p><p>78 m²</p><p>3 Zi.</p><p>Beispielweg 3, Altstadt, Beispielstadt</p></td></tr></table>"))

KLEIN = _mail("Kleinanzeigen <suchauftrag@kleinanzeigen.de>", (
    "<p>Dein Suchauftrag</p><p>in Musterstadt und 50 km Umkreis: Mietwohnungen, bis 400 EUR, ab 50 m²</p>"
    "<table><tr><td><a href='https://www.kleinanzeigen.de/s-anzeige/wohnung/1'>[TAUSCHWOHNUNG] 2 zkb wohnung ohne balkon</a>"
    "<p>12345 Musterstadt</p><p>400 €</p><p>Mietpreis</p><p>50 m²</p><p>Wohnfläche</p><p>2</p><p>Zimmer</p></td></tr>"
    "<tr><td><a href='https://www.kleinanzeigen.de/s-anzeige/wohnung/2'>Gartenwohnung, Haustiere erlaubt</a>"
    "<p>12346 Beispieldorf</p><p>520 €</p><p>Mietpreis</p><p>64 m²</p><p>Wohnfläche</p><p>2,5</p><p>Zimmer</p></td></tr></table>"))


def test_immowelt_eine_karte_mit_kaltmiete_und_plz():
    [a] = wohnung.angebote_aus_mail(_immowelt("516 €", "Schöne 2-Zimmerwohnung mit Terrasse", "2 Zimmer · 62 m²", "Nord, Musterstadt (45739)"))
    assert (a["portal"], a["titel"], a["kalt"], a["warm"], a["zimmer"], a["flaeche"], a["plz"]) == (
        "Immowelt", "Schöne 2-Zimmerwohnung mit Terrasse", 516, None, 2, 62, "45739")
    assert a["ort"] == "Nord, Musterstadt (45739)" and a["merkmale"] == ["terrasse"] and a["datum"] == "2026-11-22"


def test_immoscout24_kopf_ist_kein_angebot_titel_mit_m2_bleibt_titel():
    angebote = wohnung.angebote_aus_mail(IS24)
    assert [(a["kennung"], a["titel"], a["kalt"], a["flaeche"], a["zimmer"]) for a in angebote] == [
        ("is24:111111111", "!!! 2-Zimmer !!! WBS für mind. 62 m² !!!", 485, 62.36, 2),
        ("is24:222222222", "Helle Wohnung mit Balkon", 1150, 78, 3)]
    assert angebote[0]["url"] == "https://www.immobilienscout24.de/expose/111111111"
    assert angebote[0]["merkmale"] == ["wbs"] and angebote[1]["merkmale"] == ["balkon"]


def test_kleinanzeigen_zimmer_in_eigener_zeile_und_ohne_balkon_zaehlt_nicht():
    a, b = wohnung.angebote_aus_mail(KLEIN)
    assert (a["ort"], a["kalt"], a["flaeche"], a["zimmer"], a["merkmale"]) == ("12345 Musterstadt", 400, 50, 2, ["tausch"])
    assert (b["zimmer"], b["merkmale"]) == (2.5, ["garten", "haustiere"])


def test_fremde_absender_und_mails_ohne_angebot():
    assert wohnung.angebote_aus_mail(_mail("Jemand <x@example.org>", "<p>500 €</p><p>60 m²</p>")) == []
    assert wohnung.angebote_aus_mail(_mail("Kleinanzeigen <noreply@kleinanzeigen.de>", "<p>Neue Anmeldung – bist du es?</p>")) == []


@pytest.fixture
def conn(tmp_path):
    c = db.verbinden(tmp_path / "w.db")
    yield c
    c.close()


def test_speichern_bewahrt_status_und_frischt_preis_auf(conn):
    angebote = wohnung.angebote_aus_mail(IS24)
    assert wohnung.speichere(conn, angebote) == 2
    wohnung.setze_status(conn, "is24:222222222", "gemerkt")
    teurer = [{**a, "kalt": 1200.0, "datum": "2026-11-23"} for a in angebote]
    assert wohnung.speichere(conn, teurer) == 0
    z = conn.execute("SELECT * FROM wohnungen WHERE kennung = 'is24:222222222'").fetchone()
    assert (z["status"], z["kalt"], z["datum"]) == ("gemerkt", 1200.0, "2026-11-23")


def test_filter_gruende_und_reihenfolge(conn, monkeypatch):
    monkeypatch.setattr(wohnung, "TAGE", 100000)
    wohnung.speichere(conn, wohnung.angebote_aus_mail(IS24) + wohnung.angebote_aus_mail(KLEIN))
    conn.execute("UPDATE wohnungen SET bundesland = 'Bayern' WHERE titel LIKE 'Garten%'")
    conn.execute("UPDATE wohnungen SET km = 30, km_ziel = 'Musterstadt' WHERE kennung LIKE 'is24%'")
    conn.commit()
    f = wohnung.Filter(max_miete=900, min_zimmer=2, min_flaeche=55, max_km=50)
    ergebnis = {z["titel"]: (ok, gruende) for z, ok, gruende in wohnung.liste(conn, f)}
    # 485 € kalt + 62,36 m² × 2,80 € ≈ 660 € warm: passt, WBS ist kein Ausschluss
    assert ergebnis["!!! 2-Zimmer !!! WBS für mind. 62 m² !!!"] == (True, [])
    assert ergebnis["Helle Wohnung mit Balkon"] == (False, ["1368 € warm"])
    assert ergebnis["[TAUSCHWOHNUNG] 2 zkb wohnung ohne balkon"] == (False, ["50 m²", "Tausch"])
    assert ergebnis["Gartenwohnung, Haustiere erlaubt"] == (False, ["Bayern"])
    f.merkmale = ("wbs",)
    assert wohnung.passt(next(z for z, _, _ in wohnung.liste(conn, f) if z["titel"].startswith("Helle")), f)[1][-1] == "ohne WBS"
    f.laender = ()
    f.merkmale = ("garten",)
    reihe = wohnung.liste(conn, f)
    assert reihe[0][0]["titel"] == "Gartenwohnung, Haustiere erlaubt" and reihe[0][1]
    wohnung.setze_status(conn, "is24:111111111", "gemerkt")
    assert wohnung.liste(conn, f)[0][0]["kennung"] == "is24:111111111"  # gemerkt steht oben
    wohnung.setze_status(conn, "is24:222222222", "verworfen")
    assert "is24:222222222" not in [z["kennung"] for z, _, _ in wohnung.liste(conn, f)]


def test_filter_und_arbeitsorte_bleiben_gespeichert(conn):
    wohnung.merke_filter(conn, wohnung.Filter(max_miete=800, laender=("Bremen",), merkmale=("balkon",)))
    f = wohnung.lade_filter(conn)
    assert (f.max_miete, f.laender, f.merkmale, f.ohne_tausch) == (800, ("Bremen",), ("balkon",), True)
    wohnung.setze_arbeitsorte(conn, [" Musterstadt ", "", "12345 Beispieldorf"])
    assert wohnung.arbeitsorte(conn) == ["Musterstadt", "12345 Beispieldorf"]


def test_ortsanfrage():
    assert wohnung._ortsanfrage("Hiltrup, Musterstadt (48165)") == "48165 Musterstadt"
    assert wohnung._ortsanfrage("Musterstraße 6, Nord, Musterstadt") == "Musterstraße 6, Musterstadt"
    assert wohnung._ortsanfrage("12345 Musterstadt") == "12345 Musterstadt"


class FakeImap:
    mails: dict[int, bytes] = {}
    suchen: list = []

    def __init__(self, host, port):
        pass

    def login(self, benutzer, kennwort):
        return "OK", []

    def select(self, postfach, readonly=False):
        assert readonly
        return "OK", [b"3"]

    def response(self, name):
        return name, [b"7"]

    def uid(self, befehl, *argumente):
        if befehl == "SEARCH":
            FakeImap.suchen.append(argumente[-1])
            absender = argumente[-1].strip('"')
            treffer = [u for u, roh in FakeImap.mails.items() if absender.encode() in roh.split(b"\n\n")[0]]
            return "OK", [" ".join(map(str, treffer)).encode()]
        if befehl == "FETCH":
            assert argumente[-1] == "(BODY.PEEK[])"
            uids = [int(u) for u in argumente[0].split(",")]
            return "OK", [(f"{u} (UID {u} BODY[] {{1}}".encode(), FakeImap.mails[u]) for u in uids]
        return "NO", []

    def logout(self):
        pass


def test_lesen_aus_postfach_nur_wohnungsportale_und_nur_einmal(conn, monkeypatch):
    FakeImap.mails = {1: _immowelt("516 €", "Wohnung mit Terrasse", "2 Zimmer · 62 m²", "Nord, Musterstadt (45739)"), 2: KLEIN}
    FakeImap.suchen = []
    monkeypatch.setattr(postfach, "passwort", lambda conn, p: "geheim")
    monkeypatch.setattr(wohnung, "verorte", lambda *a, **k: 0)
    monkeypatch.setattr(wohnung, "entfernungen", lambda *a, **k: 0)
    wohnung.waehle_konten(conn, [postfach.Postfach("imap.example.org", 993, "p@example.org")])
    neu, hinweise = wohnung.lies(conn, imap_klasse=FakeImap)
    assert (neu, hinweise) == (3, [])
    assert {s.strip('"') for s in FakeImap.suchen} == {p.absender for p in wohnung.PORTALE}
    neu, _ = wohnung.lies(conn, imap_klasse=FakeImap)
    assert neu == 0


def test_ohne_postfach_ein_hinweis(conn, monkeypatch):
    monkeypatch.setattr(postfach, "gewaehlte", lambda conn: [])
    assert wohnung.lies(conn) == (0, ["Wohnungen: Noch kein Postfach gewählt."])


def test_seite_zeigt_ampel_und_filtert(app_fenster):
    f = app_fenster
    from jobsuche.ui import fenster
    wohnung.speichere(f.conn, [{**a, "datum": __import__("datetime").date.today().isoformat()}
                               for a in wohnung.angebote_aus_mail(IS24) + wohnung.angebote_aus_mail(KLEIN)])
    f.zeige_seite(fenster.WohnungSeite)
    seite = f.stapel.currentWidget()
    seite.aktualisieren()
    assert seite.tab.rowCount() == 4
    farben = [seite.tab.item(r, 0).data(fenster.Qt.UserRole + 1) for r in range(4)]
    assert farben.count(fenster.AMPEL["gruen"]) == 3 and farben.count(fenster.AMPEL["rot"]) == 1  # ohne Filter fällt nur Tausch raus
    seite.miete.setValue(500)
    assert json.loads(db.einstellung(f.conn, "wohnung.filter", "{}"))["max_miete"] == 500
    assert [seite.tab.item(r, 0).data(fenster.Qt.UserRole + 1) for r in range(4)].count(fenster.AMPEL["gruen"]) == 0
    seite.tab.selectRow(0)
    seite.status("gemerkt")
    assert seite.tab.item(0, 0).data(fenster.Qt.UserRole + 1) == "#f5a623"


def test_seite_klappt_vorschau_auf_und_zu(app_fenster):
    f = app_fenster
    from jobsuche.ui import fenster
    if fenster.QWebEngineView is None:
        import pytest
        pytest.skip("Kein QtWebEngine in dieser Umgebung")
    wohnung.speichere(f.conn, [{**a, "datum": __import__("datetime").date.today().isoformat()}
                               for a in wohnung.angebote_aus_mail(IS24)])
    f.zeige_seite(fenster.WohnungSeite)
    seite = f.stapel.currentWidget()
    seite.aktualisieren()
    seite._klick(0, 1)  # Klick auf die Zeile: die Anzeige lädt, das Rollo fährt unter der Zeile herunter
    assert seite._web_url and seite._rollo_zeile == 0 and not seite.rollo.isHidden()
    ziel = seite.anim.endValue()
    zeile = seite.tab.visualRect(seite.tab.model().index(0, 0))
    assert ziel.y() == zeile.bottom() + 1 and ziel.height() > 0
    seite._klick(0, 1)  # nochmal Klick: es fährt hoch
    assert seite._rollo_zeile is None and seite.anim.endValue().height() == 0
    seite.anim.setCurrentTime(seite.anim.duration())
    assert seite.rollo.isHidden() and seite._web_url == ""


@pytest.fixture
def app_fenster(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from jobsuche import config
    from jobsuche.ui import fenster
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "f.db")
    w = fenster.Hauptfenster(einrichten=False)
    w.meldungen = []
    w.melde = w.meldungen.append
    yield w
    w.close()


def test_vorauswahl_der_bundeslaender(conn):
    assert wohnung.Filter().laender == ("Nordrhein-Westfalen", "Niedersachsen")
    assert wohnung.lade_filter(conn).laender == ("Nordrhein-Westfalen", "Niedersachsen")
    assert set(wohnung.STANDARD_LAENDER) <= set(wohnung.BUNDESLAENDER)


def _bewerbung(conn, name, sitz, status, gesendet="2026-11-01T10:00:00", score=None):
    from jobsuche import firmen
    fid = firmen.lege_an(conn, name, email=f"info@{name.split()[0].lower()}.example", sitz=sitz)
    if status:
        conn.execute("INSERT INTO entwuerfe (firma_id, empfaenger, betreff, text, ki_text, modell, status, erstellt, gesendet_am) "
                     "VALUES (?, 'a@b.example', 'b', 't', 't', 'm', ?, ?, ?)", (fid, status, gesendet, gesendet))
    if score is not None:
        conn.execute("UPDATE firmen SET mein_score = ? WHERE id = ?", (score, fid))
    conn.commit()
    return fid


def test_ort_aus_sitz():
    assert wohnung.ort_aus_sitz("Musterweg 1, 12345 Musterstadt") == "12345 Musterstadt"
    assert wohnung.ort_aus_sitz("Beispielstadt") == "Beispielstadt"
    assert wohnung.ort_aus_sitz("verschiedene Arbeitsorte") == "" and wohnung.ort_aus_sitz("  ") == ""
    assert wohnung.ort_aus_sitz("Musterhausen (Niederrhein) (+1 weiterer Standort)") == "Musterhausen (Niederrhein)"
    assert wohnung.ort_aus_sitz("Beispielstadt (+3 weitere Standorte)") == "Beispielstadt"


def test_arbeitsorte_aus_bewerbungen_mit_schonfrist(conn):
    from datetime import date
    _bewerbung(conn, "Aktiv GmbH", "Allee 2, 11111 Aktivstadt", "gesendet")
    _bewerbung(conn, "Antwort AG", "22222 Antwortstadt", "antwort")
    fid = _bewerbung(conn, "Absage GmbH", "33333 Absagestadt", "absage")
    db.protokolliere(conn, "Status geändert", "absage", fid)
    conn.execute("UPDATE ereignisse SET zeit = '2026-11-20T09:00:00' WHERE firma_id = ?", (fid,))
    _bewerbung(conn, "Entwurf KG", "44444 Entwurfstadt", "entwurf")      # nur ein Entwurf: zählt nicht
    _bewerbung(conn, "Gemocht GmbH", "55555 Mochstadt", None, score=80)  # selbst hoch bewertet
    _bewerbung(conn, "Egal GmbH", "66666 Egalstadt", None, score=40)
    _bewerbung(conn, "Ohne Sitz GmbH", "", "gesendet")
    conn.commit()
    orte = dict(wohnung.arbeitsorte_aus_bewerbungen(conn, heute=date(2026, 12, 1)))
    assert orte == {"11111 Aktivstadt": "Aktiv GmbH (Bewerbung)", "22222 Antwortstadt": "Antwort AG (Bewerbung)",
                    "33333 Absagestadt": "Absage GmbH (Bewerbung, abgeschlossen)", "55555 Mochstadt": "Gemocht GmbH (interessant)"}
    # 30 Tage nach der Absage fällt die Firma raus, offene Bewerbungen bleiben.
    spaeter = dict(wohnung.arbeitsorte_aus_bewerbungen(conn, heute=date(2026, 12, 25)))
    assert "33333 Absagestadt" not in spaeter and "11111 Aktivstadt" in spaeter


def test_arbeitsorte_von_hand_und_automatisch(conn):
    wohnung.speichere(conn, [{"kennung": "x1", "portal": "Immowelt", "titel": "T", "ort": "O", "plz": "", "kalt": 500,
                              "warm": None, "flaeche": 60, "zimmer": 2, "merkmale": "", "url": "u", "datum": "2026-11-22"}])
    conn.execute("UPDATE wohnungen SET km = 12, km_ziel = 'alt'")
    conn.commit()
    wohnung.setze_arbeitsorte(conn, ["77777 Handstadt"])
    assert not wohnung.arbeitsorte_geaendert(conn)  # gerade gesetzt: nichts zu tun
    _bewerbung(conn, "Neu GmbH", "88888 Neustadt", "freigegeben")
    conn.execute("UPDATE wohnungen SET km = 12")
    conn.commit()
    assert [o for o, _ in wohnung.alle_arbeitsorte(conn)] == ["77777 Handstadt", "88888 Neustadt"]
    assert wohnung.arbeitsorte_geaendert(conn)  # neue Bewerbung: Wege neu rechnen
    assert conn.execute("SELECT km FROM wohnungen").fetchone()[0] is None
    assert not wohnung.arbeitsorte_geaendert(conn)
    wohnung.setze_arbeitsorte_automatisch(conn, False)
    assert wohnung.alle_arbeitsorte(conn) == [("77777 Handstadt", "von Hand")]
    assert wohnung.arbeitsorte_geaendert(conn)


def test_seite_zeigt_arbeitsorte_aus_bewerbungen(app_fenster, monkeypatch):
    f = app_fenster
    from jobsuche.ui import fenster
    gestartet = []
    monkeypatch.setattr(f, "starte", lambda titel, *a, **k: gestartet.append(titel))
    _bewerbung(f.conn, "Muster GmbH", "Weg 1, 12345 Musterstadt", "gesendet")
    f.zeige_seite(fenster.WohnungSeite)
    seite = f.stapel.currentWidget()
    seite.aktualisieren()
    assert seite.auto_orte.isChecked()
    assert "12345 Musterstadt – Muster GmbH (Bewerbung)" in seite.orte_liste.text()
    assert gestartet == ["Arbeitsweg berechnen"]
    seite.auto_orte.setChecked(False)
    assert seite.orte_liste.text() == "" and not wohnung.arbeitsorte_automatisch(f.conn)
