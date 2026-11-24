"""Job Agent – Web-Oberfläche (FastAPI) für mehrere Mandanten. Nutzt den Kern der Desktop-App; hier liegen nur die
HTTP-Schicht, Anmeldung/Sitzungen, Schutzmaßnahmen und die Hintergrundaufträge.

Öffentlich erreichbar über einen Cloudflare-Tunnel, im LAN über nginx. Regeln:
- Konten nur per Einladungscode, argon2id, Sperre nach Fehlversuchen, Rate-Limits je IP/Konto/Mandant.
- Sitzungs-Cookie Secure + HttpOnly + SameSite=Strict (Präfix __Host-), dazu CSRF-Token im Kopf jeder Änderung.
- Jede Anfrage arbeitet in genau einem Mandanten: Verbindung mit SET ROLE auf dessen Datenbankrolle.
- Claude (Abo des Betreibers über die KI-Brücke) nur für freigegebene Mandanten, sonst Ollama.
- Mail nur über das eigene SMTP-Konto des Mandanten, Passwort AES-GCM-verschlüsselt beim Mandanten.
- Keine Stacktraces nach außen.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import (__version__, angaben, config, db, firmen, geheim, ki, lernen, llm, mailkonten, mandanten, nacht, person,
                profil, quellen, rag, regeln, suche, uebersicht, versand)
from .auftraege import MELDBAR, Auftraege, ZuViele
from .sicherheit import KOPFZEILEN, Begrenzer

log = logging.getLogger("jobagent.web")
STATISCH = Path(__file__).parent / "static"
SITZUNG_DAUER = timedelta(hours=12)
STATUS_TEXT = {"entwurf": "Entwurf", "freigegeben": "Freigegeben", "gesendet": "Gesendet", "antwort": "Antwort erhalten",
               "absage": "Absage", "keine_antwort": "Keine Antwort", "verworfen": "Verworfen"}
UPLOAD_ENDUNGEN = {".pdf", ".docx", ".txt", ".md"}
MAX_UPLOAD = 10 * 1024 * 1024


# --- Eingaben -----------------------------------------------------------------------------------------------------------
class AnmeldenIn(BaseModel):
    email: str = Field(max_length=254)
    passwort: str = Field(max_length=256)


class RegistrierenIn(AnmeldenIn):
    code: str = Field(max_length=100)
    name: str = Field("", max_length=120)


class PasswortIn(BaseModel):
    alt: str = Field(max_length=256)
    neu: str = Field(max_length=256)


class MandantIn(BaseModel):
    id: int


class KiIn(BaseModel):
    modell: str = Field(max_length=100)


class SucheIn(BaseModel):
    modus: str = "suchen"  # suchen | passende
    begriffe: str = Field("", max_length=500)
    ort: str = Field("", max_length=120)
    land: str = Field("de", max_length=4)
    umkreis: int = Field(50, ge=0, le=500)
    bundesland: str = Field("", max_length=60)
    branche: str = Field("", max_length=40)
    arbeitszeit: str = Field("", max_length=20)
    quellen: list[str] = []
    nur_ki: bool = False
    max_tage: int = Field(suche.MAX_TAGE, ge=0, le=100)
    max_treffer: int = Field(suche.MAX_TREFFER, ge=0, le=2000)
    aufgefrischte: bool = True
    remote: str = Field("", max_length=20)
    stichwoerter: str = Field("", max_length=500)
    ausschluss: str = Field("", max_length=500)


class FirmaIn(BaseModel):
    name: str = Field(max_length=200)
    website: str = Field("", max_length=300)
    sitz: str = Field("", max_length=200)
    personen: str = Field("", max_length=200)
    email: str = Field("", max_length=254)
    warum: str = Field("", max_length=2000)


class BewertungIn(BaseModel):
    score: int = Field(ge=0, le=100)
    notiz: str = Field("", max_length=2000)


class EntwurfIn(BaseModel):
    empfaenger: str = Field(max_length=254)
    betreff: str = Field(max_length=300)
    text: str = Field(max_length=20000)


class StatusIn(BaseModel):
    status: str


class FaktIn(BaseModel):
    kategorie: str = Field("praktisch", max_length=30)
    text: str = Field(max_length=2000)


class TextIn(BaseModel):
    text: str = Field(max_length=5000)


class ProfilIn(BaseModel):
    name: str = Field(max_length=120)
    email: str = Field(max_length=254)
    telefon: str = Field("", max_length=60)
    ort: str = Field("", max_length=120)
    links: str = Field("", max_length=500)
    vorlage: str = Field("", max_length=20000)
    signatur: str = Field("", max_length=2000)
    woertlich: bool = False
    ohne_alter: bool = True


class MailkontoIn(BaseModel):
    email: str = Field(max_length=254)
    host: str = Field(max_length=253)
    port: int = Field(587, ge=1, le=65535)
    sicherheit: str = "starttls"
    benutzer: str = Field("", max_length=254)
    passwort: str | None = Field(None, max_length=256)


class NachtIn(BaseModel):
    an: bool
    uhrzeit: str = Field("01:00", max_length=5)


class EinladungIn(BaseModel):
    notiz: str = Field("", max_length=200)
    mandant_id: int | None = None


class SchalterIn(BaseModel):
    an: bool


class UnterlageIn(BaseModel):
    anhang: bool


# --- Hilfen -------------------------------------------------------------------------------------------------------------
def _einstellungen_aus_umgebung() -> dict:
    """PostgreSQL-Zugang des Webdienstes; das Passwort steht nur in der Umgebung (Datei mit 600)."""
    return {"art": "postgres", "host": os.environ.get("JOBAGENT_PG_HOST", "192.168.178.214"),
            "port": int(os.environ.get("JOBAGENT_PG_PORT", "5432")), "datenbank": os.environ.get("JOBAGENT_PG_DB", "jobsuche"),
            "benutzer": os.environ.get("JOBAGENT_PG_USER", mandanten.WEB_ROLLE),
            "passwort": os.environ.get("JOBAGENT_PG_PASSWORT", ""), "sslmode": os.environ.get("JOBAGENT_PG_SSLMODE", "require")}


def _ip(request: Request) -> str:
    # uvicorn übernimmt X-Forwarded-For nur von 127.0.0.1 (nginx); nginx setzt bei Tunnel-Verkehr CF-Connecting-IP ein.
    return request.client.host if request.client else ""


def _ausgabe(wert):
    if isinstance(wert, (str, int, float, bool)) or wert is None:
        return wert
    if isinstance(wert, dict):
        return {str(k): _ausgabe(v) for k, v in wert.items()}
    if isinstance(wert, (list, tuple, set)):
        return [_ausgabe(v) for v in wert]
    if isinstance(wert, Path):
        return wert.name
    if hasattr(wert, "isoformat"):
        return wert.isoformat()
    return str(wert)


def _zeile(z) -> dict:
    return {k: _ausgabe(z[k]) for k in z.keys()}


def _km(t: dict) -> str:
    if t.get("entfernung") is None:
        return ""
    return f"≈{t['entfernung']}" if t.get("luftlinie", True) else str(t["entfernung"])


def _treffer_zeile(i: int, t: dict) -> dict:
    return {"i": i, "naehe": t.get("naehe"), "firma": t.get("firma", ""), "titel": t.get("titel", ""),
            "ort": t.get("ort", ""), "remote": t.get("remote", ""), "km": _km(t), "quelle": t.get("quelle", ""),
            "status": "in Liste" if t.get("bekannt") else "Konzern" if t.get("konzern") else "",
            "ki": ", ".join(t.get("ki") or []), "alter": suche.alter_tage(t.get("datum", "")),
            "ampel": suche.ampel(t), "alter_text": suche.alter_text(t)}


SORTIER = {"naehe", "firma", "titel", "ort", "km", "quelle", "status", "ki", "alter"}


def _sortiere(zeilen: list[dict], spalte: str, ab: bool) -> list[dict]:
    def schluessel(z):
        w = z[spalte]
        if spalte == "km":
            ziffern = "".join(ch for ch in str(w) if ch.isdigit())
            return (0, int(ziffern)) if ziffern else (1, 0)
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            return (0, w)
        if w in ("", None):
            return (1, 0)
        return (0, str(w).lower())
    mit = [z for z in zeilen if schluessel(z)[0] == 0]
    ohne = [z for z in zeilen if schluessel(z)[0] == 1]
    try:
        mit.sort(key=lambda z: schluessel(z)[1], reverse=ab)
    except TypeError:
        mit.sort(key=lambda z: str(schluessel(z)[1]), reverse=ab)
    return mit + ohne


def _sicherer_name(name: str) -> str:
    stamm = re.sub(r"[^\w.\- ]+", "_", Path(name or "").name).strip(" .")[:100]
    return stamm or "datei"


# --- App ----------------------------------------------------------------------------------------------------------------
def erstelle_app(server: dict | None = None, schluessel: bytes | None = None, daten: Path | None = None,
                 cookie_secure: bool = True, hosts: list[str] | None = None, nachtlauf: bool = False) -> FastAPI:
    server = server or _einstellungen_aus_umgebung()
    if schluessel is None:
        text = os.environ.get("JOBAGENT_SCHLUESSEL", "")
        if not text:
            raise SystemExit("JOBAGENT_SCHLUESSEL fehlt (32 Byte Base64, aus der Geheimnis-Datei des Dienstes).")
        schluessel = geheim.schluessel_aus_text(text)
    ablage = geheim.Verschluesselt(schluessel)
    daten = Path(daten or os.environ.get("JOBAGENT_DATEN", "/var/lib/jobagent-web"))
    hosts = hosts or [h.strip() for h in os.environ.get("JOBAGENT_HOSTS", "localhost").split(",") if h.strip()]
    db._server = server  # gilt für den ganzen Prozess: Der Webdienst kennt nur PostgreSQL
    config.DB_PFAD = daten / "kein-sqlite.db"
    config.MODELLE = daten / "modelle"
    cookie_name = "__Host-jobagent" if cookie_secure else "jobagent"

    app = FastAPI(title="Job Agent", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*hosts, "127.0.0.1", "localhost", "testserver"])
    auftraege = Auftraege()
    treffer: dict[tuple[int, int], list[dict]] = {}  # (Mandant, Konto) -> letzte Suchtreffer
    begrenzer_anmelden = Begrenzer(10, 900)
    begrenzer_konto = Begrenzer(20, 3600)
    begrenzer_registrieren = Begrenzer(5, 3600)
    begrenzer_teuer = Begrenzer(60, 3600)
    begrenzer_allgemein = Begrenzer(600, 60)
    lokal = threading.local()

    # Zentrale Verbindung je Thread, als Anmelde-Benutzer des Webdienstes (ohne Rechte an Mandantendaten)
    def zentral() -> psycopg.Connection:
        roh = getattr(lokal, "roh", None)
        if roh is None or roh.closed or roh.broken:
            lokal.roh = roh = pg_roh()
        return roh

    def pg_roh():
        from .. import pg
        return pg.roh_verbinden(server)

    def zentral_ausfuehren(funktion):
        try:
            return funktion(zentral())
        except psycopg.OperationalError:
            lokal.roh = None  # Verbindung weg: einmal neu
            return funktion(zentral())

    # --- Middleware: Kopfzeilen, CSRF-Grundschutz, allgemeines Limit -----------------------------------------------
    @app.middleware("http")
    async def schutz(request: Request, call_next):
        if not begrenzer_allgemein.erlaubt(_ip(request)):
            return JSONResponse({"detail": "Zu viele Anfragen."}, status_code=429)
        if request.method not in ("GET", "HEAD") and request.url.path.startswith("/api/"):
            if request.headers.get("x-jobagent") != "1":
                return JSONResponse({"detail": "Anfrage ohne Kennung abgelehnt."}, status_code=403)
            herkunft = request.headers.get("origin")
            if herkunft and herkunft.split("://", 1)[-1].split("/")[0].split(":")[0] not in (*hosts, "127.0.0.1", "localhost", "testserver"):
                return JSONResponse({"detail": "Fremde Herkunft abgelehnt."}, status_code=403)
        antwort = await call_next(request)
        for k, v in KOPFZEILEN.items():
            antwort.headers[k] = v
        if request.url.path.startswith("/api/"):
            antwort.headers["Cache-Control"] = "no-store"
        return antwort

    @app.exception_handler(Exception)
    async def unerwartet(request: Request, fehler: Exception):
        log.exception("Fehler bei %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Interner Fehler."}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def ungueltig(request: Request, fehler: RequestValidationError):
        return JSONResponse({"detail": "Ungültige Eingabe."}, status_code=422)

    # --- Sitzung -------------------------------------------------------------------------------------------------------
    class Kontext:
        def __init__(self, token, konto, mandant_info, csrf):
            self.token, self.konto, self.info, self.csrf = token, konto, mandant_info, csrf
            self.mandant = mandanten.als_mandant(mandant_info, daten / "mandanten" / str(mandant_info.id))

        def setze(self):
            geheim.setze_ablage(ablage)
            llm.claude_erlaubt.set(bool(self.info.claude_erlaubt))

    def kontext(request: Request, aendernd: bool | None = None) -> Kontext:
        token = request.cookies.get(cookie_name, "")
        s = zentral_ausfuehren(lambda r: mandanten.sitzung(r, token, SITZUNG_DAUER))
        if not s:
            raise HTTPException(401, "Bitte anmelden.")
        aendernd = request.method not in ("GET", "HEAD") if aendernd is None else aendernd
        if aendernd and not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), s["csrf"].encode()):
            raise HTTPException(403, "Sitzungsschutz (CSRF) passt nicht. Seite neu laden.")
        try:
            info = zentral_ausfuehren(lambda r: mandanten.mandant_fuer(r, s["konto"].id, s["mandant_id"]))
        except mandanten.KontoFehler as fehler:
            raise HTTPException(403, str(fehler))
        return Kontext(token, s["konto"], info, s["csrf"])

    def admin(request: Request) -> Kontext:
        k = kontext(request)
        if not k.konto.admin:
            raise HTTPException(403, "Nur für Administratoren.")
        return k

    def mit_conn(k: Kontext, funktion):
        """Arbeitet synchron im Mandanten: SET ROLE auf seine Rolle, Ablage und KI-Freigabe gesetzt."""
        with db.mit_mandant(k.mandant):
            t1 = geheim.setze_ablage(ablage)
            t2 = llm.claude_erlaubt.set(bool(k.info.claude_erlaubt))
            conn = db.verbinden()
            try:
                return funktion(conn)
            except MELDBAR as fehler:
                raise HTTPException(400, str(fehler))
            finally:
                conn.close()
                llm.claude_erlaubt.reset(t2)
                geheim._ablage.reset(t1)

    def starte(k: Kontext, titel: str, arbeit, teuer: bool = True) -> dict:
        if teuer and not begrenzer_teuer.erlaubt(f"m{k.info.id}"):
            raise HTTPException(429, "Zu viele aufwendige Aufträge in der letzten Stunde.")
        try:
            a = auftraege.starte(k.mandant, titel, arbeit, k.setze)
        except ZuViele as fehler:
            raise HTTPException(429, str(fehler))
        return {"auftrag": a.id, "titel": titel}

    def modell(conn, k: Kontext) -> str:
        m = db.einstellung(conn, "modell", config.STANDARD_MODELL)
        return m if (not llm.ist_claude(m) or k.info.claude_erlaubt) else config.STANDARD_MODELL

    def cookie_setzen(antwort: Response, token: str):
        antwort.set_cookie(cookie_name, token, max_age=int(SITZUNG_DAUER.total_seconds()), path="/",
                           secure=cookie_secure, httponly=True, samesite="strict")

    # --- Öffentlich ----------------------------------------------------------------------------------------------------
    @app.get("/api/gesundheit")
    def gesundheit():
        return {"ok": True, "version": __version__}

    @app.post("/api/anmelden")
    def anmelden(daten_in: AnmeldenIn, request: Request, antwort: Response):
        ip = _ip(request)
        if not begrenzer_anmelden.erlaubt(ip) or not begrenzer_konto.erlaubt(daten_in.email.strip().lower()):
            raise HTTPException(429, "Zu viele Anmeldeversuche. Bitte später erneut.")

        def arbeit(r):
            konto = mandanten.anmelden(r, daten_in.email, daten_in.passwort, ip)
            info = mandanten.mandant_fuer(r, konto.id, None)
            return mandanten.sitzung_anlegen(r, konto.id, info.id, SITZUNG_DAUER, ip)
        try:
            token, csrf = zentral_ausfuehren(arbeit)
        except mandanten.KontoFehler as fehler:
            raise HTTPException(401, str(fehler))
        cookie_setzen(antwort, token)
        return {"ok": True, "csrf": csrf}

    @app.post("/api/registrieren")
    def registrieren(daten_in: RegistrierenIn, request: Request, antwort: Response):
        ip = _ip(request)
        if not begrenzer_registrieren.erlaubt(ip):
            raise HTTPException(429, "Zu viele Versuche. Bitte später erneut.")

        def arbeit(r):
            konto = mandanten.registrieren(r, daten_in.code, daten_in.email, daten_in.passwort, daten_in.name, ip)
            info = mandanten.mandant_fuer(r, konto.id, None)
            return mandanten.sitzung_anlegen(r, konto.id, info.id, SITZUNG_DAUER, ip)
        try:
            token, csrf = zentral_ausfuehren(arbeit)
        except mandanten.KontoFehler as fehler:
            raise HTTPException(400, str(fehler))
        cookie_setzen(antwort, token)
        return {"ok": True, "csrf": csrf}

    @app.post("/api/abmelden")
    def abmelden(request: Request, antwort: Response):
        k = kontext(request)
        zentral_ausfuehren(lambda r: mandanten.sitzung_beenden(r, k.token))
        antwort.delete_cookie(cookie_name, path="/", secure=cookie_secure, httponly=True, samesite="strict")
        return {"ok": True}

    # --- Konto ---------------------------------------------------------------------------------------------------------
    @app.get("/api/ich")
    def ich(request: Request):
        k = kontext(request)
        alle = zentral_ausfuehren(lambda r: mandanten.mandanten_von(r, k.konto.id))

        def lies(conn):
            return {"name": person.name(conn), "modell": db.einstellung(conn, "modell", config.STANDARD_MODELL),
                    "stand": uebersicht.stand(conn)["pill"], "demo": False}
        p = mit_conn(k, lies)
        hat_avatar = zentral_ausfuehren(lambda r: r.execute(
            f"SELECT avatar IS NOT NULL FROM {mandanten.ZENTRAL}.konten WHERE id = %s", (k.konto.id,)).fetchone()[0])
        return {"konto": {"id": k.konto.id, "email": k.konto.email, "name": k.konto.name, "admin": k.konto.admin,
                          "avatar": bool(hat_avatar)},
                "mandant": {"id": k.info.id, "name": k.info.name, "claude_erlaubt": k.info.claude_erlaubt},
                "mandanten": [{"id": m.id, "name": m.name, "rolle": m.mitgliedsrolle} for m in alle],
                "csrf": k.csrf, "version": __version__, "person": p["name"], "modell": p["modell"],
                "modell_wirksam": p["modell"] if (not llm.ist_claude(p["modell"]) or k.info.claude_erlaubt) else config.STANDARD_MODELL,
                "stand": p["stand"], "laufend": [{"id": a.id, "titel": a.titel} for a in auftraege.laufende(k.info.id)]}

    @app.post("/api/mandant")
    def mandant_wechseln(daten_in: MandantIn, request: Request):
        k = kontext(request)

        def arbeit(r):
            info = mandanten.mandant_fuer(r, k.konto.id, daten_in.id)
            mandanten.sitzung_mandant(r, k.token, info.id)
            return info
        try:
            info = zentral_ausfuehren(arbeit)
        except mandanten.KontoFehler as fehler:
            raise HTTPException(403, str(fehler))
        return {"ok": True, "mandant": info.name}

    @app.get("/api/konto/avatar")
    def avatar_holen(request: Request):
        k = kontext(request)
        bild = zentral_ausfuehren(lambda r: mandanten.avatar_lesen(r, k.konto.id))
        if not bild:
            raise HTTPException(404, "Kein Avatar.")
        return Response(bild[0], media_type=bild[1],
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": "inline"})

    @app.post("/api/konto/avatar")
    async def avatar_hochladen(request: Request, datei: UploadFile = File(...)):
        k = kontext(request)
        if not begrenzer_konto.erlaubt(f"av{k.konto.id}"):
            raise HTTPException(429, "Zu viele Versuche.")
        inhalt = await datei.read(mandanten.AVATAR_MAX + 1)
        try:
            zentral_ausfuehren(lambda r: mandanten.avatar_setzen(r, k.konto.id, inhalt))
        except mandanten.KontoFehler as fehler:
            raise HTTPException(400, str(fehler))
        return {"ok": True}

    @app.delete("/api/konto/avatar")
    def avatar_entfernen(request: Request):
        k = kontext(request)
        zentral_ausfuehren(lambda r: mandanten.avatar_setzen(r, k.konto.id, None))
        return {"ok": True}

    @app.post("/api/konto/passwort")
    def passwort_aendern(daten_in: PasswortIn, request: Request):
        k = kontext(request)
        if not begrenzer_konto.erlaubt(f"pw{k.konto.id}"):
            raise HTTPException(429, "Zu viele Versuche.")
        try:
            zentral_ausfuehren(lambda r: mandanten.aendere_passwort(r, k.konto.id, daten_in.alt, daten_in.neu))
        except mandanten.KontoFehler as fehler:
            raise HTTPException(400, str(fehler))
        zentral_ausfuehren(lambda r: mandanten.andere_sitzungen_beenden(r, k.konto.id, k.token))
        return {"ok": True}

    @app.get("/api/konto/ki")
    def ki_lesen(request: Request):
        k = kontext(request)
        try:
            lokal_modelle = llm.modelle()
            ollama = ""
        except llm.LLMFehler as fehler:
            lokal_modelle, ollama = [], str(fehler)
        modelle = lokal_modelle + (llm.CLAUDE_MODELLE if k.info.claude_erlaubt else [])
        aktuell = mit_conn(k, lambda conn: db.einstellung(conn, "modell", config.STANDARD_MODELL))
        return {"modell": aktuell, "modelle": modelle, "claude_erlaubt": k.info.claude_erlaubt, "ollama_problem": ollama}

    @app.post("/api/konto/ki")
    def ki_setzen(daten_in: KiIn, request: Request):
        k = kontext(request)
        if llm.ist_claude(daten_in.modell) and not k.info.claude_erlaubt:
            raise HTTPException(403, "Claude ist für diesen Mandanten nicht freigegeben.")
        if llm.ist_claude(daten_in.modell) and daten_in.modell not in llm.CLAUDE_MODELLE:
            raise HTTPException(400, "Unbekanntes Modell.")
        if not re.fullmatch(r"[\w.:\-/]{1,100}", daten_in.modell):
            raise HTTPException(400, "Unbekanntes Modell.")
        mit_conn(k, lambda conn: db.setze_einstellung(conn, "modell", daten_in.modell))
        return {"ok": True}

    # --- Aufträge ------------------------------------------------------------------------------------------------------
    @app.get("/api/auftraege/{auftrag_id}")
    def auftrag(auftrag_id: str, request: Request):
        k = kontext(request)
        a = auftraege.hole(auftrag_id, k.info.id)
        if not a:
            raise HTTPException(404, "Auftrag unbekannt.")
        return {"status": a.status, "titel": a.titel, "fortschritt": a.fortschritt, "fehler": a.fehler,
                "ergebnis": _ausgabe(a.ergebnis) if a.status == "fertig" else None}

    # --- Start ---------------------------------------------------------------------------------------------------------
    @app.get("/api/start")
    def start(request: Request):
        k = kontext(request)
        return mit_conn(k, lambda conn: _ausgabe(uebersicht.stand(conn)))

    # --- Suche ---------------------------------------------------------------------------------------------------------
    @app.get("/api/suche")
    def suche_lesen(request: Request):
        k = kontext(request)

        def lies(conn):
            e = lambda n, v="": db.einstellung(conn, f"suche.{n}", v)
            return {"begriffe": e("begriffe") or e("abgeleitet"), "ort": e("ort") or suche.suchort(conn), "land": e("land", "de"),
                    "umkreis": int(e("umkreis", "50") or 50), "bundesland": e("bundesland"), "branche": e("branche"),
                    "arbeitszeit": e("arbeitszeit"), "quellen": [q for q in e("quellen", ",".join(suche.Auftrag.quellen)).split(",") if q],
                    "nur_ki": e("nur_ki") == "1", "max_tage": int(e("max_tage", str(suche.MAX_TAGE)) or 0),
                    "max_treffer": int(e("max_treffer", str(suche.MAX_TREFFER)) or 0), "aufgefrischte": e("aufgefrischte", "1") == "1",
                    "remote": e("remote"), "stichwoerter": e("stichwoerter"), "abgeleitet_stichwoerter": e("abgeleitet_stichwoerter"),
                    "ausschluss": e("ausschluss")}
        werte = mit_conn(k, lies)
        return {"werte": werte, "laender": quellen.LAENDER, "bundeslaender": list(quellen.BUNDESLAENDER),
                "branchen": {c: n for c, (n, _) in quellen.BRANCHEN.items()}, "arbeitszeit": suche.ARBEITSZEIT,
                "remote": suche.REMOTE, "anzahl": len(treffer.get((k.info.id, k.konto.id), []))}

    @app.post("/api/suche")
    def suchen(daten_in: SucheIn, request: Request):
        k = kontext(request)
        erlaubt = {"verzeichnis", "jobboerse", "arbeitnow", "internet", "postfach", "portale", "adzuna", "jooble"}
        gewaehlt = tuple(q for q in daten_in.quellen if q in erlaubt)
        if not gewaehlt:
            raise HTTPException(400, "Wähle mindestens eine Quelle.")
        if daten_in.modus not in ("suchen", "passende"):
            raise HTTPException(400, "Unbekannter Modus.")

        def speichern(conn):
            searxng = db.einstellung(conn, "suche.searxng", "")
            woerter = dict.fromkeys(w.strip() for w in daten_in.ausschluss.split(",") if w.strip())
            db.setze_einstellung(conn, "suche.ausschluss", ", ".join(woerter))
            db.setze_einstellung(conn, "suche.stichwoerter", ", ".join(suche.bereinige_stichwoerter(daten_in.stichwoerter.split(","))))
            ort = daten_in.bundesland if daten_in.bundesland in quellen.BUNDESLAENDER else daten_in.ort.strip()
            umkreis = 0 if daten_in.bundesland in quellen.BUNDESLAENDER else daten_in.umkreis
            auftrag = suche.Auftrag(daten_in.begriffe, ort, daten_in.land if daten_in.land in quellen.LAENDER else "de", umkreis,
                                    daten_in.branche if daten_in.branche in quellen.BRANCHEN else "", gewaehlt, searxng,
                                    daten_in.arbeitszeit if daten_in.arbeitszeit in suche.ARBEITSZEIT else "", daten_in.nur_ki,
                                    daten_in.max_tage, daten_in.aufgefrischte, daten_in.remote if daten_in.remote in suche.REMOTE else "")
            for name, wert in (("begriffe", auftrag.begriffe), ("ort", auftrag.ort), ("land", auftrag.land),
                               ("umkreis", str(auftrag.umkreis)), ("branche", auftrag.branche), ("quellen", ",".join(gewaehlt)),
                               ("arbeitszeit", auftrag.arbeitszeit), ("bundesland", daten_in.bundesland if daten_in.bundesland in quellen.BUNDESLAENDER else ""),
                               ("nur_ki", "1" if auftrag.nur_ki else "0"), ("max_tage", str(auftrag.max_tage)),
                               ("aufgefrischte", "1" if auftrag.aufgefrischte else "0"), ("remote", auftrag.remote),
                               ("max_treffer", str(daten_in.max_treffer))):
                db.setze_einstellung(conn, f"suche.{name}", wert)
            if daten_in.modus == "suchen" and not auftrag.begriffe.strip():
                abgeleitet = db.einstellung(conn, "suche.abgeleitet", "")
                if abgeleitet:
                    auftrag = suche.Auftrag(**{**auftrag.__dict__, "begriffe": abgeleitet})
            return auftrag, modell(conn, k)
        auftrag, m = mit_conn(k, speichern)
        schluessel = (k.info.id, k.konto.id)

        def arbeit(conn, melde):
            if daten_in.modus == "passende" or not auftrag.begriffe.strip():
                ergebnis = suche.finde_passende(conn, m, auftrag, melde)
            else:
                ergebnis = suche.suche_ueberall(conn, auftrag, melde)
            treffer[schluessel] = list(ergebnis["treffer"])
            return {"anzahl": len(ergebnis["treffer"]), "hinweise": ergebnis.get("hinweise", []),
                    "begriffe": ergebnis.get("begriffe"), "ort": ergebnis.get("ort")}
        return starte(k, "Firmen suchen", arbeit)

    @app.get("/api/suche/treffer")
    def treffer_seite(request: Request, seite: int = 0, pro_seite: int = 50, sortierung: str = "", ab: bool = False):
        k = kontext(request)
        liste = treffer.get((k.info.id, k.konto.id), [])
        pro_seite = pro_seite if pro_seite in (25, 50, 100) else 50
        bekannt = mit_conn(k, suche.bekannte_firmen)
        for t in liste:
            t["bekannt"] = regeln.kurzname(t["firma"]).lower() in bekannt
        zeilen = [_treffer_zeile(i, t) for i, t in enumerate(liste)]
        if sortierung in SORTIER:
            zeilen = _sortiere(zeilen, sortierung, ab)
        seiten = max(1, -(-len(zeilen) // pro_seite))
        seite = min(max(0, seite), seiten - 1)
        return {"gesamt": len(zeilen), "seite": seite, "seiten": seiten, "pro_seite": pro_seite,
                "zeilen": zeilen[seite * pro_seite:(seite + 1) * pro_seite]}

    def _ein_treffer(k: Kontext, i: int) -> dict:
        liste = treffer.get((k.info.id, k.konto.id), [])
        if not 0 <= i < len(liste):
            raise HTTPException(404, "Treffer unbekannt. Bitte neu suchen.")
        return liste[i]

    @app.get("/api/suche/treffer/{i}")
    def treffer_detail(i: int, request: Request):
        k = kontext(request)
        t = _ein_treffer(k, i)
        links = []
        if str(t.get("url", "")).startswith(("http://", "https://")):
            links.append({"url": t["url"], "text": "Anzeige/Fundstelle öffnen"})
        if t.get("start") and isinstance(t.get("lat"), (int, float)) and isinstance(t.get("lon"), (int, float)):
            links.append({"url": suche.routen_link(t["start"], (t["lat"], t["lon"])), "text": "Route anzeigen"})
        if str(t.get("website", "")).startswith(("http://", "https://")):
            links.append({"url": t["website"], "text": "Website der Firma"})
        return {**_treffer_zeile(i, t), "links": links, "vorschau": _ausgabe(t.get("vorschau"))}

    @app.post("/api/suche/treffer/{i}/vorschau")
    def treffer_vorschau(i: int, request: Request):
        k = kontext(request)
        t = _ein_treffer(k, i)

        def arbeit(conn, melde):
            land, searxng = db.einstellung(conn, "suche.land", "de"), db.einstellung(conn, "suche.searxng", "")
            t["vorschau"] = suche.vorschau(t, land, searxng)
            return t["vorschau"]
        return starte(k, "Firma ansehen", arbeit, teuer=False)

    @app.post("/api/suche/treffer/{i}/{aktion}")
    def treffer_aktion(i: int, aktion: str, request: Request):
        k = kontext(request)
        t = _ein_treffer(k, i)
        if aktion not in ("uebernehmen", "inhaber", "sofort"):
            raise HTTPException(404, "Unbekannte Aktion.")

        def arbeit(conn, melde):
            land, searxng = db.einstellung(conn, "suche.land", "de"), db.einstellung(conn, "suche.searxng", "")
            if aktion == "uebernehmen":
                return {"firma_id": suche.uebernimm(conn, t)}
            if aktion == "inhaber":
                return suche.uebernimm_und_finde(conn, t, land, searxng)
            ergebnis = suche.sofort_bewerben(conn, t, modell(conn, k), land, searxng, melde)
            if ergebnis.get("eignung"):
                ergebnis["eignung_text"] = ki.eignung_text(ergebnis["eignung"])
            return ergebnis
        return starte(k, {"uebernehmen": "Firma übernehmen", "inhaber": "Inhaber finden", "sofort": "Sofort bewerben"}[aktion], arbeit)

    # --- Firmen --------------------------------------------------------------------------------------------------------
    @app.get("/api/firmen")
    def firmen_liste(request: Request):
        k = kontext(request)

        def lies(conn):
            reihen = conn.execute(
                "SELECT f.id, f.name, f.sitz, f.passung, f.ki_score, f.mein_score, (SELECT status FROM entwuerfe e "
                "WHERE e.firma_id = f.id AND e.status != 'verworfen' ORDER BY e.id DESC LIMIT 1) AS mail "
                "FROM firmen f ORDER BY f.id").fetchall()
            return [{"id": r["id"], "name": r["name"], "sitz": r["sitz"], "passung": r["passung"],
                     "score": r["mein_score"] if r["mein_score"] is not None else r["ki_score"],
                     "mail": STATUS_TEXT.get(r["mail"], "")} for r in reihen]
        return mit_conn(k, lies)

    @app.get("/api/firmen/{fid}")
    def firma_detail(fid: int, request: Request):
        k = kontext(request)

        def lies(conn):
            r = conn.execute("SELECT * FROM firmen WHERE id = ?", (fid,)).fetchone()
            if not r:
                raise HTTPException(404, "Firma unbekannt.")
            d = {s: _ausgabe(r[s]) for s in ("id", "name", "sitz", "personen", "email", "website", "passung", "warum", "quelle",
                                               "seiten_geladen", "ki_score", "ki_begruendung", "ki_modell", "mein_score", "meine_notiz")}
            d["abschnitte"] = conn.execute("SELECT count(*) FROM chunks WHERE art = 'firma' AND firma_id = ?", (fid,)).fetchone()[0]
            d["belege"] = json.loads(r["ki_belege"] or "[]")
            d["ungeprueft"] = json.loads(r["ki_ungeprueft"] or "[]")
            d["eignung"] = ki.eignung_text(json.loads(r["ki_eignung"])) if r["ki_eignung"] else ""
            return d
        return mit_conn(k, lies)

    @app.post("/api/firmen")
    def firma_anlegen(daten_in: FirmaIn, request: Request):
        k = kontext(request)
        return {"id": mit_conn(k, lambda conn: firmen.lege_an(conn, **daten_in.model_dump()))}

    @app.delete("/api/firmen/{fid}")
    def firma_entfernen(fid: int, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: firmen.entferne(conn, fid))
        return {"ok": True}

    @app.post("/api/firmen/{fid}/bewertung")
    def firma_meine_bewertung(fid: int, daten_in: BewertungIn, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: lernen.merke_bewertung(conn, fid, daten_in.score, daten_in.notiz))
        return {"ok": True}

    @app.post("/api/firmen/{fid}/{aktion}")
    def firma_aktion(fid: int, aktion: str, request: Request):
        k = kontext(request)
        if aktion not in ("inhaber", "bewerten", "eignung", "mail"):
            raise HTTPException(404, "Unbekannte Aktion.")

        def arbeit(conn, melde):
            m = modell(conn, k)
            land, searxng = db.einstellung(conn, "suche.land", "de"), db.einstellung(conn, "suche.searxng", "")
            if not conn.execute("SELECT 1 FROM firmen WHERE id = ?", (fid,)).fetchone():
                raise ValueError("Firma unbekannt.")
            if aktion == "inhaber":
                return suche.hole_inhaber(conn, fid, land, searxng)
            if aktion == "bewerten":
                z = conn.execute("SELECT seiten_geladen, website FROM firmen WHERE id = ?", (fid,)).fetchone()
                if not z["seiten_geladen"] and z["website"]:
                    firmen.lade_seiten(conn, fid)
                return ki.bewerte(conn, fid, m)
            if aktion == "eignung":
                return {"text": ki.eignung_text(suche.pruefe_eignung(conn, fid, m, melde=melde))}
            return {"entwurf_id": suche.schlage_mail_vor(conn, fid, m, land, searxng, melde)}
        return starte(k, {"inhaber": "Inhaber finden", "bewerten": "Firma bewerten", "eignung": "Eignung prüfen",
                          "mail": "Mail vorschlagen"}[aktion], arbeit)

    # --- Entwürfe ------------------------------------------------------------------------------------------------------
    @app.get("/api/entwuerfe")
    def entwuerfe(request: Request, gesendet: bool = False):
        k = kontext(request)
        status = ("gesendet", "antwort", "absage", "keine_antwort") if gesendet else ("entwurf", "freigegeben")

        def lies(conn):
            reihen = conn.execute(
                "SELECT e.id, e.status, e.betreff, e.erstellt, e.gesendet_am, f.name FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id "
                f"WHERE e.status IN ({','.join('?' * len(status))}) ORDER BY e.id DESC", status).fetchall()
            return [{**_zeile(r), "status_text": STATUS_TEXT.get(r["status"], r["status"])} for r in reihen]
        return mit_conn(k, lies)

    @app.get("/api/entwuerfe/{eid}")
    def entwurf(eid: int, request: Request):
        k = kontext(request)

        def lies(conn):
            e = conn.execute("SELECT e.*, f.name AS firma FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id WHERE e.id = ?",
                             (eid,)).fetchone()
            if not e:
                raise HTTPException(404, "Entwurf unbekannt.")
            absender = person.lade(conn)
            anhaenge = profil.anhaenge_des_entwurfs(conn, eid)
            return {"id": eid, "firma": e["firma"], "status": e["status"], "status_text": STATUS_TEXT.get(e["status"], e["status"]),
                    "modell": e["modell"], "empfaenger": e["empfaenger"], "betreff": e["betreff"], "text": e["text"],
                    "von": f"{absender['name']} <{absender['email']}>",
                    "anhaenge": [{"name": p.name, "da": p.is_file()} for p in anhaenge],
                    "befunde": [{"stufe": b.stufe, "text": b.text} for b in ki.pruefe_entwurf(conn, eid)]}
        return mit_conn(k, lies)

    @app.put("/api/entwuerfe/{eid}")
    def entwurf_speichern(eid: int, daten_in: EntwurfIn, request: Request):
        k = kontext(request)
        if not ki.EINE_ADRESSE.match(daten_in.empfaenger.strip()):
            raise HTTPException(400, "Genau eine gültige Empfängeradresse.")
        mit_conn(k, lambda conn: ki.speichere_text(conn, eid, daten_in.betreff, daten_in.text, daten_in.empfaenger.strip()))
        return {"ok": True}

    @app.post("/api/entwuerfe/{eid}/freigeben")
    def entwurf_freigeben(eid: int, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: ki.gib_frei(conn, eid))
        return {"ok": True}

    @app.post("/api/entwuerfe/{eid}/verwerfen")
    def entwurf_verwerfen(eid: int, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: ki.verwirf_entwurf(conn, eid))
        return {"ok": True}

    @app.post("/api/entwuerfe/{eid}/status")
    def entwurf_status(eid: int, daten_in: StatusIn, request: Request):
        k = kontext(request)
        if daten_in.status not in ("antwort", "absage", "keine_antwort"):
            raise HTTPException(400, "Unbekannter Status.")
        mit_conn(k, lambda conn: ki.setze_status(conn, eid, daten_in.status))
        return {"ok": True}

    @app.post("/api/entwuerfe/{eid}/senden")
    def entwurf_senden(eid: int, request: Request):
        k = kontext(request)

        def arbeit(conn, melde):
            konto = versand.lade_konto(conn)
            if not konto:
                raise versand.VersandFehler("Es ist noch kein Mailkonto eingerichtet (Profil → Mailkonto).")
            passwort = versand.gespeichertes_passwort(konto)
            if not passwort:
                raise versand.VersandFehler("Für dein Mailkonto ist kein Passwort hinterlegt (Profil → Mailkonto).")
            melde("Sende")
            versand.sende_entwurf(conn, eid, passwort)
            return {"ok": True}
        return starte(k, "Mail senden", arbeit)

    @app.post("/api/entwuerfe/{eid}/varianten")
    def entwurf_varianten(eid: int, request: Request):
        k = kontext(request)
        return starte(k, "Varianten schreiben", lambda conn, melde: ki.entwurf_varianten(conn, eid, modell(conn, k), melde=melde))

    # --- Fakten und Fragen ---------------------------------------------------------------------------------------------
    @app.get("/api/fakten")
    def fakten(request: Request):
        k = kontext(request)

        def lies(conn):
            reihen = conn.execute("SELECT id, kategorie, text, quelle, status FROM fakten ORDER BY "
                                  "CASE status WHEN 'vorgeschlagen' THEN 0 WHEN 'bestaetigt' THEN 1 ELSE 2 END, id").fetchall()
            fragen = [{"schluessel": f.schluessel, "titel": f.titel, "frage": f.frage, "beispiel": f.beispiel}
                      for f in angaben.offen(conn)]
            return {"fakten": [_zeile(r) for r in reihen], "fragen": fragen, "kategorien": profil.KATEGORIEN,
                    "uebersprungene": angaben.hat_uebersprungene(conn)}
        return mit_conn(k, lies)

    @app.post("/api/fakten")
    def fakt_ergaenzen(daten_in: FaktIn, request: Request):
        k = kontext(request)
        if daten_in.kategorie not in profil.KATEGORIEN:
            raise HTTPException(400, "Unbekannte Kategorie.")
        mit_conn(k, lambda conn: profil.lege_eigenen_fakt_an(conn, daten_in.kategorie, daten_in.text))
        return {"ok": True}

    @app.post("/api/fakten/vorschlagen")
    def fakten_vorschlagen(request: Request):
        k = kontext(request)
        return starte(k, "Fakten vorschlagen", lambda conn, melde: profil.extrahiere(conn, modell(conn, k), fortschritt=melde))

    @app.post("/api/fakten/alle_bestaetigen")
    def fakten_alle(request: Request):
        k = kontext(request)
        return {"anzahl": mit_conn(k, profil.bestaetige_ki_vorschlaege)}

    @app.post("/api/fakten/{fid}/{aktion}")
    def fakt_aktion(fid: int, aktion: str, request: Request):
        k = kontext(request)
        if aktion == "bestaetigen":
            mit_conn(k, lambda conn: profil.bestaetige(conn, fid))
        elif aktion == "ablehnen":
            mit_conn(k, lambda conn: profil.lehne_ab(conn, fid))
        else:
            raise HTTPException(404, "Unbekannte Aktion.")
        return {"ok": True}

    @app.put("/api/fakten/{fid}")
    def fakt_aendern(fid: int, daten_in: TextIn, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: profil.aendere(conn, fid, daten_in.text))
        return {"ok": True}

    @app.post("/api/fragen/{schluessel}")
    def frage_beantworten(schluessel: str, daten_in: TextIn, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: angaben.beantworte(conn, schluessel, daten_in.text))
        return {"ok": True}

    @app.post("/api/fragen/{schluessel}/ueberspringen")
    def frage_ueberspringen(schluessel: str, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: angaben.ueberspringe(conn, schluessel))
        return {"ok": True}

    # --- Profil, Unterlagen, Mailkonto ---------------------------------------------------------------------------------
    @app.get("/api/profil")
    def profil_lesen(request: Request):
        k = kontext(request)

        def lies(conn):
            return {**person.lade(conn), "vorlage": db.einstellung(conn, "vorlage", ""),
                    "signatur": db.einstellung(conn, "mail.signatur", ""),
                    "woertlich": db.einstellung(conn, "mail.modus", "ki") == "vorlage", "ohne_alter": ki.ohne_alter(conn),
                    "unterlagen": [{"id": r["id"], "titel": r["titel"], "art": r["art"], "anhang": bool(r["anhang"]),
                                    "da": db.datei(r["pfad"]).is_file()}
                                   for r in conn.execute("SELECT id, titel, art, anhang, pfad FROM dokumente ORDER BY id")],
                    "arten": profil.UNTERLAGEN_ARTEN}
        return mit_conn(k, lies)

    @app.put("/api/profil")
    def profil_speichern(daten_in: ProfilIn, request: Request):
        k = kontext(request)

        def arbeit(conn):
            person.speichere(conn, name=daten_in.name, email=daten_in.email, telefon=daten_in.telefon, ort=daten_in.ort,
                             links=daten_in.links)
            db.setze_einstellung(conn, "vorlage", daten_in.vorlage)
            db.setze_einstellung(conn, "mail.signatur", daten_in.signatur)
            db.setze_einstellung(conn, "mail.modus", "vorlage" if daten_in.woertlich else "ki")
            db.setze_einstellung(conn, "mail.ohne_alter", "1" if daten_in.ohne_alter else "0")
        mit_conn(k, arbeit)
        return {"ok": True}

    @app.post("/api/unterlagen")
    async def unterlage_hochladen(request: Request, datei: UploadFile = File(...), art: str = Form("lebenslauf"),
                                  anhang: bool = Form(False)):
        k = kontext(request)
        if art not in profil.UNTERLAGEN_ARTEN:
            raise HTTPException(400, "Unbekannte Art.")
        name = _sicherer_name(datei.filename or "")
        if Path(name).suffix.lower() not in UPLOAD_ENDUNGEN:
            raise HTTPException(400, "Erlaubt sind PDF, DOCX, TXT und MD.")
        inhalt = await datei.read(MAX_UPLOAD + 1)
        if len(inhalt) > MAX_UPLOAD:
            raise HTTPException(413, "Höchstens 10 MB.")
        ordner = k.mandant.ordner / "unterlagen"
        ordner.mkdir(parents=True, exist_ok=True)
        ziel = ordner / name
        ziel.write_bytes(inhalt)
        ziel.chmod(0o600)

        def arbeit(conn, melde):
            profil.fuege_unterlage_hinzu(conn, ziel, art, anhang)
            melde("Lese die Unterlage ein")
            probleme = profil.lies_unterlagen(conn)
            angaben.uebernimm_aus_unterlagen(conn)
            return {"probleme": probleme}
        return starte(k, "Unterlage einlesen", arbeit)

    @app.post("/api/unterlagen/{did}/anhang")
    def unterlage_anhang(did: int, daten_in: UnterlageIn, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: profil.setze_anhang(conn, did, daten_in.anhang))
        return {"ok": True}

    @app.delete("/api/unterlagen/{did}")
    def unterlage_entfernen(did: int, request: Request):
        k = kontext(request)
        mit_conn(k, lambda conn: profil.entferne_unterlage(conn, did))
        return {"ok": True}

    @app.get("/api/mailkonto")
    def mailkonto(request: Request):
        k = kontext(request)

        def lies(conn):
            konto = versand.lade_konto(conn)
            if not konto:
                return {"konto": None}
            return {"konto": {"email": konto.email, "host": konto.host, "port": konto.port, "sicherheit": konto.sicherheit,
                              "benutzer": konto.benutzer}, "passwort_hinterlegt": bool(versand.gespeichertes_passwort(konto))}
        return mit_conn(k, lies)

    @app.put("/api/mailkonto")
    def mailkonto_speichern(daten_in: MailkontoIn, request: Request):
        k = kontext(request)
        if daten_in.sicherheit not in ("ssl", "starttls"):
            raise HTTPException(400, "Nur SSL/TLS oder STARTTLS.")
        if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", daten_in.host):
            raise HTTPException(400, "Servername ungültig.")
        konto = mailkonten.Konto("Web", daten_in.email.strip(), daten_in.host.strip(), daten_in.port, daten_in.sicherheit,
                                 daten_in.benutzer.strip() or daten_in.email.strip())

        def arbeit(conn):
            versand.speichere_konto(conn, konto)
            if daten_in.passwort:
                versand.speichere_passwort(konto, daten_in.passwort)
        mit_conn(k, arbeit)
        return {"ok": True}

    @app.post("/api/mailkonto/test")
    def mailkonto_test(request: Request):
        k = kontext(request)

        def arbeit(conn, melde):
            konto = versand.lade_konto(conn)
            pw = versand.gespeichertes_passwort(konto) if konto else None
            if not pw:
                raise versand.VersandFehler("Mailkonto und Passwort zuerst speichern.")
            return {"an": versand.sende_test(conn, pw)}
        return starte(k, "Testmail", arbeit)

    # --- Morgenbericht -------------------------------------------------------------------------------------------------
    @app.get("/api/bericht")
    def bericht(request: Request):
        k = kontext(request)
        an, uhrzeit, zuletzt = zentral_ausfuehren(lambda r: mandanten.nachtlauf(r, k.info.id))
        return {"nachtlauf": an, "uhrzeit": uhrzeit, "zuletzt": _ausgabe(zuletzt),
                "bericht": _ausgabe(mit_conn(k, nacht.letzter_bericht))}

    @app.post("/api/bericht/nachtlauf")
    def bericht_nachtlauf(daten_in: NachtIn, request: Request):
        k = kontext(request)
        try:
            zentral_ausfuehren(lambda r: mandanten.setze_nachtlauf(r, k.info.id, daten_in.an, daten_in.uhrzeit))
        except mandanten.KontoFehler as fehler:
            raise HTTPException(400, str(fehler))
        return {"ok": True}

    @app.post("/api/bericht/jetzt")
    def bericht_jetzt(request: Request):
        k = kontext(request)
        return starte(k, "Nachtlauf", lambda conn, melde: nacht.lauf(conn, modell(conn, k), melde))

    # --- System --------------------------------------------------------------------------------------------------------
    @app.get("/api/system")
    def system(request: Request):
        k = kontext(request)

        def lies(conn):
            groesse = conn.execute("SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                                   "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() "
                                   "AND c.relkind = 'r'").fetchone()[0]
            return {"tabellen": {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                                 for t in ("firmen", "fakten", "entwuerfe", "dokumente", "chunks", "lektionen", "berichte")},
                    "groesse": int(groesse), "lernen": lernen.statistik(conn),
                    "modell": db.einstellung(conn, "modell", config.STANDARD_MODELL)}
        werte = mit_conn(k, lies)
        return {**werte, "version": __version__, "speicher": "PostgreSQL (eigenes Schema und eigene Datenbankrolle je Mandant)",
                "ollama": config.OLLAMA_URL.split("//")[-1].split(":")[0] and "eingerichtet",
                "claude_erlaubt": k.info.claude_erlaubt,
                "sicherung": "Der Server wird jede Nacht vollständig gesichert (pg_dump, 14 Tage). "
                             "„Daten exportieren“ lädt eine Kopie deiner Daten als JSON herunter."}

    @app.get("/api/system/export")
    def export(request: Request):
        k = kontext(request)

        def lies(conn):
            aus = {"version": __version__, "zeit": db.jetzt(), "mandant": k.info.name, "tabellen": {}}
            for t in ("person", "fakten", "firmen", "entwuerfe", "dokumente", "lektionen", "berichte", "regeln", "cv_stationen",
                      "angaben", "ereignisse", "einstellungen"):
                zeilen = [_zeile(z) for z in conn.execute(f"SELECT * FROM {t}").fetchall()]
                for z in zeilen:
                    z.pop("embedding", None)
                if t == "einstellungen":
                    zeilen = [z for z in zeilen if not str(z.get("key", "")).startswith(geheim.Verschluesselt.PRAEFIX)]
                aus["tabellen"][t] = zeilen
            return aus
        inhalt = json.dumps(mit_conn(k, lies), ensure_ascii=False, indent=1, default=str)
        name = f"jobagent-export-{datetime.now():%Y-%m-%d}.json"
        return Response(inhalt, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/dateien/{art}/{name}")
    def datei(art: str, name: str, request: Request):
        k = kontext(request)
        if art not in ("unterlagen", "cv", "agenda") or name != _sicherer_name(name):
            raise HTTPException(404, "Nicht gefunden.")
        pfad = (k.mandant.ordner / art / name)
        if not pfad.is_file() or pfad.resolve().parent != (k.mandant.ordner / art).resolve():
            raise HTTPException(404, "Nicht gefunden.")
        return FileResponse(pfad, filename=name)

    # --- Verwaltung (Administratoren) ----------------------------------------------------------------------------------
    @app.get("/api/admin")
    def admin_uebersicht(request: Request):
        admin(request)
        return zentral_ausfuehren(lambda r: _ausgabe({"mandanten": mandanten.alle_mandanten(r), "konten": mandanten.konten(r),
                                                       "einladungen": mandanten.einladungen(r)}))

    @app.post("/api/admin/einladungen")
    def admin_einladung(daten_in: EinladungIn, request: Request):
        k = admin(request)
        code = zentral_ausfuehren(lambda r: mandanten.einladung_erzeugen(r, k.konto.id, daten_in.notiz, daten_in.mandant_id))
        return {"code": code, "gueltig_tage": mandanten.EINLADUNG_GUELTIG.days}

    @app.delete("/api/admin/einladungen/{eid}")
    def admin_einladung_weg(eid: int, request: Request):
        admin(request)
        zentral_ausfuehren(lambda r: mandanten.einladung_zurueckziehen(r, eid))
        return {"ok": True}

    @app.post("/api/admin/mandanten/{mid}/claude")
    def admin_claude(mid: int, daten_in: SchalterIn, request: Request):
        k = admin(request)
        zentral_ausfuehren(lambda r: (mandanten.setze_claude(r, mid, daten_in.an),
                                      mandanten.protokolliere(r, k.konto.id, "Claude-Freigabe", detail=f"Mandant {mid}: {daten_in.an}")))
        return {"ok": True}

    @app.post("/api/admin/konten/{kid}/aktiv")
    def admin_aktiv(kid: int, daten_in: SchalterIn, request: Request):
        k = admin(request)
        if kid == k.konto.id and not daten_in.an:
            raise HTTPException(400, "Das eigene Konto lässt sich nicht deaktivieren.")
        zentral_ausfuehren(lambda r: mandanten.setze_aktiv(r, kid, daten_in.an))
        return {"ok": True}

    # --- Nachtlauf je Mandant (serverseitig) ---------------------------------------------------------------------------
    def nachtlauf_pruefen():
        jetzt = datetime.now()
        faellig = zentral_ausfuehren(lambda r: mandanten.faellige_nachtlaeufe(r, jetzt))
        for info in faellig:
            zentral_ausfuehren(lambda r: mandanten.nachtlauf_erledigt(r, info.id, jetzt.date()))
            m = mandanten.als_mandant(info, daten / "mandanten" / str(info.id))

            def setzen(info=info):
                geheim.setze_ablage(ablage)
                llm.claude_erlaubt.set(bool(info.claude_erlaubt))

            def arbeit(conn, melde, info=info):
                gewaehlt = db.einstellung(conn, "modell", config.STANDARD_MODELL)
                m_ = gewaehlt if (not llm.ist_claude(gewaehlt) or info.claude_erlaubt) else config.STANDARD_MODELL
                return {k_: v for k_, v in nacht.lauf(conn, m_, melde).items() if k_ in ("gefunden", "geprueft", "fehler")}
            try:
                auftraege.starte(m, "Nachtlauf", arbeit, setzen)
                log.info("Nachtlauf für Mandant %s gestartet", info.id)
            except ZuViele:
                log.warning("Nachtlauf für Mandant %s verschoben: es laufen Arbeiten", info.id)
                zentral_ausfuehren(lambda r: mandanten.nachtlauf_erledigt(r, info.id, jetzt.date() - timedelta(days=1)))

    app.state.nachtlauf_pruefen = nachtlauf_pruefen
    app.state.auftraege = auftraege
    if nachtlauf:
        def schleife():
            while True:
                try:
                    nachtlauf_pruefen()
                except Exception:
                    log.exception("Nachtlauf-Planung fehlgeschlagen")
                time.sleep(300)
        threading.Thread(target=schleife, name="nachtlauf", daemon=True).start()

    # --- Oberfläche ----------------------------------------------------------------------------------------------------
    app.mount("/", StaticFiles(directory=STATISCH, html=True), name="oberflaeche")
    return app
