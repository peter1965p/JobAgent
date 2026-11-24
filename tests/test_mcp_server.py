"""MCP-Server: Werkzeugliste ohne Senden/Freigeben, Lesen gegen eine SQLite-Testdatenbank."""
import asyncio
import json

import pytest

from jobsuche import config, db, mcp_server


@pytest.fixture
def lokal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "jobsuche.db")
    monkeypatch.setattr(db, "server", lambda: None)
    monkeypatch.setattr(mcp_server, "_angemeldet", False)
    conn = db.verbinden(config.DB_PFAD)
    conn.execute("INSERT INTO fakten (kategorie, text, status, erstellt) VALUES ('technik', 'Python und FastAPI', 'bestaetigt', '2026-01-01')")
    conn.execute("INSERT INTO fakten (kategorie, text, status, erstellt) VALUES ('technik', 'nur vorgeschlagen', 'vorgeschlagen', '2026-01-01')")
    conn.execute("INSERT INTO firmen (name, sitz) VALUES ('Muster GmbH', 'Osnabrück')")
    conn.commit()
    conn.close()


def _rufe(name, args=None):
    ergebnis = asyncio.run(mcp_server.server.call_tool(name, args or {}))
    return [json.loads(c.text) for c in ergebnis.content]


def test_kein_werkzeug_zum_senden_oder_freigeben():
    namen = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert {"stand", "fakten", "firmen", "entwuerfe", "stellen_suchen", "entwurf_vorschlagen"} <= namen
    assert not {n for n in namen if any(w in n for w in ("send", "freigeb", "versand"))}


def test_fakten_nur_bestaetigte(lokal):
    texte = [f["text"] for f in _rufe("fakten")]
    assert texte == ["Python und FastAPI"]


def test_firmen_mit_suchtext(lokal):
    assert [f["name"] for f in _rufe("firmen", {"suchtext": "osnab"})] == ["Muster GmbH"]
    assert _rufe("firmen", {"suchtext": "berlin"}) == []
