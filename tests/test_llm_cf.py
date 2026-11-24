"""Workers AI als Anbieter: Aufruf, JSON aus Text, Rückfall aufs lokale Modell."""
import httpx
import pytest

from jobsuche import config, llm


class Antwort:
    def __init__(self, status, daten):
        self.status_code, self._d = status, daten

    def json(self):
        return self._d


@pytest.fixture
def zugang(monkeypatch):
    monkeypatch.setenv("JOBAGENT_CF_KI_URL", "https://ki.example.org")
    monkeypatch.setenv("JOBAGENT_CF_KI_TOKEN", "t")


def test_cf_liefert_objekt_und_schickt_token(zugang, monkeypatch):
    gesehen = {}

    def post(url, **kw):
        gesehen.update(url=url, **kw)
        return Antwort(200, {"antwort": {"passung": 7}})
    monkeypatch.setattr(httpx, "post", post)
    assert llm.chat(llm.CF_MODELLE[0], "s", "n", {"type": "object"}) == {"passung": 7}
    assert gesehen["url"] == "https://ki.example.org/v1/chat"
    assert gesehen["headers"]["Authorization"] == "Bearer t"
    assert gesehen["json"]["model"].startswith("@cf/")


def test_cf_json_aus_text(zugang, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: Antwort(200, {"antwort": 'Hier: {"a": 1} fertig'}))
    assert llm.chat(llm.CF_MODELLE[1], "s", "n", {}) == {"a": 1}


def test_cf_fehler_faellt_auf_lokal_zurueck(zugang, monkeypatch):
    aufrufe = []

    def post(url, **kw):
        aufrufe.append(url)
        if "ki.example.org" in url:
            return Antwort(502, {"fehler": "kaputt"})
        return type("A", (), {"status_code": 200, "json": lambda self: {"message": {"content": '{"ok": true}'}}, "text": ""})()
    monkeypatch.setattr(httpx, "post", post)
    vorher = len(llm.AUSWEICHEN)
    assert llm.chat(llm.CF_MODELLE[0], "s", "n", {}) == {"ok": True}
    assert config.OLLAMA_URL in aufrufe[-1] and "Workers AI" in llm.AUSWEICHEN[-1] and len(llm.AUSWEICHEN) > vorher


def test_name_kurz():
    assert llm.name_kurz("cf:@cf/meta/llama-3.1-8b-instruct-fast") == "llama-3.1-8b-instruct-fast · Cloudflare"
    assert llm.name_kurz("qwen2.5:3b") == "qwen2.5:3b · lokal"
