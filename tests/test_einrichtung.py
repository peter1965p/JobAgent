"""System-Erkennung und Einrichtung. Nichts wird wirklich installiert."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from jobsuche import anmeldung, config, db, einrichtung, mailkonten, person, system
from jobsuche.system import System
from jobsuche.ui import anmelden, start


def _sys(art="linux", familie="debian", ram=16.0, frei=100.0):
    return System(art, "Testsystem", familie, "x86_64", ram, frei, "CPU", "3.14")


def _app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("text,familie", [
    ('ID=cachyos\nID_LIKE=arch\nPRETTY_NAME="CachyOS Linux"', "arch"),
    ('ID=arch', "arch"), ('ID=manjaro\nID_LIKE="arch"', "arch"), ('ID=endeavouros\nID_LIKE=arch', "arch"),
    ('ID=ubuntu\nID_LIKE=debian', "debian"), ('ID=debian', "debian"), ('ID=linuxmint\nID_LIKE="ubuntu debian"', "debian"),
    ('ID=fedora', "fedora"), ('ID="rocky"\nID_LIKE="rhel centos fedora"', "fedora"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"', "suse"), ('ID=nixos', "nixos"), ('ID=gentoo', "andere"), ('', "andere"),
])
def test_distributionen(text, familie):
    assert system.familie_aus(system.lies_os_release(text)) == familie


def test_erkennung_auf_diesem_rechner_und_mit_os_release(tmp_path):
    datei = tmp_path / "os-release"
    datei.write_text('PRETTY_NAME="Testlinux 1"\nID=fedora\n', encoding="utf-8")
    s = system.erkenne(tmp_path, datei)
    assert s.ram_gb > 0 and s.frei_gb > 0 and s.architektur and s.python
    if s.art == "linux":
        assert (s.name, s.familie) == ("Testlinux 1", "fedora")
    assert "RAM" in s.beschreibung()


@pytest.mark.parametrize("art,familie,vorhanden,erwartet", [
    ("linux", "arch", {"pacman"}, "pacman -S"),
    ("linux", "debian", set(), "install.sh"),
    ("linux", "fedora", set(), "install.sh"),
    ("linux", "nixos", set(), "services.ollama"),
    ("windows", "andere", {"winget"}, "winget install"),
    ("windows", "andere", set(), "ollama.com/download"),
    ("macos", "andere", {"brew"}, "brew install"),
    ("macos", "andere", set(), "ollama.com/download"),
    ("andere", "andere", set(), "ollama.com/download"),
])
def test_ollama_weg_pro_system(monkeypatch, art, familie, vorhanden, erwartet):
    monkeypatch.setattr(einrichtung.shutil, "which", lambda b: b if b in vorhanden else None)
    assert erwartet in einrichtung.ollama_schritt(_sys(art, familie)).zeigt


def test_ohne_automatischen_weg_kommt_der_link_als_fehler(monkeypatch):
    monkeypatch.setattr(einrichtung.shutil, "which", lambda b: None)
    with pytest.raises(einrichtung.EinrichtFehler, match="ollama.com/download"):
        einrichtung.ollama_schritt(_sys("windows", "andere")).ausfuehren(lambda t: None, lambda p: None)


def test_ohne_pkexec_nennt_die_app_den_befehl(monkeypatch):
    monkeypatch.setattr(einrichtung.shutil, "which", lambda b: None)
    with pytest.raises(einrichtung.EinrichtFehler, match="sudo pacman"):
        einrichtung._als_admin(["pacman", "-S", "ollama"])


def _ergebnis(fehlt):
    alle = ("ram", "platz", "ollama", "dienst", "modell", "embedding", "schluesselbund", "starter")
    return [einrichtung.Pruefung(k, k, k not in fehlt, "", pflicht=k not in ("ram", "schluesselbund", "starter")) for k in alle]


def test_nichts_fehlt_kein_plan_und_startklar(tmp_path):
    p = _ergebnis(set())
    assert einrichtung.plan(p, _sys(), tmp_path / "i.png") == [] and einrichtung.ist_startklar(p)


def test_plan_in_richtiger_reihenfolge(tmp_path, monkeypatch):
    monkeypatch.setattr(einrichtung.shutil, "which", lambda b: "pacman" if b == "pacman" else None)
    schritte = einrichtung.plan(_ergebnis({"ollama", "dienst", "modell", "embedding", "starter"}), _sys("linux", "arch"), tmp_path / "i.png")
    assert [s.titel.split()[0] for s in schritte] == ["Ollama", "Ollama-Dienst", "Sprachmodell", "Suchmodell", "Eintrag"]
    assert all(s.zeigt for s in schritte)  # Jeder Schritt sagt vorher, was er tut.


def test_dienst_fehlt_allein_installiert_nicht_neu(tmp_path):
    assert [s.titel for s in einrichtung.plan(_ergebnis({"dienst"}), _sys(), tmp_path / "i.png")] == ["Ollama-Dienst starten"]


def test_nur_empfehlung_fehlt_gilt_als_startklar():
    assert einrichtung.ist_startklar(_ergebnis({"ram", "schluesselbund", "starter"}))
    assert not einrichtung.ist_startklar(_ergebnis({"ollama"}))


def test_modell_download_meldet_fortschritt_und_fehler():
    zeilen = [{"status": "pulling manifest"}, {"status": "x", "total": 200, "completed": 50}, {"status": "x", "total": 200, "completed": 200}]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="\n".join(json.dumps(z) for z in zeilen))))
    meldungen, prozente = [], []
    einrichtung.ziehe_modell("m", meldungen.append, prozente.append, client=client)
    assert meldungen == ["pulling manifest"] and prozente == [25, 100]
    kaputt = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='{"error": "file does not exist"}')))
    with pytest.raises(einrichtung.EinrichtFehler, match="does not exist"):
        einrichtung.ziehe_modell("m", print, print, client=kaputt)


def test_fehlgeschlagener_befehl_wird_gemeldet():
    with pytest.raises(einrichtung.EinrichtFehler, match="gibtsnicht"):
        einrichtung._laeuft(["gibtsnicht-befehl-xyz"], print)
    import sys
    with pytest.raises(einrichtung.EinrichtFehler, match="Fehler 3"):
        einrichtung._laeuft([sys.executable, "-c", "print('x'); raise SystemExit(3)"], lambda t: None)
    ausgabe = []
    einrichtung._laeuft([sys.executable, "-c", "print('hallo')"], ausgabe.append)
    assert ausgabe == ["hallo"]


def test_warten_auf_dienst_gibt_irgendwann_auf(monkeypatch):
    monkeypatch.setattr(einrichtung, "ollama_laeuft", lambda: False)
    with pytest.raises(einrichtung.EinrichtFehler, match="antwortet nicht"):
        einrichtung.warte_auf_dienst(print, versuche=2, warten=lambda s: None)


def test_startmenue_eintrag(monkeypatch, tmp_path):
    monkeypatch.setattr(einrichtung.Path, "home", classmethod(lambda c: tmp_path))
    einrichtung.lege_starter_an(tmp_path / "i.png", lambda t: None)
    text = einrichtung.starterpfad().read_text(encoding="utf-8")
    assert "Name=Job Agent" in text and "-m jobsuche" in text and f"Icon={tmp_path / 'i.png'}" in text


# --- Startskript ---

@pytest.fixture
def startskript():
    spec = importlib.util.spec_from_file_location("start_skript", Path(__file__).resolve().parent.parent / "start.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_startskript_installiert_nur_bei_bedarf(startskript, tmp_path):
    anforderungen = tmp_path / "r.txt"
    anforderungen.write_text("a\n", encoding="utf-8")
    ordner = tmp_path / "venv"
    assert startskript.muss_installieren(ordner, anforderungen)  # Keine Umgebung.
    python = startskript.venv_python(ordner)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    assert startskript.muss_installieren(ordner, anforderungen)  # Keine Marke.
    (ordner / ".tueroeffner-anforderungen").write_text(startskript.pruefsumme(anforderungen))
    assert not startskript.muss_installieren(ordner, anforderungen)
    anforderungen.write_text("a\nb\n", encoding="utf-8")
    assert startskript.muss_installieren(ordner, anforderungen)  # Anforderungen geändert.


def test_startskript_ist_ohne_fremde_pakete_lauffaehig():
    quelle = (Path(__file__).resolve().parent.parent / "start.py").read_text(encoding="utf-8")
    for zeile in quelle.splitlines():
        if zeile.startswith(("import ", "from ")):
            assert zeile.split()[1].split(".")[0] in ("hashlib", "subprocess", "sys", "venv", "pathlib")


# --- Oberfläche ---

def test_splash_und_pinguin_zeichnen_wirklich():
    _app()
    bild = start.pinguin_bild(64).toImage()
    gefaerbt = sum(bild.pixelColor(x, y).alpha() > 0 for x in range(64) for y in range(64))
    assert 500 < gefaerbt < 64 * 64  # Etwas gezeichnet, aber nicht die ganze Fläche.
    s = start.Splash()
    assert not s.bild.isNull()
    s.melde("Prüfe …")
    s.show()
    QApplication.processEvents()
    assert not s.grab().isNull() and s.status == "Prüfe …"
    s.close()


def test_splash_haelt_mindestdauer_und_fuellt_den_balken_erst_nach_der_pruefung(monkeypatch):
    import time
    _app()
    monkeypatch.setattr(start.Splash, "MIN_DAUER_MS", 300)
    s = start.Splash()
    s.show()
    s.laufe()
    ende = time.time() + 0.6
    while time.time() < ende:
        QApplication.processEvents()
    assert s.fortschritt == 0.9  # ohne fertige Prüfung nie voll
    erledigt = []
    s.fertig(lambda: erledigt.append(True))
    ende = time.time() + 2
    while not erledigt and time.time() < ende:
        QApplication.processEvents()
    assert erledigt == [True] and s.fortschritt == 1.0
    s.close()


def test_einrichtfenster_zeigt_plan_und_installiert_erst_nach_klick(tmp_path, monkeypatch):
    _app()
    aufrufe = []
    monkeypatch.setattr(einrichtung, "ollama_schritt", lambda s: einrichtung.Schritt("Ollama installieren", "sudo testbefehl", lambda m, f: aufrufe.append("ollama")))
    monkeypatch.setattr(einrichtung, "starte_dienst", lambda m, _=None: aufrufe.append("dienst"))
    monkeypatch.setattr(einrichtung, "warte_auf_dienst", lambda m, _=None: aufrufe.append("warten"))
    monkeypatch.setattr(einrichtung, "pruefe", lambda s: _ergebnis(set()))
    f = start.EinrichtFenster(_sys(), _ergebnis({"ollama"}), tmp_path / "i.png")
    assert "sudo testbefehl" in f.plan_text.text() and "Testsystem" in f.system_text.text()
    assert aufrufe == [] and "Ohne KI" in f.weiter.text()  # Noch nichts passiert.
    f.einrichten()
    import time
    ende = time.time() + 5
    while (f.pool.activeThreadCount() or not f.los.isEnabled()) and time.time() < ende:
        QApplication.processEvents()
        time.sleep(0.01)
    assert aufrufe == ["ollama", "dienst", "warten"] and f.schritte == [] and f.weiter.text() == "Weiter"


def test_einrichtfenster_zeigt_fehler_und_prueft_neu(tmp_path, monkeypatch):
    _app()

    def scheitert(m, f):
        raise einrichtung.EinrichtFehler("Passwortdialog abgebrochen")

    monkeypatch.setattr(einrichtung, "ollama_schritt", lambda s: einrichtung.Schritt("Ollama installieren", "x", scheitert))
    monkeypatch.setattr(einrichtung, "pruefe", lambda s: _ergebnis({"ollama"}))
    f = start.EinrichtFenster(_sys(), _ergebnis({"ollama"}), tmp_path / "i.png")
    f.einrichten()
    import time
    ende = time.time() + 5
    while not f.los.isEnabled() and time.time() < ende:
        QApplication.processEvents()
        time.sleep(0.01)
    assert "Passwortdialog abgebrochen" in f.log.toPlainText() and f.los.isVisibleTo(f)


def test_neue_adresse_legt_konto_mit_namen_an(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(config, "DATEN", tmp_path)
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "x.db")
    d = anmelden.AnmeldeFenster()
    d.show()
    d.email.setText("neu@x.de")
    assert d.name.isVisible() and d.anmelden_knopf.text() == "Konto anlegen"
    konto = mailkonten.Konto("T", "neu@x.de", "m", 587, "starttls", "neu@x.de")
    ergebnis = anmeldung.Ergebnis(anmeldung.datenbank_fuer("neu@x.de"), konto)
    anmeldung.oeffne(ergebnis, "neu@x.de", "pw", "Kim Neu")
    conn = db.verbinden(ergebnis.pfad)
    assert person.lade(conn)["name"] == "Kim Neu" and person.lade(conn)["email"] == "neu@x.de"
    conn.close()
    d.email.setText("")
    d.email.setText("neu@x.de")
    assert not d.name.isVisible() and d.anmelden_knopf.text() == "Anmelden"  # Beim zweiten Mal Anmeldung.
    d.close()


def test_wache_verhindert_echte_systemeingriffe():
    import subprocess
    with pytest.raises(AssertionError, match="wirklich"):
        subprocess.Popen(["pkexec", "systemctl", "enable", "--now", "ollama"])
    with pytest.raises(AssertionError, match="wirklich"):
        einrichtung._laeuft(["/usr/bin/systemctl", "status"], print)


def test_splash_protokoll_ersetzt_laufende_zeile_durch_ergebnis():
    _app()
    s = start.Splash()
    s.melde("… Verbinde mit dem Ollama-Dienst")
    s.melde("✓ Ollama-Dienst: läuft")
    s.melde("… Frage Claude Code an")
    assert s.protokoll == ["✓ Ollama-Dienst: läuft", "… Frage Claude Code an"]
    for i in range(10):
        s.melde(f"✓ Schritt {i}")
    assert len(s.protokoll) == s.ZEILEN + 1 and s.protokoll[-1] == "✓ Schritt 9"
    s.show()
    QApplication.processEvents()
    assert not s.grab().isNull()
    s.close()


def test_pruefung_meldet_jeden_schritt(monkeypatch):
    monkeypatch.setattr(einrichtung, "ollama_laeuft", lambda: True)
    monkeypatch.setattr(einrichtung, "sprachmodell_da", lambda: False)
    monkeypatch.setattr(einrichtung.llm, "claude_status", lambda: "")
    monkeypatch.setattr(einrichtung, "schluesselbund_da", lambda: True)
    meldungen = []
    info = system.erkenne(None)
    pruefungen = einrichtung.pruefe(info, meldungen.append)
    assert "… Verbinde mit dem Ollama-Dienst" in meldungen and "✓ Ollama-Dienst: läuft" in meldungen
    assert any(m.startswith("✗ Sprachmodell") for m in meldungen)
    assert len([m for m in meldungen if m[:2] in ("✓ ", "✗ ", "• ")]) == len(pruefungen)
