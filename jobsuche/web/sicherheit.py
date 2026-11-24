"""Schutz der öffentlichen Web-Oberfläche: Rate-Limits, Sicherheits-Kopfzeilen, Client-Adresse."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")

KOPFZEILEN = {
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class Begrenzer:
    """Gleitendes Fenster je Schlüssel (IP, Konto, Mandant). Im Speicher des Prozesses: Der Dienst läuft als ein Prozess."""

    def __init__(self, anzahl: int, sekunden: float):
        self.anzahl, self.sekunden = anzahl, sekunden
        self._zeiten: dict[str, deque] = defaultdict(deque)
        self._sperre = threading.Lock()

    def erlaubt(self, schluessel: str, jetzt: float | None = None) -> bool:
        jetzt = time.monotonic() if jetzt is None else jetzt
        with self._sperre:
            q = self._zeiten[schluessel]
            while q and q[0] <= jetzt - self.sekunden:
                q.popleft()
            if len(q) >= self.anzahl:
                return False
            q.append(jetzt)
            if len(self._zeiten) > 50_000:  # Schutz vor Speicherwachstum durch viele Adressen
                for k in [k for k, v in self._zeiten.items() if not v][:10_000]:
                    self._zeiten.pop(k, None)
            return True
