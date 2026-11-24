"""Betrieb der Mandanten-Datenbank vom Rechner des Betreibers aus (Anmelde-Benutzer aus daten/datenbank.json,
Passwort im Schlüsselbund). Geheimnisse gibt dieses Werkzeug nie aus: Passwörter landen im Schlüsselbund oder, für den
Server, in einer Datei mit Rechten 600.

    python -m jobsuche.betrieb einrichten [--web-passwort-datei PFAD]
    python -m jobsuche.betrieb uebernehmen --email ADRESSE [--schema js_…] [--name NAME]
    python -m jobsuche.betrieb passwort --email ADRESSE        # neues Konto-Passwort, nur in den Schlüsselbund
    python -m jobsuche.betrieb einladung [--notiz TEXT]         # Einladungscode (einmal angezeigt)
    python -m jobsuche.betrieb stand
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import keyring
from psycopg import sql as pgsql

from . import config, db, mandanten, pg


def konto_eintrag(email: str) -> str:
    """Name des Schlüsselbund-Eintrags (Dienst „jobsuche“) für das Konto-Passwort."""
    return f"konto:{email.strip().lower()}"


def _roh():
    s = db.server()
    if not s:
        raise SystemExit("Kein PostgreSQL-Server eingestellt (System → Daten oder daten/datenbank.json).")
    return pg.roh_verbinden(s), s


def einrichten(web_passwort_datei: str | None) -> None:
    roh, s = _roh()
    with roh:
        if not roh.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (mandanten.WEB_ROLLE,)).fetchone():
            pw = secrets.token_urlsafe(30)
            roh.execute(pgsql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                pgsql.Identifier(mandanten.WEB_ROLLE), pgsql.Literal(pw)))
            keyring.set_password("jobsuche", f"postgres:{mandanten.WEB_ROLLE}@{s['host']}", pw)
            print(f"Rolle {mandanten.WEB_ROLLE} angelegt, Passwort im Schlüsselbund "
                  f"(jobsuche / postgres:{mandanten.WEB_ROLLE}@{s['host']}).")
        roh.execute(pgsql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            pgsql.Identifier(s["datenbank"]), pgsql.Identifier(mandanten.WEB_ROLLE)))
        mandanten.einrichten(roh)
        print(f"Zentrales Schema {mandanten.ZENTRAL} ist eingerichtet.")
    if web_passwort_datei:
        pw = keyring.get_password("jobsuche", f"postgres:{mandanten.WEB_ROLLE}@{s['host']}")
        if not pw:
            raise SystemExit("Passwort der Web-Rolle nicht im Schlüsselbund.")
        ziel = Path(web_passwort_datei)
        alt = os.umask(0o077)
        try:
            ziel.write_text(pw)
        finally:
            os.umask(alt)
        ziel.chmod(0o600)


def uebernehmen(email: str, schema: str | None, name: str) -> None:
    """Die bisherige Datenbank (Schema der Anmeldung per Mailadresse) wird der erste Mandant mit Admin-Konto."""
    from . import anmeldung
    roh, _ = _roh()
    with roh:
        if not mandanten.ist_eingerichtet(roh):
            raise SystemExit("Erst: python -m jobsuche.betrieb einrichten")
        schema = schema or pg.schema_fuer(anmeldung.datenbank_fuer(email), config.DATEN)
        pw = secrets.token_urlsafe(18)
        konto = mandanten.konto_anlegen(roh, email, pw, name, admin=True)
        keyring.set_password("jobsuche", konto_eintrag(email), pw)
        mid = mandanten.schema_uebernehmen(roh, schema, name or "Mandant 1", konto.id)
        mandanten.setze_claude(roh, mid, True)
        zahlen = {t: roh.execute(pgsql.SQL("SELECT count(*) FROM {}.{}").format(pgsql.Identifier(schema), pgsql.Identifier(t))).fetchone()[0]
                  for t in ("firmen", "fakten", "entwuerfe", "dokumente", "chunks", "lektionen", "einstellungen", "ereignisse")}
    print(f"Mandant {mid} aus Schema {schema} übernommen, Konto {konto.id} (Admin). "
          f"Passwort im Schlüsselbund: jobsuche / {konto_eintrag(email)}")
    print("Zeilen: " + ", ".join(f"{t} {n}" for t, n in zahlen.items()))


def passwort(email: str) -> None:
    roh, _ = _roh()
    with roh:
        z = roh.execute(f"SELECT id FROM {mandanten.ZENTRAL}.konten WHERE email = %s", (email.strip().lower(),)).fetchone()
        if not z:
            raise SystemExit("Konto unbekannt.")
        pw = secrets.token_urlsafe(18)
        mandanten.setze_passwort(roh, z[0], pw)
        keyring.set_password("jobsuche", konto_eintrag(email), pw)
    print(f"Neues Passwort im Schlüsselbund: jobsuche / {konto_eintrag(email)}")


def einladung(notiz: str) -> None:
    roh, _ = _roh()
    with roh:
        admin = roh.execute(f"SELECT id FROM {mandanten.ZENTRAL}.konten WHERE admin ORDER BY id LIMIT 1").fetchone()
        code = mandanten.einladung_erzeugen(roh, admin[0] if admin else None, notiz)
    print(f"Einladungscode (einmalig, {mandanten.EINLADUNG_GUELTIG.days} Tage gültig): {code}")


def stand() -> None:
    roh, _ = _roh()
    with roh:
        if not mandanten.ist_eingerichtet(roh):
            print("Zentrales Schema fehlt.")
            return
        for m in mandanten.alle_mandanten(roh):
            print(f"Mandant {m['id']} {m['name']!r} schema={m['schema']} claude={m['claude_erlaubt']} "
                  f"nachtlauf={m['nachtlauf']} konten={len(m['konten'])}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m jobsuche.betrieb")
    b = p.add_subparsers(dest="befehl", required=True)
    e = b.add_parser("einrichten")
    e.add_argument("--web-passwort-datei")
    u = b.add_parser("uebernehmen")
    u.add_argument("--email", required=True)
    u.add_argument("--schema")
    u.add_argument("--name", default="")
    pw = b.add_parser("passwort")
    pw.add_argument("--email", required=True)
    ei = b.add_parser("einladung")
    ei.add_argument("--notiz", default="")
    b.add_parser("stand")
    a = p.parse_args(argv)
    if a.befehl == "einrichten":
        einrichten(a.web_passwort_datei)
    elif a.befehl == "uebernehmen":
        uebernehmen(a.email, a.schema, a.name)
    elif a.befehl == "passwort":
        passwort(a.email)
    elif a.befehl == "einladung":
        einladung(a.notiz)
    else:
        stand()
    return 0


if __name__ == "__main__":
    sys.exit(main())
