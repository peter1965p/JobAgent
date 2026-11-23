"""0.14: mehr Treffer von der Jobbörse, Grenze je Suche, Seiten in der Trefferliste und die Sicherungskarte."""
import sqlite3
import time
from datetime import datetime, timedelta

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from jobsuche import config, db, person, sicherung, suche
from jobsuche.ui import fenster


# --- Jobbörse: mehrere Seiten, ohne leere Folgeabfragen ---

def _stellen(start, anzahl):
    return [{"referenznummer": f"R{i}", "firma": f"Firma {i} GmbH", "stellenangebotsTitel": "Admin"} for i in range(start, start + anzahl)]


def test_jobboerse_holt_weitere_seiten_bis_die_liste_endet():
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        seite = int(anfrage.url.params["page"])
        return httpx.Response(200, json={"maxErgebnisse": 150, "ergebnisliste": _stellen((seite - 1) * 100, 100 if seite == 1 else 50)})
    treffer = suche.suche("Admin", "Bremen", 50, client=httpx.Client(transport=httpx.MockTransport(handler)), seiten=3)
    assert len(treffer) == 150
    assert [a.url.params["page"] for a in anfragen] == ["1", "2"]  # Seite 3 wird gar nicht erst gefragt
    assert anfragen[0].url.params["size"] == "100"


def test_jobboerse_fragt_keine_zweite_seite_wenn_die_erste_alles_hatte():
    anfragen = []

    def handler(anfrage):
        anfragen.append(anfrage)
        return httpx.Response(200, json={"maxErgebnisse": 7, "ergebnisliste": _stellen(0, 7)})
    assert len(suche.suche("Admin, KI", "Bremen", client=httpx.Client(transport=httpx.MockTransport(handler)))) == 7
    assert len(anfragen) == 2  # je Suchwort eine Seite


# --- Grenze je Suche ---

def test_maximale_treffer_kappt_und_sagt_es(conn):
    antwort = {"ergebnisliste": _stellen(0, 30)}
    client = httpx.Client(transport=httpx.MockTransport(lambda a: httpx.Response(200, json=antwort)))
    db.setze_einstellung(conn, "suche.max_treffer", "10")
    ergebnis = suche.suche_ueberall(conn, suche.Auftrag("Admin", "", quellen=("jobboerse",), max_tage=0), client=client)
    assert len(ergebnis["treffer"]) == 10
    assert any("30 gefunden, die besten 10" in h for h in ergebnis["hinweise"])
    db.setze_einstellung(conn, "suche.max_treffer", "0")
    assert len(suche.suche_ueberall(conn, suche.Auftrag("Admin", "", quellen=("jobboerse",), max_tage=0), client=client)["treffer"]) == 30


def test_maximale_treffer_vorgabe_und_unsinn(conn):
    assert suche.max_treffer(conn) == suche.MAX_TREFFER == 200
    db.setze_einstellung(conn, "suche.max_treffer", "abc")
    assert suche.max_treffer(conn) == 200


# --- Seiten in der Trefferliste ---

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
    ende = time.time() + 10
    while w.warteschlange and time.time() < ende:
        app.processEvents()
        time.sleep(0.01)
    w.conn.close()


def _treffer(n):
    # Absichtlich nicht nach Firma sortiert geliefert: Firma 119 zuerst
    return [{"referenz": f"R{i}", "firma": f"Firma {i:03d} GmbH", "titel": "Admin", "beruf": "", "ort": "Bremen",
             "entfernung": i, "url": "u", "naehe": i % 100, "bekannt": False} for i in reversed(range(n))]


def test_trefferliste_zeigt_seiten_und_blaettert(app, f):
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.gefunden({"treffer": _treffer(120), "hinweise": []})
    assert s.tab.rowCount() == 50 and s.seiten_info.text() == "Treffer 1–50 von 120"
    assert not s.zurueck.isEnabled() and s.weiter.isEnabled()
    s.weiter.click()
    assert s.seiten_info.text() == "Treffer 51–100 von 120"
    s._blaettere(2)
    assert s.tab.rowCount() == 20 and s.seiten_info.text() == "Treffer 101–120 von 120" and not s.weiter.isEnabled()
    s.pro_seite.setCurrentIndex(2)  # 100 pro Seite: zurück auf Seite 1
    assert s.tab.rowCount() == 100 and s.seiten_info.text() == "Treffer 1–100 von 120"


def test_sortieren_gilt_ueber_alle_seiten(app, f):
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.gefunden({"treffer": _treffer(120), "hinweise": []})
    assert s.tab.item(0, 1).text() == "Firma 119 GmbH"
    s._sortiere(1)  # Firma aufsteigend: die kleinste aller 120 steht vorn, nicht die kleinste der ersten Seite
    assert s.tab.item(0, 1).text() == "Firma 000 GmbH"
    s._blaettere(2)
    assert s.tab.item(s.tab.rowCount() - 1, 1).text() == "Firma 119 GmbH"
    s._sortiere(1)  # nochmal: absteigend, wieder ab Seite 1
    assert s.seite_nr == 0 and s.tab.item(0, 1).text() == "Firma 119 GmbH"
    s._sortiere(4)  # km als Zahl, nicht als Text („100 km“ nach „99 km“)
    werte = [s.tab.item(r, 4).text() for r in range(s.tab.rowCount())]
    assert werte[:3] == [fenster.km_text({"entfernung": i}) for i in range(3)]
    # Auswahl führt zum richtigen Treffer, auch auf späteren Seiten
    s._blaettere(1)
    s.tab.selectRow(0)
    assert s._gewaehlt()["firma"] == "Firma 050 GmbH"


def test_neue_suche_beginnt_wieder_vorn(app, f):
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.gefunden({"treffer": _treffer(120), "hinweise": []})
    s._sortiere(1)
    s._blaettere(2)
    s.gefunden({"treffer": _treffer(30), "hinweise": []})
    assert s.seite_nr == 0 and s.sortierung is None and s.seiten_info.text() == "Treffer 1–30 von 30"


def test_maximale_treffer_feld_wird_gespeichert(app, f):
    f.zeige_seite(fenster.SucheSeite)
    s = f.stapel.currentWidget()
    s.max_treffer.setValue(350)
    s._auftrag()
    assert db.einstellung(f.conn, "suche.max_treffer", "") == "350"
    s.max_treffer.setValue(0)
    s.vorbelegt = False  # wie beim nächsten Öffnen der App: Felder aus den Einstellungen
    s.aktualisieren()
    assert s.max_treffer.value() == 350


# --- Sicherung ---

def _lege(ordner, stempel, dbs=("jobsuche", "mhdsafety")):
    for name in dbs:
        (ordner / f"{name}_{stempel}.dump").write_bytes(b"x" * 2048)
    (ordner / f"globals_{stempel}.sql").write_text("--")


def test_stand_der_pg_sicherung(tmp_path):
    _lege(tmp_path, "2026-11-22_0330")
    _lege(tmp_path, "2026-11-23_0330", ("jobsuche",))
    (tmp_path / "LIESMICH.txt").write_text("x")
    stand = sicherung.stand_pg(tmp_path, jetzt=datetime(2026, 11, 23, 12, 0))
    assert stand["anzahl"] == 2 and stand["dbs"] == {"jobsuche": 2048} and not stand["warnung"]
    assert round(stand["alter_h"]) == 8
    text = sicherung.stand_text(stand)
    assert "23.11.2026 03:30" in text and "jobsuche 2,0 KB" in text and "2 Stände" in text
    alt = sicherung.stand_pg(tmp_path, jetzt=datetime(2026, 11, 25, 12, 0))
    assert alt["warnung"] and "Älter als 36 Stunden" in sicherung.stand_text(alt)


def test_keine_sicherung_ist_eine_warnung(tmp_path):
    assert sicherung.stand_pg(tmp_path / "fehlt")["warnung"]
    assert sicherung.stand_pg(tmp_path)["warnung"]


def test_sqlite_datei_sichern_und_alte_aufraeumen(tmp_path, monkeypatch):
    quelle = sqlite3.connect(":memory:")
    quelle.execute("CREATE TABLE t (x)")
    quelle.execute("INSERT INTO t VALUES (42)")
    quelle.commit()
    monkeypatch.setattr(sicherung, "SQLITE_BEHALTEN", 2)
    for i in range(3):
        (tmp_path / f"jobsuche_2026-01-0{i + 1}_000000.db").write_bytes(b"")
    datei = sicherung.sichere_sqlite(quelle, tmp_path)
    assert sqlite3.connect(datei).execute("SELECT x FROM t").fetchone()[0] == 42
    assert oct(datei.stat().st_mode)[-3:] == "600"
    assert len(list(tmp_path.glob("jobsuche_*.db"))) == 2 and datei.exists()


def test_systemseite_zeigt_die_sicherung(app, f):
    f.zeige_seite(fenster.SystemSeite)
    s = f.stapel.currentWidget()
    if db.server():  # Postgres-Lauf: Dienst fehlt im Test, also Knopf aus
        assert not s.sichern_knopf.isEnabled() and "pg-sicherung" in s.sicherung_ergebnis.text()
        _lege(sicherung.PG_ORDNER, datetime.now().strftime("%Y-%m-%d_%H%M"))
        s._lade_sicherung()
        assert "Letzte Sicherung" in s.sicherung_info.text() and s.sicherung_info.objectName() == "gedimmt"
    else:
        assert s.sichern_knopf.text() == "Datei sichern" and s.sichern_knopf.isEnabled()
        s.jetzt_sichern()
        ende = time.time() + 10
        while f.warteschlange and time.time() < ende:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert "Gesichert:" in s.sicherung_ergebnis.text()
        assert list(sicherung.SQLITE_ORDNER.glob("*.db"))
