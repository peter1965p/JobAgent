"""Sicherung der Daten: Stand der nächtlichen PostgreSQL-Sicherung lesen und anstoßen, bei SQLite die Datei kopieren.

Die PostgreSQL-Sicherung selbst macht ein systemd-User-Dienst (`pg-sicherung.service`) außerhalb der App. Er legt je Lauf
`<datenbank>_<JJJJ-MM-TT_HHMM>.dump` und `globals_<…>.sql` in einen Ordner. Gibt es den Dienst nicht, bleibt der Knopf aus.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from . import db

PG_ORDNER = Path.home() / "Backups" / "postgres"
SQLITE_ORDNER = Path.home() / "Backups" / "jobsuche"
DIENST = "pg-sicherung.service"
MAX_ALTER_STUNDEN = 36
SQLITE_BEHALTEN = 14
_DATEI = re.compile(r"^(?P<db>.+)_(?P<stempel>\d{4}-\d{2}-\d{2}_\d{4})\.dump$")
_GLOBALS = re.compile(r"^globals_(?P<stempel>\d{4}-\d{2}-\d{2}_\d{4})\.sql$")


def pg_ordner(conn=None) -> Path:
    wert = db.einstellung(conn, "sicherung.pg_ordner", "") if conn is not None else ""
    return Path(wert).expanduser() if wert else PG_ORDNER


def sqlite_ordner(conn=None) -> Path:
    wert = db.einstellung(conn, "sicherung.sqlite_ordner", "") if conn is not None else ""
    return Path(wert).expanduser() if wert else SQLITE_ORDNER


def _zeit(stempel: str) -> datetime:
    return datetime.strptime(stempel, "%Y-%m-%d_%H%M")


def stand_pg(ordner: Path, jetzt: datetime | None = None) -> dict:
    """Letzte Sicherung: Zeit, Größe je Datenbank, Zahl der Stände und ob sie zu alt ist."""
    jetzt = jetzt or datetime.now()
    if not ordner.is_dir():
        return {"vorhanden": False, "text": f"Noch keine Sicherung in {ordner}.", "warnung": True}
    staende: dict[str, dict[str, int]] = {}
    for datei in ordner.iterdir():
        if (m := _DATEI.match(datei.name)) and datei.is_file():
            staende.setdefault(m["stempel"], {})[m["db"]] = datei.stat().st_size
        elif (m := _GLOBALS.match(datei.name)):
            staende.setdefault(m["stempel"], {})
    if not staende:
        return {"vorhanden": False, "text": f"Noch keine Sicherung in {ordner}.", "warnung": True}
    letzter = max(staende)
    zeit = _zeit(letzter)
    alter_h = (jetzt - zeit).total_seconds() / 3600
    return {"vorhanden": True, "zeit": zeit, "alter_h": alter_h, "dbs": dict(sorted(staende[letzter].items())),
            "anzahl": len(staende), "warnung": alter_h > MAX_ALTER_STUNDEN or not staende[letzter]}


def groesse(b: int) -> str:
    for einheit in ("B", "KB", "MB", "GB"):
        if b < 1024 or einheit == "GB":
            return f"{b:.0f} {einheit}" if einheit == "B" else f"{b:.1f} {einheit}".replace(".", ",")
        b /= 1024
    return ""


def stand_text(stand: dict) -> str:
    if not stand.get("vorhanden"):
        return stand["text"]
    alter = stand["alter_h"]
    wann = f"vor {alter:.0f} Stunden" if alter >= 1 else "vor weniger als einer Stunde"
    dbs = ", ".join(f"{name} {groesse(b)}" for name, b in stand["dbs"].items()) or "keine Datenbank im letzten Lauf"
    text = f"Letzte Sicherung {stand['zeit']:%d.%m.%Y %H:%M} ({wann}): {dbs}. {stand['anzahl']} Stände gespeichert."
    if alter > MAX_ALTER_STUNDEN:
        text += f" Älter als {MAX_ALTER_STUNDEN} Stunden: Läuft der PC nachts, und ist der Dienst {DIENST} aktiv?"
    return text


def dienst_vorhanden() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "cat", DIENST], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def jetzt_sichern_pg(melde=lambda _: None) -> tuple[bool, list[str]]:
    """Startet den Dienst und wartet, bis er fertig ist. Gibt Erfolg und die ok/FEHLER-Zeilen aus dem Journal zurück."""
    melde("Sicherung läuft")
    try:
        lauf = subprocess.run(["systemctl", "--user", "start", DIENST], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, ["Die Sicherung braucht länger als 15 Minuten."]
    journal = subprocess.run(["journalctl", "--user", "-u", DIENST, "-n", "20", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=20).stdout
    zeilen = [z.strip() for z in journal.splitlines() if z.strip().startswith(("ok ", "FEHLER"))]
    return lauf.returncode == 0, zeilen[-10:] or ([lauf.stderr.strip()] if lauf.stderr.strip() else [])


def sichere_sqlite(conn: sqlite3.Connection, ziel: Path, name: str = "jobsuche") -> Path:
    """Kopiert die laufende SQLite-Datenbank konsistent (Backup-API) und behält die letzten Stände."""
    ziel.mkdir(parents=True, exist_ok=True)
    datei = ziel / f"{name}_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    kopie = sqlite3.connect(datei)
    try:
        conn.backup(kopie)
    finally:
        kopie.close()
    datei.chmod(0o600)
    alte = sorted(ziel.glob(f"{name}_*.db"))[:-SQLITE_BEHALTEN]
    for d in alte:
        d.unlink(missing_ok=True)
    return datei
