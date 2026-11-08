import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import sqlite_vec

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Tests dürfen die echten App-Einstellungen nicht anfassen.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="tueroeffner-test-")

from jobsuche import config, db, firmen, person, quellen, rag, suche  # noqa: E402


def _fake_embed(texte):
    """Deterministische Wort-Hash-Vektoren: schnell und ohne Modell-Download."""
    ergebnis = []
    for text in texte:
        v = [0.0] * 64
        for wort in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(wort.encode()).hexdigest(), 16) % 64] += 1.0
        v[0] += 0.001
        ergebnis.append(sqlite_vec.serialize_float32(v))
    return ergebnis


@pytest.fixture(autouse=True)
def schnelle_embeddings(monkeypatch):
    monkeypatch.setattr(rag, "embedde", _fake_embed)


@pytest.fixture(autouse=True)
def keine_hoeflichkeitspausen(monkeypatch):
    monkeypatch.setattr(firmen, "SEITEN_PAUSE", 0)
    monkeypatch.setattr(quellen, "NOMINATIM_PAUSE", 0)
    monkeypatch.setattr(quellen, "OVERPASS_PAUSE", 0)
    # Tests dürfen nicht von einem Suchdienst abhängen, der zufällig auf dem Rechner läuft.
    monkeypatch.setattr(quellen, "finde_lokale_instanz", lambda: "")
    # Übernommene Jobbörsen-Treffer laden ihre Anzeige. Ohne Fake-Client darf das nicht die echte Schnittstelle treffen.
    monkeypatch.setattr(suche, "DETAILS", "http://127.0.0.1:9/jobdetails/{}")
    monkeypatch.setattr(suche, "ANZEIGEN_SUCHE", "http://127.0.0.1:9/firmenanzeigen")


@pytest.fixture(autouse=True)
def kein_echter_datenordner(monkeypatch, tmp_path_factory):
    """Erzeugte Dateien wie der Lebenslauf für eine Stelle landen neben der DB. Nie in Peters `daten/`."""
    monkeypatch.setattr(config, "DB_PFAD", tmp_path_factory.mktemp("daten") / "jobsuche.db")


@pytest.fixture(autouse=True)
def keine_echten_systemeingriffe(monkeypatch):
    """Tests dürfen nie Administratorrechte anfordern, Dienste starten oder wirklich installieren."""
    echt = subprocess.Popen

    def wache(befehl, *args, **kwargs):
        name = Path(str(befehl[0] if isinstance(befehl, (list, tuple)) else befehl)).name
        if name in ("pkexec", "sudo", "systemctl", "ollama", "pacman", "winget", "brew"):
            raise AssertionError(f"Test wollte wirklich ausführen: {befehl}")
        return echt(befehl, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", wache)


@pytest.fixture
def conn():
    verbindung = db.verbinden(":memory:")
    person.speichere(verbindung, name="Test Person", email="test@example.org")
    yield verbindung
    verbindung.close()


@pytest.fixture
def firma(conn):
    cursor = conn.execute(
        "INSERT INTO firmen (name, sitz, personen, email, website, passung, warum) VALUES "
        "('Muster IT GmbH', 'Bremen', 'Alex Muster, Kim Beispiel', 'info@muster-it.de', "
        "'https://www.muster-it.de', 'Hoch', 'KI-Lösungen und Cloud')"
    )
    conn.commit()
    return cursor.lastrowid
