"""Bewerbung über ein Bewerbungsportal (zuerst Personio): Link auflösen, Stelle und Formular lesen, Mappe zusammenstellen.

Die Mappe liegt unter ~/Dokumente/Bewerbungen/<Firma>_<Stelle>: Anschreiben (PDF und DOCX zum Nachbearbeiten),
Lebenslauf für die Stelle, Zeugnisse, Stellentext, Formularwerte und ein Lesezeichen, das die Textfelder im Browser
ausfüllt. Hochladen und Senden bleiben beim Menschen (Human-in-the-Loop): Die App schickt nie ein Formular ab.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import shutil
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

from . import config, db, firmen, gehalt, ki, llm, person, rag, stellen_cv

PERSONIO = re.compile(r"^https?://(?P<firma>[a-z0-9-]+)\.jobs\.personio\.(?P<tld>de|com)/job/(?P<id>\d+)", re.I)
VERFUEGBAR = "ab sofort"
GEHALT_AUFSCHLAG = 2000


class PortalFehler(RuntimeError):
    """Link unbekannt, Stelle nicht (mehr) ausgeschrieben oder Portal nicht erreichbar."""


@dataclass
class Feld:
    name: str
    label: str
    art: str                      # "text", "auswahl" oder "datei"
    pflicht: bool = False
    optionen: list[tuple[str, str]] = field(default_factory=list)   # (Wert, Text), nur bei "auswahl"


@dataclass
class Ausschreibung:
    anbieter: str
    titel: str
    firma: str
    ort: str
    text: str
    url_stelle: str
    url_formular: str
    felder: list[Feld]
    referenz: str


def erkenne(url: str) -> dict | None:
    """{"anbieter": "personio", "basis": "https://x.jobs.personio.de", "id": "123"} oder None."""
    m = PERSONIO.match((url or "").strip())
    if not m:
        return None
    return {"anbieter": "personio", "basis": f"https://{m['firma'].lower()}.jobs.personio.{m['tld'].lower()}", "id": m["id"],
            "slug": m["firma"].lower()}


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(follow_redirects=True, timeout=25, headers={"User-Agent": config.USER_AGENT})


def _text(html: str) -> str:
    """HTML der Personio-Beschreibung als lesbarer Text mit Aufzählungen."""
    t = re.sub(r"(?i)<li[^>]*>", "\n- ", html or "")
    t = re.sub(r"(?i)</p>|<br\s*/?>|</h\d>|</ul>", "\n", t)
    t = html_mod.unescape(re.sub(r"<[^>]+>", "", t)).replace("​", "").replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", "\n".join(z.strip() for z in t.splitlines())).strip()


def stelle_aus_feed(xml: str, job_id: str) -> dict:
    """Titel, Firma, Ort und Beschreibung aus dem öffentlichen XML-Feed von Personio."""
    try:
        wurzel = ET.fromstring(xml)
    except ET.ParseError as fehler:
        raise PortalFehler("Personio lieferte keinen lesbaren Stellenfeed.") from fehler
    for p in wurzel.iter("position"):
        if (p.findtext("id") or "").strip() != job_id:
            continue
        teile = [f"## {d.findtext('name') or ''}\n{_text(d.findtext('value') or '')}" for d in p.iter("jobDescription")]
        return {"titel": (p.findtext("name") or "").strip(), "firma": (p.findtext("subcompany") or "").strip(),
                "ort": (p.findtext("office") or "").strip(), "text": "\n\n".join(teile).strip()}
    raise PortalFehler("Die Stelle steht nicht mehr im Stellenfeed der Firma, sie ist wohl schon besetzt.")


def _sauber(t: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def felder_aus_formular(seite: str) -> list[Feld]:
    """Eingabefelder des Bewerbungsformulars samt Beschriftung, Pflicht und Auswahlwerten."""
    labels = {m["id"]: m["text"] for m in re.finditer(r'<label[^>]*\bfor="(?P<id>[^"]+)"[^>]*>(?P<text>.*?)</label>', seite, re.S)}
    gruppen = {m["id"]: m["text"] for m in re.finditer(r'<(?:div|span)[^>]*\bid="(?P<id>(?:doc-label|name-group-label)[^"]*)"[^>]*>'
                                                       r'(?P<text>.*?)</(?:div|span)>(?:\s*</span>)?', seite, re.S)}
    felder: list[Feld] = []
    treffer = [m for m in re.finditer(r"<(?P<tag>input|textarea)\b(?P<attr>[^>]*)>", seite)]
    treffer += [m for m in re.finditer(r"<(?P<tag>select)\b(?P<attr>[^>]*)>(?P<inhalt>.*?)</select>", seite, re.S)]
    for m in sorted(treffer, key=lambda m: m.start()):
        attr = m["attr"]
        name = re.search(r'\bname="([^"]+)"', attr)
        if not name or re.search(r'type="(?:hidden|submit|checkbox|radio)"', attr) or name.group(1) == "website":
            continue
        name = name.group(1)
        kennung = re.search(r'\bid="([^"]+)"', attr)
        if m["tag"] == "input" and 'type="file"' in attr:
            roh = gruppen.get(f"doc-label-{name.split('.', 1)[-1]}", "") or (re.search(r'aria-label="(?:Upload )?([^"]+)"', attr) or [None, name])[1]
            art = "datei"
        else:
            roh = labels.get(kennung.group(1), "") if kennung else ""
            roh = roh or (re.search(r'placeholder="([^"]+)"', attr) or [None, name])[1]
            art = "auswahl" if m["tag"] == "select" else "text"
        label = _sauber(re.sub(r"\(\s*(?:erforderlich|optional)\s*\)|\*", "", _sauber(roh), flags=re.I))
        pflicht = bool(re.search(r"erforderlich|required|\*", _sauber(roh), re.I))
        if name in ("first_name", "last_name"):
            pflicht = True
        optionen = []
        if art == "auswahl":
            optionen = [(o["wert"], _sauber(o["text"])) for o in
                        re.finditer(r'<option[^>]*value="(?P<wert>[^"]*)"[^>]*>(?P<text>.*?)</option>', m.groupdict().get("inhalt") or "", re.S) if o["wert"]]
        felder.append(Feld(name, label, art, pflicht, optionen))
    return felder


def lade(url: str, client: httpx.Client | None = None) -> Ausschreibung:
    info = erkenne(url)
    if not info:
        raise PortalFehler("Diesen Link kenne ich noch nicht. Unterstützt wird bisher Personio (…jobs.personio.de/job/…).")
    c = _client(client)
    try:
        feed = c.get(f"{info['basis']}/xml", params={"language": "de"})
        formular_url = f"{info['basis']}/job/{info['id']}/apply?display=de&language=de"
        formular = c.get(formular_url)
    except httpx.HTTPError as fehler:
        raise PortalFehler(f"Personio ist gerade nicht erreichbar: {fehler}") from fehler
    if feed.status_code != 200:
        raise PortalFehler(f"Der Stellenfeed von Personio meldet {feed.status_code}.")
    stelle = stelle_aus_feed(feed.text, info["id"])
    felder = felder_aus_formular(formular.text) if formular.status_code == 200 else []
    firma = stelle["firma"] or info["slug"]
    return Ausschreibung("Personio", stelle["titel"], firma, stelle["ort"], stelle["text"],
                         f"{info['basis']}/job/{info['id']}?display=de", formular_url, felder, f"personio:{info['slug']}:{info['id']}")


# ── Werte fürs Formular ──────────────────────────────────────────────────────────────────────────────────────────────

def gehaltswunsch(conn: sqlite3.Connection, *texte: str) -> str:
    """Niedrigster Wert der ausgeschriebenen Spanne plus Aufschlag (Einstellung „bewerbung.gehalt_aufschlag“, 2.000 €).
    Ohne Angabe in Anzeige oder Treffer bleibt das Feld leer: geraten wird kein Gehalt."""
    for t in texte:
        untergrenze = gehalt.untergrenze_jahr(t or "")
        if untergrenze:
            try:
                aufschlag = int(db.einstellung(conn, "bewerbung.gehalt_aufschlag", str(GEHALT_AUFSCHLAG)) or GEHALT_AUFSCHLAG)
            except ValueError:
                aufschlag = GEHALT_AUFSCHLAG
            return f"{untergrenze + aufschlag:,.0f} €".replace(",", ".")
    return ""


def _option(feld: Feld, *wuensche: str) -> tuple[str, str] | None:
    for w in wuensche:
        for wert, text in feld.optionen:
            if text.strip().lower() == w.lower():
                return wert, text
    return None


def werte(conn: sqlite3.Connection, a: Ausschreibung, gehalt_text: str = "") -> tuple[dict[str, tuple[str, str]], list[str]]:
    """({Feldname: (Wert fürs Formular, Anzeige)}, offene Pflichtfelder). Datei-Felder bleiben außen vor."""
    p = person.lade(conn)
    vorname, _, nachname = (p["name"] or "").partition(" ")
    verfuegbar = db.einstellung(conn, "bewerbung.verfuegbar_ab", VERFUEGBAR) or VERFUEGBAR
    gehaltswert = gehaltswunsch(conn, gehalt_text, a.text)
    ergebnis: dict[str, tuple[str, str]] = {}
    offen: list[str] = []
    for f in a.felder:
        if f.art == "datei":
            continue
        name, label = f.name.lower(), f.label.lower()
        wert: tuple[str, str] | None = None
        if name == "first_name":
            wert = (vorname, vorname)
        elif name == "last_name":
            wert = (nachname, nachname)
        elif name == "email" or "e-mail" in label:
            wert = (p["email"], p["email"])
        elif name == "phone" or "telefon" in label:
            wert = (p["telefon"], p["telefon"])
        elif name == "available_from" or "verfügbar" in label or "eintritt" in label:
            wert = (verfuegbar, verfuegbar)
        elif "gehalt" in label or "salary" in label:
            wert = (gehaltswert, gehaltswert) if gehaltswert else None
        elif f.art == "auswahl" and ("wohnsitz" in label or "land" in label):
            wert = _option(f, "Deutschland", "Germany")
        elif f.art == "auswahl" and "deutsch" in label:
            wert = _option(f, "Muttersprache", "Native", "C2")
        if wert and wert[0]:
            ergebnis[f.name] = wert
        elif f.pflicht:
            offen.append(f.label)
    return ergebnis, offen


def lesezeichen(werte_: dict[str, tuple[str, str]], felder: list[Feld]) -> str:
    """javascript:-Lesezeichen, das die Werte ins Formular schreibt (React-tauglich über den nativen Setter)."""
    auswahl = {f.name for f in felder if f.art == "auswahl"}
    daten = {k: v[0] for k, v in werte_.items()}
    js = ("(()=>{const W=" + json.dumps(daten, ensure_ascii=False) + ",S=" + json.dumps(sorted(auswahl & set(daten))) + ";let n=0;"
          "const set=(el,v)=>{const d=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el),'value');d.set.call(el,v);"
          "['input','change','blur'].forEach(t=>el.dispatchEvent(new Event(t,{bubbles:true})));n++;};"
          "for(const[k,v]of Object.entries(W)){const el=document.querySelector('[name=\"'+k+'\"]');if(el)set(el,v);}"
          "const z=document.createElement('div');z.textContent='Job Agent: '+n+' Felder ausgefüllt. Dateien hochladen, prüfen, selbst senden.';"
          "z.style.cssText='position:fixed;top:12px;right:12px;z-index:99999;background:#ea580c;color:#fff;padding:12px 16px;"
          "border-radius:10px;font:600 14px sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.3)';"
          "document.body.appendChild(z);setTimeout(()=>z.remove(),8000);})();")
    return "javascript:" + urllib.parse.quote(js, safe="(){}[]=>;:,.'+!&|/*?-_ \"")


# ── Mappe ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def ordner_fuer(a: Ausschreibung, basis: Path | None = None) -> Path:
    basis = basis or Path.home() / "Dokumente" / "Bewerbungen"
    kurz = re.sub(r"\s*\((?:m/w/d|w/m/d|m/f/d|all genders)\)", "", a.titel, flags=re.I).split(",")[0]
    name = re.sub(r"[^\w.-]+", "_", f"{a.firma}_{kurz}").strip("_")[:90]
    return basis / name


def _absenderzeilen(p: dict) -> list[str]:
    return [z for z in (p["name"], p["ort"], " · ".join(t for t in (p["email"], p["telefon"]) if t)) if z]


def _datum() -> str:
    from .pdf import MONATE
    heute = date.today()
    return f"{heute.day}. {MONATE[heute.month - 1]} {heute.year}"


def schreibe_anschreiben(conn: sqlite3.Connection, ziel: Path, a: Ausschreibung, text: str) -> list[Path]:
    """Anschreiben als PDF (zum Hochladen) und DOCX (zum Nachbearbeiten). `text` beginnt mit der Anrede."""
    from docx import Document
    from docx.shared import Pt
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from . import pdf
    p = person.lade(conn)
    ort = (p["ort"] or "").split("(")[0].strip()
    datum = f"{ort}, {_datum()}" if ort else _datum()
    betreff = f"Bewerbung als {a.titel}"
    absaetze = [t.strip() for t in re.split(r"\n\s*\n", text.strip()) if t.strip()]

    stile = pdf.stile()
    story = [Paragraph(f"<b>{escape(p['name'])}</b>", stile["text"])]
    story += [Paragraph(escape(z), stile["unter"]) for z in _absenderzeilen(p)[1:]]
    story += [Spacer(1, 8 * mm), Paragraph(escape(a.firma), stile["text"]), Spacer(1, 6 * mm),
              Paragraph(escape(datum), stile["text"]), Spacer(1, 4 * mm), Paragraph(f"<b>{escape(betreff)}</b>", stile["text"]),
              Spacer(1, 3 * mm)]
    story += [Paragraph(escape(t).replace("\n", "<br/>"), stile["text"]) for t in absaetze]
    datei = re.sub(r"[^\w-]+", "_", f"Anschreiben_{p['name']}_{a.firma}").strip("_")[:80]
    pdf_pfad, docx_pfad = ziel / f"{datei}.pdf", ziel / f"{datei}.docx"
    SimpleDocTemplate(str(pdf_pfad), pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                      title=betreff, author=p["name"]).build(story)

    dok = Document()
    stil = dok.styles["Normal"]
    stil.font.name, stil.font.size = "Calibri", Pt(10.5)
    kopf = dok.add_paragraph()
    kopf.add_run(p["name"]).bold = True
    for z in _absenderzeilen(p)[1:]:
        dok.add_paragraph(z)
    dok.add_paragraph()
    dok.add_paragraph(a.firma)
    dok.add_paragraph()
    dok.add_paragraph(datum)
    dok.add_paragraph().add_run(betreff).bold = True
    for t in absaetze:
        dok.add_paragraph(t)
    dok.save(str(docx_pfad))
    return [pdf_pfad, docx_pfad]


def _markdown_formular(a: Ausschreibung, werte_: dict[str, tuple[str, str]], offen: list[str], dateien: dict[str, str]) -> str:
    zeilen = [f"# Formular: {a.titel} – {a.firma}", "", f"Formular: {a.url_formular}", "",
              "Das Lesezeichen aus `Lesezeichen.txt` füllt die Textfelder aus. Dateien hochladen und **selbst senden**.", "",
              "| Feld | Wert |", "|---|---|"]
    for f in a.felder:
        if f.art == "datei":
            continue
        wert = werte_.get(f.name, ("", ""))[1] or "**OFFEN – bitte selbst eintragen**"
        zeilen.append(f"| {f.label}{'*' if f.pflicht else ''} | {wert} |")
    zeilen += ["", "## Dokumente", "", "| Upload | Datei |", "|---|---|"]
    for f in a.felder:
        if f.art == "datei":
            zeilen.append(f"| {f.label}{'*' if f.pflicht else ''} | {dateien.get(f.name, '–')} |")
    if offen:
        zeilen += ["", "**Noch offen:** " + ", ".join(offen)]
    return "\n".join(zeilen) + "\n"


def stelle_md(a: Ausschreibung) -> str:
    return f"# {a.titel}\n\n{a.firma} · {a.ort} · über {a.anbieter}\n\nStelle: {a.url_stelle}\n\n{a.text}\n"


def baue_mappe(conn: sqlite3.Connection, a: Ausschreibung, anschreiben: str, firma_id: int | None = None,
               gehalt_text: str = "", basis: Path | None = None) -> dict:
    """Legt den Ordner an und füllt ihn. Rückgabe: {"ordner", "dateien", "werte", "offen"}."""
    ziel = ordner_fuer(a, basis)
    ziel.mkdir(parents=True, exist_ok=True)
    dateien: list[Path] = schreibe_anschreiben(conn, ziel, a, anschreiben)
    upload: dict[str, str] = {}
    anschreiben_pdf = dateien[0]

    lebenslauf = (stellen_cv.erstelle(conn, firma_id) if firma_id else []) or []
    if not lebenslauf:
        zeile = conn.execute("SELECT pfad FROM dokumente WHERE art = 'lebenslauf' ORDER BY anhang DESC, id").fetchone()
        lebenslauf = [Path(zeile[0])] if zeile and Path(zeile[0]).is_file() else []
    kopiert_cv = []
    for quelle in lebenslauf:
        dateien.append(shutil.copy2(quelle, ziel / quelle.name))
        kopiert_cv.append(quelle.name)
    zeugnisse = []
    for (pfad,) in conn.execute("SELECT pfad FROM dokumente WHERE art = 'zeugnis' ORDER BY id"):
        if Path(pfad).is_file():
            dateien.append(Path(shutil.copy2(pfad, ziel / Path(pfad).name)))
            zeugnisse.append(Path(pfad).name)

    for f in a.felder:
        if f.art != "datei":
            continue
        kennung = f"{f.name} {f.label}".lower()
        if "cv" in kennung or "lebenslauf" in kennung or "resume" in kennung:
            upload[f.name] = ", ".join(kopiert_cv) or "Lebenslauf fehlt: unter „Unterlagen“ hinterlegen"
        elif "cover" in kennung or "anschreiben" in kennung or "motivation" in kennung:
            upload[f.name] = anschreiben_pdf.name
        elif "zeugnis" in kennung or "reference" in kennung or "certificate" in kennung:
            upload[f.name] = ", ".join(zeugnisse) or "–"

    werte_, offen = werte(conn, a, gehalt_text)
    (ziel / "Stelle.md").write_text(stelle_md(a), encoding="utf-8")
    (ziel / "Formular.md").write_text(_markdown_formular(a, werte_, offen, upload), encoding="utf-8")
    (ziel / "Lesezeichen.txt").write_text(lesezeichen(werte_, a.felder), encoding="utf-8")
    db.protokolliere(conn, "Bewerbungsmappe erstellt", f"{ziel} ({a.anbieter}), offen: {', '.join(offen) or 'nichts'}", firma_id)
    return {"ordner": ziel, "dateien": [Path(d) for d in dateien], "werte": werte_, "offen": offen}


# ── Ablauf ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def uebernimm(conn: sqlite3.Connection, a: Ausschreibung) -> int:
    """Firma mit Anzeige anlegen (oder vorhandene nehmen), damit Eignung und Anschreiben die Anzeige kennen."""
    vorhanden = conn.execute("SELECT id FROM firmen WHERE name = ?", (a.firma,)).fetchone()
    firma_id = vorhanden[0] if vorhanden else firmen.lege_an(conn, a.firma, sitz=a.ort, warum=f"Stelle: {a.titel}",
                                                                quelle=f"{a.anbieter} {a.referenz}")
    quelle = f"{a.url_stelle}#stellenanzeige"
    if not conn.execute("SELECT 1 FROM chunks WHERE firma_id = ? AND quelle = ?", (firma_id, quelle)).fetchone():
        stuecke = [f"Stellenangebot: {a.titel}. Beruf: . Ort: {a.ort}."] + rag.zerlege(a.text)
        rag.speichere(conn, "firma", stuecke, firma_id=firma_id, quellen=[quelle] * len(stuecke), abruf=db.jetzt())
    return firma_id


def anschreiben_text(conn: sqlite3.Connection, firma_id: int, modell: str, chat=llm.chat) -> str:
    """Text über die geprüfte Schreib-Pipeline (nur belegte Fakten), Anrede und Gruß wie in der Mail."""
    ki.pruefe_ausschluss(conn, firma_id)
    kern, _, _ = ki._schreibe_kern(conn, firma_id, modell, chat)
    firma = ki._firma(conn, firma_id)
    return ki.setze_zusammen(firma, kern, person.name(conn), "", ki.anzeige_der_firma(conn, firma_id)[0])


def bewirb(conn: sqlite3.Connection, url: str, modell: str, chat=llm.chat, client=None, melde=lambda _: None,
           gehalt_text: str = "", basis: Path | None = None, eignung_pruefen: bool = True) -> dict:
    """Link → Stelle und Formular lesen → Firma übernehmen → Eignung → Anschreiben → Mappe. Gesendet wird nichts."""
    melde("Lese Stelle und Bewerbungsformular")
    a = lade(url, client)
    firma_id = uebernimm(conn, a)
    eignung = None
    if eignung_pruefen:
        melde("Prüfe deine Eignung anhand von Lebenslauf und Zeugnis, das kann einige Minuten dauern")
        try:
            eignung = ki.pruefe_eignung(conn, firma_id, modell, chat, melde)
        except ki.NichtMoeglich:
            eignung = None
    melde("Schreibe das Anschreiben, das kann einige Minuten dauern")
    text = anschreiben_text(conn, firma_id, modell, chat)
    melde("Stelle die Bewerbungsmappe zusammen")
    mappe = baue_mappe(conn, a, text, firma_id, gehalt_text, basis)
    return {**mappe, "firma_id": firma_id, "ausschreibung": a, "eignung": eignung}
