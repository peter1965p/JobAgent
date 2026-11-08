"""Erkennt, worauf die App läuft: Betriebssystem, Distribution, Paketverwaltung und Hardware."""

import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

FAMILIEN = {"arch": "arch", "debian": "debian", "ubuntu": "debian", "fedora": "fedora", "rhel": "fedora", "centos": "fedora",
            "suse": "suse", "opensuse": "suse", "nixos": "nixos"}


@dataclass(frozen=True)
class System:
    art: str  # "linux", "windows", "macos" oder "andere"
    name: str
    familie: str  # bei Linux "arch", "debian", "fedora", "suse", "nixos" oder "andere"
    architektur: str
    ram_gb: float
    frei_gb: float
    gpu: str
    python: str

    def beschreibung(self) -> str:
        return f"{self.name}, {self.architektur}, {self.ram_gb:.0f} GB RAM, {self.frei_gb:.0f} GB frei, {self.gpu}"


def lies_os_release(text: str) -> dict:
    werte = {}
    for zeile in text.splitlines():
        if "=" in zeile and not zeile.lstrip().startswith("#"):
            schluessel, wert = zeile.split("=", 1)
            werte[schluessel.strip()] = wert.strip().strip('"\'')
    return werte


def familie_aus(werte: dict) -> str:
    for kennung in [werte.get("ID", ""), *werte.get("ID_LIKE", "").split()]:
        for wort, familie in FAMILIEN.items():
            if wort in kennung.lower():
                return familie
    return "andere"


def _gpu(art: str, architektur: str) -> str:
    if shutil.which("nvidia-smi"):
        return "NVIDIA-GPU"
    if art == "macos" and architektur == "arm64":
        return "Apple-Chip"
    return "keine passende GPU erkannt, das LLM läuft auf der CPU"


def erkenne(ordner: Path | None = None, os_release: Path = Path("/etc/os-release")) -> System:
    roh = platform.system()
    architektur = platform.machine() or "unbekannt"
    art, name, familie = "andere", roh or "Unbekannt", "andere"
    if roh == "Linux" or sys.platform.startswith("linux"):
        art = "linux"
        werte = lies_os_release(os_release.read_text(errors="replace")) if os_release.exists() else {}
        name = werte.get("PRETTY_NAME") or werte.get("NAME") or "Linux"
        familie = familie_aus(werte)
    elif roh == "Windows":
        art, name = "windows", f"Windows {platform.release()}"
    elif roh == "Darwin":
        art, name = "macos", f"macOS {platform.mac_ver()[0]}".strip()
    speicher = psutil.virtual_memory().total / 1024**3
    frei = shutil.disk_usage(ordner or Path.home()).free / 1024**3
    return System(art, name, familie, architektur, speicher, frei, _gpu(art, architektur), platform.python_version())
