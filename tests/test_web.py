"""Web-Oberfläche: Anmeldung, Schutzmaßnahmen und Mandantentrennung über die HTTP-Schnittstelle.
Server-Tests nur im PostgreSQL-Lauf (JOBSUCHE_TEST_PG)."""

import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="Web-Pakete fehlen (pip install -r requirements-web.txt)")

from jobsuche import config, llm, mandanten, pg  # noqa: E402
from jobsuche.web import app as webapp  # noqa: E402
from jobsuche.web.sicherheit import Begrenzer  # noqa: E402

from conftest import _PG  # noqa: E402

nur_mit_server = pytest.mark.skipif(not _PG, reason="kein PostgreSQL-Testserver (JOBSUCHE_TEST_PG)")
STATISCH = Path(webapp.__file__).parent / "static"


def test_begrenzer_gleitendes_fenster():
    b = Begrenzer(3, 10)
    assert [b.erlaubt("ip", t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert b.erlaubt("andere", 3)
    assert b.erlaubt("ip", 10.5)  # die erste ist aus dem Fenster


def test_sortierung_ueber_alle_treffer():
    zeilen = [{"km": "≈12", "naehe": 3}, {"km": "", "naehe": None}, {"km": "5", "naehe": 9}]
    assert [z["km"] for z in webapp._sortiere(zeilen, "km", False)] == ["5", "≈12", ""]
    assert [z["naehe"] for z in webapp._sortiere(zeilen, "naehe", True)] == [9, 3, None]


def test_oberflaeche_ohne_inline_skripte_und_fremde_quellen():
    html = (STATISCH / "index.html").read_text()
    assert "<script>" not in html and "style=" not in html and "onclick=" not in html
    assert not re.search(r'(src|href)="https?://', html)
    js = (STATISCH / "app.js").read_text()
    assert "innerHTML" not in js and "eval(" not in js  # Fremdes wird nur als Text gesetzt
    assert 'fetch("api/' in js  # relative Pfade: läuft unter / (Tunnel) und /jobagent/ (LAN)


def test_sichere_dateinamen():
    assert webapp._sicherer_name("../../etc/passwd") == "passwd"
    assert webapp._sicherer_name("Lebens lauf<script>.pdf") == "Lebens lauf_script_.pdf"
    assert webapp._sicherer_name("") == "datei"


# --- mit Server ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def umgebung(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    roh = pg.roh_verbinden(_PG)
    mandanten.einrichten(roh, web_rolle=None)
    monkeypatch.setattr(config, "MODELLE", tmp_path / "modelle")
    monkeypatch.setattr(config, "DB_PFAD", tmp_path / "x.db")
    monkeypatch.setattr(llm, "modelle", lambda: ["qwen2.5:3b"])
    app = webapp.erstelle_app(server=_PG, schluessel=os.urandom(32), daten=tmp_path, cookie_secure=False,
                              hosts=["testserver"])
    admin = mandanten.konto_anlegen(roh, "admin@example.org", "admin-passwort-1", "Admin", admin=True)
    mandanten.mandant_anlegen(roh, "Betrieb", admin.id)

    def client():
        c = TestClient(app, raise_server_exceptions=False)
        c.headers.update({"X-Jobagent": "1"})
        return c

    def anmelden(c, email, pw):
        r = c.post("/api/anmelden", json={"email": email, "passwort": pw})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF"] = r.json()["csrf"]
        return r

    yield {"app": app, "roh": roh, "client": client, "anmelden": anmelden, "admin": admin}
    for (mid,) in roh.execute("SELECT id FROM js_zentral.mandanten").fetchall():
        mandanten.mandant_entfernen(roh, mid)
    roh.execute("DROP SCHEMA js_zentral CASCADE")
    roh.close()


def _warte(c, auftrag_id, sekunden=20):
    ende = time.time() + sekunden
    while time.time() < ende:
        s = c.get(f"/api/auftraege/{auftrag_id}").json()
        if s["status"] in ("fertig", "fehler"):
            return s
        time.sleep(0.1)
    raise AssertionError("Auftrag nicht fertig")


@nur_mit_server
def test_anmelden_cookie_csrf_und_kopfzeilen(umgebung):
    c = umgebung["client"]()
    assert c.get("/api/ich").status_code == 401
    r = umgebung["anmelden"](c, "admin@example.org", "admin-passwort-1")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    for k in ("content-security-policy", "strict-transport-security", "x-frame-options", "x-content-type-options"):
        assert k in r.headers
    assert "script-src 'self'" in r.headers["content-security-policy"]
    ich = c.get("/api/ich").json()
    assert ich["konto"]["admin"] and ich["mandant"]["name"] == "Betrieb" and not ich["mandant"]["claude_erlaubt"]
    # Ändern ohne CSRF-Token oder ohne Kennungs-Kopf: abgelehnt
    csrf = c.headers.pop("X-CSRF")
    assert c.put("/api/profil", json={"name": "A", "email": "a@example.org"}).status_code == 403
    c.headers["X-CSRF"] = csrf
    assert c.put("/api/profil", json={"name": "A", "email": "a@example.org"}, headers={"X-Jobagent": "0"}).status_code == 403
    assert c.put("/api/profil", json={"name": "A", "email": "a@example.org"}, headers={"Origin": "https://boese.example"}).status_code == 403
    assert c.put("/api/profil", json={"name": "Admin Test", "email": "a@example.org"}).status_code == 200
    assert c.get("/api/profil").json()["name"] == "Admin Test"
    assert c.post("/api/abmelden").status_code == 200
    assert c.get("/api/ich").status_code == 401


@nur_mit_server
def test_fremder_host_und_keine_stacktraces(umgebung):
    c = umgebung["client"]()
    assert c.get("/api/gesundheit", headers={"Host": "angreifer.example"}).status_code == 400
    umgebung["anmelden"](c, "admin@example.org", "admin-passwort-1")
    r = c.get("/api/firmen/abc")
    assert r.status_code == 422 and "Traceback" not in r.text and r.json() == {"detail": "Ungültige Eingabe."}
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


@nur_mit_server
def test_rate_limit_bei_der_anmeldung(umgebung):
    c = umgebung["client"]()
    codes = [c.post("/api/anmelden", json={"email": f"x{i}@example.org", "passwort": "falsch-falsch"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


@nur_mit_server
def test_einladung_registrierung_und_mandantentrennung(umgebung):
    admin = umgebung["client"]()
    umgebung["anmelden"](admin, "admin@example.org", "admin-passwort-1")
    assert admin.post("/api/firmen", json={"name": "Geheime Firma GmbH"}).status_code == 200
    fid = admin.get("/api/firmen").json()[0]["id"]
    # offene Registrierung gibt es nicht
    neu = umgebung["client"]()
    r = neu.post("/api/registrieren", json={"code": "geraten", "email": "neu@example.org", "passwort": "neues-passwort-1"})
    assert r.status_code == 400
    code = admin.post("/api/admin/einladungen", json={"notiz": "Test"}).json()["code"]
    r = neu.post("/api/registrieren", json={"code": code, "email": "neu@example.org", "passwort": "neues-passwort-1", "name": "Neu"})
    assert r.status_code == 200
    neu.headers["X-CSRF"] = r.json()["csrf"]
    assert neu.get("/api/ich").json()["mandant"]["name"] == "Neu"
    # Der neue Mandant sieht die Firma des Betriebs nicht, auch nicht über die ID
    assert neu.get("/api/firmen").json() == []
    assert neu.get(f"/api/firmen/{fid}").status_code == 404
    assert neu.delete(f"/api/firmen/{fid}").status_code == 200  # wirkt nur im eigenen Schema
    assert [f["name"] for f in admin.get("/api/firmen").json()] == ["Geheime Firma GmbH"]
    # Verwaltung nur für Admins, Mandantenwechsel nur in eigene Mandanten
    assert neu.get("/api/admin").status_code == 403
    betrieb = admin.get("/api/ich").json()["mandant"]["id"]
    assert neu.post("/api/mandant", json={"id": betrieb}).status_code == 403
    # Claude: nicht freigegeben -> Auswahl abgelehnt, freigegeben -> erlaubt
    assert neu.post("/api/konto/ki", json={"modell": "claude:claude-haiku-5-5"}).status_code == 403
    mid = neu.get("/api/ich").json()["mandant"]["id"]
    assert admin.post(f"/api/admin/mandanten/{mid}/claude", json={"an": True}).status_code == 200
    assert neu.post("/api/konto/ki", json={"modell": "claude:claude-haiku-5-5"}).status_code == 200
    assert neu.get("/api/ich").json()["modell_wirksam"] == "claude:claude-haiku-5-5"


@nur_mit_server
def test_mailkonto_passwort_verschluesselt_und_nie_ausgegeben(umgebung):
    c = umgebung["client"]()
    umgebung["anmelden"](c, "admin@example.org", "admin-passwort-1")
    assert c.put("/api/mailkonto", json={"email": "ich@example.org", "host": "smtp.example.org", "port": 587,
                                         "sicherheit": "starttls", "passwort": "mail-geheim-123"}).status_code == 200
    m = c.get("/api/mailkonto").json()
    assert m["passwort_hinterlegt"] and "mail-geheim-123" not in str(m)
    assert c.put("/api/mailkonto", json={"email": "ich@example.org", "host": "smtp.example.org", "port": 25,
                                         "sicherheit": "keine"}).status_code == 400
    export = c.get("/api/system/export").text
    assert "mail-geheim-123" not in export and "geheim:" not in export


@nur_mit_server
def test_auftrag_und_trefferseiten(umgebung, monkeypatch):
    c = umgebung["client"]()
    umgebung["anmelden"](c, "admin@example.org", "admin-passwort-1")
    treffer = [{"firma": f"Firma {i}", "titel": "Entwicklung", "ort": "Ort", "quelle": "Jobbörse", "naehe": i, "entfernung": i,
                "luftlinie": False, "bekannt": False} for i in range(120)]
    monkeypatch.setattr(webapp.suche, "suche_ueberall", lambda conn, auftrag, melde: {"treffer": treffer, "hinweise": ["Test"]})
    r = c.post("/api/suche", json={"modus": "suchen", "begriffe": "Entwicklung", "quellen": ["jobboerse"]})
    assert r.status_code == 200
    aid = r.json()["auftrag"]
    s = _warte(c, aid)
    assert s["status"] == "fertig" and s["ergebnis"]["anzahl"] == 120
    seite = c.get("/api/suche/treffer?seite=2&pro_seite=50&sortierung=naehe&ab=true").json()
    assert seite["gesamt"] == 120 and seite["seiten"] == 3 and len(seite["zeilen"]) == 20
    assert seite["zeilen"][0]["naehe"] == 19
    assert c.get("/api/suche").json()["werte"]["begriffe"] == "Entwicklung"
    # fremde Aufträge sind unsichtbar
    andere = umgebung["client"]()
    code = c.post("/api/admin/einladungen", json={}).json()["code"]
    r = andere.post("/api/registrieren", json={"code": code, "email": "b@example.org", "passwort": "anderes-passwort-1"})
    andere.headers["X-CSRF"] = r.json()["csrf"]
    assert andere.get(f"/api/auftraege/{aid}").status_code == 404
    assert andere.get("/api/suche/treffer").json()["gesamt"] == 0


@nur_mit_server
def test_gespeicherte_suchen_ergebnis_neu_und_gehalt(umgebung, monkeypatch):
    c = umgebung["client"]()
    umgebung["anmelden"](c, "admin@example.org", "admin-passwort-1")
    runde = {"n": 0}

    def ueberall(conn, auftrag, melde):
        runde["n"] += 1
        t = [{"firma": "Muster GmbH", "titel": "Admin", "ort": "Bremen", "quelle": "Jobbörse", "naehe": 5, "entfernung": 3,
              "luftlinie": False, "bekannt": False, "gehalt": "48.000 €/Jahr", "gehalt_jahr": 48000}]
        if runde["n"] > 1:
            t.append({"firma": "Beispiel AG", "titel": "Admin", "ort": "Bremen", "quelle": "Jobbörse", "naehe": 4, "entfernung": 2,
                      "luftlinie": False, "bekannt": False})
        return {"treffer": t, "hinweise": []}
    monkeypatch.setattr(webapp.suche, "suche_ueberall", ueberall)
    werte = {"modus": "suchen", "begriffe": "Admin", "ort": "Bremen", "quellen": ["jobboerse"]}
    assert _warte(c, c.post("/api/suche", json=werte).json()["auftrag"])["status"] == "fertig"
    st = c.post("/api/suchen", json={"name": "Admin Bremen", "werte": werte}).json()
    sid = st["aktiv"]
    assert [x["name"] for x in st["suchen"]] == ["Admin Bremen"] and st["suchen"][0]["anzahl"] == 1
    assert c.patch(f"/api/suchen/{sid}", json={"nachts": True}).json()["suchen"][0]["nachts"] is True
    e = _warte(c, c.post("/api/suche", json=werte).json()["auftrag"])["ergebnis"]
    assert e["anzahl"] == 2 and e["neu"] == 1
    zeilen = c.get("/api/suche/treffer?sortierung=gehalt&ab=true").json()["zeilen"]
    assert [(z["firma"], z["status"], z["gehalt"]) for z in zeilen] == [("Muster GmbH", "", "48.000 €/Jahr"), ("Beispiel AG", "NEU", "")]
    # Neustart simuliert: Treffer im Speicher weg, das gespeicherte Ergebnis ist noch da
    assert c.post(f"/api/suchen/{sid}/laden").json()["anzahl"] == 2
    assert c.post("/api/suchen/zuletzt/laden").status_code == 200
    assert c.post("/api/suchen", json={"name": "  ", "werte": werte}).status_code == 400
    assert [x["name"] for x in c.delete(f"/api/suchen/{sid}").json()["suchen"]] == []
    assert c.delete(f"/api/suchen/{sid}").status_code == 404
