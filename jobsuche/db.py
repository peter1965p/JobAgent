import contextvars
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlite_vec

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS einstellungen (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS berichte (id INTEGER PRIMARY KEY, datum TEXT NOT NULL, inhalt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nacht_gesehen (schluessel TEXT PRIMARY KEY, datum TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS dokumente (
    id INTEGER PRIMARY KEY,
    pfad TEXT NOT NULL UNIQUE,
    titel TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    eingelesen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS firmen (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sitz TEXT NOT NULL DEFAULT '',
    personen TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    passung TEXT NOT NULL DEFAULT '',
    warum TEXT NOT NULL DEFAULT '',
    quelle TEXT NOT NULL DEFAULT '',
    seiten_geladen TEXT,
    ki_score INTEGER,
    ki_begruendung TEXT,
    ki_belege TEXT,
    ki_ungeprueft TEXT,
    ki_modell TEXT,
    ki_zeit TEXT,
    mein_score INTEGER,
    meine_notiz TEXT
);

CREATE TABLE IF NOT EXISTS fakten (
    id INTEGER PRIMARY KEY,
    kategorie TEXT NOT NULL,
    text TEXT NOT NULL UNIQUE,
    quelle TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'vorgeschlagen'
        CHECK (status IN ('vorgeschlagen', 'bestaetigt', 'abgelehnt')),
    erstellt TEXT NOT NULL,
    bestaetigt_am TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    art TEXT NOT NULL CHECK (art IN ('dokument', 'firma', 'fakt')),
    dokument_id INTEGER REFERENCES dokumente(id) ON DELETE CASCADE,
    firma_id INTEGER REFERENCES firmen(id) ON DELETE CASCADE,
    fakt_id INTEGER REFERENCES fakten(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    quelle TEXT NOT NULL DEFAULT '',
    abruf TEXT,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_art ON chunks(art, firma_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS entwuerfe (
    id INTEGER PRIMARY KEY,
    firma_id INTEGER NOT NULL REFERENCES firmen(id) ON DELETE CASCADE,
    empfaenger TEXT NOT NULL,
    betreff TEXT NOT NULL,
    text TEXT NOT NULL,
    ki_text TEXT NOT NULL,
    modell TEXT NOT NULL,
    fakt_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'entwurf' CHECK (status IN
        ('entwurf', 'freigegeben', 'gesendet', 'antwort', 'absage', 'keine_antwort', 'verworfen')),
    erstellt TEXT NOT NULL,
    freigegeben_am TEXT,
    gesendet_am TEXT
);

CREATE TABLE IF NOT EXISTS lektionen (
    id INTEGER PRIMARY KEY,
    art TEXT NOT NULL CHECK (art IN ('regel', 'korrektur', 'bewertung', 'vorbild')),
    entwurf_id INTEGER UNIQUE REFERENCES entwuerfe(id) ON DELETE CASCADE,
    firma_id INTEGER REFERENCES firmen(id) ON DELETE CASCADE,
    vorher TEXT NOT NULL DEFAULT '',
    nachher TEXT NOT NULL DEFAULT '',
    notiz TEXT NOT NULL DEFAULT '',
    ergebnis TEXT,
    embedding BLOB,
    erstellt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    telefon TEXT NOT NULL DEFAULT '',
    ort TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS regeln (
    id INTEGER PRIMARY KEY,
    art TEXT NOT NULL CHECK (art IN ('verboten', 'zusammen', 'muster')),
    wert TEXT NOT NULL,
    zweiter TEXT NOT NULL DEFAULT '',
    meldung TEXT NOT NULL DEFAULT '',
    erstellt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_stationen (
    id INTEGER PRIMARY KEY,
    art TEXT NOT NULL CHECK (art IN ('beruf', 'ausbildung')),
    titel TEXT NOT NULL,
    organisation TEXT NOT NULL DEFAULT '',
    ort TEXT NOT NULL DEFAULT '',
    von TEXT NOT NULL DEFAULT '',
    bis TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS angaben (
    schluessel TEXT PRIMARY KEY,
    wert TEXT NOT NULL,
    quelle TEXT NOT NULL,
    zeit TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ereignisse (
    id INTEGER PRIMARY KEY,
    zeit TEXT NOT NULL,
    aktion TEXT NOT NULL,
    firma_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Speicherort: lokal (SQLite, Standard) oder Server (PostgreSQL) ---------------------------------------------------
# Die Wahl gilt für den Rechner, nicht für eine Person: Sie steht vor der Anmeldung fest und liegt deshalb in einer
# Datei neben den Daten. Das Passwort steht nie darin, nur im Schlüsselbund (Dienst „jobsuche“).

_UNGELADEN = object()
_server: dict | None | object = _UNGELADEN
MEMORY_AUF_SERVER = False  # Nur für Tests: auch ":memory:" landet dann in einem frischen Schema auf dem Server.


def server_datei() -> Path:
    return config.DATEN / "datenbank.json"


def server() -> dict | None:
    """Die PostgreSQL-Einstellung oder None für SQLite."""
    global _server
    if _server is _UNGELADEN:
        try:
            daten = json.loads(server_datei().read_text())
        except (OSError, ValueError):
            daten = {}
        _server = daten if daten.get("art") == "postgres" else None
    return _server  # type: ignore[return-value]


def speichere_server(einstellung: dict | None) -> None:
    """None schaltet zurück auf SQLite. Gilt ab dem nächsten Verbinden; offene Verbindungen bleiben, wo sie sind."""
    global _server
    datei = server_datei()
    datei.parent.mkdir(parents=True, exist_ok=True)
    if einstellung is None:
        datei.write_text(json.dumps({"art": "sqlite"}, indent=2))
        _server = None
        return
    sauber = {k: einstellung[k] for k in ("host", "port", "datenbank", "benutzer", "sslmode") if k in einstellung}
    datei.write_text(json.dumps({"art": "postgres", **sauber}, indent=2))
    os.chmod(datei, 0o600)
    _server = {"art": "postgres", **sauber}


def ist_postgres(conn) -> bool:
    from . import pg
    return isinstance(conn, pg.Verbindung)


def _ist_demo(pfad) -> bool:
    """Die Demo bleibt immer eine Wegwerf-Datei, auch wenn ein Server eingestellt ist."""
    from . import demo
    return bool(demo.ORDNER) and str(pfad) != ":memory:" and Path(demo.ORDNER) in Path(pfad).parents


def _auf_server(pfad) -> bool:
    if str(pfad) == ":memory:":
        return bool(server()) and MEMORY_AUF_SERVER
    return bool(server()) and not _ist_demo(pfad)


def existiert(pfad: Path | str) -> bool:
    """Gibt es für diesen Pfad schon einen Speicher? Unter SQLite die Datei, auf dem Server das Schema."""
    if str(pfad) != ":memory:" and _auf_server(pfad):
        from . import pg
        return pg.schema_existiert(server(), pg.schema_fuer(pfad, config.DATEN))
    return Path(pfad).exists()


# --- Mandant: Konto-Anmeldung gegen die zentrale Tabelle (nur PostgreSQL) -------------------------------------------
# Ist ein Mandant aktiv, verbindet `verbinden()` ohne Pfad mit seinem Schema und wechselt per SET ROLE in seine
# Datenbankrolle. Die Rolle darf nur ihr eigenes Schema benutzen: Fremde Daten verweigert der Server, nicht die App.
# Desktop: ein Mandant je Prozess (setze_mandant). Web: je Anfrage bzw. Hintergrundauftrag (mit_mandant, Kontextvariable).

@dataclass(frozen=True)
class Mandant:
    id: int
    name: str
    schema: str
    rolle: str
    ordner: Path  # Dateien des Mandanten: Lebenslauf für Stellen, Agenda, hochgeladene Unterlagen
    claude_erlaubt: bool = False


_mandant_prozess: Mandant | None = None
_mandant_kontext: contextvars.ContextVar = contextvars.ContextVar("mandant", default=None)


def aktiver_mandant() -> Mandant | None:
    return _mandant_kontext.get() or _mandant_prozess


def setze_mandant(mandant: Mandant | None) -> None:
    """Desktop: gilt für den ganzen Prozess bis zum Abmelden."""
    global _mandant_prozess
    _mandant_prozess = mandant


@contextmanager
def mit_mandant(mandant: Mandant | None):
    """Web: gilt nur im aktuellen Kontext (Anfrage oder Auftrag), andere Threads sehen ihn nicht."""
    token = _mandant_kontext.set(mandant)
    try:
        yield mandant
    finally:
        _mandant_kontext.reset(token)


def datenordner() -> Path:
    """Wo Dateien der angemeldeten Person liegen: beim Mandanten dessen Ordner, sonst neben der SQLite-Datei."""
    m = aktiver_mandant()
    return m.ordner if m else Path(config.DB_PFAD).parent


def datei(pfad: Path | str) -> Path:
    """Pfad einer gespeicherten Datei. Liegt sie beim Mandanten nicht am gespeicherten Ort (z. B. Pfad vom Desktop,
    Zugriff übers Web), zählt die gleichnamige Datei in seinem Ordner `unterlagen/` oder `cv/`."""
    pfad = Path(pfad)
    m = aktiver_mandant()
    if m is None:
        return pfad
    try:
        if pfad.exists():
            return pfad
    except OSError:  # z. B. Pfad unter /home, auf dem Server gesperrt
        pass
    for unter in ("unterlagen", "cv"):
        kandidat = m.ordner / unter / pfad.name
        if kandidat.is_file():
            return kandidat
    return m.ordner / "unterlagen" / pfad.name  # gibt es nicht: meldet sauber „fehlt“


def verbinden(pfad: Path | str | None = None):
    mandant = aktiver_mandant() if pfad is None else None
    if mandant is not None:
        from . import pg
        if not server():
            raise sqlite3.OperationalError("Mandanten gibt es nur mit PostgreSQL; es ist kein Server eingestellt.")
        mandant.ordner.mkdir(parents=True, exist_ok=True)
        return pg.verbinden(server(), mandant.schema, SCHEMA, _ergaenze_spalten, rolle=mandant.rolle)
    pfad = pfad or config.DB_PFAD
    if _auf_server(pfad):
        from . import pg
        schema = None if str(pfad) == ":memory:" else pg.schema_fuer(pfad, config.DATEN)
        if schema:
            Path(pfad).parent.mkdir(parents=True, exist_ok=True)  # Lebenslauf, Agenda usw. liegen weiter daneben
        return pg.verbinden(server(), schema, SCHEMA, _ergaenze_spalten)
    if str(pfad) != ":memory:":
        Path(pfad).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(pfad), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _ergaenze_spalten(conn)
    return conn


def _ergaenze_spalten(conn: sqlite3.Connection) -> None:
    """Neue Spalten für bestehende Datenbanken. Vorhandene Daten bleiben unberührt."""
    vorhanden = {z["name"] for z in conn.execute("PRAGMA table_info(dokumente)")}
    if "art" not in vorhanden:
        conn.execute("ALTER TABLE dokumente ADD COLUMN art TEXT NOT NULL DEFAULT 'sonstiges'")
    if "anhang" not in vorhanden:
        conn.execute("ALTER TABLE dokumente ADD COLUMN anhang INTEGER NOT NULL DEFAULT 0")
    if "ki_eignung" not in {z["name"] for z in conn.execute("PRAGMA table_info(firmen)")}:
        conn.execute("ALTER TABLE firmen ADD COLUMN ki_eignung TEXT")
    if "anhaenge" not in {z["name"] for z in conn.execute("PRAGMA table_info(entwuerfe)")}:
        conn.execute("ALTER TABLE entwuerfe ADD COLUMN anhaenge TEXT")  # JSON-Liste, NULL = Anhänge aus „Unterlagen“
    conn.commit()


def protokolliere(conn: sqlite3.Connection, aktion: str, detail: str = "", firma_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO ereignisse (zeit, aktion, firma_id, detail) VALUES (?, ?, ?, ?)",
        (jetzt(), aktion, firma_id, detail),
    )
    conn.commit()


def einstellung(conn: sqlite3.Connection, key: str, standard: str) -> str:
    zeile = conn.execute("SELECT value FROM einstellungen WHERE key = ?", (key,)).fetchone()
    return zeile["value"] if zeile else standard


def setze_einstellung(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO einstellungen (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
