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

# Zweiter Lauf gegen PostgreSQL: JOBSUCHE_TEST_PG="host=… dbname=jobsuche_test user=…" (Passwort aus dem
# Schlüsselbund, Dienst „jobsuche“, oder JOBSUCHE_TEST_PG_PASSWORT). Ohne die Variable läuft alles auf SQLite.
_PG = None
if os.environ.get("JOBSUCHE_TEST_PG"):
    _werte = dict(t.split("=", 1) for t in os.environ["JOBSUCHE_TEST_PG"].split())
    _PG = {"art": "postgres", "host": _werte["host"], "port": int(_werte.get("port", 5432)),
           "datenbank": _werte["dbname"], "benutzer": _werte["user"]}
    if os.environ.get("JOBSUCHE_TEST_PG_PASSWORT"):
        _PG["passwort"] = os.environ["JOBSUCHE_TEST_PG_PASSWORT"]
    else:
        import keyring
        _PG["passwort"] = keyring.get_password("jobsuche", f"postgres:{_PG['benutzer']}@{_PG['host']}") or ""


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


@pytest.fixture(scope="session", autouse=True)
def saubere_testdatenbank():
    """Vor und nach dem Postgres-Lauf: alle Test-Schemata weg."""
    if not _PG:
        yield
        return
    from jobsuche import pg

    def aufraeumen():
        with pg.roh_verbinden(_PG) as roh:
            for (name,) in roh.execute("SELECT schema_name FROM information_schema.schemata "
                                       "WHERE schema_name LIKE 'js\\_%'").fetchall():
                roh.execute(f'DROP SCHEMA "{name}" CASCADE')
    aufraeumen()
    yield
    aufraeumen()


@pytest.fixture(autouse=True)
def speicherort(monkeypatch):
    """Nie Peters echte Einstellung (daten/datenbank.json): SQLite, oder im Postgres-Lauf die Test-Datenbank."""
    monkeypatch.setattr(db, "_server", _PG)
    monkeypatch.setattr(db, "MEMORY_AUF_SERVER", bool(_PG))
    if _PG:
        # In der App zählt der Pfad relativ zu daten/. Tests legen aber viele daten/-Ordner an: absolut trennen.
        from jobsuche import pg
        echt = pg.schema_fuer
        monkeypatch.setattr(pg, "schema_fuer", lambda pfad, daten: echt(pfad, Path("/")))


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
def keine_echten_systemeingriffe(monkeypatch, tmp_path_factory):
    """Tests dürfen nie Administratorrechte anfordern, Dienste starten oder wirklich installieren."""
    echt = subprocess.Popen

    def wache(befehl, *args, **kwargs):
        name = Path(str(befehl[0] if isinstance(befehl, (list, tuple)) else befehl)).name
        if name in ("pkexec", "sudo", "systemctl", "ollama", "pacman", "winget", "brew", "claude"):
            raise AssertionError(f"Test wollte wirklich ausführen: {befehl}")
        return echt(befehl, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", wache)
    # Claude ginge an Anthropic und kostet Kontingent. Tests sehen es nie als verfügbar.
    from jobsuche import llm
    monkeypatch.setattr(llm, "claude_status", lambda: "Claude Code ist nicht installiert.")
    ordner = tmp_path_factory.mktemp("claude")
    monkeypatch.setattr(llm, "_ordner", lambda: ordner)  # Nutzungsstand der echten App bleibt unberührt
    monkeypatch.setattr(llm, "AUSWEICHEN", [])


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
