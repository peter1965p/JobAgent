"""Die Person, für die diese Installation arbeitet. Jede Installation hat genau eine."""

import sqlite3

from . import db

FELDER = ("name", "email", "telefon", "ort", "links")


def lade(conn: sqlite3.Connection) -> dict:
    zeile = conn.execute("SELECT * FROM person WHERE id = 1").fetchone()
    return {f: (zeile[f] if zeile else "") for f in FELDER}


def speichere(conn: sqlite3.Connection, **werte) -> None:
    daten = {f: str(werte.get(f, "")).strip() for f in FELDER}
    if not daten["name"]:
        raise ValueError("Der Name fehlt.")
    if "@" not in daten["email"]:
        raise ValueError("Die Mailadresse fehlt oder ist ungültig.")
    conn.execute(
        "INSERT INTO person (id, name, email, telefon, ort, links) VALUES (1, :name, :email, :telefon, :ort, :links) "
        "ON CONFLICT(id) DO UPDATE SET name = :name, email = :email, telefon = :telefon, ort = :ort, links = :links",
        daten,
    )
    conn.commit()
    db.protokolliere(conn, "Profil gespeichert")


def setze_email(conn: sqlite3.Connection, email: str) -> None:
    """Merkt sich die Anmeldeadresse. Den Namen trägt die Person danach unter „Profil“ ein."""
    conn.execute("INSERT INTO person (id, name, email) VALUES (1, '', ?) ON CONFLICT(id) DO UPDATE SET email = excluded.email", (email,))
    conn.commit()


def name(conn: sqlite3.Connection) -> str:
    return lade(conn)["name"]


def initialen(voller_name: str) -> str:
    teile = voller_name.split()
    return "".join(t[0] for t in (teile[0], teile[-1])).upper() if teile else "?"
