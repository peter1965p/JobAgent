"""Ablage für Passwörter und Schlüssel (Mailkonto, Postfächer, Adzuna/Jooble).

Desktop: Schlüsselbund des Systems (Dienst „jobsuche“), wie bisher. Web: verschlüsselt (AES-GCM) in der Datenbank des
Mandanten; der Schlüssel kommt aus der Umgebung des Dienstes (Datei mit Rechten 600) und liegt nie in der Datenbank.
Welche Ablage gilt, bestimmt eine Kontextvariable: So kann der Webdienst je Anfrage den Mandanten wechseln.
"""

from __future__ import annotations

import base64
import contextvars
import os

import keyring
from keyring.errors import KeyringError

DIENST = "jobsuche"


class GeheimFehler(Exception):
    pass


class Schluesselbund:
    def hole(self, name: str) -> str | None:
        try:
            return keyring.get_password(DIENST, name)
        except KeyringError:
            return None

    def setze(self, name: str, wert: str) -> None:
        try:
            keyring.set_password(DIENST, name, wert)
        except KeyringError as fehler:
            raise GeheimFehler(f"Der Schlüsselbund ist nicht verfügbar: {fehler}") from fehler

    def loesche(self, name: str) -> None:
        try:
            if keyring.get_password(DIENST, name) is not None:
                keyring.delete_password(DIENST, name)
        except KeyringError:
            pass


class Verschluesselt:
    """AES-256-GCM, je Wert eine neue Nonce. Der Name des Eintrags ist zusätzlich authentifiziert (AAD): Ein Wert lässt
    sich nicht unter einen anderen Namen kopieren. Gespeichert wird in `einstellungen` des aktiven Mandanten."""

    PRAEFIX = "geheim:"

    def __init__(self, schluessel: bytes):
        if len(schluessel) != 32:
            raise GeheimFehler("Der Schlüssel muss 32 Byte lang sein.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aes = AESGCM(schluessel)

    def verschluessele(self, name: str, wert: str) -> str:
        nonce = os.urandom(12)
        return "v1:" + base64.b64encode(nonce + self._aes.encrypt(nonce, wert.encode(), name.encode())).decode()

    def entschluessele(self, name: str, text: str) -> str | None:
        if not text.startswith("v1:"):
            return None
        try:
            roh = base64.b64decode(text[3:])
            return self._aes.decrypt(roh[:12], roh[12:], name.encode()).decode()
        except Exception:  # falscher Schlüssel oder manipuliert: wie „nicht gesetzt“
            return None

    def _conn(self):
        from . import db
        return db.verbinden()

    def hole(self, name: str) -> str | None:
        from . import db
        conn = self._conn()
        try:
            text = db.einstellung(conn, self.PRAEFIX + name, "")
        finally:
            conn.close()
        return self.entschluessele(name, text) if text else None

    def setze(self, name: str, wert: str) -> None:
        from . import db
        conn = self._conn()
        try:
            db.setze_einstellung(conn, self.PRAEFIX + name, self.verschluessele(name, wert))
        finally:
            conn.close()

    def loesche(self, name: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM einstellungen WHERE key = ?", (self.PRAEFIX + name,))
            conn.commit()
        finally:
            conn.close()


_STANDARD = Schluesselbund()
_ablage: contextvars.ContextVar = contextvars.ContextVar("geheim_ablage", default=None)


def ablage():
    return _ablage.get() or _STANDARD


def setze_ablage(neu) -> contextvars.Token:
    return _ablage.set(neu)


def hole(name: str) -> str | None:
    return ablage().hole(name)


def setze(name: str, wert: str) -> None:
    ablage().setze(name, wert)


def loesche(name: str) -> None:
    ablage().loesche(name)


def schluessel_aus_text(text: str) -> bytes:
    """Schlüssel aus der Umgebung: 32 Byte Base64 (urlsafe oder normal)."""
    try:
        roh = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
    except ValueError as fehler:
        raise GeheimFehler("Der Schlüssel ist kein gültiges Base64.") from fehler
    if len(roh) != 32:
        raise GeheimFehler("Der Schlüssel muss 32 Byte lang sein.")
    return roh
