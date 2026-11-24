"""Lange Arbeiten (Suche, KI, Versand) laufen im Hintergrund. Je Mandant höchstens eine gleichzeitig plus eine in der
Warteschlange; insgesamt wenige parallel, weil der Container klein ist. Der Browser fragt den Stand ab."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import db, ki, llm, quellen, versand
from ..mandanten import KontoFehler

log = logging.getLogger("jobagent.auftraege")

# Diese Fehler sind Meldungen für den Menschen. Alles andere wird nur protokolliert, nach außen geht „interner Fehler“.
MELDBAR = (ValueError, RuntimeError, ki.NichtMoeglich, llm.LLMFehler, versand.VersandFehler, quellen.SucheFehler, KontoFehler)


class ZuViele(Exception):
    pass


@dataclass
class Auftrag:
    id: str
    mandant_id: int
    titel: str
    status: str = "wartet"  # wartet | laeuft | fertig | fehler
    fortschritt: str = ""
    ergebnis: object = None
    fehler: str = ""
    erstellt: float = field(default_factory=time.time)
    fertig_um: float = 0.0


class Auftraege:
    def __init__(self, parallel: int = 2, je_mandant: int = 2):
        self._pool = ThreadPoolExecutor(parallel, thread_name_prefix="auftrag")
        self._alle: dict[str, Auftrag] = {}
        self._sperre = threading.Lock()
        self.je_mandant = je_mandant

    def starte(self, mandant: db.Mandant, titel: str, arbeit, kontext_setzen) -> Auftrag:
        """`arbeit(conn, melde)` läuft im Kontext des Mandanten. `kontext_setzen()` setzt Ablage und KI-Freigabe."""
        with self._sperre:
            self._aufraeumen()
            offen = sum(1 for a in self._alle.values() if a.mandant_id == mandant.id and a.status in ("wartet", "laeuft"))
            if offen >= self.je_mandant:
                raise ZuViele("Es laufen schon Arbeiten. Warte, bis eine fertig ist.")
            a = Auftrag(uuid.uuid4().hex, mandant.id, titel)
            self._alle[a.id] = a

        def lauf():
            a.status = "laeuft"
            with db.mit_mandant(mandant):
                kontext_setzen()
                conn = None
                try:
                    conn = db.verbinden()
                    a.ergebnis = arbeit(conn, lambda text: setattr(a, "fortschritt", str(text)[:300]))
                    a.status = "fertig"
                except MELDBAR as fehler:
                    a.fehler, a.status = str(fehler), "fehler"
                except Exception:
                    log.exception("Auftrag %s (%s) fehlgeschlagen", a.titel, a.id)
                    a.fehler, a.status = "Interner Fehler. Der Betreiber kann im Protokoll nachsehen.", "fehler"
                finally:
                    a.fertig_um = time.time()
                    if conn is not None:
                        conn.close()

        ctx = contextvars.copy_context()  # eigener Kontext je Auftrag: Mandant gilt nur hier
        self._pool.submit(ctx.run, lauf)
        return a

    def hole(self, auftrag_id: str, mandant_id: int) -> Auftrag | None:
        a = self._alle.get(auftrag_id)
        return a if a and a.mandant_id == mandant_id else None

    def laufende(self, mandant_id: int) -> list[Auftrag]:
        return [a for a in self._alle.values() if a.mandant_id == mandant_id and a.status in ("wartet", "laeuft")]

    def _aufraeumen(self):
        grenze = time.time() - 3600
        for k in [k for k, a in self._alle.items() if a.fertig_um and a.fertig_um < grenze]:
            self._alle.pop(k, None)
