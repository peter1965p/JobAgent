from jobsuche import analyse, db, ki, profil, rag


def _firma(conn, name, ki_score=None, mein_score=None):
    return conn.execute(
        "INSERT INTO firmen (name, email, ki_score, mein_score) VALUES (?, 'a@b.de', ?, ?)", (name, ki_score, mein_score)
    ).lastrowid


def _mail(conn, firma, status, ki_text="eins zwei drei vier", text="eins zwei drei vier"):
    return conn.execute(
        "INSERT INTO entwuerfe (firma_id, empfaenger, betreff, text, ki_text, modell, status, erstellt, freigegeben_am) "
        "VALUES (?, 'a@b.de', 'b', ?, ?, 'm', ?, ?, ?)", (firma, text, ki_text, status, db.jetzt(), db.jetzt())
    ).lastrowid


def test_auswertungen_laufen_auf_leerer_datenbank(conn):
    assert analyse.trichter(conn)["Anzahl"].sum() == 0
    assert analyse.bewertungsguete(conn)["paare"] == 0
    assert analyse.korrekturgrad(conn).empty
    assert analyse.ergebnis_nach_score(conn)["gesendet"].sum() == 0
    assert analyse.firmenabdeckung(conn).empty


def test_trichter_zaehlt_firmen_nicht_doppelt(conn):
    a = _firma(conn, "A")
    _mail(conn, a, "verworfen")
    _mail(conn, a, "antwort")
    t = analyse.trichter(conn).set_index("Schritt")["Anzahl"]
    assert (t["Entwurf erstellt"], t["Gesendet"], t["Antwort"]) == (1, 1, 1)


def test_bewertungsguete(conn):
    _firma(conn, "A", 90, 80)
    _firma(conn, "B", 50, 30)
    _firma(conn, "C", 10, 10)
    _firma(conn, "D", 77, None)
    g = analyse.bewertungsguete(conn)
    assert g == {"paare": 3, "mittlere_abweichung": 10.0, "rangkorrelation": 1.0}


def test_korrekturgrad(conn):
    a = _firma(conn, "A")
    _mail(conn, a, "entwurf")
    _mail(conn, a, "entwurf", text="ganz anderer text hier")
    assert analyse.korrekturgrad(conn)["aenderung_prozent"].tolist() == [0.0, 100.0]


def test_ergebnis_nach_score_baender(conn):
    hoch, tief = _firma(conn, "A", 85), _firma(conn, "B", 20)
    _mail(conn, hoch, "antwort")
    _mail(conn, tief, "absage")
    t = analyse.ergebnis_nach_score(conn).set_index("band")
    assert t.loc["80–100", "antwortquote_prozent"] == 100
    assert t.loc["0–39", "absagen"] == 1


def test_suchqualitaet_misst_alle_modi(conn):
    profil.lege_fakt_an(conn, "technik", "SCCM und Imaging im Rollout", "t")
    profil.lege_fakt_an(conn, "technik", "Vue und Nuxt im Frontend", "t")
    uebersicht, details = analyse.suchqualitaet(conn, [{"frage": "SCCM", "erwartet": "SCCM"}], k=1)
    assert set(uebersicht["modus"]) == {"vektor", "wort", "hybrid"}
    assert uebersicht.set_index("modus").loc["wort", "treffer_auf_1"] == 1.0
    assert len(details) == 3
