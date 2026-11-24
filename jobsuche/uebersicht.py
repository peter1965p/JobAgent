"""Startseite: Stand der Jobsuche und nächster Schritt. Gemeinsam für Desktop und Web."""

from __future__ import annotations

from . import angaben, db, lernen, nacht, person


def stand(conn) -> dict:
    z = lambda sql: conn.execute(sql).fetchone()[0]
    offen = z("SELECT count(*) FROM fakten WHERE status = 'vorgeschlagen'")
    bestaetigt = z("SELECT count(*) FROM fakten WHERE status = 'bestaetigt'")
    firmen_n = z("SELECT count(*) FROM firmen")
    bewertet = z("SELECT count(*) FROM firmen WHERE ki_score IS NOT NULL")
    entwuerfe = z("SELECT count(*) FROM entwuerfe WHERE status = 'entwurf'")
    freigegeben = z("SELECT count(*) FROM entwuerfe WHERE status = 'freigegeben'")
    s = lernen.statistik(conn)
    werte = [("Fakten bestätigt", f"{bestaetigt} ({offen} offen)"), ("Firmen bewertet", f"{bewertet} von {firmen_n}"),
             ("Entwürfe offen", entwuerfe), ("Freigegeben", freigegeben), ("Gesendet", s["gesendet"]),
             ("Antworten", s["antworten"]), ("Absagen", s["absagen"])]
    fragen = len(angaben.offen(conn))
    unterlagen = z("SELECT count(*) FROM dokumente")
    if not person.name(conn):
        text, ziel = "Willkommen! Trage zuerst deinen Namen und deine Mailadresse ein.", "profil"
    elif not unterlagen and bestaetigt == 0:
        text, ziel = ("Hast du einen Lebenslauf? Dann füge ihn unter „Unterlagen“ hinzu. "
                      "Sonst erstellt der CV-Assistent mit dir einen."), "cv"
    elif bestaetigt == 0:
        text, ziel = f"Noch kein Fakt gilt. Prüfe die {offen} offenen Fakten oder lasse dir welche vorschlagen.", "fakten"
    elif fragen:
        text, ziel = f"Die App hat noch {fragen} Fragen an dich. Das fehlt in deinen Unterlagen.", "fakten"
    elif firmen_n == 0:
        text, ziel = "Lass dir passende Firmen finden oder trage die erste selbst ein.", "suche"
    elif bewertet == 0:
        text, ziel = "Lade die Firmenseiten und lasse die Firmen bewerten.", "firmen"
    elif (bericht := nacht.letzter_bericht(conn)) and bericht["datum"][:10] == db.jetzt()[:10] and bericht["firmen"]:
        text, ziel = f"Der Morgenbericht ist da: {len(bericht['firmen'])} aussichtsreiche Firmen.", "bericht"
    elif entwuerfe:
        text, ziel = "Offene Entwürfe warten auf deine Prüfung.", "entwuerfe"
    else:
        text, ziel = "Wähle die passendste Firma und erstelle einen Entwurf.", "firmen"
    pill = {"firmen": firmen_n, "fakten": bestaetigt, "antworten": s["antworten"]}
    return {"werte": werte, "schritt": text, "ziel": ziel, "pill": pill}
