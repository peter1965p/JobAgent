"""Der Nachtlauf: suchen, aussortieren, bewerten, Mails vorschlagen und morgens berichten. Er sendet nie etwas.

Start über einen systemd-Timer des Benutzers: `python -m jobsuche.nacht --db <Datenbank>`.
"""

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import config, db, firmen, ki, llm, regeln, suche

MAX_PRUEFEN = 60  # Firmenseiten lesen dauert Sekunden. Der Rest kommt in den nächsten Nächten dran.
MAX_BEWERTEN = 10  # Eine Bewertung dauert auf der CPU Minuten.
MAX_MAILS = 3
MIN_SCORE = 50  # Darunter fliegt die Firma wieder aus der Liste.
VERSUCHE = 3
PAUSE = 600
NAME = "tueroeffner-nacht"


STELLENQUELLEN = ("Jobbörse", "Arbeitnow", "(Alert)", "LinkedIn", "StepStone")


def ist_direkt(treffer: dict) -> bool:
    """Die Firma kam nicht über eine Stellenbörse, inseriert dort also zumindest bei dieser Suche nicht."""
    return not any(name in treffer["quelle"] for name in STELLENQUELLEN)


def _gesehen(conn: sqlite3.Connection) -> set[str]:
    return {z[0] for z in conn.execute("SELECT schluessel FROM nacht_gesehen")}


def _merke(conn: sqlite3.Connection, firma: str) -> None:
    conn.execute("INSERT OR IGNORE INTO nacht_gesehen (schluessel, datum) VALUES (?, ?)",
                 (regeln.kurzname(firma).lower(), db.jetzt()))
    conn.commit()


def _suche(conn, modell, melde, chat, client, warte) -> dict:
    auftrag = suche.gespeicherter_auftrag(conn)
    ergebnis = {}
    for versuch in range(VERSUCHE):
        if auftrag.begriffe.strip():
            ergebnis = suche.suche_ueberall(conn, auftrag, melde, client)
        else:
            ergebnis = suche.finde_passende(conn, modell, auftrag, melde, chat, client)
        # Die OSM-Server sind oft nur kurz überlastet. Nachts ist Zeit für einen zweiten Versuch.
        if versuch + 1 == VERSUCHE or not any("antwortet nicht" in h for h in ergebnis["hinweise"]):
            return ergebnis
        melde("Das Firmenverzeichnis antwortet nicht, ich versuche es später noch einmal")
        warte(PAUSE)
    return ergebnis


def lauf(conn: sqlite3.Connection, modell: str, melde=lambda _: None, chat=llm.chat, client=None, warte=time.sleep) -> dict:
    ausgewichen = len(llm.AUSWEICHEN)
    bericht = {"datum": db.jetzt(), "gefunden": 0, "direkt": 0, "geprueft": 0, "ohne_adresse": 0, "zu_niedrig": 0, "firmen": [], "hinweise": [], "fehler": ""}
    try:
        ergebnis = _suche(conn, modell, melde, chat, client, warte)
        bericht["hinweise"] = ergebnis["hinweise"]
        bericht["gefunden"] = len(ergebnis["treffer"])
        gesehen = _gesehen(conn)
        neu = [t for t in ergebnis["treffer"] if not t["bekannt"] and regeln.kurzname(t["firma"]).lower() not in gesehen]
        # Firmen ohne Stellenportal zuerst: Sie sucht sonst kaum jemand, und ihre Website ist schon bekannt.
        neu.sort(key=lambda t: (not ist_direkt(t), bool(t.get("konzern")), -(t["naehe"] or 0)))
        bericht["direkt"] = sum(ist_direkt(t) for t in neu)

        kandidaten = []
        for treffer in neu[:MAX_PRUEFEN]:
            melde(f"Lese die Firmenseiten: {treffer['firma']}")
            _merke(conn, treffer["firma"])
            kontakt = suche.uebernimm_und_finde(conn, treffer, db.einstellung(conn, "suche.land", "de"),
                                                db.einstellung(conn, "suche.searxng", ""), client)
            bericht["geprueft"] += 1
            firma_id = conn.execute("SELECT id FROM firmen WHERE name = ?", (treffer["firma"],)).fetchone()[0]
            if not kontakt["email"]:
                firmen.entferne(conn, firma_id)
                bericht["ohne_adresse"] += 1
                continue
            stellen = firmen.offene_stellen(conn, firma_id)
            if stellen:
                # So kann sich Bewertung und Mail auf die Stelle beziehen, die nur auf der Firmenseite steht.
                firmen.aendere(conn, firma_id, warum=f"Stellen auf der eigenen Website: {'; '.join(stellen[:5])}")
            kandidaten.append({"id": firma_id, "kontakt": kontakt, "treffer": treffer, "stellen": stellen,
                               "naehe": firmen.inhaltsnaehe(conn, firma_id) or 0})
        # Nicht mehr der Name entscheidet, sondern was auf den Seiten steht.
        # Mittelstand hat Vorrang: Konzernmerkmale kosten Punkte, schließen aber nicht aus.
        kandidaten.sort(key=lambda k: -(k["naehe"] + (5 if k["stellen"] else 0) + (5 if k["kontakt"]["personen"] else 0)
                                        - (10 if k["kontakt"].get("konzern") else 0)))

        # Was heute nicht bewertet wird, kommt in der nächsten Nacht wieder dran, statt die Firmenliste zu füllen.
        for k in kandidaten[MAX_BEWERTEN:]:
            firmen.entferne(conn, k["id"])
            conn.execute("DELETE FROM nacht_gesehen WHERE schluessel = ?", (regeln.kurzname(k["treffer"]["firma"]).lower(),))
        conn.commit()

        bewertet = []
        for k in kandidaten[:MAX_BEWERTEN]:
            firma_id, kontakt, treffer = k["id"], k["kontakt"], k["treffer"]
            melde(f"Bewerte: {treffer['firma']}")
            try:
                score = ki.bewerte(conn, firma_id, modell, chat)["score"]
            except llm.LLMFehler as fehler:
                bericht["hinweise"].append(f"{treffer['firma']}: {fehler}")
                continue
            if score < MIN_SCORE:
                firmen.entferne(conn, firma_id)
                bericht["zu_niedrig"] += 1
                continue
            bewertet.append((score, firma_id, kontakt, treffer, k["stellen"]))
        bewertet.sort(key=lambda b: -b[0])

        for rang, (score, firma_id, kontakt, treffer, stellen) in enumerate(bewertet):
            entwurf_id = None
            eignung = None
            if rang < MAX_MAILS and ki.anzeige_der_firma(conn, firma_id)[0]:
                melde(f"Prüfe die Eignung: {treffer['firma']}")
                try:
                    eignung = ki.pruefe_eignung(conn, firma_id, modell, chat)
                except (llm.LLMFehler, ki.NichtMoeglich) as fehler:
                    bericht["hinweise"].append(f"{treffer['firma']}: Eignung nicht geprüft, {fehler}")
            if eignung and eignung["score"] < ki.MIN_EIGNUNG:
                bericht["hinweise"].append(f"{treffer['firma']}: Eignung {eignung['score']} %, keine Mail geschrieben.")
            elif rang < MAX_MAILS:
                melde(f"Schreibe die Mail: {treffer['firma']}")
                try:
                    entwurf_id = ki.erstelle_entwurf(conn, firma_id, modell, chat)
                except (llm.LLMFehler, ki.NichtMoeglich) as fehler:
                    bericht["hinweise"].append(f"{treffer['firma']}: {fehler}")
            begruendung = conn.execute("SELECT ki_begruendung FROM firmen WHERE id = ?", (firma_id,)).fetchone()[0]
            bericht["firmen"].append({
                "firma_id": firma_id, "name": kontakt["name"], "score": score, "personen": kontakt["personen"],
                "email": kontakt["email"], "art": kontakt["art"], "website": kontakt["website"], "quelle": treffer["quelle"],
                "begruendung": begruendung, "entwurf_id": entwurf_id, "stellen": stellen, "direkt": ist_direkt(treffer),
                "eignung": eignung["score"] if eignung else None,
            })
    except Exception as fehler:  # Morgens soll im Bericht stehen, was schiefging, statt nur im Systemlog.
        bericht["fehler"] = f"{type(fehler).__name__}: {fehler}"
    if len(llm.AUSWEICHEN) > ausgewichen:
        bericht["hinweise"].append(f"Claude nicht genutzt, lokales Modell eingesprungen: {llm.AUSWEICHEN[-1]}")
    conn.execute("INSERT INTO berichte (datum, inhalt) VALUES (?, ?)", (bericht["datum"], json.dumps(bericht, ensure_ascii=False)))
    conn.commit()
    db.protokolliere(conn, "Nachtlauf", f"{bericht['gefunden']} gefunden, {len(bericht['firmen'])} im Bericht {bericht['fehler']}".strip())
    return bericht


def letzter_bericht(conn: sqlite3.Connection) -> dict | None:
    zeile = conn.execute("SELECT inhalt FROM berichte ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(zeile[0]) if zeile else None


def _ordner() -> Path:
    basis = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(basis) / "systemd" / "user"


def einheiten(datenbank: Path | str, uhrzeit: str) -> tuple[str, str]:
    befehl = shlex.join([sys.executable, "-m", "jobsuche.nacht", "--db", str(datenbank)])
    dienst = (
        "[Unit]\nDescription=Job Agent: Nachtlauf\nAfter=network-online.target\n\n"
        f"[Service]\nType=oneshot\nWorkingDirectory={config.ROOT}\nExecStart={befehl}\nNice=19\nIOSchedulingClass=idle\n"
    )
    # Persistent holt einen verpassten Lauf nach, wenn der Rechner nachts aus war.
    timer = (
        "[Unit]\nDescription=Job Agent: Nachtlauf täglich\n\n"
        f"[Timer]\nOnCalendar=*-*-* {uhrzeit}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return dienst, timer


def einschalten(datenbank: Path | str, uhrzeit: str = "01:00", run=subprocess.run) -> None:
    if config.DEMO:
        raise RuntimeError("Die Demo hat keinen Nachtlauf.")
    ordner = _ordner()
    ordner.mkdir(parents=True, exist_ok=True)
    dienst, timer = einheiten(datenbank, uhrzeit)
    (ordner / f"{NAME}.service").write_text(dienst, encoding="utf-8")
    (ordner / f"{NAME}.timer").write_text(timer, encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"], check=True)
    run(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"], check=True)


def ausschalten(run=subprocess.run) -> None:
    run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"], check=False)
    for endung in ("service", "timer"):
        (_ordner() / f"{NAME}.{endung}").unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"], check=True)


def ist_eingeschaltet() -> bool:
    return (_ordner() / f"{NAME}.timer").exists()


def main() -> int:
    argumente = argparse.ArgumentParser(description="Job Agent: Nachtlauf")
    argumente.add_argument("--db", required=True)
    pfad = argumente.parse_args().db
    if not Path(pfad).exists():
        print(f"Datenbank fehlt: {pfad}", file=sys.stderr)
        return 1
    config.DB_PFAD = Path(pfad)
    conn = db.verbinden(pfad)
    bericht = lauf(conn, db.einstellung(conn, "modell", config.STANDARD_MODELL), print)
    print(json.dumps({k: bericht[k] for k in ("gefunden", "geprueft", "fehler")}, ensure_ascii=False))
    return 1 if bericht["fehler"] else 0


if __name__ == "__main__":
    sys.exit(main())
