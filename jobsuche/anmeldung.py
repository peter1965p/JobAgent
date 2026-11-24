"""Anmeldung mit Mailadresse und Mailpasswort. Der Mailserver prüft, jede Person hat ihre eigene Datenbank.

Die Trennung schützt vor Verwechslung, nicht vor Zugriff: Die Dateien selbst bleiben unverschlüsselt.
"""

import hashlib
import hmac
import os
import smtplib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from . import config, db, mailkonten, person, versand


class KontoFehlt(Exception):
    """Für diese Adresse ist kein Server bekannt. Die Person muss ihn eintragen."""


@dataclass(frozen=True)
class Ergebnis:
    pfad: Path
    konto: mailkonten.Konto
    offline: bool = False


def _adresse(email: str) -> str:
    return email.strip().lower()


def datenbank_fuer(email: str) -> Path:
    """Eine ältere gemeinsame Datenbank bleibt bei ihrer Besitzerin, alle anderen bekommen einen eigenen Ordner."""
    alt = config.DATEN / "jobsuche.db"
    if db.existiert(alt):
        conn = db.verbinden(alt) if db.server() else sqlite3.connect(alt)
        try:
            zeile = conn.execute("SELECT email FROM person WHERE id = 1").fetchone()
        except sqlite3.Error:
            zeile = None
        finally:
            conn.close()
        if zeile and _adresse(zeile[0]) == _adresse(email):
            return alt
    return config.DATEN / "personen" / hashlib.sha256(_adresse(email).encode()).hexdigest()[:16] / "jobsuche.db"


def _bekanntes_konto(pfad: Path) -> tuple[mailkonten.Konto | None, str]:
    if not db.existiert(pfad):
        return None, ""
    conn = db.verbinden(pfad)
    try:
        return versand.lade_konto(conn), db.einstellung(conn, "login.pruefwert", "")
    finally:
        conn.close()


def _pruefwert(passwort: str, salz: bytes | None = None) -> str:
    salz = salz or os.urandom(16)
    wert = hashlib.scrypt(passwort.encode(), salt=salz, n=2**14, r=8, p=1, dklen=32)
    return f"{salz.hex()}:{wert.hex()}"


def _passt(passwort: str, gespeichert: str) -> bool:
    if ":" not in gespeichert:
        return False
    salz, _ = gespeichert.split(":", 1)
    return hmac.compare_digest(_pruefwert(passwort, bytes.fromhex(salz)), gespeichert)


def pruefe(email: str, passwort: str, konto: mailkonten.Konto | None = None, home: Path | None = None,
           smtp_ssl=smtplib.SMTP_SSL, smtp_klar=smtplib.SMTP) -> Ergebnis:
    """Meldet beim Mailserver an, ohne etwas zu senden. Ist er nicht erreichbar, zählt die letzte erfolgreiche Anmeldung."""
    email = email.strip()
    if "@" not in email or not passwort:
        raise versand.AnmeldeFehler("Gib deine Mailadresse und dein Passwort ein.")
    pfad = datenbank_fuer(email)
    bekannt, pruefwert = _bekanntes_konto(pfad)
    konto = konto or bekannt
    if not konto:
        passend = [k for k in mailkonten.finde_alle(home) if _adresse(k.email) == _adresse(email)]
        if not passend:
            raise KontoFehlt("Für diese Adresse habe ich in deinen Mailprogrammen keinen Server gefunden.")
        konto = passend[0]
    if konto.sicherheit not in ("ssl", "starttls"):
        raise versand.AnmeldeFehler("Dieses Konto ist ohne Verschlüsselung eingerichtet. So meldet sich die App nicht an.")
    try:
        versand.pruefe_anmeldung(konto, passwort, smtp_ssl, smtp_klar)
    except versand.NichtErreichbar:
        if pruefwert and _passt(passwort, pruefwert):
            return Ergebnis(pfad, konto, offline=True)
        raise
    return Ergebnis(pfad, konto)


def ist_neu(email: str) -> bool:
    """Wahr, wenn für diese Adresse noch kein Konto auf diesem Rechner existiert."""
    return "@" in email and not db.existiert(datenbank_fuer(email))


def oeffne(ergebnis: Ergebnis, email: str, passwort: str, name: str = "") -> None:
    """Macht die Datenbank der Person zur aktiven. Das Passwort selbst wird nicht gespeichert."""
    config.DB_PFAD = ergebnis.pfad
    config.DEMO = False
    conn = db.verbinden(ergebnis.pfad)
    try:
        person.setze_email(conn, email.strip())
        if name.strip():
            person.speichere(conn, **{**person.lade(conn), "name": name, "email": email})
        if not ergebnis.offline:
            db.setze_einstellung(conn, "login.pruefwert", _pruefwert(passwort))
        if not versand.lade_konto(conn):
            versand.speichere_konto(conn, ergebnis.konto)
        db.protokolliere(conn, "Anmeldung", "ohne Netz" if ergebnis.offline else "")
    finally:
        conn.close()


def _schluessel(email: str) -> str:
    return f"login:{_adresse(email)}"


def gemerktes_passwort(email: str, konto: bool = False) -> str:
    try:
        return keyring.get_password(versand.DIENST, konto_eintrag(email) if konto else _schluessel(email)) or ""
    except KeyringError:
        return ""


def merke_passwort(email: str, passwort: str, merken: bool, konto: bool = False) -> str | None:
    """Gibt bei einem Problem eine Meldung zurück. Die Anmeldung selbst scheitert daran nicht."""
    eintrag = konto_eintrag(email) if konto else _schluessel(email)
    try:
        if merken:
            keyring.set_password(versand.DIENST, eintrag, passwort)
        elif gemerktes_passwort(email, konto):
            keyring.delete_password(versand.DIENST, eintrag)
    except KeyringError as fehler:
        return f"Der Schlüsselbund ist nicht verfügbar: {fehler}"
    return None


# --- Konto-Anmeldung gegen die Mandanten-Datenbank (PostgreSQL mit js_zentral) ------------------------------------------
# Ist ein Server eingestellt und das zentrale Schema eingerichtet, meldet sich die Desktop-App mit Konto und
# Konto-Passwort an (wie im Web). Das Mailpasswort dient dann nur noch dem Versand. Ohne Server bleibt alles wie oben.

@dataclass(frozen=True)
class KontoErgebnis:
    konto: object  # mandanten.Konto
    mandant: object  # mandanten.MandantInfo
    mandanten: tuple = ()


def konten_verfuegbar() -> bool:
    if not db.server():
        return False
    from . import mandanten
    try:
        with mandanten.zentral_verbinden() as roh:
            return mandanten.ist_eingerichtet(roh)
    except Exception:  # Server nicht erreichbar: die Anmeldung meldet das dann selbst
        return True


def konto_eintrag(email: str) -> str:
    return f"konto:{_adresse(email)}"


def pruefe_konto(email: str, passwort: str, mandant_id: int | None = None) -> KontoErgebnis:
    from . import mandanten
    if "@" not in email or not passwort:
        raise versand.AnmeldeFehler("Gib deine Mailadresse und dein Passwort ein.")
    try:
        with mandanten.zentral_verbinden() as roh:
            konto = mandanten.anmelden(roh, email, passwort, "desktop")
            info = mandanten.mandant_fuer(roh, konto.id, mandant_id)
            return KontoErgebnis(konto, info, tuple(mandanten.mandanten_von(roh, konto.id)))
    except mandanten.KontoFehler as fehler:
        raise versand.AnmeldeFehler(str(fehler)) from None
    except (sqlite3.Error, OSError) as fehler:
        raise versand.NichtErreichbar(f"Die Datenbank ist nicht erreichbar: {str(fehler).splitlines()[0][:200]}") from None
    except Exception as fehler:  # psycopg-Fehler beim Verbinden
        if type(fehler).__module__.startswith("psycopg"):
            raise versand.NichtErreichbar(f"Die Datenbank ist nicht erreichbar: {str(fehler).splitlines()[0][:200]}") from None
        raise


def mandant_ordner(info) -> Path:
    """Dateien des Mandanten auf diesem Rechner. Die übernommene Altdatenbank behält ihren Ordner `daten/`."""
    from . import pg
    if info.schema == pg.schema_fuer(config.DATEN / "jobsuche.db", config.DATEN):
        return config.DATEN
    return config.DATEN / "mandanten" / str(info.id)


def oeffne_mandant(ergebnis: KontoErgebnis) -> None:
    from . import mandanten
    ordner = mandant_ordner(ergebnis.mandant)
    db.setze_mandant(mandanten.als_mandant(ergebnis.mandant, ordner))
    config.DB_PFAD = ordner / "jobsuche.db"  # nur für Dateien daneben; gelesen wird aus dem Schema des Mandanten
    config.DEMO = False
    conn = db.verbinden()
    try:
        if not person.lade(conn)["email"]:
            person.setze_email(conn, ergebnis.konto.email)
        db.protokolliere(conn, "Anmeldung", f"Konto, Mandant {ergebnis.mandant.name}")
    finally:
        conn.close()


def wechsle_mandant(ergebnis: KontoErgebnis, mandant_id: int) -> KontoErgebnis:
    from . import mandanten
    with mandanten.zentral_verbinden() as roh:
        info = mandanten.mandant_fuer(roh, ergebnis.konto.id, mandant_id)
    return KontoErgebnis(ergebnis.konto, info, ergebnis.mandanten)


def aendere_konto_passwort(ergebnis: KontoErgebnis, alt: str, neu: str) -> None:
    from . import mandanten
    try:
        with mandanten.zentral_verbinden() as roh:
            mandanten.aendere_passwort(roh, ergebnis.konto.id, alt, neu)
    except mandanten.KontoFehler as fehler:
        raise ValueError(str(fehler)) from None


def schliesse_mandant() -> None:
    db.setze_mandant(None)
