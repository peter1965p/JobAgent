"""Lokales LLM über Ollama. Es gibt bewusst keinen Cloud-Anbieter."""

import json

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


def chat(modell: str, system: str, nutzer: str, schema: dict, temperatur: float = 0.3) -> dict:
    """Fragt das Modell mit JSON-Schema an. Ungültiges JSON wird einmal wiederholt."""
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
