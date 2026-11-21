"""Prüft beim Start, was die App braucht, und richtet Fehlendes nach Zustimmung ein."""

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import keyring
from keyring.errors import KeyringError

from . import config, llm, rag
from .system import System

OLLAMA_DOWNLOAD = "https://ollama.com/download"
OLLAMA_SKRIPT = "https://ollama.com/install.sh"
MIN_RAM_GB = 8
MIN_FREI_GB = 6


class EinrichtFehler(Exception):
    pass


@dataclass(frozen=True)
class Pruefung:
    schluessel: str
    titel: str
    ok: bool
    detail: str
    pflicht: bool = True


@dataclass(frozen=True)
class Schritt:
    titel: str
    zeigt: str  # Was genau passiert, für die Anzeige vor der Zustimmung.
    ausfuehren: Callable


def starterpfad() -> Path:
    return Path.home() / ".local/share/applications/tueroeffner.desktop"


def ollama_laeuft() -> bool:
    try:
        return httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def sprachmodell_da() -> bool:
    try:
        return bool(llm.modelle())
    except llm.LLMFehler:
        return False


def embedding_da() -> bool:
    return any(config.MODELLE.glob("*paraphrase-multilingual*")) if config.MODELLE.exists() else False


def schluesselbund_da() -> bool:
    try:
        keyring.get_password("jobsuche", "pruefung")
        return "fail" not in type(keyring.get_keyring()).__module__
    except KeyringError:
        return False


def pruefe(system: System, melde=lambda _: None) -> list[Pruefung]:
    """`melde` bekommt jeden Schritt live: „… Verbinde …“ davor, „✓ …“/„✗ …“ danach (Splash)."""
    ergebnis: list[Pruefung] = []

    def schritt(text, arbeit):
        melde(f"… {text}")
        return arbeit()

    def fertig(p: Pruefung) -> None:
        ergebnis.append(p)
        melde(("✓ " if p.ok else "✗ " if p.pflicht else "• ") + f"{p.titel}: {p.detail}")

    fertig(Pruefung("ram", "Arbeitsspeicher", system.ram_gb >= MIN_RAM_GB, f"{system.ram_gb:.0f} GB"
                    + ("" if system.ram_gb >= MIN_RAM_GB else f", ab {MIN_RAM_GB} GB läuft das LLM flüssiger"), pflicht=False))
    fertig(Pruefung("platz", "Freier Speicherplatz", system.frei_gb >= MIN_FREI_GB, f"{system.frei_gb:.0f} GB frei"
                    + ("" if system.frei_gb >= MIN_FREI_GB else f", die Modelle brauchen rund {MIN_FREI_GB} GB")))
    ollama = schritt("Suche Ollama", lambda: shutil.which("ollama") is not None)
    fertig(Pruefung("ollama", "Ollama", ollama, "installiert" if ollama else "nicht installiert"))
    laeuft = schritt("Verbinde mit dem Ollama-Dienst", ollama_laeuft)
    fertig(Pruefung("dienst", "Ollama-Dienst", laeuft, "läuft" if laeuft else "läuft nicht"))
    modell = schritt("Lade die Liste der Sprachmodelle", sprachmodell_da)
    fertig(Pruefung("modell", "Sprachmodell", modell, "vorhanden" if modell else f"{config.STANDARD_MODELL} fehlt"))
    suchmodell = schritt("Prüfe das Suchmodell", embedding_da)
    fertig(Pruefung("embedding", "Suchmodell", suchmodell, "vorhanden" if suchmodell else "fehlt, rund 240 MB"))
    claude = schritt("Frage Claude Code an", llm.claude_status)
    fertig(Pruefung("claude", "Claude Code (optional)", not claude, claude or "angemeldet, lässt sich unter „System“ wählen",
                    pflicht=False))
    bund = schritt("Öffne den Schlüsselbund", schluesselbund_da)
    fertig(Pruefung("schluesselbund", "Schlüsselbund", bund,
                    "verfügbar" if bund else "fehlt, Passwörter lassen sich dann nicht merken", pflicht=False))
    if system.art == "linux":
        da = starterpfad().exists()
        fertig(Pruefung("starter", "Eintrag im Startmenü", da, "vorhanden" if da else "fehlt", pflicht=False))
    return ergebnis


def ist_startklar(pruefungen: list[Pruefung]) -> bool:
    return all(p.ok for p in pruefungen if p.pflicht)


def _laeuft(befehl: list[str], melde: Callable[[str], None]) -> None:
    try:
        prozess = subprocess.Popen(befehl, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except OSError as fehler:
        raise EinrichtFehler(f"{befehl[0]} ließ sich nicht starten: {fehler}") from fehler
    for zeile in prozess.stdout:
        if zeile.strip():
            melde(zeile.strip()[:160])
    if prozess.wait():
        raise EinrichtFehler(f"„{' '.join(befehl)}“ endete mit Fehler {prozess.returncode}.")


def _als_admin(befehl: list[str]) -> list[str]:
    """pkexec fragt grafisch nach dem Passwort. sudo ginge hier nicht, es gibt kein Terminal."""
    if not shutil.which("pkexec"):
        raise EinrichtFehler(f"Für diesen Schritt braucht es Administratorrechte. Führe im Terminal aus: sudo {' '.join(befehl)}")
    return ["pkexec", *befehl]


def _ollama_skript(melde) -> None:
    with tempfile.TemporaryDirectory() as ordner:
        skript = Path(ordner) / "install.sh"
        melde(f"Lade {OLLAMA_SKRIPT}")
        try:
            antwort = httpx.get(OLLAMA_SKRIPT, follow_redirects=True, timeout=60)
            antwort.raise_for_status()
        except httpx.HTTPError as fehler:
            raise EinrichtFehler(f"Das Installationsskript ließ sich nicht laden: {fehler}") from fehler
        skript.write_text(antwort.text, encoding="utf-8")
        _laeuft(_als_admin(["sh", str(skript)]), melde)


def ollama_schritt(system: System) -> Schritt:
    """Der jeweils übliche Weg pro System. Ohne passenden Weg bleibt der Link zum Download."""
    if system.art == "linux" and system.familie == "arch" and shutil.which("pacman"):
        befehl = ["pacman", "-S", "--needed", "--noconfirm", "ollama"]
        return Schritt("Ollama installieren", "sudo " + " ".join(befehl), lambda melde, _: _laeuft(_als_admin(befehl), melde))
    if system.art == "linux" and system.familie == "nixos":
        return _nur_hinweis("In NixOS gehört Ollama in die Systemkonfiguration: services.ollama.enable = true;")
    if system.art == "linux":
        return Schritt("Ollama installieren", f"Offizielles Skript von {OLLAMA_SKRIPT} mit Administratorrechten ausführen",
                       lambda melde, _: _ollama_skript(melde))
    if system.art == "windows" and shutil.which("winget"):
        befehl = ["winget", "install", "--id", "Ollama.Ollama", "-e", "--accept-package-agreements", "--accept-source-agreements"]
        return Schritt("Ollama installieren", " ".join(befehl), lambda melde, _: _laeuft(befehl, melde))
    if system.art == "macos" and shutil.which("brew"):
        befehl = ["brew", "install", "ollama"]
        return Schritt("Ollama installieren", " ".join(befehl), lambda melde, _: _laeuft(befehl, melde))
    return _nur_hinweis(f"Lade Ollama von {OLLAMA_DOWNLOAD} und starte die Prüfung dann erneut.")


def _nur_hinweis(text: str) -> Schritt:
    def geht_nicht(melde, _):
        raise EinrichtFehler(text)
    return Schritt("Ollama installieren", f"Von Hand: {text}", geht_nicht)


def starte_dienst(melde, _=None) -> None:
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        vorhanden = subprocess.run(["systemctl", "cat", "ollama"], capture_output=True).returncode == 0
        if vorhanden:
            _laeuft(_als_admin(["systemctl", "enable", "--now", "ollama"]), melde)
            return
    # Ohne Systemdienst läuft Ollama als eigener Prozess weiter, auch wenn die App endet.
    optionen = {"start_new_session": True} if sys.platform != "win32" else {"creationflags": 0x00000008}
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **optionen)
    except OSError as fehler:
        raise EinrichtFehler(f"Ollama ließ sich nicht starten: {fehler}") from fehler


def warte_auf_dienst(melde, _=None, versuche: int = 30, warten=None) -> None:
    import time
    warten = warten or time.sleep
    for _versuch in range(versuche):
        if ollama_laeuft():
            return
        warten(1)
    raise EinrichtFehler("Der Ollama-Dienst antwortet nicht.")


def ziehe_modell(modell: str, melde: Callable[[str], None], fortschritt: Callable[[int], None], client=None) -> None:
    """Lädt das Modell und meldet den Fortschritt. Ein abgebrochener Download setzt beim nächsten Mal fort."""
    client = client or httpx.Client(timeout=None)
    try:
        with client.stream("POST", f"{config.OLLAMA_URL}/api/pull", json={"model": modell}) as antwort:
            if antwort.status_code != 200:
                raise EinrichtFehler(f"Ollama meldet {antwort.status_code} für {modell}.")
            for zeile in antwort.iter_lines():
                daten = json.loads(zeile) if zeile.strip() else {}
                if daten.get("error"):
                    raise EinrichtFehler(f"Modell {modell}: {daten['error']}")
                if daten.get("total"):
                    fortschritt(int(100 * daten.get("completed", 0) / daten["total"]))
                elif daten.get("status"):
                    melde(str(daten["status"])[:160])
    except httpx.HTTPError as fehler:
        raise EinrichtFehler(f"Der Download von {modell} brach ab: {fehler}") from fehler


def lade_embedding(melde, _=None) -> None:
    melde("Lade das Suchmodell, rund 240 MB")
    rag.embedde(["Test"])


def starter_inhalt(python: str, ordner: Path, icon: Path) -> str:
    return ("[Desktop Entry]\nType=Application\nName=Job Agent\nComment=Direktansprache von Firmen\n"
            f'Exec="{python}" -m jobsuche\nPath={ordner}\nIcon={icon}\nTerminal=false\nCategories=Office;\n')


def lege_starter_an(icon: Path, melde, _=None) -> None:
    pfad = starterpfad()
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(starter_inhalt(sys.executable, config.ROOT, icon), encoding="utf-8")
    melde(f"Eintrag angelegt: {pfad}")


def plan(pruefungen: list[Pruefung], system: System, icon: Path) -> list[Schritt]:
    """Alles, was noch fehlt, in der richtigen Reihenfolge. Es läuft erst nach Zustimmung."""
    fehlt = {p.schluessel for p in pruefungen if not p.ok}
    schritte = []
    if "ollama" in fehlt:
        schritte.append(ollama_schritt(system))
    if {"ollama", "dienst"} & fehlt:
        schritte.append(Schritt("Ollama-Dienst starten", "Dienst einschalten, bei einem Systemdienst mit Administratorrechten",
                                lambda melde, _: (starte_dienst(melde), warte_auf_dienst(melde))))
    if "modell" in fehlt:
        modell = config.STANDARD_MODELL
        schritte.append(Schritt(f"Sprachmodell {modell} laden", f"ollama pull {modell}, rund 2 GB",
                                lambda melde, fort: ziehe_modell(modell, melde, fort)))
    if "embedding" in fehlt:
        schritte.append(Schritt("Suchmodell laden", "Mehrsprachiges Embedding-Modell, rund 240 MB", lade_embedding))
    if "starter" in fehlt:
        schritte.append(Schritt("Eintrag im Startmenü anlegen", str(starterpfad()),
                                lambda melde, _: lege_starter_an(icon, melde)))
    return schritte
