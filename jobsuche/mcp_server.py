"""MCP-Server des Job Agent: Werkzeuge für KI-Assistenten wie Claude Code (Model Context Protocol, stdio).

Start:  python -m jobsuche.mcp_server
In Claude Code:  claude mcp add jobagent -- <venv>/bin/python -m jobsuche.mcp_server  (Arbeitsverzeichnis: Projektordner)

Grundsätze:
- Lesen und Vorschlagen ja, Versenden nie. Freigabe und Versand bleiben beim Menschen in der App (Human-in-the-Loop).
- Gleiche Anmeldung wie die Desktop-App: mit PostgreSQL das Konto aus dem Schlüsselbund und dessen Mandant,
  sonst die lokale SQLite-Datenbank. Der Mandant ist an seine Datenbankrolle gebunden, fremde Daten verweigert der Server.
- Ausgaben ohne Geheimnisse: keine Passwörter, keine Schlüssel, Mailadressen von Ansprechpersonen nur, wenn sie
  ohnehin im Entwurf stehen.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager, redirect_stdout

from mcp.server.mcpserver import MCPServer

from . import config, db, ki, suche, uebersicht

ANLEITUNG = (
    "Werkzeuge für die Jobsuche des Nutzers: Stand, Fakten, Firmen, Stellen suchen, Mailentwürfe lesen und vorschlagen. "
    "Es gibt bewusst kein Werkzeug zum Senden oder Freigeben: Das macht der Mensch in der App. "
    "Mailentwürfe nur mit belegten Fakten; Befunde der Stufe 'sperre' müssen vor dem Versand in der App gelöst werden."
)
server = MCPServer("jobagent", title="Job Agent", instructions=ANLEITUNG, version="1")
_angemeldet = False


def _anmelden() -> None:
    """Einmal je Prozess: Mandant wählen wie die Desktop-App (Konto-Passwort aus dem Schlüsselbund)."""
    global _angemeldet
    if _angemeldet:
        return
    if db.server():
        from . import anmeldung
        email = os.environ.get("JOBAGENT_EMAIL", "") or _einzige_konto_adresse()
        if not email:
            raise RuntimeError("Kein Konto gefunden: JOBAGENT_EMAIL setzen oder einmal in der Desktop-App anmelden.")
        import keyring
        passwort = keyring.get_password("jobsuche", anmeldung.konto_eintrag(email))
        if not passwort:
            raise RuntimeError("Für das Konto liegt kein Passwort im Schlüsselbund. Einmal in der Desktop-App anmelden.")
        anmeldung.oeffne_mandant(anmeldung.pruefe_konto(email, passwort))
    _angemeldet = True


def _einzige_konto_adresse() -> str:
    """Genau ein gespeichertes Konto im Schlüsselbund → dessen Adresse. Mehrere → keine Vermutung."""
    try:
        import secretstorage
        sammlung = secretstorage.get_default_collection(secretstorage.dbus_init())
        adressen = [a["username"][len("konto:"):] for a in (i.get_attributes() for i in sammlung.get_all_items())
                    if a.get("service") == "jobsuche" and a.get("username", "").startswith("konto:")]
    except Exception:
        return ""
    return adressen[0] if len(adressen) == 1 else ""


@contextmanager
def _conn():
    # stdout gehört dem MCP-Protokoll: Ausgaben aus dem Kern gehen nach stderr.
    with redirect_stdout(sys.stderr):
        _anmelden()
        conn = db.verbinden()
        try:
            yield conn
        finally:
            conn.close()


def _modell(conn) -> str:
    return db.einstellung(conn, "modell", config.STANDARD_MODELL)


# --- Lesen -------------------------------------------------------------------------------------------------------------

@server.tool(description="Überblick: Zahl der Firmen, Fakten und Antworten sowie das eingestellte KI-Modell.")
def stand() -> dict:
    with _conn() as conn:
        return {"stand": uebersicht.stand(conn)["pill"], "modell": _modell(conn)}


@server.tool(description="Fakten aus Lebenslauf und Zeugnissen. Nur bestätigte Fakten dürfen in Bewerbungen stehen.")
def fakten(kategorie: str = "", nur_bestaetigt: bool = True, anzahl: int = 50) -> list[dict]:
    sql = "SELECT id, kategorie, text, status FROM fakten WHERE 1 = 1"
    werte: list = []
    if nur_bestaetigt:
        sql += " AND status = 'bestaetigt'"
    if kategorie:
        sql += " AND kategorie = ?"
        werte.append(kategorie)
    sql += " ORDER BY kategorie, id LIMIT ?"
    werte.append(max(1, min(anzahl, 200)))
    with _conn() as conn:
        return [dict(z) for z in conn.execute(sql, werte).fetchall()]


@server.tool(description="Gespeicherte Firmen, die besten Bewertungen zuerst. suchtext filtert nach Name oder Sitz.")
def firmen(suchtext: str = "", anzahl: int = 20) -> list[dict]:
    with _conn() as conn:
        zeilen = conn.execute(
            "SELECT id, name, sitz, website, ki_score, passung FROM firmen "
            "WHERE (? = '' OR lower(name) LIKE ? OR lower(sitz) LIKE ?) "
            "ORDER BY ki_score IS NULL, ki_score DESC, id DESC LIMIT ?",
            (suchtext, f"%{suchtext.lower()}%", f"%{suchtext.lower()}%", max(1, min(anzahl, 100)))).fetchall()
        return [dict(z) for z in zeilen]


@server.tool(description="Mailentwürfe mit Firma, Betreff und Status (entwurf, freigegeben, gesendet, antwort, absage …).")
def entwuerfe(status: str = "", anzahl: int = 20) -> list[dict]:
    with _conn() as conn:
        zeilen = conn.execute(
            "SELECT e.id, f.name AS firma, e.betreff, e.status FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id "
            "WHERE (? = '' OR e.status = ?) ORDER BY e.id DESC LIMIT ?",
            (status, status, max(1, min(anzahl, 100)))).fetchall()
        return [dict(z) for z in zeilen]


@server.tool(description="Ein Entwurf mit Empfänger, Text und den Prüfbefunden (hinweis = prüfen, sperre = blockiert den Versand).")
def entwurf_lesen(entwurf_id: int) -> dict:
    with _conn() as conn:
        z = conn.execute("SELECT e.id, f.name AS firma, e.empfaenger, e.betreff, e.text, e.status FROM entwuerfe e "
                         "JOIN firmen f ON f.id = e.firma_id WHERE e.id = ?", (entwurf_id,)).fetchone()
        if not z:
            raise ValueError("Entwurf unbekannt.")
        befunde = [{"stufe": b.stufe, "text": b.text} for b in ki.pruefe_entwurf(conn, entwurf_id)]
        return {**dict(z), "befunde": befunde}


# --- Suchen und Vorschlagen ----------------------------------------------------------------------------------------------

SCHNELLE_QUELLEN = ("jobboerse", "arbeitnow")


@server.tool(description=(
    "Sucht Stellen und Firmen. Standard sind die schnellen Quellen Jobbörse und Arbeitnow; weitere: verzeichnis, internet, "
    "postfach, portale (langsamer). Liefert die besten Treffer nach Nähe zu den Fakten und die Zeiten je Schritt. "
    "Speichert nichts, übernimmt keine Firma."))
def stellen_suchen(begriffe: str, ort: str = "", umkreis_km: int = 50, quellen: list[str] | None = None,
                   nur_ki: bool = False, anzahl: int = 15) -> dict:
    erlaubt = {"verzeichnis", "jobboerse", "arbeitnow", "internet", "postfach", "portale"}
    gewaehlt = tuple(q for q in (quellen or SCHNELLE_QUELLEN) if q in erlaubt) or SCHNELLE_QUELLEN
    with _conn() as conn:
        auftrag = suche.gespeicherter_auftrag(conn)
        auftrag.begriffe, auftrag.ort, auftrag.umkreis = begriffe, ort or auftrag.ort, max(0, min(umkreis_km, 200))
        auftrag.quellen, auftrag.nur_ki = gewaehlt, nur_ki
        ergebnis = suche.suche_ueberall(conn, auftrag)
    felder = ("firma", "titel", "ort", "entfernung", "quelle", "naehe", "remote", "ki", "url", "datum")
    return {"treffer": [{f: t.get(f) for f in felder if t.get(f) not in (None, "", [])}
                        for t in ergebnis["treffer"][:max(1, min(anzahl, 50))]],
            "anzahl_gesamt": len(ergebnis["treffer"]), "hinweise": ergebnis["hinweise"],
            "zeiten": ergebnis.get("zeiten_text", "")}


@server.tool(description=(
    "Schreibt für eine gespeicherte Firma einen Mailentwurf mit dem eingestellten KI-Modell. Der Entwurf landet in der App "
    "unter Entwürfe und wird NICHT gesendet; Freigabe und Versand macht der Mensch."))
def entwurf_vorschlagen(firma_id: int) -> dict:
    with _conn() as conn:
        if not conn.execute("SELECT 1 FROM firmen WHERE id = ?", (firma_id,)).fetchone():
            raise ValueError("Firma unbekannt.")
        land, searxng = db.einstellung(conn, "suche.land", "de"), db.einstellung(conn, "suche.searxng", "")
        eid = suche.schlage_mail_vor(conn, firma_id, _modell(conn), land, searxng, lambda _: None)
    return {"entwurf_id": eid, **entwurf_lesen(eid)} if eid else {"entwurf_id": None,
                                                                   "hinweis": "Kein Entwurf: Es fehlt ein Empfänger oder die Prüfung sperrt."}


def main() -> int:
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
