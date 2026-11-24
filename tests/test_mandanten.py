"""Mandanten: Konten, Trennung per Datenbankrolle, RLS auf der Mandantentabelle, Einladungen, Sperre, verschlüsselte
Ablage. Die Server-Tests laufen nur im PostgreSQL-Lauf (JOBSUCHE_TEST_PG)."""

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest

from jobsuche import db, geheim, llm, mandanten, pg

from conftest import _PG

nur_mit_server = pytest.mark.skipif(not _PG, reason="kein PostgreSQL-Testserver (JOBSUCHE_TEST_PG)")


# --- ohne Server ---------------------------------------------------------------------------------------------------------

def test_argon2_hash_und_passwortregel():
    h = mandanten.hash_passwort("ein-langes-passwort")
    assert h.startswith("$argon2id$") and "ein-langes-passwort" not in h
    assert mandanten.passwort_passt(h, "ein-langes-passwort")
    assert not mandanten.passwort_passt(h, "falsch-falsch-falsch")
    assert not mandanten.passwort_passt("kaputt", "egal")
    with pytest.raises(mandanten.KontoFehler, match="12 Zeichen"):
        mandanten.pruefe_passwortregel("kurz")
    with pytest.raises(mandanten.KontoFehler, match="Mailadresse"):
        mandanten.pruefe_passwortregel("person@example.org", "Person@example.org")


def test_verschluesselte_ablage_bindet_den_namen():
    ablage = geheim.Verschluesselt(os.urandom(32))
    text = ablage.verschluessele("smtp:a@b", "geheim123")
    assert "geheim123" not in text and text.startswith("v1:")
    assert ablage.entschluessele("smtp:a@b", text) == "geheim123"
    assert ablage.entschluessele("anderer:name", text) is None  # unter fremdem Namen wertlos
    assert geheim.Verschluesselt(os.urandom(32)).entschluessele("smtp:a@b", text) is None  # anderer Schlüssel
    k = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert len(geheim.schluessel_aus_text(k)) == 32
    with pytest.raises(geheim.GeheimFehler):
        geheim.schluessel_aus_text(base64.urlsafe_b64encode(b"zu kurz").decode())


def test_claude_nur_wenn_freigegeben(monkeypatch):
    aufrufe = []
    monkeypatch.setattr(llm, "_claude_chat", lambda *a: aufrufe.append("claude") or {"ok": 1})

    class Antwort:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": '{"ok": 2}'}}
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: aufrufe.append("ollama") or Antwort())
    assert llm.chat("claude:claude-haiku-5-5", "s", "n", {}) == {"ok": 1}
    token = llm.claude_erlaubt.set(False)
    try:
        assert llm.chat("claude:claude-haiku-5-5", "s", "n", {}) == {"ok": 2}
    finally:
        llm.claude_erlaubt.reset(token)
    assert aufrufe == ["claude", "ollama"]


def test_ohne_mandant_bleibt_alles_wie_bisher(tmp_path, monkeypatch):
    """SQLite-Einzelbetrieb: kein Mandant, Dateien neben der Datenbank."""
    monkeypatch.setattr(db, "_server", None)
    monkeypatch.setattr(db.config, "DB_PFAD", tmp_path / "jobsuche.db")
    assert db.aktiver_mandant() is None and db.datenordner() == tmp_path
    conn = db.verbinden()
    try:
        assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 0
    finally:
        conn.close()
    m = db.Mandant(1, "x", "js_x", "js_x", tmp_path / "m1")
    with db.mit_mandant(m):
        assert db.datenordner() == tmp_path / "m1"
        with pytest.raises(Exception, match="nur mit PostgreSQL"):
            db.verbinden()
    assert db.aktiver_mandant() is None


# --- mit Server ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def roh():
    verbindung = pg.roh_verbinden(_PG)
    mandanten.einrichten(verbindung, web_rolle=None)
    yield verbindung
    for (mid,) in verbindung.execute("SELECT id FROM js_zentral.mandanten").fetchall():
        mandanten.mandant_entfernen(verbindung, mid)
    verbindung.execute("DROP SCHEMA js_zentral CASCADE")
    verbindung.close()


@nur_mit_server
def test_mandanten_sehen_nur_ihre_eigenen_daten(roh, tmp_path):
    a = mandanten.konto_anlegen(roh, "a@example.org", "passwort-von-a-1")
    b = mandanten.konto_anlegen(roh, "b@example.org", "passwort-von-b-1")
    mandanten.mandant_anlegen(roh, "A", a.id)
    mandanten.mandant_anlegen(roh, "B", b.id)
    ia, ib = mandanten.mandant_fuer(roh, a.id, None), mandanten.mandant_fuer(roh, b.id, None)
    assert ia.schema != ib.schema and ia.rolle != ib.rolle
    with db.mit_mandant(mandanten.als_mandant(ia, tmp_path / "a")):
        conn = db.verbinden()
        try:
            conn.execute("INSERT INTO firmen (name) VALUES ('Firma von A')")
            assert conn.execute("SELECT current_user").fetchone()[0] == ia.rolle
            # Fremdes Schema, zentrale Konten: Der Server verweigert, nicht die App
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(f'SELECT count(*) FROM "{ib.schema}".firmen')
            with pytest.raises(Exception, match="permission denied"):
                conn.execute("SELECT count(*) FROM js_zentral.konten")
            with pytest.raises(Exception, match="permission denied"):
                conn.execute("UPDATE js_zentral.mandanten SET claude_erlaubt = true")
            # RLS: nur die eigene Mandantenzeile
            assert conn.execute("SELECT count(*), min(name) FROM js_zentral.mandanten").fetchone()[:] == (1, "A")
        finally:
            conn.close()
    with db.mit_mandant(mandanten.als_mandant(ib, tmp_path / "b")):
        conn = db.verbinden()
        try:
            assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 0
        finally:
            conn.close()
    with pytest.raises(mandanten.KontoFehler, match="Kein Zugriff"):
        mandanten.mandant_fuer(roh, a.id, ib.id)


@nur_mit_server
def test_anmeldung_sperre_und_passwortwechsel(roh):
    mandanten.konto_anlegen(roh, "Person@Example.org", "richtiges-passwort")
    k = mandanten.anmelden(roh, "person@example.org", "richtiges-passwort")
    jetzt = datetime.now(timezone.utc)
    for _ in range(mandanten.MAX_FEHLVERSUCHE):
        with pytest.raises(mandanten.KontoFehler, match="stimmt nicht"):
            mandanten.anmelden(roh, "person@example.org", "falsches-passwort", jetzt=jetzt)
    with pytest.raises(mandanten.KontoFehler, match="gesperrt"):
        mandanten.anmelden(roh, "person@example.org", "richtiges-passwort", jetzt=jetzt)
    # nach Ablauf der Sperre wieder möglich
    assert mandanten.anmelden(roh, "person@example.org", "richtiges-passwort", jetzt=jetzt + timedelta(minutes=16)).id == k.id
    with pytest.raises(mandanten.KontoFehler, match="bisherige"):
        mandanten.aendere_passwort(roh, k.id, "falsch-falsch-falsch", "neues-passwort-123")
    mandanten.aendere_passwort(roh, k.id, "richtiges-passwort", "neues-passwort-123")
    assert mandanten.anmelden(roh, "person@example.org", "neues-passwort-123").id == k.id
    with pytest.raises(mandanten.KontoFehler, match="stimmt nicht"):
        mandanten.anmelden(roh, "niemand@example.org", "egal-egal-egal")


@nur_mit_server
def test_registrierung_nur_mit_einladung(roh):
    admin = mandanten.konto_anlegen(roh, "admin@example.org", "admin-passwort-1", admin=True)
    mandanten.mandant_anlegen(roh, "Betrieb", admin.id)
    with pytest.raises(mandanten.KontoFehler, match="Einladungscode"):
        mandanten.registrieren(roh, "erfunden", "neu@example.org", "neues-passwort-1")
    code = mandanten.einladung_erzeugen(roh, admin.id, "für Tests")
    assert roh.execute("SELECT count(*) FROM js_zentral.einladungen WHERE code_hash = %s", (code,)).fetchone()[0] == 0
    k = mandanten.registrieren(roh, code, "neu@example.org", "neues-passwort-1", "Neu")
    assert [m.name for m in mandanten.mandanten_von(roh, k.id)] == ["Neu"]
    with pytest.raises(mandanten.KontoFehler, match="Einladungscode"):
        mandanten.registrieren(roh, code, "zweiter@example.org", "neues-passwort-2")  # nur einmal
    # Einladung in einen bestehenden Mandanten
    mid = mandanten.mandanten_von(roh, admin.id)[0].id
    code2 = mandanten.einladung_erzeugen(roh, admin.id, mandant_id=mid)
    k2 = mandanten.registrieren(roh, code2, "mit@example.org", "neues-passwort-3")
    assert [(m.id, m.mitgliedsrolle) for m in mandanten.mandanten_von(roh, k2.id)] == [(mid, "mitglied")]


@nur_mit_server
def test_schema_uebernahme_verlustfrei(roh, tmp_path):
    """Ein Schema der bisherigen Anmeldung wird ohne Kopieren zum Mandanten; die Daten bleiben lesbar."""
    alt = "js_" + os.urandom(8).hex()
    v = pg.verbinden(_PG, alt, db.SCHEMA, db._ergaenze_spalten)
    v.execute("INSERT INTO firmen (name) VALUES ('Alt GmbH')")
    v.execute("INSERT INTO fakten (kategorie, text, erstellt) VALUES ('technik', 'Python', '2026-01-01')")
    v.close()
    k = mandanten.konto_anlegen(roh, "alt@example.org", "altes-konto-pw-1", admin=True)
    mid = mandanten.schema_uebernehmen(roh, alt, "Alt", k.id)
    info = mandanten.mandant_fuer(roh, k.id, mid)
    assert info.schema == alt
    with db.mit_mandant(mandanten.als_mandant(info, tmp_path)):
        conn = db.verbinden()
        try:
            assert conn.execute("SELECT name FROM firmen").fetchone()[0] == "Alt GmbH"
            conn.execute("INSERT INTO firmen (name) VALUES ('Neu GmbH')")  # Sequenz gehört jetzt der Rolle
            assert conn.execute("SELECT count(*) FROM fakten").fetchone()[0] == 1
        finally:
            conn.close()
    with pytest.raises(mandanten.KontoFehler, match="schon einem Mandanten"):
        mandanten.schema_uebernehmen(roh, alt, "nochmal", k.id)


@nur_mit_server
def test_verschluesselte_ablage_beim_mandanten(roh, tmp_path):
    k = mandanten.konto_anlegen(roh, "g@example.org", "geheim-test-pw-1")
    mandanten.mandant_anlegen(roh, "G", k.id)
    info = mandanten.mandant_fuer(roh, k.id, None)
    ablage = geheim.Verschluesselt(os.urandom(32))
    with db.mit_mandant(mandanten.als_mandant(info, tmp_path)):
        token = geheim.setze_ablage(ablage)
        try:
            geheim.setze("ich@example.org@smtp.example.org", "mailpasswort")
            assert geheim.hole("ich@example.org@smtp.example.org") == "mailpasswort"
            conn = db.verbinden()
            try:
                roh_wert = conn.execute("SELECT value FROM einstellungen WHERE key LIKE 'geheim:%'").fetchone()[0]
            finally:
                conn.close()
            assert "mailpasswort" not in roh_wert
            geheim.loesche("ich@example.org@smtp.example.org")
            assert geheim.hole("ich@example.org@smtp.example.org") is None
        finally:
            geheim._ablage.reset(token)


@nur_mit_server
def test_faellige_nachtlaeufe(roh):
    k = mandanten.konto_anlegen(roh, "n@example.org", "nachtlauf-pw-12")
    mid = mandanten.mandant_anlegen(roh, "N", k.id)
    jetzt = datetime(2026, 11, 24, 3, 5)
    assert mandanten.faellige_nachtlaeufe(roh, jetzt) == []
    mandanten.setze_nachtlauf(roh, mid, True, "02:00")
    assert [m.id for m in mandanten.faellige_nachtlaeufe(roh, jetzt)] == [mid]
    mandanten.nachtlauf_erledigt(roh, mid, jetzt.date())
    assert mandanten.faellige_nachtlaeufe(roh, jetzt) == []
    assert mandanten.faellige_nachtlaeufe(roh, jetzt + timedelta(days=1)) != []
    with pytest.raises(mandanten.KontoFehler):
        mandanten.setze_nachtlauf(roh, mid, True, "25:00")


@nur_mit_server
def test_webrolle_ohne_set_role_ohne_mandantendaten(tmp_path):
    """Wie in Betrieb: Der Webdienst ist ein eigener Login ohne Besitzrechte. Er sieht Konten und alle Mandantenzeilen
    (Betreiber), Mandantendaten aber nur nach SET ROLE; neue Mandanten legt er über die Funktion an."""
    import secrets as s
    from psycopg import sql as pgsql
    web = "jobagent_web_test_" + s.token_hex(4)
    pw = s.token_urlsafe(24)
    roh = pg.roh_verbinden(_PG)
    roh.execute(pgsql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(pgsql.Identifier(web), pgsql.Literal(pw)))
    try:
        roh.execute(pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(pgsql.Identifier(_PG["datenbank"]), pgsql.Identifier(web)))
        mandanten.einrichten(roh, web_rolle=web)
        k = mandanten.konto_anlegen(roh, "w@example.org", "web-rolle-pw-123")
        mandanten.mandant_anlegen(roh, "Vorher", k.id)
        w = pg.roh_verbinden({**_PG, "benutzer": web, "passwort": pw})
        try:
            mid = mandanten.mandant_anlegen(w, "Neu über Web", k.id)  # SECURITY DEFINER, ohne CREATEROLE
            namen = {m.name: m for m in mandanten.mandanten_von(w, k.id)}
            assert set(namen) == {"Vorher", "Neu über Web"}  # RLS: Betreiber sieht alle Zeilen
            for info in namen.values():
                with pytest.raises(Exception, match="permission denied"):
                    w.execute(pgsql.SQL("SELECT 1 FROM {}.firmen").format(pgsql.Identifier(info.schema)))
                w.execute(pgsql.SQL("SET ROLE {}").format(pgsql.Identifier(info.rolle)))
                w.execute(pgsql.SQL("CREATE TABLE IF NOT EXISTS {}.probe (x int)").format(pgsql.Identifier(info.schema)))
                assert w.execute("SELECT count(*) FROM js_zentral.mandanten").fetchone()[0] == 1
                w.execute("RESET ROLE")
            assert mid in [m.id for m in namen.values()]
            with pytest.raises(Exception, match="permission denied"):
                w.execute("INSERT INTO js_zentral.betreiber VALUES ('x')")
        finally:
            w.close()
    finally:
        for (mid,) in roh.execute("SELECT id FROM js_zentral.mandanten").fetchall():
            mandanten.mandant_entfernen(roh, mid)
        roh.execute("DROP SCHEMA js_zentral CASCADE")
        roh.execute(pgsql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(pgsql.Identifier(_PG["datenbank"]), pgsql.Identifier(web)))
        roh.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(web)))
        roh.close()


def test_dateien_eines_mandanten(tmp_path):
    m = db.Mandant(1, "x", "js_x", "js_x", tmp_path / "m")
    (tmp_path / "m" / "cv").mkdir(parents=True)
    (tmp_path / "m" / "cv" / "Lebenslauf.pdf").write_bytes(b"%PDF")
    vorhanden = tmp_path / "anderswo.pdf"
    vorhanden.write_bytes(b"%PDF")
    assert db.datei("/gibt/es/nicht/Lebenslauf.pdf") == db.Path("/gibt/es/nicht/Lebenslauf.pdf")  # ohne Mandant unverändert
    with db.mit_mandant(m):
        assert db.datei(vorhanden) == vorhanden
        assert db.datei("/anderer/rechner/Lebenslauf.pdf") == tmp_path / "m" / "cv" / "Lebenslauf.pdf"
        fehlt = db.datei("/anderer/rechner/Zeugnis.pdf")
        assert fehlt == tmp_path / "m" / "unterlagen" / "Zeugnis.pdf" and not fehlt.exists()


def test_avatar_typ_erkennt_nur_bilder():
    from jobsuche import mandanten as m
    assert m.avatar_typ(b"\x89PNG\r\n\x1a\n" + b"0" * 10) == "image/png"
    assert m.avatar_typ(b"\xff\xd8\xff\xe0" + b"0" * 10) == "image/jpeg"
    assert m.avatar_typ(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert m.avatar_typ(b"<svg onload=alert(1)>") is None
    assert m.avatar_typ(b"GIF89a") is None
