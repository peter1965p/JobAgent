"""Bewerbung über Bewerbungsportale (Personio): Link, Stellenfeed, Formular, Werte, Lesezeichen, Mappe."""
import urllib.parse
from pathlib import Path

import httpx
import pytest

from jobsuche import db, ki, person, portal

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position><id>111</id><name>Andere Stelle</name><subcompany>Beispiel GmbH</subcompany><office>Köln</office></position>
  <position>
    <id>2857377</id><subcompany>Beispiel Software GmbH</subcompany><office>Remote</office>
    <name>Senior Fullstack Developer (m/w/d) TypeScript, Node.js</name>
    <jobDescriptions>
      <jobDescription><name>Deine Rolle</name><value><![CDATA[<p>Du baust&nbsp;Dienstpläne.</p>]]></value></jobDescription>
      <jobDescription><name>Das bringst Du mit</name><value><![CDATA[<ul><li>TypeScript</li><li>PostgreSQL</li></ul>
        <p>Gehalt: 75.000 – 90.000 € pro Jahr</p>]]></value></jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>"""

FORMULAR = """<form><fieldset><div role="group" aria-labelledby="name-group-label"><span id="name-group-label">Name<span aria-hidden="true">*</span>
<span> (erforderlich)</span></span><div><label for="field-first_name">Vorname</label><input id="field-first_name" type="text" name="first_name" value=""/>
<label for="field-last_name">Nachname</label><input id="field-last_name" type="text" name="last_name" value=""/></div></div>
<label for="field-email">E-Mail<span>*</span><span> (erforderlich)</span></label><input id="field-email" type="email" name="email" value=""/>
<label for="field-phone">Telefon<span>*</span><span> (erforderlich)</span></label><input id="field-phone" type="text" name="phone" value=""/>
<label for="field-available_from">Verfügbar ab<span>*</span><span> (erforderlich)</span></label><input id="field-available_from" type="text" name="available_from"/>
<label for="field-custom_attribute_1">Gehaltsvorstellung (Brutto/Jahr bei 40 Std. Woche)<span>*</span><span> (erforderlich)</span></label>
<input id="field-custom_attribute_1" type="text" name="custom_attribute_1" value=""/>
<label for="field-custom_attribute_2">Wohnsitz<span>*</span><span> (erforderlich)</span></label>
<select name="custom_attribute_2" id="field-custom_attribute_2"><option value="" selected="">Bitte auswählen</option>
<option value="custom_option_1">Deutschland</option><option value="custom_option_2">anderes Land</option></select>
<label for="field-custom_attribute_3">Deutschlevel<span>*</span><span> (erforderlich)</span></label>
<select name="custom_attribute_3" id="field-custom_attribute_3"><option value="">Bitte auswählen</option>
<option value="custom_option_9">Muttersprache</option><option value="custom_option_10">C2</option></select>
<label for="field-custom_attribute_4">Lieblingsfarbe<span>*</span><span> (erforderlich)</span></label>
<input id="field-custom_attribute_4" type="text" name="custom_attribute_4"/>
<input type="hidden" name="token" value="x"/>
<div role="group" aria-labelledby="doc-label-cv"><div id="doc-label-cv">Lebenslauf<span>*</span><span> (erforderlich)</span></div>
<input id="doc-input-cv" type="file" aria-label="Upload Lebenslauf" name="documents.cv"/></div>
<div role="group" aria-labelledby="doc-label-cover-letter"><div id="doc-label-cover-letter">Anschreiben<span> (Optional)</span></div>
<input id="doc-input-cover-letter" type="file" aria-label="Upload Anschreiben" name="documents.cover-letter"/></div>
<div role="group" aria-labelledby="doc-label-employment-reference"><div id="doc-label-employment-reference">Arbeitszeugnis<span> (Optional)</span></div>
<input id="doc-input-employment-reference" type="file" aria-label="Upload Arbeitszeugnis" name="documents.employment-reference"/></div>
</fieldset></form>"""

LINK = "https://beispiel.jobs.personio.de/job/2857377/apply?display=de&language=de&pid=abc&_ghcid=xyz"


def _client():
    def antwort(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/xml":
            return httpx.Response(200, text=FEED)
        if request.url.path == "/job/2857377/apply":
            return httpx.Response(200, text=FORMULAR)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(antwort))


@pytest.fixture
def mit_person(conn):
    person.speichere(conn, name="Erika Muster", email="erika@example.org", telefon="0123 456", ort="Musterstadt (Eifel)", links="")
    return conn


def test_link_wird_erkannt_und_fremde_nicht():
    assert portal.erkenne(LINK) == {"anbieter": "personio", "basis": "https://beispiel.jobs.personio.de", "id": "2857377", "slug": "beispiel"}
    assert portal.erkenne("https://www.linkedin.com/jobs/view/123/") is None
    with pytest.raises(portal.PortalFehler, match="Personio"):
        portal.lade("https://example.org/job/1", _client())


def test_stelle_und_formular_werden_gelesen():
    a = portal.lade(LINK, _client())
    assert (a.titel, a.firma, a.ort) == ("Senior Fullstack Developer (m/w/d) TypeScript, Node.js", "Beispiel Software GmbH", "Remote")
    assert "Du baust Dienstpläne." in a.text and "- TypeScript" in a.text and "## Das bringst Du mit" in a.text
    namen = [f.name for f in a.felder]
    assert namen[:5] == ["first_name", "last_name", "email", "phone", "available_from"]
    assert "token" not in namen
    wohnsitz = next(f for f in a.felder if f.name == "custom_attribute_2")
    assert wohnsitz.art == "auswahl" and wohnsitz.pflicht and ("custom_option_1", "Deutschland") in wohnsitz.optionen
    dateien = {f.name: (f.label, f.pflicht) for f in a.felder if f.art == "datei"}
    assert dateien == {"documents.cv": ("Lebenslauf", True), "documents.cover-letter": ("Anschreiben", False),
                       "documents.employment-reference": ("Arbeitszeugnis", False)}


def test_besetzte_stelle_meldet_sich():
    with pytest.raises(portal.PortalFehler, match="besetzt"):
        portal.stelle_aus_feed(FEED, "999")


def test_werte_aus_profil_und_regeln(mit_person):
    a = portal.lade(LINK, _client())
    werte, offen = portal.werte(mit_person, a)
    assert werte["first_name"][0] == "Erika" and werte["last_name"][0] == "Muster"
    assert werte["email"][0] == "erika@example.org" and werte["phone"][0] == "0123 456"
    assert werte["available_from"][0] == "ab sofort"
    assert werte["custom_attribute_1"][0] == "77.000 €"          # niedrigster Wert 75.000 € plus 2.000 €
    assert werte["custom_attribute_2"] == ("custom_option_1", "Deutschland")
    assert werte["custom_attribute_3"] == ("custom_option_9", "Muttersprache")
    assert offen == ["Lieblingsfarbe"]                          # unbekannte Pflichtfelder rät die App nicht


def test_gehalt_aus_treffer_geht_vor_und_ohne_angabe_bleibt_es_offen(mit_person):
    assert portal.gehaltswunsch(mit_person, "55.000 €-70.000 €/Jahr", "Gehalt 75.000 € pro Jahr") == "57.000 €"
    db.setze_einstellung(mit_person, "bewerbung.gehalt_aufschlag", "3000")
    assert portal.gehaltswunsch(mit_person, "", "4.000 € brutto pro Monat") == "51.000 €"
    assert portal.gehaltswunsch(mit_person, "", "Wir bieten ein faires Gehalt.") == ""


def test_lesezeichen_enthaelt_die_werte_und_sendet_nichts(mit_person):
    a = portal.lade(LINK, _client())
    werte, _ = portal.werte(mit_person, a)
    js = urllib.parse.unquote(portal.lesezeichen(werte, a.felder))
    assert js.startswith("javascript:(()=>{")
    assert '"custom_attribute_2": "custom_option_1"' in js and '"available_from": "ab sofort"' in js
    assert "submit" not in js.lower() and "click(" not in js


def test_mappe_liegt_vollstaendig_im_ordner(mit_person, tmp_path):
    cv = tmp_path / "CV.pdf"; cv.write_bytes(b"%PDF-1.4 cv")
    zeugnis = tmp_path / "Zeugnis.pdf"; zeugnis.write_bytes(b"%PDF-1.4 z")
    mit_person.execute("INSERT INTO dokumente (pfad, titel, sha256, eingelesen, art, anhang) VALUES (?, 'CV', 'a', '', 'lebenslauf', 1)", (str(cv),))
    mit_person.execute("INSERT INTO dokumente (pfad, titel, sha256, eingelesen, art, anhang) VALUES (?, 'Z', 'b', '', 'zeugnis', 1)", (str(zeugnis),))
    mit_person.commit()
    a = portal.lade(LINK, _client())
    m = portal.baue_mappe(mit_person, a, "Hallo Team,\n\nich bewerbe mich.\n\nViele Grüße\nErika Muster", basis=tmp_path / "Bewerbungen")
    ordner = m["ordner"]
    assert ordner == tmp_path / "Bewerbungen" / "Beispiel_Software_GmbH_Senior_Fullstack_Developer_TypeScript"
    namen = sorted(p.name for p in ordner.iterdir())
    assert "CV.pdf" in namen and "Zeugnis.pdf" in namen and "Stelle.md" in namen and "Lesezeichen.txt" in namen
    assert any(n.startswith("Anschreiben_") and n.endswith(".pdf") for n in namen)
    assert any(n.startswith("Anschreiben_") and n.endswith(".docx") for n in namen)
    formular = (ordner / "Formular.md").read_text()
    assert "| Lebenslauf* | CV.pdf |" in formular and "| Arbeitszeugnis | Zeugnis.pdf |" in formular
    assert "| Lieblingsfarbe* | **OFFEN" in formular and "selbst senden" in formular
    assert m["offen"] == ["Lieblingsfarbe"]


def test_bewirb_legt_firma_mit_anzeige_an_und_schreibt_ueber_die_pipeline(mit_person, tmp_path, monkeypatch):
    gesehen = {}

    def kern(conn, firma_id, modell, chat, stil=""):
        gesehen["anzeige"] = ki.anzeige_der_firma(conn, firma_id)[0]
        return "ich bewerbe mich auf die Stelle.", "Bewerbung", []
    monkeypatch.setattr(ki, "_schreibe_kern", kern)
    m = portal.bewirb(mit_person, LINK, "test", client=_client(), basis=tmp_path, eignung_pruefen=False)
    firma = mit_person.execute("SELECT name, quelle FROM firmen WHERE id = ?", (m["firma_id"],)).fetchone()
    assert firma["name"] == "Beispiel Software GmbH" and "personio:beispiel:2857377" in firma["quelle"]
    assert "Dienstpläne" in gesehen["anzeige"]                    # Anzeige ist für Eignung und Anschreiben auffindbar
    assert Path(m["ordner"]).is_dir() and m["werte"]["custom_attribute_1"][0] == "77.000 €"
    # Ein zweiter Lauf legt die Firma nicht doppelt an
    portal.bewirb(mit_person, LINK, "test", client=_client(), basis=tmp_path, eignung_pruefen=False)
    assert mit_person.execute("SELECT count(*) FROM firmen WHERE name = 'Beispiel Software GmbH'").fetchone()[0] == 1
