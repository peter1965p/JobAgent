"""Gehaltsangaben erkennen; gespeicherte Suchen mit Ergebnis und NEU-Markierung."""
import pytest

from jobsuche import db, gehalt, suchen


@pytest.mark.parametrize("text, erwartet, jahr", [
    ("48.000 € pro Jahr", "48.000 €/Jahr", 48000),
    ("20 € pro Stunde", "20 €/Std.", 41600),
    ("3.500 – 4.200 € brutto/Monat", "3.500–4.200 €/Monat", 46200),
    ("ab 60.000 € jährlich", "ab 60.000 €/Jahr", 60000),
    ("€ 25,50/Std.", "25,50 €/Std.", 53040),
    ("bis zu 70.000 Euro im Jahr", "bis 70.000 €/Jahr", 70000),
    ("55.000 € - 65.000 € (Arbeitgeberangabe)", "55.000–65.000 €/Jahr", 60000),
    ("Gehalt: 45.000 €", "45.000 €/Jahr", 45000),
])
def test_gehalt_wird_erkannt_und_vereinheitlicht(text, erwartet, jahr):
    assert gehalt.erkenne(text) == {"text": erwartet, "jahr": jahr}


@pytest.mark.parametrize("text", ["Preis 9,99 €", "Bonus 500 €", "Weihnachtsbonus 1.000 € pro Jahr", "12345 Euro", "Vollzeit", ""])
def test_kein_gehalt_ohne_gehaltsangabe(text):
    assert gehalt.erkenne(text) is None


def test_ergaenze_nimmt_zusatzzeilen_und_beschreibung():
    t = gehalt.ergaenze({"titel": "Admin (m/w/d)", "beschreibung": "Wir zahlen 4.000 € brutto im Monat."})
    assert t["gehalt"] == "4.000 €/Monat" and t["gehalt_jahr"] == 48000
    assert "gehalt" not in gehalt.ergaenze({"titel": "Admin", "beschreibung": "Tolles Team"})


@pytest.fixture
def conn(tmp_path):
    c = db.verbinden(tmp_path / "t.db")
    yield c
    c.close()


def test_suche_speichern_laden_umbenennen_loeschen(conn):
    db.setze_einstellung(conn, "suche.begriffe", "Admin, Linux")
    db.setze_einstellung(conn, "suche.ort", "Bremen")
    s = suchen.speichern(conn, "  Admin   Bremen ", nachts=True)
    assert s["name"] == "Admin Bremen" and s["angaben"]["begriffe"] == "Admin, Linux" and s["nachts"]
    db.setze_einstellung(conn, "suche.begriffe", "Koch")
    assert suchen.laden(conn, s["id"])["name"] == "Admin Bremen"
    assert db.einstellung(conn, "suche.begriffe", "") == "Admin, Linux" and suchen.aktiv(conn) == s["id"]
    zweite = suchen.speichern(conn, "admin bremen", angaben={"begriffe": "Admin"})        # gleicher Name: überschreiben
    assert zweite["id"] == s["id"] and len(suchen.liste(conn)) == 1 and zweite["nachts"]
    suchen.speichern(conn, "KI remote")
    with pytest.raises(suchen.SuchenFehler):
        suchen.aendern(conn, s["id"], name="KI Remote")
    suchen.aendern(conn, s["id"], name="Admin Nord", nachts=False)
    assert [x["name"] for x in suchen.liste(conn)] == ["Admin Nord", "KI remote"] and suchen.nachts(conn) == []
    suchen.loeschen(conn, s["id"])
    assert [x["name"] for x in suchen.liste(conn)] == ["KI remote"] and suchen.aktiv(conn) == suchen.ZULETZT
    with pytest.raises(suchen.SuchenFehler):
        suchen.speichern(conn, "   ")


def test_ergebnis_merken_markiert_neue_treffer(conn):
    s = suchen.speichern(conn, "Admin")
    erst = suchen.ergebnis_merken(conn, s["id"], {"treffer": [{"firma": "Muster GmbH", "titel": "Admin"}], "hinweise": ["x"]})
    assert erst["neu"] is None and erst["treffer"][0]["neu"] is False                 # erster Lauf: nichts ist „neu“
    zweit = suchen.ergebnis_merken(conn, s["id"], {"treffer": [{"firma": "Muster GmbH", "titel": "Admin"},
                                                               {"firma": "Beispiel AG", "titel": "Admin"}]})
    assert zweit["neu"] == 1 and [t["neu"] for t in zweit["treffer"]] == [False, True]
    gemerkt = suchen.letztes_ergebnis(conn, s["id"])
    assert len(gemerkt["treffer"]) == 2 and suchen.hole(conn, s["id"])["anzahl"] == 2 and suchen.hole(conn, s["id"])["neu"] == 1
    suchen.ergebnis_merken(conn, suchen.ZULETZT, {"treffer": [{"firma": "A", "titel": "B"}]})
    assert suchen.letztes_ergebnis(conn, suchen.ZULETZT)["treffer"][0]["firma"] == "A"
