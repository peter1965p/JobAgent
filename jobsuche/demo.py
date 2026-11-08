"""Eine Demo mit erfundenen Daten in einem Wegwerf-Ordner. Sie kennt keine echten Personen und sendet nichts."""

import json
import shutil
import tempfile
from pathlib import Path

from . import config, db, firmen, ki, lernen, person, profil, regeln

ORDNER: Path | None = None
MAILTEXT = (
    "Ich betreue seit acht Jahren Linux-Server und suche eine Festanstellung im Raum Bremen. "
    "Zuletzt habe ich 50 Server betreut und das Monitoring neu aufgebaut.\n\n"
    "Hätten Sie diese Woche 15 Minuten für ein kurzes Gespräch?"
)
FAKTEN = [
    ("positionierung", "Administratorin für sicheren Serverbetrieb."),
    ("erfahrung", "Systemadministratorin bei der Beispiel AG (03/2018 – heute), 50 Linux-Server betreut."),
    ("erfahrung", "Monitoring und Backup bei der Beispiel AG neu aufgebaut."),
    ("technik", "Kenntnisse: Linux, Ansible, Docker, Python."),
    ("praktisch", "Führerschein: Klasse B, Serverschränke montieren und verkabeln."),
    ("suche", "Sucht eine Festanstellung im Raum Bremen."),
]
FIRMEN = [
    {"name": "Muster Systemhaus GmbH", "personen": "Alex Muster", "email": "info@muster-systemhaus.example",
     "sitz": "Bremen", "warum": "Betreibt Linux-Server für den Mittelstand", "score": 82,
     "begruendung": "Passt: Linux-Server im Betrieb decken sich mit den Fakten; Monitoring passt zum Angebot.\n"
                    "Passt nicht: Keine Angaben zu Ansible."},
    {"name": "Beispiel Cloud KG", "personen": "Kim Vorbild", "email": "kontakt@beispiel-cloud.example",
     "sitz": "Oldenburg", "warum": "Cloud-Plattform und Managed Services", "score": 64,
     "begruendung": "Passt: Docker und Python passen zur Plattform.\nPasst nicht: Sitz liegt außerhalb von Bremen."},
    {"name": "Nord Handwerk IT", "personen": "", "email": "", "sitz": "Bremerhaven",
     "warum": "Interne IT eines Handwerksbetriebs", "score": None, "begruendung": None},
]
FIRMENSATZ = "Ihre Arbeit mit Linux-Servern für den Mittelstand passt zu dem, was ich suche."


def vorbereiten() -> None:
    """Schaltet die App in einen leeren Wegwerf-Ordner. Die echten Daten bleiben dabei unberührt."""
    global ORDNER
    aufraeumen()
    ORDNER = Path(tempfile.mkdtemp(prefix="tueroeffner-demo-"))
    config.DB_PFAD = ORDNER / "demo.db"
    config.DEMO = True


def befuellen(conn) -> None:
    person.speichere(conn, name="Kim Beispiel", email="kim@beispiel.example", ort="Bremen", telefon="0000 000000")
    for kategorie, text in FAKTEN:
        profil.lege_eigenen_fakt_an(conn, kategorie, text, "Demo")
    db.setze_einstellung(conn, "vorlage", MAILTEXT)
    db.setze_einstellung(conn, "mail.modus", "vorlage")
    db.setze_einstellung(conn, "betreff", "Administratorin für Ihren Serverbetrieb")
    regeln.lege_regel_an(conn, "verboten", "Blockchain", meldung="Blockchain nicht erwähnen, das ist nicht belegt.")
    lernen.merke_regel(conn, "Kurz schreiben, ohne Floskeln.")
    ids = []
    for f in FIRMEN:
        firma_id = firmen.lege_an(conn, f["name"], f["personen"], f["email"], sitz=f["sitz"], warum=f["warum"], quelle="Demo")
        ids.append(firma_id)
        if f["score"] is not None:
            conn.execute(
                "UPDATE firmen SET ki_score = ?, ki_begruendung = ?, ki_belege = '[]', ki_ungeprueft = ?, ki_modell = 'Demo', "
                "ki_zeit = ? WHERE id = ?",
                (f["score"], f["begruendung"], json.dumps(ki.IMMER_UNGEPRUEFT, ensure_ascii=False), db.jetzt(), firma_id),
            )
    conn.commit()
    ki.erstelle_entwurf(conn, ids[0], "Demo", chat=lambda *a, **k: {"firmenbezug": FIRMENSATZ})


def aufraeumen() -> None:
    global ORDNER
    if ORDNER:
        shutil.rmtree(ORDNER, ignore_errors=True)
    ORDNER = None
