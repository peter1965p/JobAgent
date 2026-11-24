"""Desktop mit Konto-Anmeldung: Anmeldefenster im Kontomodus, Konto-Bereich unten links. Ohne echten Server."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from jobsuche import anmeldung, config, llm, mandanten, versand
from jobsuche.ui import anmelden, fenster

KONTO = mandanten.Konto(7, "ich@example.org", "Kim Test", False)
M1 = mandanten.MandantInfo(1, "Erster", "js_a", "js_a", False)
M2 = mandanten.MandantInfo(2, "Zweiter", "js_b", "js_b", False)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_anmeldefenster_im_kontomodus(app, monkeypatch):
    from PySide6.QtCore import QSettings
    QSettings("jobsuche", "jobsuche").remove("letzte_adresse")
    monkeypatch.setattr(anmeldung, "konten_verfuegbar", lambda: True)
    monkeypatch.setattr(anmeldung, "gemerktes_passwort", lambda e, konto=False: "")
    gesehen = []
    monkeypatch.setattr(anmeldung, "merke_passwort", lambda e, p, m, konto=False: gesehen.append(konto))
    monkeypatch.setattr(anmeldung, "pruefe_konto", lambda e, p: anmeldung.KontoErgebnis(KONTO, M1, (M1, M2)))
    d = anmelden.AnmeldeFenster()
    try:
        assert d.kontomodus and d.pw.placeholderText() == "Konto-Passwort" and d.anmelden_knopf.text() == "Anmelden"
        d.email.setText("ich@example.org")
        d.pw.setText("ein-konto-passwort")
        d.anmelden()
        ende = time.time() + 5
        while d.ergebnis is None and time.time() < ende:
            app.processEvents()
            time.sleep(0.01)
        assert d.ergebnis.mandant == M1 and gesehen == [True]
    finally:
        d.pool.waitForDone(2000)
        d.close()


def test_anmeldefenster_kontomodus_fehler(app, monkeypatch):
    monkeypatch.setattr(anmeldung, "konten_verfuegbar", lambda: True)

    def falsch(e, p):
        raise versand.AnmeldeFehler("Adresse oder Passwort stimmt nicht.")
    monkeypatch.setattr(anmeldung, "pruefe_konto", falsch)
    d = anmelden.AnmeldeFenster()
    try:
        d.email.setText("ich@example.org")
        d.pw.setText("falsch-falsch-falsch")
        d.anmelden()
        ende = time.time() + 5
        while not d.meldung.isVisibleTo(d) and time.time() < ende:
            app.processEvents()
            time.sleep(0.01)
        assert "stimmt nicht" in d.meldung.text() and d.pw.text() == ""
    finally:
        d.pool.waitForDone(2000)
        d.close()


def test_kontobereich_unten_links(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "t.db")
    monkeypatch.setattr(llm, "modelle", lambda: ["qwen2.5:3b"])
    ergebnis = anmeldung.KontoErgebnis(KONTO, M1, (M1, M2))
    w = fenster.Hauptfenster(einrichten=False, konto=ergebnis)
    try:
        w.melde = lambda t: None
        assert w.konto_knopf.isVisibleTo(w)
        assert "Erster ▾" in w.fuss_text.text()
        d = fenster.KontoDialog(w)
        assert d.modell.count() >= 1
        geaendert = []
        monkeypatch.setattr(anmeldung, "aendere_konto_passwort", lambda e, a, n: geaendert.append((a, n)))
        monkeypatch.setattr(anmeldung, "gemerktes_passwort", lambda e, konto=False: "")
        d.alt.setText("altes-passwort-1")
        d.neu.setText("neues-passwort-12")
        d.neu2.setText("neues-passwort-12")
        d.passwort_aendern()
        assert geaendert == [("altes-passwort-1", "neues-passwort-12")] and d.meldung.text() == "Passwort geändert."
        d.modell.setCurrentIndex(0)
        d.ki_waehlen()
        d.close()
        monkeypatch.setattr(anmeldung, "wechsle_mandant", lambda e, mid: anmeldung.KontoErgebnis(KONTO, M2, (M1, M2)))
        w.wechsle_mandant(2)
        assert w.wechsel_ziel.mandant == M2 and w.abmelden_gewuenscht
    finally:
        w.conn.close()


def test_ohne_konto_kein_kontoknopf(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "t.db")
    w = fenster.Hauptfenster(einrichten=False)
    try:
        assert not w.konto_knopf.isVisibleTo(w) and "▾" not in w.fuss_text.text()
    finally:
        w.conn.close()
