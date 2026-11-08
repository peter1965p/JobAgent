#!/usr/bin/env python3
"""Startet Job Agent. Richtet beim ersten Mal die Python-Umgebung ein. Braucht nur Python ab 3.10.

    python3 start.py
"""

import hashlib
import subprocess
import sys
import venv
from pathlib import Path

WURZEL = Path(__file__).resolve().parent
MINDESTVERSION = (3, 10)


def venv_python(ordner: Path) -> Path:
    return ordner / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def pruefsumme(datei: Path) -> str:
    return hashlib.sha256(datei.read_bytes()).hexdigest()


def muss_installieren(ordner: Path, anforderungen: Path) -> bool:
    """Neu installiert wird nur, wenn die Umgebung fehlt oder sich die Anforderungen geändert haben."""
    marke = ordner / ".tueroeffner-anforderungen"
    return not venv_python(ordner).exists() or not marke.exists() or marke.read_text() != pruefsumme(anforderungen)


def richte_ein(ordner: Path, anforderungen: Path) -> None:
    if not venv_python(ordner).exists():
        print(f"Lege die Python-Umgebung an: {ordner}")
        try:
            venv.EnvBuilder(with_pip=True).create(ordner)
        except (subprocess.CalledProcessError, OSError) as fehler:
            sys.exit(f"Die Umgebung ließ sich nicht anlegen: {fehler}\n"
                     "Unter Debian und Ubuntu hilft meist: sudo apt install python3-venv")
    print("Installiere die Pakete. Sie belegen rund 1,3 GB, das erste Mal dauert einige Minuten.")
    ergebnis = subprocess.run([str(venv_python(ordner)), "-m", "pip", "install", "--disable-pip-version-check",
                               "-r", str(anforderungen)])
    if ergebnis.returncode:
        sys.exit("Die Installation der Pakete ist fehlgeschlagen. Die Meldung steht oben.")
    (ordner / ".tueroeffner-anforderungen").write_text(pruefsumme(anforderungen))


def main() -> int:
    if sys.version_info < MINDESTVERSION:
        sys.exit(f"Job Agent braucht Python {MINDESTVERSION[0]}.{MINDESTVERSION[1]} oder neuer, "
                 f"hier läuft {sys.version.split()[0]}. Download: https://www.python.org/downloads/")
    ordner, anforderungen = WURZEL / "venv", WURZEL / "requirements.txt"
    if muss_installieren(ordner, anforderungen):
        richte_ein(ordner, anforderungen)
    return subprocess.run([str(venv_python(ordner)), "-m", "jobsuche", *sys.argv[1:]], cwd=WURZEL).returncode


if __name__ == "__main__":
    raise SystemExit(main())
