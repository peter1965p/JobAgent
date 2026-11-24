"""Sprachmodell: lokal über Ollama oder, wenn die Person es wählt, Claude über ihre Claude-Code-CLI (ihr Abo).

Bei Claude gehen Prompt und Unterlagen an Anthropic. Darum wird es nie still gewählt, sondern nur auf Zustimmung.
Andere Cloud-Anbieter gibt es bewusst nicht.
"""

import contextvars
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from . import config


class LLMFehler(Exception):
    pass


def modelle() -> list[str]:
    """Installierte Sprachmodelle; Code-Modelle wie `qwen2.5-coder` bleiben außen vor."""
    try:
        antwort = httpx.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        antwort.raise_for_status()
    except httpx.HTTPError as fehler:
        raise LLMFehler(f"Ollama ist nicht erreichbar ({config.OLLAMA_URL}).") from fehler
    return [m["name"] for m in antwort.json().get("models", []) if "coder" not in m["name"]]


CLAUDE_PREFIX = "claude:"
CLAUDE_MODELLE = ["claude:claude-haiku-5-5", "claude:claude-sonnet-5-5"]  # feste IDs, kein Alias
# Die Mail ist der eine Text, der zählt: Mit Claude schreibt sie das stärkere Modell, Prüfungen bleiben beim schnellen.
CLAUDE_SCHREIBMODELL = "claude:claude-sonnet-5-5"


def schreibmodell(modell: str) -> str:
    return CLAUDE_SCHREIBMODELL if ist_claude(modell) else modell
CLAUDE_EFFORT = "medium"  # gilt nur für die Aufrufe von Job Agent, Claude Code selbst bleibt unberührt
CLAUDE_ZEIT = 300
# Ab diesem Anteil am 5-Stunden- oder Wochenkontingent nimmt die App bis zum Reset das lokale Modell,
# damit für die eigene Arbeit mit Claude genug übrig bleibt.
CLAUDE_GRENZE = 0.60


def ist_claude(modell: str) -> bool:
    return modell.startswith(CLAUDE_PREFIX)


def claude_pfad() -> str | None:
    """Auch im Nachtlauf, wo systemd `~/.local/bin` oft nicht im PATH hat."""
    gefunden = shutil.which("claude")
    if gefunden:
        return gefunden
    lokal = Path.home() / ".local/bin/claude"
    return str(lokal) if lokal.is_file() else None


def claude_status() -> str:
    """Leer, wenn Claude Code installiert und angemeldet ist, sonst der Grund."""
    pfad = claude_pfad()
    if not pfad:
        return "Claude Code ist nicht installiert."
    try:
        lauf = subprocess.run([pfad, "auth", "status", "--json"], capture_output=True, text=True, timeout=20)
        angemeldet = json.loads(lauf.stdout or "{}").get("loggedIn")
    except (OSError, subprocess.SubprocessError, ValueError):
        return "Claude Code antwortet nicht."
    return "" if angemeldet else "Claude Code ist installiert, aber nicht angemeldet: claude auth login"


def _ordner() -> Path:
    ordner = Path(tempfile.gettempdir()) / "jobagent-claude"  # leerer Ordner: kein Projektkontext, keine CLAUDE.md
    ordner.mkdir(exist_ok=True)
    return ordner


def _nutzungsdatei() -> Path:
    return _ordner() / "nutzung.json"


def claude_nutzung() -> dict:
    """Letzter bekannter Stand je Fenster: {"five_hour": {"utilization": 0.29, "resetsAt": 1794171600}, ...}.
    Liegt in einer Datei, damit App und Nachtlauf denselben Stand sehen."""
    try:
        return json.loads(_nutzungsdatei().read_text())
    except (OSError, ValueError):
        return {}


def claude_gesperrt(jetzt: float | None = None) -> str:
    """Grund, warum die App Claude gerade nicht nutzt, sonst leer."""
    jetzt = time.time() if jetzt is None else jetzt
    namen = {"five_hour": "5-Stunden", "seven_day": "Wochen"}
    for fenster, stand in claude_nutzung().items():
        if not isinstance(stand, dict):
            continue
        anteil, reset = stand.get("utilization") or 0, stand.get("resetsAt") or 0
        if reset > jetzt and (anteil >= CLAUDE_GRENZE or stand.get("status") == "rejected"):
            bis = time.strftime("%d.%m. %H:%M", time.localtime(reset))
            return (f"{namen.get(fenster, fenster)}kontingent bei {round(anteil * 100)} %, Grenze der App "
                    f"{round(CLAUDE_GRENZE * 100)} %. Bis {bis} läuft das lokale Modell.")
    return ""


def _merke_nutzung(info: dict) -> None:
    fenster = info.get("unifiedWindows")
    if isinstance(fenster, dict) and fenster:
        try:
            _nutzungsdatei().write_text(json.dumps(fenster))
        except OSError:
            pass


# Web: Claude läuft über das Abo des Betreibers (KI-Brücke auf seinem PC). Erlaubt nur für freigegebene Mandanten; alle anderen
# bekommen das lokale Modell, auch wenn in ihren Einstellungen Claude steht. Desktop: immer erlaubt (eigenes Abo).
claude_erlaubt: contextvars.ContextVar = contextvars.ContextVar("claude_erlaubt", default=True)
BRUECKE_URL = os.environ.get("JOBAGENT_CLAUDE_BRUECKE_URL", "").rstrip("/")
BRUECKE_TOKEN = os.environ.get("JOBAGENT_CLAUDE_BRUECKE_TOKEN", "")


def _claude_bruecke(modell: str, system: str, nutzer: str, schema: dict) -> dict:
    try:
        antwort = httpx.post(f"{BRUECKE_URL}/frage", timeout=CLAUDE_ZEIT + 15, headers={"X-Bruecke-Token": BRUECKE_TOKEN},
                             json={"prompt": nutzer, "system": system, "json_schema": schema,
                                   "modell": modell[len(CLAUDE_PREFIX):]})
    except httpx.HTTPError as fehler:
        raise LLMFehler("Die KI-Brücke zu Claude ist nicht erreichbar.") from fehler
    if antwort.status_code != 200:
        try:
            meldung = antwort.json().get("fehler", "")
        except ValueError:
            meldung = ""
        raise LLMFehler(f"KI-Brücke meldet {antwort.status_code}: {meldung[:200]}")
    text = str(antwort.json().get("antwort") or "")
    try:
        ergebnis = json.loads(text)
    except ValueError:
        anfang, ende = text.find("{"), text.rfind("}")
        try:
            ergebnis = json.loads(text[anfang:ende + 1]) if anfang >= 0 else None
        except ValueError:
            ergebnis = None
    if not isinstance(ergebnis, dict):
        raise LLMFehler("Claude hat kein gültiges JSON geliefert.")
    return ergebnis


def _claude_chat(modell: str, system: str, nutzer: str, schema: dict) -> dict:
    if BRUECKE_URL:
        return _claude_bruecke(modell, system, nutzer, schema)
    pfad = claude_pfad()
    if not pfad:
        raise LLMFehler("Claude Code ist nicht installiert. Wähle unter „System“ ein lokales Modell.")
    befehl = [pfad, "-p", "--model", modell[len(CLAUDE_PREFIX):], "--effort", CLAUDE_EFFORT,
              "--output-format", "stream-json", "--verbose", "--no-session-persistence",
              "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--system-prompt", system,
              "--json-schema", json.dumps(schema)]
    try:
        lauf = subprocess.run(befehl, input=nutzer, capture_output=True, text=True, timeout=CLAUDE_ZEIT, cwd=_ordner())
    except subprocess.TimeoutExpired as fehler:
        raise LLMFehler(f"Claude hat nach {CLAUDE_ZEIT} s nicht geantwortet.") from fehler
    except OSError as fehler:
        raise LLMFehler(f"Claude Code ließ sich nicht starten: {fehler}") from fehler
    antwort = None
    for zeile in lauf.stdout.splitlines():
        try:
            ereignis = json.loads(zeile)
        except ValueError:
            continue
        if ereignis.get("type") == "rate_limit_event":
            _merke_nutzung(ereignis.get("rate_limit_info") or {})
        elif ereignis.get("type") == "result":
            antwort = ereignis
    if antwort is None:
        raise LLMFehler(f"Claude Code meldet: {(lauf.stderr or lauf.stdout)[:200]}")
    if antwort.get("is_error"):
        raise LLMFehler(f"Claude meldet: {str(antwort.get('result'))[:200]}")
    ergebnis = antwort.get("structured_output")
    if not isinstance(ergebnis, dict):
        try:
            ergebnis = json.loads(antwort.get("result") or "")
        except ValueError:
            ergebnis = None
    if not isinstance(ergebnis, dict):
        raise LLMFehler("Claude hat kein gültiges JSON geliefert.")
    return ergebnis


AUSWEICHEN: list[str] = []  # Gründe, aus denen statt Claude das lokale Modell lief, für Meldungen und Protokoll


def beschriftung(modell: str, seit: int) -> str:
    """Name für Protokoll und Anzeige. Ist seit dem Stand `seit` von AUSWEICHEN ausgewichen worden, steht das dabei."""
    if ist_claude(modell) and len(AUSWEICHEN) > seit:
        return f"{config.STANDARD_MODELL} statt Claude: {AUSWEICHEN[-1]}"
    return modell


def chat(modell: str, system: str, nutzer: str, schema: dict, temperatur: float = 0.3) -> dict:
    """Fragt das Modell mit JSON-Schema an. Ungültiges JSON wird einmal wiederholt."""
    if ist_claude(modell) and not claude_erlaubt.get():
        modell = config.STANDARD_MODELL  # nicht freigegeben: still das lokale Modell, ohne Rückfall-Meldung
    if ist_claude(modell):
        grund = claude_gesperrt()
        if not grund:
            try:
                return _claude_chat(modell, system, nutzer, schema)
            except LLMFehler as fehler:
                grund = f"Claude ging nicht ({fehler})."
        # Lieber schlechter als gar nicht: Das lokale Modell übernimmt.
        AUSWEICHEN.append(grund)
        modell = config.STANDARD_MODELL
    nutzlast = {
        "model": modell,
        "stream": False,
        "format": schema,
        "options": {"num_ctx": 4096, "temperature": temperatur},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": nutzer}],
    }
    for versuch in range(2):
        try:
            # Auf der CPU kann eine Antwort mehrere Minuten dauern.
            antwort = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=nutzlast, timeout=1200)
        except httpx.HTTPError as fehler:
            raise LLMFehler(f"Ollama antwortet nicht: {fehler}") from fehler
        if antwort.status_code == 404:
            raise LLMFehler(f"Modell „{modell}“ ist nicht installiert: ollama pull {modell}")
        if antwort.status_code != 200:
            raise LLMFehler(f"Ollama meldet {antwort.status_code}: {antwort.text[:200]}")
        try:
            ergebnis = json.loads(antwort.json()["message"]["content"])
        except (KeyError, ValueError):
            continue
        if isinstance(ergebnis, dict):
            return ergebnis
    raise LLMFehler("Das Modell hat zweimal kein gültiges JSON geliefert.")
