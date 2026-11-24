"""Versand per SMTP. Sendet nur freigegebene Mails und nur mit Verschlüsselung.

Das Passwort steht nie im Klartext in der Datenbank oder im Protokoll: Desktop im Schlüsselbund des Systems, Web
verschlüsselt (AES-GCM) beim Mandanten, siehe geheim.py.
"""

import mimetypes
import smtplib
import socket
import sqlite3
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from . import config, db, geheim, ki, mailkonten, profil, regeln

DIENST = "jobsuche"
MAX_ANHANG_BYTES = 20 * 1024 * 1024
SICHERHEIT = {"ssl": "SSL/TLS", "starttls": "STARTTLS", "keine": "keine"}
FELDER = ("email", "host", "port", "sicherheit", "benutzer", "quelle", "oauth")


class VersandFehler(Exception):
    pass


class AnmeldeFehler(VersandFehler):
    pass


class NichtErreichbar(VersandFehler):
    pass


def speichere_konto(conn: sqlite3.Connection, konto: mailkonten.Konto) -> None:
    if "@" not in konto.email or not konto.host or not konto.benutzer:
        raise ValueError("Adresse, Server oder Benutzername fehlt.")
    if not 0 < int(konto.port) < 65536:
        raise ValueError("Der Port ist ungültig.")
    if konto.sicherheit not in ("ssl", "starttls"):
        raise ValueError("Ohne Verschlüsselung sendet die App nicht, sonst ginge dein Passwort im Klartext übers Netz.")
    for feld in FELDER:
        db.setze_einstellung(conn, f"smtp.{feld}", str(getattr(konto, feld)))
    db.protokolliere(conn, "Mailkonto gewählt", f"{konto.email} über {konto.host} ({konto.quelle})")


def lade_konto(conn: sqlite3.Connection) -> mailkonten.Konto | None:
    host = db.einstellung(conn, "smtp.host", "")
    if not host:
        return None
    return mailkonten.Konto(
        db.einstellung(conn, "smtp.quelle", ""), db.einstellung(conn, "smtp.email", ""), host,
        int(db.einstellung(conn, "smtp.port", "587")), db.einstellung(conn, "smtp.sicherheit", "starttls"),
        db.einstellung(conn, "smtp.benutzer", ""), db.einstellung(conn, "smtp.oauth", "False") == "True",
    )


def _schluessel(konto: mailkonten.Konto) -> str:
    return f"{konto.benutzer}@{konto.host}"


def gespeichertes_passwort(konto: mailkonten.Konto) -> str | None:
    return geheim.hole(_schluessel(konto))


def speichere_passwort(konto: mailkonten.Konto, passwort: str) -> None:
    try:
        geheim.setze(_schluessel(konto), passwort)
    except geheim.GeheimFehler as fehler:
        raise VersandFehler(str(fehler)) from fehler


def loesche_passwort(konto: mailkonten.Konto) -> None:
    geheim.loesche(_schluessel(konto))


def anhang_problem(pfade: list[Path]) -> str | None:
    fehlend = [p.name for p in pfade if not p.exists()]
    if fehlend:
        return "Anhang fehlt: " + ", ".join(fehlend)
    if sum(p.stat().st_size for p in pfade) > MAX_ANHANG_BYTES:
        return "Die Anhänge sind zusammen größer als 20 MB."
    return None


def baue_nachricht(absender_name: str, absender: str, empfaenger: str, betreff: str, text: str, anhaenge: list[Path]) -> EmailMessage:
    nachricht = EmailMessage()
    nachricht["From"] = f"{absender_name} <{absender}>" if absender_name else absender
    nachricht["To"] = empfaenger
    nachricht["Subject"] = betreff
    nachricht["Date"] = formatdate(localtime=True)
    nachricht["Message-ID"] = make_msgid(domain=absender.split("@")[-1])
    nachricht.set_content(text)
    for pfad in anhaenge:
        art, _ = mimetypes.guess_type(pfad.name)
        haupt, unter = (art or "application/octet-stream").split("/", 1)
        nachricht.add_attachment(pfad.read_bytes(), maintype=haupt, subtype=unter, filename=pfad.name)
    return nachricht


def _verbinde(konto: mailkonten.Konto, passwort: str, smtp_ssl, smtp_klar):
    kontext = ssl.create_default_context()
    try:
        if konto.sicherheit == "ssl":
            server = smtp_ssl(konto.host, konto.port, timeout=30, context=kontext)
        elif konto.sicherheit == "starttls":
            server = smtp_klar(konto.host, konto.port, timeout=30)
            server.ehlo()
            server.starttls(context=kontext)
            server.ehlo()
        else:
            raise VersandFehler("Ohne Verschlüsselung sendet die App nicht.")
        server.login(konto.benutzer, passwort)
        return server
    except smtplib.SMTPAuthenticationError as fehler:
        hinweis = " Dieses Konto nutzt OAuth: Du brauchst beim Anbieter ein App-Passwort." if konto.oauth else \
            " Manche Anbieter verlangen ein App-Passwort."
        raise AnmeldeFehler(f"Der Server hat die Anmeldung abgelehnt.{hinweis}") from fehler
    except ssl.SSLError as fehler:
        raise VersandFehler(f"Die Verschlüsselung zu {konto.host} ist fehlgeschlagen: {fehler}") from fehler
    except smtplib.SMTPException as fehler:
        raise VersandFehler(f"Der Server meldet einen Fehler: {fehler}") from fehler
    except OSError as fehler:
        raise NichtErreichbar(f"{konto.host}:{konto.port} ist nicht erreichbar: {fehler}") from fehler


def _uebertrage(konto, passwort, nachricht, empfaenger, smtp_ssl, smtp_klar) -> None:
    server = _verbinde(konto, passwort, smtp_ssl, smtp_klar)
    try:
        server.send_message(nachricht, from_addr=konto.email, to_addrs=empfaenger)
    except smtplib.SMTPRecipientsRefused as fehler:
        raise VersandFehler(f"Der Server lehnt den Empfänger ab: {', '.join(fehler.recipients)}") from fehler
    except (smtplib.SMTPException, socket.timeout, OSError) as fehler:
        raise VersandFehler(f"Beim Senden ging etwas schief, die Mail gilt als nicht gesendet: {fehler}") from fehler
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass


def pruefe_anmeldung(konto: mailkonten.Konto, passwort: str, smtp_ssl=smtplib.SMTP_SSL, smtp_klar=smtplib.SMTP) -> None:
    """Meldet sich an und wieder ab. Gesendet wird nichts."""
    server = _verbinde(konto, passwort, smtp_ssl, smtp_klar)
    try:
        server.quit()
    except (smtplib.SMTPException, OSError):
        pass


def _kein_demo_versand() -> None:
    if config.DEMO:
        raise VersandFehler("In der Demo wird nichts gesendet. Melde dich dafür mit deinem Mailkonto an.")


def sende_test(conn: sqlite3.Connection, passwort: str, smtp_ssl=smtplib.SMTP_SSL, smtp_klar=smtplib.SMTP) -> str:
    """Prüft das Konto mit einer Mail an die eigene Adresse. An Firmen geht dabei nichts."""
    _kein_demo_versand()
    konto = lade_konto(conn)
    if not konto:
        raise VersandFehler("Es ist noch kein Mailkonto gewählt.")
    nachricht = baue_nachricht("", konto.email, konto.email, "Job Agent: Testmail", "Das Mailkonto funktioniert.", [])
    _uebertrage(konto, passwort, nachricht, [konto.email], smtp_ssl, smtp_klar)
    db.protokolliere(conn, "Testmail gesendet", konto.email)
    return konto.email


def sende_entwurf(conn: sqlite3.Connection, entwurf_id: int, passwort: str, smtp_ssl=smtplib.SMTP_SSL, smtp_klar=smtplib.SMTP) -> None:
    """Sendet eine freigegebene Mail. Eine Kopie geht per Bcc an dich, weil SMTP nichts unter „Gesendet“ ablegt."""
    _kein_demo_versand()
    e = conn.execute("SELECT e.*, f.name AS firma FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id WHERE e.id = ?",
                     (entwurf_id,)).fetchone()
    if e["status"] != "freigegeben":
        raise VersandFehler("Gesendet werden nur freigegebene Mails, und jede nur einmal.")
    befunde = ki.pruefe_entwurf(conn, entwurf_id)
    if regeln.gesperrt(befunde):
        raise VersandFehler("Gesperrt: " + " ".join(b.text for b in befunde if b.stufe == "sperre"))
    konto = lade_konto(conn)
    if not konto:
        raise VersandFehler("Es ist noch kein Mailkonto gewählt.")
    anhaenge = profil.anhaenge_des_entwurfs(conn, entwurf_id)
    problem = anhang_problem(anhaenge)
    if problem:
        raise VersandFehler(problem)
    name = conn.execute("SELECT name FROM person WHERE id = 1").fetchone()
    nachricht = baue_nachricht(name["name"] if name else "", konto.email, e["empfaenger"], e["betreff"], e["text"], anhaenge)
    _uebertrage(konto, passwort, nachricht, [e["empfaenger"], konto.email], smtp_ssl, smtp_klar)
    ki.setze_status(conn, entwurf_id, "gesendet")
    db.protokolliere(conn, "Mail gesendet", f"An {e['empfaenger']}, {len(anhaenge)} Anhänge", e["firma_id"])
