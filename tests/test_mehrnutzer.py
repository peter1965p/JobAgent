"""Die App muss für jede Person funktionieren, nicht nur für eine."""

from pathlib import Path

import pytest
from pypdf import PdfReader

from jobsuche import config, cv, db, firmen, ki, person, profil, regeln

GUT = {"betreff": "Bewerbung", "text": "Ich suche eine neue Stelle."}


class FakeLLM:
    def __init__(self, antwort=GUT):
        self.antwort = antwort
        self.prompts = []

    def __call__(self, modell, system, nutzer, schema, temperatur=0.3):
        self.prompts.append(system + "\n" + nutzer)
        return self.antwort


def test_kein_personenbezug_im_code():
    wurzel = Path(__file__).resolve().parent.parent / "jobsuche"
    verboten = ("Peter", "Päffgen", "paeffgen", "plexora", "Hemmersbach", "/home/peter")
    for datei in wurzel.rglob("*.py"):
        text = datei.read_text(encoding="utf-8")
        for wort in verboten:
            assert wort.lower() not in text.lower(), f"{wort} steht in {datei.name}"


def test_leere_installation_kennt_niemanden():
    conn = db.verbinden(":memory:")
    assert person.lade(conn)["name"] == ""
    assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM fakten").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM regeln").fetchone()[0] == 0


@pytest.mark.parametrize("werte", [{"name": "", "email": "a@b.de"}, {"name": "A", "email": "kein-at"}])
def test_profil_braucht_name_und_mail(conn, werte):
    with pytest.raises(ValueError):
        person.speichere(conn, **werte)


def test_entwurf_ohne_profil_wird_abgewiesen():
    conn = db.verbinden(":memory:")
    fid = firmen.lege_an(conn, "X GmbH", email="a@x.de")
    with pytest.raises(ki.NichtMoeglich, match="Profil"):
        ki.erstelle_entwurf(conn, fid, "m", chat=FakeLLM())


def _fakt(conn, text="Zehn Jahre Support im Betrieb"):
    profil.lege_eigenen_fakt_an(conn, "erfahrung", text)


def test_entwurf_traegt_namen_der_person_und_kommt_ohne_ansprechperson_aus(conn):
    _fakt(conn)
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    llm = FakeLLM()
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=llm)
    e = conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (eid,)).fetchone()
    assert e["text"].startswith("Guten Tag,") and e["text"].endswith("Test Person")
    assert not e["betreff"].startswith("z. Hd.")
    assert "Ich-Form von Test Person" in llm.prompts[0]


def test_ohne_vorlage_schreibt_das_modell_die_mail_und_15_minuten_erlaubt(conn):
    _fakt(conn)
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    llm = FakeLLM({"betreff": "b", "text": "Hätten Sie 15 Minuten Zeit?"})
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=llm)
    assert "Hätten Sie 15 Minuten" in conn.execute("SELECT text FROM entwuerfe").fetchone()[0]
    assert not regeln.gesperrt(ki.pruefe_entwurf(conn, eid))


def test_mit_vorlage_bleibt_der_text_woertlich_und_das_modell_sieht_keine_fakten(conn):
    _fakt(conn, "Geheimer Fakt ueber den Bewerber")
    db.setze_einstellung(conn, "vorlage", "Ich habe 42 Server betreut.")
    db.setze_einstellung(conn, "mail.modus", "vorlage")
    db.setze_einstellung(conn, "betreff", "Mein Betreff")
    fid = firmen.lege_an(conn, "X GmbH", personen="Kim Test", email="info@x.de", warum="KI-Beratung")
    llm = FakeLLM({"firmenbezug": "Ihre KI-Beratung passt zu meiner Suche.", "betreff": "Böse", "text": "Böse"})
    # „meiner“ ist eine Aussage über den Absender, also fällt der Satz durch.
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=llm)
    e = conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (eid,)).fetchone()
    assert e["text"] == (f"Guten Tag Kim Test,\n\n{ki.BEZUG_PLATZHALTER}\n\nIch habe 42 Server betreut.\n\n"
                         "Viele Grüße\nTest Person")
    assert e["betreff"] == "z. Hd. Kim Test: Mein Betreff" and "Geheimer Fakt" not in llm.prompts[0]
    assert regeln.gesperrt(ki.pruefe_entwurf(conn, eid))  # Der Platzhalter sperrt die Freigabe.

    conn.execute("UPDATE entwuerfe SET status = 'verworfen'")
    llm = FakeLLM({"firmenbezug": "Ihre KI-Beratung in Bremen klingt interessant. Zweiter Satz fliegt raus."})
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=llm)
    text = conn.execute("SELECT text FROM entwuerfe WHERE id = ?", (eid,)).fetchone()[0]
    assert "Ihre KI-Beratung in Bremen klingt interessant.\n\nIch habe 42" in text and "Zweiter" not in text
    assert not regeln.gesperrt(ki.pruefe_entwurf(conn, eid))


@pytest.mark.parametrize("satz", [
    "", "Ich habe 30 Jahre Erfahrung, deshalb passt das.", "Meine Erfahrung passt gut zu Ihnen.",
    " ".join(["Wort"] * 40) + ".",
])
def test_ungeeigneter_firmensatz_wird_verworfen(satz):
    assert ki._pruefe_bezug(satz) == ""


def test_erfundene_zahl_im_firmensatz_sperrt_die_freigabe(conn):
    _fakt(conn)
    db.setze_einstellung(conn, "vorlage", "Mein Text.")
    db.setze_einstellung(conn, "mail.modus", "vorlage")
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=FakeLLM({"firmenbezug": "Ihre 500 Kunden beeindrucken."}))
    assert regeln.gesperrt(ki.pruefe_entwurf(conn, eid))


def test_eigene_regel_sperrt_freigabe_und_erreicht_den_prompt(conn):
    _fakt(conn)
    regeln.lege_regel_an(conn, "verboten", "Blockchain", meldung="Blockchain nie erwähnen.")
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    llm = FakeLLM({"betreff": "b", "text": "Ich mache Blockchain."})
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=llm)
    assert "Blockchain nie erwähnen." in llm.prompts[0]
    with pytest.raises(ki.NichtMoeglich, match="Blockchain"):
        ki.gib_frei(conn, eid)
    regel = regeln.lade_eigene(conn)[0]
    regeln.loesche_regel(conn, regel["id"])
    assert not regeln.gesperrt(ki.pruefe_entwurf(conn, eid))


@pytest.mark.parametrize("art,wert,zweiter", [("muster", "(kaputt", ""), ("zusammen", "a", ""), ("verboten", "  ", ""), ("gibtsnicht", "a", "")])
def test_ungueltige_regeln_werden_abgewiesen(conn, art, wert, zweiter):
    with pytest.raises(ValueError):
        regeln.lege_regel_an(conn, art, wert, zweiter)
    assert regeln.lade_eigene(conn) == []


def test_begriff_trifft_nur_ganze_woerter():
    regel = {"art": "verboten", "wert": "Java", "zweiter": "", "meldung": ""}
    assert regeln.verstoesse(regel, "Ich kann java.") and not regeln.verstoesse(regel, "Ich kann JavaScript.")


# --- Firmen ---

def test_firma_anlegen_leitet_website_ab_und_verhindert_doppelte(conn):
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    assert conn.execute("SELECT website FROM firmen WHERE id = ?", (fid,)).fetchone()[0] == "https://www.x.de"
    with pytest.raises(ValueError, match="schon"):
        firmen.lege_an(conn, "X GmbH")
    assert firmen.lege_an(conn, "Y AG", website="y.de") and conn.execute(
        "SELECT website FROM firmen WHERE name = 'Y AG'").fetchone()[0] == "https://y.de"


def test_firma_ohne_website_gibt_klare_meldung(conn):
    fid = firmen.lege_an(conn, "Nur Name")
    with pytest.raises(RuntimeError, match="keine Website"):
        firmen.lade_seiten(conn, fid)


def test_firma_mit_gesendeter_mail_bleibt_erhalten(conn):
    _fakt(conn)
    fid = firmen.lege_an(conn, "X GmbH", email="info@x.de")
    eid = ki.erstelle_entwurf(conn, fid, "m", chat=FakeLLM())
    ki.gib_frei(conn, eid)
    ki.setze_status(conn, eid, "gesendet")
    with pytest.raises(ValueError, match="Auswertung"):
        firmen.entferne(conn, fid)
    frei = firmen.lege_an(conn, "Z GmbH")
    firmen.entferne(conn, frei)
    assert conn.execute("SELECT count(*) FROM firmen").fetchone()[0] == 1


# --- Unterlagen ---

def test_anhaenge_kommen_aus_der_datenbank(conn, tmp_path):
    a, b = tmp_path / "cv.txt", tmp_path / "notiz.txt"
    a.write_text("x" * 200, encoding="utf-8")
    b.write_text("y" * 200, encoding="utf-8")
    profil.fuege_unterlage_hinzu(conn, a, "lebenslauf", True)
    did = profil.fuege_unterlage_hinzu(conn, b, "sonstiges", False)
    assert profil.anhaenge(conn) == [a]
    profil.setze_anhang(conn, did, True)
    assert set(profil.anhaenge(conn)) == {a, b}


def test_problemfaelle_beim_einlesen(conn, tmp_path):
    with pytest.raises(ValueError):
        profil.fuege_unterlage_hinzu(conn, tmp_path / "gibtsnicht.pdf", "lebenslauf", True)
    kaputt = tmp_path / "kaputt.pdf"
    kaputt.write_bytes(b"kein pdf")
    leer = tmp_path / "leer.txt"
    leer.write_text("kurz", encoding="utf-8")
    weg = tmp_path / "weg.txt"
    weg.write_text("z" * 200, encoding="utf-8")
    for pfad in (kaputt, leer, weg):
        profil.fuege_unterlage_hinzu(conn, pfad, "lebenslauf", True)
    weg.unlink()
    probleme = " ".join(profil.lies_unterlagen(conn))
    assert "nicht lesbar" in probleme and "vermutlich ein Scan" in probleme and "Datei fehlt" in probleme


def test_geaenderte_datei_wird_neu_eingelesen(conn, tmp_path):
    a = tmp_path / "cv.txt"
    a.write_text("Erste Fassung " * 20, encoding="utf-8")
    profil.fuege_unterlage_hinzu(conn, a, "lebenslauf", True)
    profil.lies_unterlagen(conn)
    a.write_text("Zweite Fassung " * 20, encoding="utf-8")
    profil.lies_unterlagen(conn)
    texte = " ".join(z[0] for z in conn.execute("SELECT text FROM chunks"))
    assert "Zweite" in texte and "Erste" not in texte


def test_ki_vorschlaege_gesammelt_bestaetigen_nur_diese(conn):
    profil.lege_fakt_an(conn, "technik", "Vorschlag der KI", "KI-Vorschlag aus: CV")
    profil.lege_fakt_an(conn, "technik", "Vorschlag mit Gehalt 50.000", "KI-Vorschlag aus: CV")
    profil.lege_fakt_an(conn, "positionierung", "Offene Positionierung", "Notiz")
    assert profil.bestaetige_ki_vorschlaege(conn) == 1
    status = dict(tuple(z) for z in conn.execute("SELECT text, status FROM fakten"))
    assert status["Vorschlag der KI"] == "bestaetigt"
    assert status["Vorschlag mit Gehalt 50.000"] == "vorgeschlagen"
    assert status["Offene Positionierung"] == "vorgeschlagen"


# --- CV-Assistent ---

def _alles_beantworten(conn):
    cv.fuege_station_hinzu(conn, "beruf", "Systemadministrator", "Muster AG", "Bremen", "2018", "heute",
                           "50 Server betreut\nBackups & Monitoring <aufgebaut>")
    cv.station_abschliessen(conn, "beruf")
    cv.fuege_station_hinzu(conn, "ausbildung", "Fachinformatiker", "BBS Bremen")
    cv.station_abschliessen(conn, "ausbildung")
    cv.beantworte(conn, "profil", "Admin mit zehn Jahren Erfahrung.")
    cv.beantworte(conn, "kenntnisse", "Linux, Python")
    cv.beantworte(conn, "sprachen", "Deutsch, Englisch")
    cv.beantworte(conn, "praktisch", "Führerschein Klasse B")


def _pdf_text(pfad):
    return " ".join(" ".join(s.extract_text().split()) for s in PdfReader(str(pfad)).pages)


def test_assistent_fragt_der_reihe_nach():
    conn = db.verbinden(":memory:")
    assert cv.naechster_schritt(conn).art == "kontakt"
    person.speichere(conn, name="A B", email="a@b.de")
    s = cv.naechster_schritt(conn)
    assert (s.art, s.schluessel) == ("station", "beruf") and "aktuelle oder letzte" in s.frage
    cv.fuege_station_hinzu(conn, "beruf", "Admin")
    assert "davor" in cv.naechster_schritt(conn).frage
    cv.station_abschliessen(conn, "beruf")
    assert cv.naechster_schritt(conn).schluessel == "ausbildung"
    cv.station_abschliessen(conn, "ausbildung")
    assert cv.naechster_schritt(conn).schluessel == "profil"
    for schritt in cv.TEXTE:
        cv.beantworte(conn, schritt.schluessel, "")
    assert cv.naechster_schritt(conn) is None


def test_cv_erst_wenn_alles_beantwortet(conn, tmp_path):
    with pytest.raises(ValueError, match="offene Fragen"):
        cv.erstelle(conn, tmp_path / "cv.pdf")


def test_cv_pdf_enthaelt_nur_die_antworten(conn, tmp_path):
    _alles_beantworten(conn)
    text = _pdf_text(cv.erstelle(conn, tmp_path / "cv.pdf"))
    for teil in ("Test Person", "test@example.org", "Systemadministrator", "Muster AG", "50 Server betreut",
                 "Backups & Monitoring <aufgebaut>", "Fachinformatiker", "Linux, Python", "Führerschein"):
        assert teil in text


def test_uebersprungene_abschnitte_fehlen_im_cv(conn, tmp_path):
    _alles_beantworten(conn)
    cv.beantworte(conn, "sprachen", "")
    assert "Sprachen" not in _pdf_text(cv.erstelle(conn, tmp_path / "cv.pdf"))


def test_erstellen_aendert_weder_fakten_noch_anhaenge(conn, tmp_path):
    vorhanden = tmp_path / "alt.txt"
    vorhanden.write_text("x" * 200, encoding="utf-8")
    profil.fuege_unterlage_hinzu(conn, vorhanden, "lebenslauf", True)
    _alles_beantworten(conn)
    cv.erstelle(conn, tmp_path / "cv.pdf")
    assert profil.anhaenge(conn) == [vorhanden]
    assert conn.execute("SELECT count(*) FROM fakten").fetchone()[0] == 0


def test_uebernehmen_macht_fakten_und_ersetzt_den_anhang(conn, tmp_path):
    vorhanden = tmp_path / "alt.txt"
    vorhanden.write_text("x" * 200, encoding="utf-8")
    zeugnis = tmp_path / "zeugnis.txt"
    zeugnis.write_text("y" * 200, encoding="utf-8")
    profil.fuege_unterlage_hinzu(conn, vorhanden, "lebenslauf", True)
    profil.fuege_unterlage_hinzu(conn, zeugnis, "zeugnis", True)
    _fakt(conn, "Eigener Fakt bleibt")
    _alles_beantworten(conn)
    pfad = cv.erstelle(conn, tmp_path / "cv.pdf")
    assert cv.uebernehmen(conn, pfad) == []
    assert set(profil.anhaenge(conn)) == {zeugnis, pfad}
    texte = {z["text"]: z for z in conn.execute("SELECT * FROM fakten")}
    assert "Eigener Fakt bleibt" in texte
    fakt = next(t for t in texte if t.startswith("Systemadministrator"))
    assert "Muster AG (2018 – heute)" in fakt and "50 Server betreut." in fakt
    assert all(z["status"] == "bestaetigt" for z in texte.values())


def test_wiederholtes_uebernehmen_macht_keine_doppelten_fakten(conn, tmp_path):
    _alles_beantworten(conn)
    pfad = cv.erstelle(conn, tmp_path / "cv.pdf")
    cv.uebernehmen(conn, pfad)
    anzahl = conn.execute("SELECT count(*) FROM fakten").fetchone()[0]
    cv.verwerfe(conn, "text:sprachen")
    cv.beantworte(conn, "sprachen", "Französisch")
    cv.uebernehmen(conn, cv.erstelle(conn, tmp_path / "cv.pdf"))
    texte = [z[0] for z in conn.execute("SELECT text FROM fakten")]
    assert len(texte) == anzahl and "Sprachen: Französisch" in texte and "Sprachen: Deutsch, Englisch" not in texte


def test_gesundheit_wird_abgewiesen(conn):
    with pytest.raises(ValueError):
        cv.beantworte(conn, "praktisch", "Wegen Bandscheibe nichts Schweres")
    with pytest.raises(ValueError):
        cv.fuege_station_hinzu(conn, "beruf", "Admin", details="Krankheitsbedingt beendet")


def test_verworfene_station_wird_neu_erfragt(conn):
    _alles_beantworten(conn)
    assert cv.naechster_schritt(conn) is None
    sid = cv.stationen(conn, "beruf")[0]["id"]
    cv.verwerfe(conn, f"station:{sid}")
    assert cv.naechster_schritt(conn).schluessel == "beruf"


def test_config_enthaelt_keine_person():
    assert not [n for n in dir(config) if n in ("ABSENDER", "ABSENDER_NAME", "ANHAENGE", "UNTERLAGEN", "VORLAGE", "PROTOKOLL")]


def test_assistent_fragt_beantwortetes_nicht_doppelt(conn, tmp_path):
    from jobsuche import angaben
    _alles_beantworten(conn)
    cv.uebernehmen(conn, cv.erstelle(conn, tmp_path / "cv.pdf"))
    offen = [f.schluessel for f in angaben.offen(conn)]
    assert "fuehrerschein" not in offen and "koerperlich" not in offen
    assert "reisen" in offen and "gehalt" in offen  # Danach hat der Assistent nicht gefragt.
    assert "Führerschein" not in angaben.interne_kriterien(conn)


def test_textausschnitt_aus_unterlage_bleibt_vorschlag(conn, tmp_path):
    from jobsuche import angaben
    datei = tmp_path / "cv.txt"
    datei.write_text("Kenntnisse viele Dinge ohne Punkt und Komma Führerschein Klasse B danach noch mehr Text " * 5, encoding="utf-8")
    profil.fuege_unterlage_hinzu(conn, datei, "lebenslauf", True)
    profil.lies_unterlagen(conn)
    angaben.uebernimm_aus_unterlagen(conn)
    fakt = conn.execute("SELECT text, status FROM fakten").fetchone()
    assert fakt["status"] == "vorgeschlagen" and "Führerschein" in fakt["text"] and len(fakt["text"]) <= 100
