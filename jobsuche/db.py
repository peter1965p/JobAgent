import sqlite3
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


def verbinden(pfad: Path | str | None = None) -> sqlite3.Connection:
    pfad = pfad or config.DB_PFAD
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
