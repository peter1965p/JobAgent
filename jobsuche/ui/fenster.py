"""Hauptfenster und Seiten. Lange Arbeiten laufen nacheinander in einem Hintergrund-Thread."""

import html
import json
import re
import subprocess
import sys
import traceback

from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QIntValidator, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import __version__, agenda, anmeldung, angaben, config, cv, db, demo, einrichtung, firmen, ki, lernen, llm, mailkonten, nacht, person, postfach, profil, quellen, rag, regeln, suche, versand
from . import start, theme
from .anmelden import AnmeldeFenster
from .schalter import Schalter

STATUS_TEXT = {
    "entwurf": "Entwurf", "freigegeben": "Freigegeben", "gesendet": "Gesendet", "antwort": "Antwort erhalten",
    "absage": "Absage", "keine_antwort": "Keine Antwort",
}
FAKT_STATUS = {"vorgeschlagen": "offen", "bestaetigt": "bestätigt", "abgelehnt": "abgelehnt"}
KATEGORIE_TEXT = {"positionierung": "Positionierung", "erfahrung": "Erfahrung", "projekt": "Projekt",
                  "technik": "Technik", "praktisch": "Praktisch / physisch", "suche": "Suche"}


class Signale(QObject):
    """Lebt so lange wie das Hauptfenster. So überleben die Meldungen das Ende einer Aufgabe."""

    fertig = Signal(int, object)
    fehler = Signal(int, str)
    fortschritt = Signal(int, str)


class Aufgabe(QRunnable):
    def __init__(self, nummer, funktion, signale):
        super().__init__()
        self.nummer = nummer
        self.funktion = funktion
        self.signale = signale

    def run(self):
        conn = db.verbinden()
        try:
            melde = lambda text: self.signale.fortschritt.emit(self.nummer, text)
            self.signale.fertig.emit(self.nummer, self.funktion(conn, melde))
        except (ValueError, RuntimeError, ki.NichtMoeglich, llm.LLMFehler, versand.VersandFehler, suche.SucheFehler) as fehler:
            self.signale.fehler.emit(self.nummer, str(fehler))
        except Exception:
            self.signale.fehler.emit(self.nummer, traceback.format_exc(limit=-6))   # die letzten Zeilen: dort steht die Ursache
        finally:
            conn.close()


def label(text="", name=None, umbruch=False):
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(umbruch)
    w.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return w


def knopf(text, aktion, primaer=False):
    b = QPushButton(text)
    if primaer:
        b.setObjectName("primaer")
    b.clicked.connect(aktion)
    return b


class Karte(QFrame):
    def __init__(self, titel=""):
        super().__init__()
        self.setObjectName("karte")
        self.lage = QVBoxLayout(self)
        self.lage.setContentsMargins(20, 18, 20, 18)
        self.lage.setSpacing(10)
        if titel:
            self.lage.addWidget(label(titel, "karteTitel"))


class Ring(QWidget):
    """Das Symbol im Seitenkopf: orangener Ring mit Punkt."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(46, 46)
        self.farbe = QColor("#f5a623")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(self.farbe, 2))
        p.drawEllipse(2, 2, 42, 42)
        p.setPen(Qt.NoPen)
        p.setBrush(self.farbe)
        p.drawEllipse(15, 15, 16, 16)


def leere(lage):
    """Entfernt Widgets sofort. deleteLater allein ließe sie bis zum nächsten Event sichtbar liegen."""
    while lage.count():
        w = lage.takeAt(0).widget()
        if w:
            w.hide()
            w.setParent(None)
            w.deleteLater()


def tabelle(spalten, breiten):
    """`breiten`: feste Pixelbreite je Spalte, None streckt die Spalte auf den Rest."""
    t = QTableWidget(0, len(spalten))
    t.setHorizontalHeaderLabels(spalten)
    t.verticalHeader().hide()
    t.setShowGrid(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setWordWrap(True)
    t.setTextElideMode(Qt.ElideNone)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    kopf = t.horizontalHeader()
    kopf.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for spalte, breite in enumerate(breiten):
        if breite is None:
            kopf.setSectionResizeMode(spalte, QHeaderView.Stretch)
        else:
            kopf.setSectionResizeMode(spalte, QHeaderView.Fixed)
            t.setColumnWidth(spalte, breite)
    # Erst die endgültige Spaltenbreite bestimmt die nötige Zeilenhöhe.
    kopf.sectionResized.connect(lambda *_: t.resizeRowsToContents())
    return t


def fuelle(t: QTableWidget, zeilen: list[tuple], ids: list[int]):
    """Füllt die Tabelle und behält die Auswahl über die Datensatz-ID."""
    auswahl = zeilen_id(t)
    sortiert = t.isSortingEnabled()
    t.setSortingEnabled(False)
    t.blockSignals(True)
    t.setRowCount(len(zeilen))
    for r, (zeile, datensatz) in enumerate(zip(zeilen, ids)):
        for s, wert in enumerate(zeile):
            item = QTableWidgetItem()
            item.setData(Qt.DisplayRole, wert if isinstance(wert, int) else str(wert if wert is not None else ""))
            item.setData(Qt.UserRole, datensatz)
            t.setItem(r, s, item)
    t.setSortingEnabled(sortiert)
    if auswahl is not None:
        for r in range(t.rowCount()):
            if t.item(r, 0).data(Qt.UserRole) == auswahl:
                t.selectRow(r)
    t.blockSignals(False)
    t.resizeRowsToContents()


def zeilen_id(t: QTableWidget):
    zeile = t.currentRow()
    if zeile < 0 or not t.item(zeile, 0) or not t.selectedItems():
        return None
    return t.item(zeile, 0).data(Qt.UserRole)


class Seite(QWidget):
    titel = ""
    untertitel = ""

    def __init__(self, f: "Hauptfenster"):
        super().__init__()
        self.f = f

    @property
    def conn(self):
        return self.f.conn

    def aktualisieren(self):
        pass


class StartSeite(Seite):
    titel = "Start"
    untertitel = "Stand der Jobsuche und nächster Schritt"

    def __init__(self, f):
        super().__init__(f)
        raster = QGridLayout(self)
        raster.setContentsMargins(0, 0, 0, 0)
        raster.setSpacing(18)
        self.stand = Karte("Stand")
        self.stand_raster = QGridLayout()
        self.stand_raster.setHorizontalSpacing(30)
        self.stand.lage.addLayout(self.stand_raster)
        self.schritt = Karte("Nächster Schritt")
        self.schritt_text = label("", umbruch=True)
        self.schritt.lage.addWidget(self.schritt_text)
        self.schritt.lage.addStretch()
        self.ziel = knopf("Öffnen", self.oeffne_ziel, True)
        self.schritt.lage.addWidget(self.ziel)
        self.ziel_seite = None
        self.privat = Karte("So bleibt es kontrolliert")
        self.privat.lage.addWidget(label(
            "Alles läuft lokal: Datenbank, Suche und LLM. Mailentwürfe sehen nur Fakten, die du bestätigt hast, "
            "und öffentliche Firmentexte. Ohne deine Freigabe wird nichts als sendefertig markiert.",
            "gedimmt", True))
        raster.addWidget(self.stand, 0, 0)
        raster.addWidget(self.schritt, 0, 1)
        raster.addWidget(self.privat, 1, 0, 1, 2)
        raster.setColumnStretch(0, 3)
        raster.setColumnStretch(1, 2)
        raster.setRowStretch(2, 1)

    def oeffne_ziel(self):
        self.f.zeige_seite(self.ziel_seite)

    def aktualisieren(self):
        z = lambda sql: self.conn.execute(sql).fetchone()[0]
        offen = z("SELECT count(*) FROM fakten WHERE status = 'vorgeschlagen'")
        bestaetigt = z("SELECT count(*) FROM fakten WHERE status = 'bestaetigt'")
        firmen_n = z("SELECT count(*) FROM firmen")
        bewertet = z("SELECT count(*) FROM firmen WHERE ki_score IS NOT NULL")
        entwuerfe = z("SELECT count(*) FROM entwuerfe WHERE status = 'entwurf'")
        freigegeben = z("SELECT count(*) FROM entwuerfe WHERE status = 'freigegeben'")
        s = lernen.statistik(self.conn)
        werte = [("Fakten bestätigt", f"{bestaetigt} ({offen} offen)"), ("Firmen bewertet", f"{bewertet} von {firmen_n}"),
                 ("Entwürfe offen", entwuerfe), ("Freigegeben", freigegeben), ("Gesendet", s["gesendet"]),
                 ("Antworten", s["antworten"]), ("Absagen", s["absagen"])]
        leere(self.stand_raster)
        for r, (name, wert) in enumerate(werte):
            self.stand_raster.addWidget(label(name, "gedimmt"), r, 0)
            self.stand_raster.addWidget(label(str(wert)), r, 1)
        fragen = len(angaben.offen(self.conn))
        unterlagen = z("SELECT count(*) FROM dokumente")
        if not person.name(self.conn):
            text, ziel = "Willkommen! Trage zuerst deinen Namen und deine Mailadresse ein.", ProfilSeite
        elif not unterlagen and bestaetigt == 0:
            text, ziel = ("Hast du einen Lebenslauf? Dann füge ihn unter „Unterlagen“ hinzu. "
                          "Sonst erstellt der CV-Assistent mit dir einen."), CVSeite
        elif bestaetigt == 0:
            text, ziel = f"Noch kein Fakt gilt. Prüfe die {offen} offenen Fakten oder lasse dir welche vorschlagen.", FaktenSeite
        elif fragen:
            text, ziel = f"Die App hat noch {fragen} Fragen an dich. Das fehlt in deinen Unterlagen.", FaktenSeite
        elif firmen_n == 0:
            text, ziel = "Lass dir passende Firmen finden oder trage die erste selbst ein.", SucheSeite
        elif bewertet == 0:
            text, ziel = "Lade die Firmenseiten und lasse die Firmen bewerten.", FirmenSeite
        elif (bericht := nacht.letzter_bericht(self.conn)) and bericht["datum"][:10] == db.jetzt()[:10] and bericht["firmen"]:
            text, ziel = f"Der Morgenbericht ist da: {len(bericht['firmen'])} aussichtsreiche Firmen.", BerichtSeite
        elif entwuerfe:
            text, ziel = "Offene Entwürfe warten auf deine Prüfung.", EntwurfSeite
        else:
            text, ziel = "Wähle die passendste Firma und erstelle einen Entwurf.", FirmenSeite
        self.schritt_text.setText(text)
        self.ziel_seite = ziel


def inhaber_text(e: dict) -> str:
    if e.get("fehler"):
        return f"{e['name']} steht in der Liste. {e['fehler']}"
    person = e["personen"] or "keinen Namen gefunden"
    adresse = f"{e['email']} ({e['art']})" if e["email"] else "keine Adresse veröffentlicht"
    return f"{e['name']}: {person}, {adresse}. Prüfe beides auf der Website."


class FirmenSeite(Seite):
    titel = "Firmen"
    untertitel = "Recherche, Bewertung und Entwurf pro Firma"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Firmenliste")
        self.tab = tabelle(["Firma", "Sitz", "Passung", "Score", "Mail"], [None, 125, 115, 55, 80])
        self.tab.setSortingEnabled(True)
        # Mehrere Firmen auf einmal markieren (Strg/Umschalt) und mit Entf oder Rechtsklick → Entfernen loswerden.
        self.tab.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tab.itemSelectionChanged.connect(self.zeige_detail)
        QShortcut(QKeySequence.Delete, self.tab, self.entfernen)
        self.tab.setContextMenuPolicy(Qt.ActionsContextMenu)
        aktion = QAction("Entfernen (Entf)", self.tab)
        aktion.triggered.connect(self.entfernen)
        self.tab.addAction(aktion)
        links.lage.addWidget(self.tab)
        reihe_liste = QHBoxLayout()
        reihe_liste.addWidget(knopf("Hinzufügen", self.hinzufuegen))
        reihe_liste.addWidget(knopf("Bearbeiten", self.bearbeiten))
        reihe_liste.addWidget(knopf("Im Web suchen", self.im_web_suchen))
        reihe_liste.addWidget(knopf("Entfernen", self.entfernen))
        reihe_liste.addWidget(knopf("Alle bewerten", self.alle))
        links.lage.addLayout(reihe_liste)
        lage.addWidget(links, 3)

        self.rechts = Karte("Details")
        self.detail = label("Wähle links eine Firma.", "gedimmt", True)
        self.rechts.lage.addWidget(self.detail)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Inhaber finden", self.inhaber))
        reihe.addWidget(knopf("Bewerten", self.bewerten))
        reihe.addWidget(knopf("Eignung prüfen", self.eignung))
        self.rechts.lage.addLayout(reihe)
        self.rechts.lage.addWidget(knopf("Mail vorschlagen", self.entwurf, True))
        self.rechts.lage.addWidget(label("Meine Bewertung (die KI lernt daraus)", "karteTitel"))
        reihe2 = QHBoxLayout()
        self.mein_score = QLineEdit()
        self.mein_score.setValidator(QIntValidator(0, 100, self.mein_score))
        self.mein_score.setPlaceholderText("0–100")
        self.mein_score.setFixedWidth(70)
        self.mein_score.setAlignment(Qt.AlignCenter)
        self.notiz = QLineEdit()
        self.notiz.setPlaceholderText("Warum? Zum Beispiel: zu klein, kein Bedarf")
        reihe2.addWidget(self.mein_score)
        reihe2.addWidget(self.notiz)
        self.rechts.lage.addLayout(reihe2)
        self.rechts.lage.addWidget(knopf("Bewertung speichern", self.bewertung_speichern))
        self.rechts.lage.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.rechts)
        lage.addWidget(scroll, 2)

    def aktualisieren(self):
        reihen = self.conn.execute(
            "SELECT f.*, (SELECT status FROM entwuerfe e WHERE e.firma_id = f.id AND e.status != 'verworfen' "
            "ORDER BY e.id DESC LIMIT 1) AS mail FROM firmen f ORDER BY f.id").fetchall()
        fuelle(self.tab, [(r["name"], r["sitz"], r["passung"],
                           r["mein_score"] if r["mein_score"] is not None else r["ki_score"],
                           STATUS_TEXT.get(r["mail"], "")) for r in reihen], [r["id"] for r in reihen])
        self.zeige_detail()

    def firma_id(self):
        return zeilen_id(self.tab)

    def zeige_detail(self):
        fid = self.firma_id()
        if fid is None:
            return
        r = self.conn.execute("SELECT * FROM firmen WHERE id = ?", (fid,)).fetchone()
        teile = [f"<b>{r['name']}</b><br>{r['sitz']}<br>An: {r['personen']}<br>{r['email']}<br>{r['website']}",
                 f"<br>Recherche: {r['warum']}"]
        if r["seiten_geladen"]:
            n = self.conn.execute("SELECT count(*) FROM chunks WHERE art = 'firma' AND firma_id = ?", (fid,)).fetchone()[0]
            teile.append(f"<br>Seiten geladen: {r['seiten_geladen']} ({n} Abschnitte)")
        else:
            teile.append("<br>Seiten noch nicht geladen.")
        if r["ki_score"] is not None:
            teile.append(f"<br><b>KI-Score {r['ki_score']}</b> ({r['ki_modell']}): {r['ki_begruendung']}")
            belege = json.loads(r["ki_belege"] or "[]")
            if belege:
                teile.append("<br>Belege: " + ", ".join(belege))
            ungeprueft = json.loads(r["ki_ungeprueft"] or "[]")
            if ungeprueft:
                teile.append("<br>Ungeprüft: " + "; ".join(ungeprueft))
        if r["ki_eignung"]:
            eignung = json.loads(r["ki_eignung"])
            zeilen = html.escape(ki.eignung_text(eignung)).split("\n")
            teile.append(f"<br><b>{zeilen[0]}</b> ({html.escape(eignung['modell'])}, gegen Lebenslauf und Zeugnis)<br>" + "<br>".join(zeilen[1:]))
        self.detail.setText("".join(teile))
        wert = r["mein_score"] if r["mein_score"] is not None else r["ki_score"]
        self.mein_score.setText("" if wert is None else str(wert))
        self.notiz.setText(r["meine_notiz"] or "")

    def _mit_firma(self, funktion, titel):
        fid = self.firma_id()
        if fid is None:
            self.f.melde("Wähle zuerst eine Firma.")
            return
        self.f.starte(titel, lambda conn, fort: funktion(conn, fid, fort))

    def _suche_einstellung(self):
        return db.einstellung(self.conn, "suche.land", "de"), db.einstellung(self.conn, "suche.searxng", "")

    def inhaber(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        land, searxng = self._suche_einstellung()
        self.f.starte("Inhaber finden", lambda conn, _: suche.hole_inhaber(conn, fid, land, searxng),
                      nach=lambda e: self.f.melde(inhaber_text(e)))

    def bewerten(self):
        modell = self.f.modell()
        def arbeit(c, fid, _):
            zeile = c.execute("SELECT seiten_geladen, website FROM firmen WHERE id = ?", (fid,)).fetchone()
            if not zeile["seiten_geladen"] and zeile["website"]:
                firmen.lade_seiten(c, fid)
            ki.bewerte(c, fid, modell)
        self._mit_firma(arbeit, "Firma bewerten")

    def eignung(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        modell = self.f.modell()

        def danach(eignung):
            eignung = ergaenze_mit_rueckfrage(self.f, self.conn, fid, eignung)
            self.f.melde(ki.eignung_text(eignung))
            self.f.neu_laden()
        self.f.starte("Eignung prüfen", lambda c, fort: suche.pruefe_eignung(c, fid, modell, melde=fort), nach=danach)

    def entwurf(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        modell = self.f.modell()
        land, searxng = self._suche_einstellung()

        def schreiben():
            self.f.starte("Mail vorschlagen", lambda conn, fort: suche.schlage_mail_vor(conn, fid, modell, land, searxng, fort),
                          nach=lambda _: self.f.zeige_seite(EntwurfSeite))

        def lesen(conn, fort):
            # Gibt es eine Anzeige, liest die App sie erst Punkt für Punkt gegen deine Unterlagen. Ohne Anzeige
            # (Initiativanfrage) gibt es nichts zu prüfen.
            try:
                return suche.pruefe_eignung(conn, fid, modell, melde=fort)
            except ki.NichtMoeglich:
                return None

        def danach(eignung):
            if eignung is None:
                return schreiben()
            # Unsicheres fragt die App nach, statt es in der Mail zu behaupten oder still wegzulassen.
            eignung = ergaenze_mit_rueckfrage(self.f, self.conn, fid, eignung)
            if self.f.frage(f"{ki.eignung_text(eignung)}\n\nIn die Mail kommt nur, was belegt ist. Jetzt schreiben?"):
                schreiben()
        self.f.starte("Anzeige lesen und Eignung prüfen", lesen, nach=danach)

    def hinzufuegen(self):
        werte = self._firmendialog("Firma hinzufügen", {})
        if werte:
            self.speichere_firma(werte)

    def bearbeiten(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        werte = self._firmendialog("Firma bearbeiten", dict(self.conn.execute("SELECT * FROM firmen WHERE id = ?", (fid,)).fetchone()))
        if werte:
            self.aendere_firma(fid, werte)

    def aendere_firma(self, fid, werte):
        try:
            firmen.aendere(self.conn, fid, **werte)
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()

    def im_web_suchen(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        r = self.conn.execute("SELECT name, sitz FROM firmen WHERE id = ?", (fid,)).fetchone()
        QDesktopServices.openUrl(QUrl(f"https://duckduckgo.com/?q={quote_plus(r['name'] + ' ' + r['sitz'] + ' Impressum')}"))

    def _firmendialog(self, titel, vorgabe):
        dialog = QDialog(self)
        dialog.setWindowTitle(titel)
        form = QFormLayout(dialog)
        felder = {}
        for schluessel, text, hinweis in (
            ("name", "Firma", ""), ("personen", "Ansprechperson", "Aus dem Impressum"),
            ("email", "Mailadresse", "Nur veröffentlichte Adressen"), ("website", "Website", "Leer: aus der Mailadresse"),
            ("sitz", "Sitz", ""), ("warum", "Warum passt sie?", ""),
        ):
            feld = QLineEdit(str(vorgabe.get(schluessel) or ""))
            feld.setPlaceholderText(hinweis)
            felder[schluessel] = feld
            form.addRow(text, feld)
        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.accepted.connect(dialog.accept)
        knoepfe.rejected.connect(dialog.reject)
        form.addRow(knoepfe)
        return {k: f.text() for k, f in felder.items()} if dialog.exec() == QDialog.Accepted else None

    def speichere_firma(self, werte):
        try:
            firmen.lege_an(self.conn, **werte)
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()

    def gewaehlte_firmen(self) -> list[int]:
        zeilen = sorted({i.row() for i in self.tab.selectedItems()})
        return [self.tab.item(z, 0).data(Qt.UserRole) for z in zeilen if self.tab.item(z, 0)]

    def entfernen(self):
        ids = self.gewaehlte_firmen()
        if not ids:
            return self.f.melde("Wähle zuerst eine Firma.")
        namen = [self.conn.execute("SELECT name FROM firmen WHERE id = ?", (fid,)).fetchone()[0] for fid in ids]
        frage = (f"{namen[0]} samt Seiten, Bewertung und Entwürfen aus der Liste entfernen?" if len(ids) == 1 else
                 f"{len(ids)} Firmen samt Seiten, Bewertung und Entwürfen entfernen?\n\n" + "\n".join(namen[:12]) +
                 ("\n…" if len(namen) > 12 else ""))
        if not self.f.frage(frage):
            return
        behalten = []
        for fid, name in zip(ids, namen):
            try:
                firmen.entferne(self.conn, fid)
            except ValueError:
                behalten.append(name)
        if behalten:
            self.f.melde("An diese Firmen ging schon eine Mail, sie bleiben für die Auswertung erhalten:\n" + "\n".join(behalten))
        self.f.neu_laden()

    def alle(self):
        modell = self.f.modell()
        def arbeit(conn, fort):
            ids = [r["id"] for r in conn.execute("SELECT id, seiten_geladen FROM firmen WHERE ki_score IS NULL")]
            fehler = []
            for n, fid in enumerate(ids, 1):
                name = conn.execute("SELECT name FROM firmen WHERE id = ?", (fid,)).fetchone()[0]
                fort(f"{n}/{len(ids)}: {name}")
                try:
                    zeile = conn.execute("SELECT seiten_geladen, website FROM firmen WHERE id = ?", (fid,)).fetchone()
                    if not zeile["seiten_geladen"] and zeile["website"]:
                        try:
                            firmen.lade_seiten(conn, fid)
                        except RuntimeError as e:
                            fehler.append(str(e))
                    ki.bewerte(conn, fid, modell)
                except ki.NichtMoeglich:
                    raise
                except llm.LLMFehler as e:
                    fehler.append(f"{name}: {e}")
            return fehler
        self.f.starte("Alle bewerten", arbeit, nach=lambda fehler: fehler and self.f.melde("\n".join(fehler)))

    def bewertung_speichern(self):
        fid = self.firma_id()
        if fid is None:
            return self.f.melde("Wähle zuerst eine Firma.")
        if not self.mein_score.text():
            return self.f.melde("Trage eine Bewertung von 0 bis 100 ein.")
        score, notiz = int(self.mein_score.text()), self.notiz.text()
        self.f.starte("Bewertung lernen", lambda conn, _: lernen.merke_bewertung(conn, fid, score, notiz))


class PostfachDialog(QDialog):
    """Welche Posteingänge die App nach Job-Alerts durchsieht. Passwörter gehen in den Schlüsselbund."""

    def __init__(self, parent, conn):
        super().__init__(parent)
        self.setWindowTitle("Postfächer für Job-Alerts")
        self.setMinimumWidth(620)
        lage = QVBoxLayout(self)
        lage.addWidget(label("Die App liest nur Mails von Stellenportalen der letzten zwei Wochen, schreibgeschützt: Ungelesenes bleibt "
                             "ungelesen, gelöscht oder verschoben wird nichts, Links in den Mails ruft sie nie auf. Für Gmail-Konten "
                             "mit OAuth braucht es ein App-Passwort von Google.", "gedimmt", True))
        gewaehlt = set(postfach.gewaehlte(conn))
        vorschlaege = list(dict.fromkeys(postfach.vorschlaege(conn) + list(gewaehlt)))
        raster = QGridLayout()
        self.zeilen = []
        for i, p in enumerate(vorschlaege):
            haken = Schalter(f"{p.benutzer} auf {p.host}:{p.port} ({p.sicherheit})")
            haken.setChecked(p in gewaehlt)
            kennwort = QLineEdit()
            kennwort.setEchoMode(QLineEdit.Password)
            vorhanden = bool(postfach.passwort(conn, p))
            kennwort.setPlaceholderText("Passwort gespeichert" if vorhanden else "App-Passwort" if p.oauth else "Passwort")
            raster.addWidget(haken, i, 0)
            raster.addWidget(kennwort, i, 1)
            self.zeilen.append((p, haken, kennwort))
        if not vorschlaege:
            lage.addWidget(label("Kein Postfach gefunden. Die App liest die Konten aus Thunderbird und dem Versandkonto.", "hinweis", True))
        lage.addLayout(raster)
        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        lage.addWidget(knoepfe)

    def speichere(self, conn):
        for p, haken, kennwort in self.zeilen:
            if haken.isChecked() and kennwort.text():
                postfach.merke_passwort(p, kennwort.text())
        postfach.waehle(conn, [p for p, haken, _ in self.zeilen if haken.isChecked()])


class SchluesselDialog(QDialog):
    """Schlüssel für Adzuna und Jooble. Beide sind kostenlos, verlangen aber eine Registrierung."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Schlüssel für Stellen-Aggregatoren")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        hinweis = label("Registrieren unter <a href='https://developer.adzuna.com/'>developer.adzuna.com</a> und "
                        "<a href='https://jooble.org/api/about'>jooble.org/api</a>. Die Schlüssel liegen im Schlüsselbund. "
                        "Leer lassen: unverändert. Ein einzelnes Minus löscht den Schlüssel.", "gedimmt", True)
        hinweis.setTextFormat(Qt.RichText)
        hinweis.setOpenExternalLinks(True)
        form.addRow(hinweis)
        self.felder = {}
        for name, text, vorlage in (("adzuna", "Adzuna", "app_id:app_key"), ("jooble", "Jooble", "Schlüssel")):
            feld = QLineEdit()
            feld.setEchoMode(QLineEdit.Password)
            feld.setPlaceholderText("gespeichert" if quellen.api_schluessel(name) else vorlage)
            self.felder[name] = feld
            form.addRow(text, feld)
        knoepfe = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        form.addRow(knoepfe)

    def speichere(self):
        for name, feld in self.felder.items():
            wert = feld.text().strip()
            if wert:
                quellen.merke_api_schluessel(name, "" if wert == "-" else wert)


class NachfrageDialog(QDialog):
    """Fragt nach, was die Prüfung in Lebenslauf und Zeugnis nicht gefunden hat. Leer heißt: trifft nicht zu."""

    def __init__(self, punkte: list[str], eltern=None):
        super().__init__(eltern)
        self.setWindowTitle("Was fehlt, kannst du ergänzen")
        self.resize(720, 560)
        lage = QVBoxLayout(self)
        lage.addWidget(label("Diese Anforderungen der Anzeige hat die Prüfung in deinen Unterlagen nicht oder nur teilweise "
                             "gefunden. Trifft etwas zu, beschreib es kurz, zum Beispiel wo und seit wann. Deine Antwort "
                             "wird ein bestätigter Fakt und steht als eigene Angabe im Lebenslauf für die Stelle. "
                             "Leer lassen heißt: trifft nicht zu.", "gedimmt", True))
        innen = QWidget()
        form = QVBoxLayout(innen)
        self.felder: dict[str, QLineEdit] = {}
        for punkt in punkte:
            form.addWidget(label(punkt, "", True))
            feld = QLineEdit()
            feld.setMaxLength(ki.MAX_ANTWORT)
            feld.setPlaceholderText("Trifft zu? Kurz beschreiben, sonst leer lassen")
            form.addWidget(feld)
            self.felder[punkt] = feld
        form.addStretch()
        rollen = QScrollArea()
        rollen.setWidgetResizable(True)
        rollen.setWidget(innen)
        lage.addWidget(rollen, 1)
        knoepfe = QDialogButtonBox()
        knoepfe.addButton("Übernehmen", QDialogButtonBox.AcceptRole)
        knoepfe.addButton("Überspringen", QDialogButtonBox.RejectRole)
        knoepfe.accepted.connect(self.accept)
        knoepfe.rejected.connect(self.reject)
        lage.addWidget(knoepfe)

    def antworten(self) -> dict[str, str]:
        return {punkt: feld.text().strip() for punkt, feld in self.felder.items() if feld.text().strip()}


def ergaenze_mit_rueckfrage(f, conn, firma_id: int, eignung: dict) -> dict:
    """Zeigt offene Anforderungen zum Ergänzen. Ohne Antwort bleibt die Eignung, wie sie ist."""
    punkte = ki.offene_punkte(eignung)
    if not punkte:
        return eignung
    antworten = f.nachfragen(punkte)
    if not antworten:
        return eignung
    try:
        return ki.ergaenze_eignung(conn, firma_id, antworten)
    except (ki.NichtMoeglich, ValueError) as fehler:
        f.melde(str(fehler))
        return eignung


ENTFERNEN = "entfernen:"
FORTSCHRITT = re.compile(r"(\d+)\s+von\s+(\d+)")


def km_text(t: dict):
    """Zahl für die Tabelle, damit sie sortierbar bleibt; Luftlinie mit ≈ davor."""
    if t.get("entfernung") is None:
        return ""
    return f"≈{t['entfernung']}" if t.get("luftlinie", True) else t["entfernung"]


def anhang_links(pfade, entfernbar=False) -> str:
    """Anhänge als anklickbare Links. Fehlende Dateien stehen durchgestrichen da, der Versand würde sie ablehnen.
    Mit entfernbar steht hinter jedem Anhang ein ✕, das ihn aus dieser Mail nimmt."""
    if not pfade:
        return "Anhänge: keine"
    teile = []
    for p in pfade:
        name = html.escape(p.name)
        ziel = html.escape(str(p), quote=True)
        teil = f'<a href="{ziel}">{name}</a>' if p.is_file() else f"<s>{name}</s> (fehlt)"
        if entfernbar:
            teil += f' <a href="{ENTFERNEN}{ziel}" title="Aus dieser Mail entfernen" style="text-decoration:none">✕</a>'
        teile.append(teil)
    return "Anhänge zum Ansehen anklicken: " + ", ".join(teile)


class VariantenDialog(QDialog):
    """Zeigt die Fassungen nebeneinander als Reiter. Befunde der Prüfung stehen unter jedem Text."""

    def __init__(self, varianten: list[dict], eltern=None):
        super().__init__(eltern)
        self.setWindowTitle("Welche Fassung soll es sein?")
        self.resize(860, 640)
        self.gewaehlt = None
        lage = QVBoxLayout(self)
        self.reiter = QTabWidget()
        self.texte = []
        for v in varianten:
            seite = QWidget()
            innen = QVBoxLayout(seite)
            feld = QPlainTextEdit(v["text"])  # darf hier schon angepasst werden
            innen.addWidget(feld, 1)
            befunde = v.get("befunde") or []
            if not befunde:
                innen.addWidget(label("✓ Alle Prüfungen bestanden.", "okText"))
            for b in befunde:
                innen.addWidget(label(("✕ " if b.stufe == "sperre" else "! ") + b.text, b.stufe, True))
            self.reiter.addTab(seite, v["name"] + ("" if not any(b.stufe == "sperre" for b in befunde) else " ✕"))
            self.texte.append(feld)
        lage.addWidget(self.reiter, 1)
        knoepfe = QDialogButtonBox()
        knoepfe.addButton("Diese übernehmen", QDialogButtonBox.AcceptRole)
        knoepfe.addButton("Abbrechen", QDialogButtonBox.RejectRole)
        knoepfe.accepted.connect(self._uebernehmen)
        knoepfe.rejected.connect(self.reject)
        lage.addWidget(knoepfe)

    def _uebernehmen(self):
        self.gewaehlt = self.texte[self.reiter.currentIndex()].toPlainText()
        self.accept()


class EntwurfSeite(Seite):
    titel = "Entwürfe"
    untertitel = "Lesen, ändern, freigeben. Gesendet wird nichts automatisch"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Mails")
        self.liste = QListWidget()
        self.liste.setSelectionMode(QAbstractItemView.SingleSelection)
        self.liste.itemSelectionChanged.connect(self.lade)
        links.lage.addWidget(self.liste)
        self.loeschen_knopf = knopf("Entwurf löschen", self.loeschen)
        self.loeschen_knopf.setToolTip("Nimmt die Mail aus der Liste. Sie bleibt im Protokoll, gesendet wird sie nie.")
        links.lage.addWidget(self.loeschen_knopf)
        lage.addWidget(links, 1)

        rechts = Karte("Mail")
        self.an = label("", "gedimmt", True)
        rechts.lage.addWidget(self.an)
        an_reihe = QHBoxLayout()
        an_reihe.addWidget(label("An:", "gedimmt"))
        self.empfaenger = QLineEdit()
        self.empfaenger.setPlaceholderText("Empfänger (eine Mailadresse)")
        an_reihe.addWidget(self.empfaenger, 1)
        rechts.lage.addLayout(an_reihe)
        self.gespeichert = label("", "okText")  # kurze Bestätigung nach „Speichern“
        rechts.lage.addWidget(self.gespeichert)
        self.gespeichert.hide()
        self.betreff = QLineEdit()
        self.betreff.setPlaceholderText("Betreff")
        rechts.lage.addWidget(self.betreff)
        self.text = QPlainTextEdit()
        rechts.lage.addWidget(self.text, 1)
        self.befunde = QVBoxLayout()
        rechts.lage.addLayout(self.befunde)
        self.anhang = label("", "gedimmt", True)
        # Ein Klick öffnet die Datei im Standardprogramm, damit du den Lebenslauf für die Stelle vor dem Senden siehst.
        self.anhang.setTextFormat(Qt.RichText)
        self.anhang.setTextInteractionFlags(Qt.TextBrowserInteraction)  # sonst sind die Links nicht klickbar
        self.anhang.linkActivated.connect(self._anhang_link)
        anhang_reihe = QHBoxLayout()
        anhang_reihe.addWidget(self.anhang, 1)
        self.anhang_knopf = knopf("Anhang hinzufügen …", self.anhang_hinzufuegen)
        anhang_reihe.addWidget(self.anhang_knopf)
        rechts.lage.addLayout(anhang_reihe)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Speichern", self.speichern))
        self.freigabe = knopf("Freigeben", self.freigeben, True)
        reihe.addWidget(self.freigabe)
        self.senden_knopf = knopf("Jetzt senden", self.senden, True)
        reihe.addWidget(self.senden_knopf)
        self.kopie = knopf("Kopieren", self.kopieren)
        reihe.addWidget(self.kopie)
        self.varianten_knopf = knopf("Varianten …", self.varianten)
        self.varianten_knopf.setToolTip("Drei Fassungen derselben Mail: sachlich-knapp, persönlich, projektstark. Gleiche Fakten, gleiche Prüfung.")
        reihe.addWidget(self.varianten_knopf)
        rechts.lage.addLayout(reihe)
        reihe2 = QHBoxLayout()
        reihe2.addWidget(label("Ergebnis festhalten:", "gedimmt"))
        for status, text in (("gesendet", "Gesendet"), ("antwort", "Antwort"), ("absage", "Absage"), ("keine_antwort", "Keine Antwort")):
            reihe2.addWidget(knopf(text, lambda _=False, s=status: self.status(s)))
        rechts.lage.addLayout(reihe2)
        lage.addWidget(rechts, 3)
        self.entwurf_id = None

    def aktualisieren(self):
        alt = self.entwurf_id
        reihen = self.conn.execute(
            "SELECT e.id, e.status, f.name FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id "
            "WHERE e.status IN ('entwurf', 'freigegeben') ORDER BY e.id DESC").fetchall()  # Gesendetes steht unter „Gesendet“
        self.liste.blockSignals(True)
        self.liste.clear()
        for r in reihen:
            item = QListWidgetItem(f"{r['name']}\n{STATUS_TEXT.get(r['status'], r['status'])}")
            item.setData(Qt.UserRole, r["id"])
            self.liste.addItem(item)
            if r["id"] == alt:
                self.liste.setCurrentItem(item)
        if self.liste.count() and not self.liste.selectedItems():
            self.liste.setCurrentRow(0)
        self.liste.blockSignals(False)
        self.lade()

    def lade(self):
        item = self.liste.currentItem()
        self.entwurf_id = item.data(Qt.UserRole) if item else None
        if self.entwurf_id is None:
            self.an.setText("Keine offenen Entwürfe. Erstelle einen unter „Firmen“, Gesendetes steht unter „Gesendet“.")
            self.empfaenger.clear()
            self.betreff.clear()
            self.text.clear()
            self._zeige_befunde([])
            return
        e = self.conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (self.entwurf_id,)).fetchone()
        absender = person.lade(self.conn)
        offen = e["status"] in ("entwurf", "freigegeben")
        self.anhang.setText(anhang_links(profil.anhaenge_des_entwurfs(self.conn, self.entwurf_id), entfernbar=offen))
        self.anhang_knopf.setEnabled(offen)
        self.varianten_knopf.setEnabled(offen)
        self.an.setText(f"Von: {absender['name']} <{absender['email']}>\n"
                        f"Status: {STATUS_TEXT.get(e['status'], e['status'])} · Modell: {e['modell']}")
        self.empfaenger.setText(e["empfaenger"])
        self.betreff.setText(e["betreff"])
        self.text.setPlainText(e["text"])
        aenderbar = e["status"] in ("entwurf", "freigegeben")
        self.empfaenger.setReadOnly(not aenderbar)
        self.betreff.setReadOnly(not aenderbar)
        self.text.setReadOnly(not aenderbar)
        self.freigabe.setEnabled(e["status"] == "entwurf")
        self.kopie.setEnabled(e["status"] != "entwurf")
        self.senden_knopf.setEnabled(e["status"] == "freigegeben")
        self._zeige_befunde(ki.pruefe_entwurf(self.conn, self.entwurf_id))

    def loeschen(self):
        if self.entwurf_id is None:
            return
        e = self.conn.execute("SELECT f.name FROM entwuerfe e JOIN firmen f ON f.id = e.firma_id WHERE e.id = ?",
                              (self.entwurf_id,)).fetchone()
        if not self.f.frage(f"Den Entwurf an {e['name']} löschen?\n\nEr verschwindet aus der Liste und wird nie gesendet. "
                            "Im Protokoll bleibt vermerkt, dass es ihn gab."):
            return
        try:
            ki.verwirf_entwurf(self.conn, self.entwurf_id)
        except ki.NichtMoeglich as fehler:
            return self.f.melde(str(fehler))
        self.entwurf_id = None
        self.f.neu_laden()

    def varianten(self):
        if self.entwurf_id is None:
            return
        eid, modell = self.entwurf_id, self.f.modell()
        self.f.starte("Varianten schreiben", lambda conn, fort: ki.entwurf_varianten(conn, eid, modell, melde=fort),
                      nach=self._varianten_zeigen)

    def _varianten_zeigen(self, varianten):
        dialog = VariantenDialog(varianten, self)
        if dialog.exec() == QDialog.Accepted and dialog.gewaehlt is not None:
            # Nur in den Editor: Gespeichert und freigegeben wird wie immer von dir.
            self.text.setPlainText(dialog.gewaehlt)
            self.gespeichert.setText("Variante übernommen – noch nicht gespeichert. Prüfen, ändern, dann „Speichern“.")
            self.gespeichert.show()

    def _anhang_link(self, ziel):
        if not ziel.startswith(ENTFERNEN):
            return QDesktopServices.openUrl(QUrl.fromLocalFile(ziel))
        if self.entwurf_id is None:
            return
        pfad = Path(ziel[len(ENTFERNEN):])
        if not self.f.frage(f"„{pfad.name}“ aus dieser Mail entfernen?\n\nDie Datei selbst bleibt, wo sie ist."):
            return
        self._anhaenge_aendern(lambda: profil.entferne_anhang_vom_entwurf(self.conn, self.entwurf_id, pfad))

    def anhang_hinzufuegen(self):
        if self.entwurf_id is None:
            return
        pfade, _ = QFileDialog.getOpenFileNames(self, "Anhang wählen", str(Path.home()),
                                                "Dokumente (*.pdf *.docx *.odt *.jpg *.jpeg *.png *.txt);;Alle Dateien (*)")
        if not pfade:
            return

        def hinzu():
            vorher = profil.anhaenge_des_entwurfs(self.conn, self.entwurf_id)
            alle = {str(p): p for p in vorher + [Path(p) for p in pfade]}.values()
            if sum(p.stat().st_size for p in alle if p.is_file()) > versand.MAX_ANHANG_BYTES:
                raise ValueError("Die Anhänge wären zusammen größer als 20 MB.")
            for p in pfade:
                profil.fuege_anhang_zum_entwurf(self.conn, self.entwurf_id, Path(p))
        self._anhaenge_aendern(hinzu)

    def _anhaenge_aendern(self, aenderung):
        if self._geaendert():
            return self.f.melde("Speichere zuerst deine Änderungen.")
        try:
            aenderung()
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()  # eine freigegebene Mail ist danach wieder Entwurf und muss neu freigegeben werden

    def _zeige_befunde(self, befunde):
        leere(self.befunde)
        if self.entwurf_id is not None and not befunde:
            self.befunde.addWidget(label("✓ Alle Prüfungen bestanden.", "okText"))
        for b in befunde:
            self.befunde.addWidget(label(("✕ " if b.stufe == "sperre" else "! ") + b.text, b.stufe, True))

    def _geaendert(self):
        e = self.conn.execute("SELECT empfaenger, betreff, text FROM entwuerfe WHERE id = ?", (self.entwurf_id,)).fetchone()
        jetzt = (self.empfaenger.text().strip(), self.betreff.text().strip(), self.text.toPlainText().strip())
        return jetzt != ((e["empfaenger"] or "").strip(), e["betreff"].strip(), e["text"].strip())

    def speichern(self):
        if self.entwurf_id is None:
            return
        eid, an, betreff, text = self.entwurf_id, self.empfaenger.text(), self.betreff.text(), self.text.toPlainText()
        if not ki.EINE_ADRESSE.match(an.strip()):
            return self.f.melde(f"„{an.strip()}“ ist keine einzelne Mailadresse.")
        self.f.starte("Änderung lernen", lambda conn, _: ki.speichere_text(conn, eid, betreff, text, an),
                      nach=lambda _: self._bestaetige("✓ Entwurf gespeichert. Zum Senden bitte neu freigeben."))

    def _bestaetige(self, text):
        self.gespeichert.setText(f"{text}  ({datetime.now():%H:%M:%S})")
        self.gespeichert.show()
        QTimer.singleShot(6000, self.gespeichert.hide)

    def freigeben(self):
        if self.entwurf_id is None:
            return
        if self._geaendert():
            return self.f.melde("Speichere deine Änderungen zuerst. Freigegeben wird genau der gespeicherte Text.")
        try:
            ki.gib_frei(self.conn, self.entwurf_id)
        except ki.NichtMoeglich as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()

    def senden(self):
        if self.entwurf_id is None:
            return
        if self._geaendert():
            return self.f.melde("Speichere deine Änderungen zuerst und gib sie erneut frei.")
        konto = versand.lade_konto(self.conn)
        if not konto:
            self.f.melde("Wähle zuerst dein Mailkonto.")
            return self.f.zeige_seite(MailkontoSeite)
        e = self.conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (self.entwurf_id,)).fetchone()
        anhaenge = ", ".join(p.name for p in profil.anhaenge_des_entwurfs(self.conn, self.entwurf_id)) or "keine"
        if not self.f.frage(f"Diese Mail jetzt wirklich senden?\n\nVon: {konto.email}\nAn: {e['empfaenger']}\n"
                            f"Betreff: {e['betreff']}\nAnhänge: {anhaenge}"):
            return
        eid = self.entwurf_id
        self.f.mit_passwort(konto, "Mail senden", lambda conn, _, pw: versand.sende_entwurf(conn, eid, pw))

    def kopieren(self):
        e = self.conn.execute("SELECT * FROM entwuerfe WHERE id = ?", (self.entwurf_id,)).fetchone() if self.entwurf_id else None
        if not e or e["status"] == "entwurf":
            return
        QGuiApplication.clipboard().setText(f"An: {e['empfaenger']}\nBetreff: {e['betreff']}\n\n{e['text']}")

    def status(self, status):
        if self.entwurf_id is None:
            return
        eid = self.entwurf_id
        self.f.starte("Status speichern", lambda conn, _: ki.setze_status(conn, eid, status))


class FaktenSeite(Seite):
    titel = "Fakten"
    untertitel = "Was in Bewertung und Mails darf. Offen ist nur, was nicht aus deinen Unterlagen stammt"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Alle Fakten")
        self.tab = tabelle(["Fakt", "Kategorie", "Status", "Quelle"], [None, 105, 80, 105])
        self.tab.itemSelectionChanged.connect(self.zeige_beleg)
        links.lage.addWidget(self.tab)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Bestätigen", self.bestaetigen, True))
        reihe.addWidget(knopf("Ablehnen", self.ablehnen))
        reihe.addWidget(knopf("Ändern", self.aendern))
        reihe.addWidget(knopf("Alle KI-Vorschläge bestätigen", self.alle_bestaetigen))
        links.lage.addLayout(reihe)
        lage.addWidget(links, 3)

        rechts = QVBoxLayout()
        rechts.setSpacing(18)
        fragen = Karte("Die App fragt")
        self.frage_text = label("", umbruch=True)
        fragen.lage.addWidget(self.frage_text)
        self.antwort = QLineEdit()
        self.antwort.returnPressed.connect(self.antworten)
        fragen.lage.addWidget(self.antwort)
        reihe_frage = QHBoxLayout()
        self.antwort_knopf = knopf("Antworten", self.antworten, True)
        self.ueberspringen_knopf = knopf("Überspringen", self.ueberspringen)
        reihe_frage.addWidget(self.antwort_knopf)
        reihe_frage.addWidget(self.ueberspringen_knopf)
        fragen.lage.addLayout(reihe_frage)
        self.erneut_knopf = knopf("Übersprungene Fragen wieder stellen", self.erneut)
        fragen.lage.addWidget(self.erneut_knopf)
        rechts.addWidget(fragen)
        neu = Karte("Fakt ergänzen")
        self.kategorie = QComboBox()
        for k in profil.KATEGORIEN:
            self.kategorie.addItem(KATEGORIE_TEXT[k], k)
        self.kategorie.setCurrentIndex(profil.KATEGORIEN.index("praktisch"))
        neu.lage.addWidget(self.kategorie)
        self.neu_text = QPlainTextEdit()
        self.neu_text.setMaximumHeight(80)
        neu.lage.addWidget(self.neu_text)
        neu.lage.addWidget(knopf("Ergänzen", self.ergaenzen))
        rechts.addWidget(neu)
        belege = Karte("Belegstelle in deinen Unterlagen")
        self.beleg = label("Wähle einen Fakt.", "gedimmt", True)
        belege.lage.addWidget(self.beleg)
        belege.lage.addWidget(knopf("Neue Fakten lokal vorschlagen lassen", self.extrahieren))
        rechts.addWidget(belege)
        rechts.addStretch()
        lage.addLayout(rechts, 2)

    def aktualisieren(self):
        reihen = self.conn.execute(
            "SELECT * FROM fakten WHERE status != 'abgelehnt' ORDER BY status = 'bestaetigt', "
            "CASE kategorie WHEN 'positionierung' THEN 0 WHEN 'erfahrung' THEN 1 WHEN 'praktisch' THEN 2 "
            "WHEN 'projekt' THEN 3 WHEN 'technik' THEN 4 ELSE 5 END, id").fetchall()
        fuelle(self.tab, [(r["text"], KATEGORIE_TEXT[r["kategorie"]], FAKT_STATUS[r["status"]], r["quelle"]) for r in reihen],
               [r["id"] for r in reihen])
        offene = angaben.offen(self.conn)
        self.aktuelle_frage = offene[0] if offene else None
        if self.aktuelle_frage:
            self.frage_text.setText(f"{self.aktuelle_frage.frage} (noch {len(offene)} offen)")
            self.antwort.setPlaceholderText(self.aktuelle_frage.beispiel)
        else:
            self.frage_text.setText("Keine Fragen offen.")
            self.antwort.setPlaceholderText("")
        for w in (self.antwort, self.antwort_knopf, self.ueberspringen_knopf):
            w.setVisible(self.aktuelle_frage is not None)
        self.erneut_knopf.setVisible(angaben.hat_uebersprungene(self.conn))

    def erneut(self):
        angaben.frage_erneut(self.conn)
        self.f.neu_laden()

    def antworten(self):
        if not self.aktuelle_frage:
            return
        schluessel, wert = self.aktuelle_frage.schluessel, self.antwort.text()
        def arbeit(conn, _):
            angaben.beantworte(conn, schluessel, wert)
        self.f.starte("Antwort speichern", arbeit, nach=lambda _: self.antwort.clear())

    def ueberspringen(self):
        if self.aktuelle_frage:
            angaben.ueberspringe(self.conn, self.aktuelle_frage.schluessel)
            self.antwort.clear()
            self.f.neu_laden()

    def zeige_beleg(self):
        fid = zeilen_id(self.tab)
        if fid is None:
            return
        text = self.conn.execute("SELECT text FROM fakten WHERE id = ?", (fid,)).fetchone()[0]
        self.f.starte("Beleg suchen", lambda conn, _: [dict(z) for z in rag.belegstellen(conn, text, 1)],
                      nach=self._beleg_da, still=True)

    def _beleg_da(self, treffer):
        if treffer:
            self.beleg.setText(f"{treffer[0]['quelle']}:\n{treffer[0]['text'][:500]}")
        else:
            self.beleg.setText("Keine Stelle gefunden.")

    def _mit_fakt(self, funktion):
        fid = zeilen_id(self.tab)
        if fid is None:
            return self.f.melde("Wähle zuerst einen Fakt.")
        try:
            funktion(fid)
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()

    def alle_bestaetigen(self):
        offen = self.conn.execute(
            "SELECT count(*) FROM fakten WHERE status = 'vorgeschlagen' AND quelle LIKE 'KI-Vorschlag%'").fetchone()[0]
        if not offen:
            return self.f.melde("Es gibt keine offenen KI-Vorschläge.")
        if self.f.frage(f"{offen} KI-Vorschläge ungeprüft übernehmen? Die KI kann dabei Fehler gemacht haben."):
            profil.bestaetige_ki_vorschlaege(self.conn)
            self.f.neu_laden()

    def bestaetigen(self):
        self._mit_fakt(lambda fid: profil.bestaetige(self.conn, fid))

    def ablehnen(self):
        self._mit_fakt(lambda fid: profil.lehne_ab(self.conn, fid))

    def aendern(self):
        fid = zeilen_id(self.tab)
        if fid is None:
            return self.f.melde("Wähle zuerst einen Fakt.")
        alt = self.conn.execute("SELECT text FROM fakten WHERE id = ?", (fid,)).fetchone()[0]
        neu, ok = QInputDialog.getMultiLineText(self, "Fakt ändern", "Deine Änderung gilt sofort:", alt)
        if ok and neu.strip() and neu.strip() != alt:
            self.f.starte("Fakt ändern", lambda conn, _: profil.aendere(conn, fid, neu))

    def ergaenzen(self):
        kategorie, text = self.kategorie.currentData(), self.neu_text.toPlainText()
        if not text.strip():
            return
        self.neu_text.clear()
        self.f.starte("Fakt ergänzen", lambda conn, _: profil.lege_eigenen_fakt_an(conn, kategorie, text))

    def extrahieren(self):
        modell = self.f.modell()
        self.f.starte("Fakten vorschlagen", lambda conn, fort: profil.extrahiere(conn, modell, fortschritt=fort))


def lies_und_uebernimm(conn, _=None):
    probleme = profil.lies_unterlagen(conn)
    angaben.uebernimm_aus_unterlagen(conn)
    return probleme


class UnterlagenSeite(Seite):
    titel = "Unterlagen"
    untertitel = "Quellen für Fakten und Belegstellen. Dein Lebenslauf und dein Zeugnis gehören hierher"

    def __init__(self, f):
        super().__init__(f)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        karte = Karte("Deine Unterlagen")
        self.tab = tabelle(["Unterlage", "Art", "Mailanhang", "Abschnitte", "Pfad"], [170, 150, 90, 85, None])
        karte.lage.addWidget(self.tab)
        self.probleme = label("", "hinweis", True)
        karte.lage.addWidget(self.probleme)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Datei hinzufügen", self.hinzufuegen, True))
        reihe.addWidget(knopf("Als Mailanhang an/aus", self.anhang_umschalten))
        reihe.addWidget(knopf("Entfernen", self.entfernen))
        reihe.addWidget(knopf("Neu einlesen", self.einlesen))
        karte.lage.addLayout(reihe)
        lage.addWidget(karte)

    def aktualisieren(self):
        reihen = self.conn.execute(
            "SELECT d.*, (SELECT count(*) FROM chunks c WHERE c.dokument_id = d.id) AS n FROM dokumente d ORDER BY d.id").fetchall()
        fuelle(self.tab, [(r["titel"], profil.UNTERLAGEN_ARTEN.get(r["art"], r["art"]), "ja" if r["anhang"] else "nein",
                           r["n"], r["pfad"]) for r in reihen], [r["id"] for r in reihen])
        self.probleme.setText("\n".join(self.f.probleme))

    def hinzufuegen(self):
        pfad, _ = QFileDialog.getOpenFileName(self, "Unterlage wählen", "", "Dokumente (*.pdf *.docx *.md *.txt)")
        if not pfad:
            return
        namen = list(profil.UNTERLAGEN_ARTEN.values())
        wahl, ok = QInputDialog.getItem(self, "Art der Unterlage", "Was ist das für eine Datei?", namen, 0, False)
        if ok:
            self.fuege_hinzu(pfad, list(profil.UNTERLAGEN_ARTEN)[namen.index(wahl)])

    def fuege_hinzu(self, pfad, art):
        def arbeit(conn, _):
            profil.fuege_unterlage_hinzu(conn, pfad, art, art != "sonstiges")
            return lies_und_uebernimm(conn)
        self.f.starte("Unterlage einlesen", arbeit, nach=self.f.setze_probleme)

    def anhang_umschalten(self):
        did = zeilen_id(self.tab)
        if did is None:
            return self.f.melde("Wähle zuerst eine Unterlage.")
        jetzt = self.conn.execute("SELECT anhang FROM dokumente WHERE id = ?", (did,)).fetchone()[0]
        profil.setze_anhang(self.conn, did, not jetzt)
        self.f.neu_laden()

    def entfernen(self):
        did = zeilen_id(self.tab)
        if did is None:
            return self.f.melde("Wähle zuerst eine Unterlage.")
        if self.f.frage("Unterlage aus der App entfernen? Die Datei selbst bleibt erhalten."):
            profil.entferne_unterlage(self.conn, did)
            self.f.neu_laden()

    def einlesen(self):
        self.f.starte("Unterlagen einlesen", lies_und_uebernimm, nach=self.f.setze_probleme)


class SucheSeite(Seite):
    titel = "Suche"
    untertitel = "Findet Firmen in Verzeichnis, Internet, Jobbörse, Job-Alerts und Portalen, dann den Inhaber auf ihren eigenen Seiten"

    def __init__(self, f):
        super().__init__(f)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        karte = Karte("Wo und was")
        self.was = QLineEdit()
        self.was.setPlaceholderText("Berufe: werden aus deinen Fakten abgeleitet")
        self.was.returnPressed.connect(self.suchen)
        karte.lage.addWidget(self.was)
        reihe = QHBoxLayout()
        self.land = QComboBox()
        for code, name in quellen.LAENDER.items():
            self.land.addItem(name, code)
        self.wo = QLineEdit()
        self.wo.setPlaceholderText("Ort oder Region, leer: ganzes Land")
        self.wo.returnPressed.connect(self.suchen)
        self.umkreis = QComboBox()
        for km in (10, 25, 50, 100, 200):
            self.umkreis.addItem(f"{km} km", km)
        self.umkreis.addItem("Ganze Region", 0)
        self.umkreis.setCurrentIndex(2)
        # Ganzes Bundesland: Ort und Umkreis ergeben sich daraus, gefiltert wird über die echte Landesgrenze.
        self.gebiet = QComboBox()
        self.gebiet.addItem("Umkreis um den Ort", "")
        for name in quellen.BUNDESLAENDER:
            self.gebiet.addItem(f"Ganz {name}", name)
        self.gebiet.setToolTip("Sucht im ganzen Bundesland. Die km zeigen dann die Fahrstrecke ab deinem Wohnort aus dem Profil.")
        self.gebiet.currentIndexChanged.connect(self._gebiet_gewaehlt)
        self.land.currentIndexChanged.connect(self._land_gewaehlt)
        self.branche = QComboBox()
        self.branche.addItem("Branche aus Fakten", "")
        for code, (name, _) in quellen.BRANCHEN.items():
            self.branche.addItem(name, code)
        self.arbeitszeit = QComboBox()
        for code, name in suche.ARBEITSZEIT.items():
            self.arbeitszeit.addItem(name, code)
        reihe.addWidget(self.land)
        reihe.addWidget(self.gebiet)
        reihe.addWidget(self.wo, 1)
        reihe.addWidget(self.umkreis)
        reihe.addWidget(self.branche)
        reihe.addWidget(self.arbeitszeit)
        karte.lage.addLayout(reihe)
        reihe_quellen = QHBoxLayout()
        self.q_verzeichnis = Schalter("Firmenverzeichnis")
        self.q_verzeichnis.setChecked(True)
        self.q_internet = Schalter("Internet")
        self.q_jobboerse = Schalter("Jobbörse")
        self.q_jobboerse.setChecked(True)
        self.q_arbeitnow = Schalter("Arbeitnow")
        self.q_arbeitnow.setChecked(True)
        self.searxng = QLineEdit()
        self.searxng.setPlaceholderText("Adresse deiner SearXNG-Instanz, nötig für „Internet“ (eine lokale erkennt die App selbst)")
        self.searxng.textChanged.connect(self._internet_moeglich)
        self.q_internet.setToolTip("Braucht eine laufende SearXNG-Instanz. Trage rechts ihre Adresse ein.")
        self.q_internet.toggled.connect(self._internet_geschaltet)
        for feld in (self.q_verzeichnis, self.q_internet, self.q_jobboerse, self.q_arbeitnow):
            reihe_quellen.addWidget(feld)
        reihe_quellen.addWidget(self.searxng, 1)
        karte.lage.addLayout(reihe_quellen)
        # Stellen aus Alerts, Portalen und Aggregatoren. Diese Quellen liefern auch Fremdes, die Stichwörter sortieren es aus.
        reihe_stellen = QHBoxLayout()
        self.q_postfach = Schalter("Job-Alerts")
        self.q_postfach.setToolTip("Liest Alert-Mails von LinkedIn, Indeed, StepStone, XING, Interamt und anderen aus deinem Postfach.")
        self.q_portale = Schalter("Portale")
        self.q_portale.setToolTip("LinkedIn und StepStone über SearXNG. Die App ruft die Portale nicht selbst ab.")
        self.q_adzuna = Schalter("Adzuna")
        self.q_jooble = Schalter("Jooble")
        for feld in (self.q_postfach, self.q_portale, self.q_adzuna, self.q_jooble):
            reihe_stellen.addWidget(feld)
        self.stichwoerter = QLineEdit()
        self.stichwoerter.setToolTip("Nur Stellen, deren Titel eines dieser Wörter enthält. Gilt für Alerts, Portale, Adzuna und Jooble.")
        reihe_stellen.addWidget(self.stichwoerter, 1)
        reihe_stellen.addWidget(knopf("Postfächer", self.postfaecher))
        reihe_stellen.addWidget(knopf("Schlüssel", self.schluessel))
        karte.lage.addLayout(reihe_stellen)
        reihe_knoepfe = QHBoxLayout()
        self.finden_knopf = knopf("Passende Firmen finden", self.passende_finden, True)
        reihe_knoepfe.addWidget(self.finden_knopf)
        reihe_knoepfe.addWidget(knopf("Mit diesen Angaben suchen", self.suchen))
        reihe_knoepfe.addSpacing(18)
        reihe_knoepfe.addWidget(label("Ausschließen:", "gedimmt"))
        self.ausschluss = QLineEdit()
        self.ausschluss.setPlaceholderText("Wörter mit Komma, z. B. TYPO3, Zeitarbeit")
        self.ausschluss.setToolTip("Treffer mit einem dieser Wörter fliegen aus der Liste. Steht eines in der Stellenanzeige, "
                                   "schreibt die App auch keine Mail.")
        reihe_knoepfe.addWidget(self.ausschluss, 1)
        karte.lage.addLayout(reihe_knoepfe)
        lage.addWidget(karte)
        ergebnis = Karte("Treffer")
        self.hinweis = label("Die Nähe vergleicht nur Name und Kurztext mit deinen Fakten, das ersetzt keine Bewertung. "
                             "Daten: © OpenStreetMap-Mitwirkende, Stellen auch von arbeitnow.com, Adzuna und Jooble.", "gedimmt", True)
        ergebnis.lage.addWidget(self.hinweis)
        self.ausfaelle = label("", "hinweis", True)
        ergebnis.lage.addWidget(self.ausfaelle)
        self.tab = tabelle(["Nähe", "Firma", "Worum es geht", "Ort", "km", "Quelle", "Status"], [50, 165, None, 95, 52, 80, 60])
        self.tab.horizontalHeaderItem(4).setToolTip("Fahrstrecke mit dem Auto. ≈ heißt Luftlinie, wenn keine Route bekannt ist.")
        self.tab.setSortingEnabled(True)
        self.tab.itemSelectionChanged.connect(self.zeige_details)
        ergebnis.lage.addWidget(self.tab, 1)
        unten = QHBoxLayout()
        unten.setSpacing(18)
        unten.addWidget(ergebnis, 1)
        details = Karte("Details")
        details.setFixedWidth(340)
        self.details = label("Klicke auf einen Treffer.", umbruch=True)
        self.details.setTextFormat(Qt.RichText)
        self.details.setOpenExternalLinks(True)
        self.details.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.details.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        rollen = QScrollArea()
        rollen.setWidgetResizable(True)
        rollen.setFrameShape(QFrame.NoFrame)
        rollen.setStyleSheet("background: transparent;")
        rollen.setWidget(self.details)
        details.lage.addWidget(rollen, 1)
        self.knopf_sofort = knopf("Sofort bewerben", self.sofort_bewerben, True)
        self.knopf_sofort.setToolTip("Übernimmt die Firma, sucht die Ansprechperson, prüft deine Eignung anhand von Lebenslauf und "
                                     "Zeugnis und schreibt die Mail. Gesendet wird erst nach deiner Freigabe.")
        self.knopf_inhaber = knopf("Übernehmen und Inhaber finden", self.inhaber_finden)
        self.knopf_nur = knopf("Nur übernehmen", self.uebernehmen)
        self.knopf_sofort.setEnabled(False)
        self.knopf_inhaber.setEnabled(False)
        self.knopf_nur.setEnabled(False)
        details.lage.addWidget(self.knopf_sofort)
        details.lage.addWidget(self.knopf_inhaber)
        details.lage.addWidget(self.knopf_nur)
        unten.addWidget(details)
        lage.addLayout(unten, 1)
        self.treffer: dict[int, dict] = {}
        self.vorbelegt = False
        self.aktive_zeile = None
        self.verzoegerung = QTimer(self)
        self.verzoegerung.setSingleShot(True)
        self.verzoegerung.setInterval(300)  # Wer durch die Liste klickt, soll nicht jede Firma abrufen.
        self.verzoegerung.timeout.connect(self._lade_vorschau)

    def _internet_moeglich(self, adresse=""):
        if not self.searxng.text().strip():
            self.q_internet.setChecked(False)

    def _internet_geschaltet(self, an):
        """Ohne Adresse springt der Schalter zurück und sagt, was fehlt."""
        if an and not self.searxng.text().strip():
            self.q_internet.setChecked(False)
            self.ausfaelle.setText("Internet: Dafür muss ein eigener Suchdienst (SearXNG) laufen. Trage oben rechts seine Adresse ein, "
                                   "dann lässt sich der Schalter einschalten.")
            self.searxng.setFocus()

    @staticmethod
    def _waehle(feld, wert):
        index = feld.findData(wert)
        if index >= 0:
            feld.setCurrentIndex(index)

    def _land_gewaehlt(self, _=None):
        self.gebiet.setVisible(self.land.currentData() == "de")

    def _gebiet_gewaehlt(self, _=None):
        land = self.gebiet.currentData()
        if land:
            self.wo.setText(land)
            self._waehle(self.umkreis, 0)
        elif self.wo.text().strip() in quellen.BUNDESLAENDER:
            self.wo.clear()
            self._waehle(self.umkreis, 50)
        self.wo.setEnabled(not land)
        self.umkreis.setEnabled(not land)

    def aktualisieren(self):
        if not self.vorbelegt:
            self.vorbelegt = True
            lies = lambda name, vorgabe="": db.einstellung(self.conn, f"suche.{name}", vorgabe)
            self.was.setText(lies("begriffe") or lies("abgeleitet"))
            self.wo.setText(lies("ort") or suche.suchort(self.conn))
            self._waehle(self.land, lies("land", "de"))
            self._waehle(self.umkreis, int(lies("umkreis", "50")))
            self._waehle(self.gebiet, lies("bundesland"))
            self.gebiet.setVisible(self.land.currentData() == "de")
            self._waehle(self.branche, lies("branche"))
            self._waehle(self.arbeitszeit, lies("arbeitszeit"))
            self.searxng.setText(lies("searxng") or quellen.finde_lokale_instanz())
            gewaehlt = lies("quellen", ",".join(suche.Auftrag.quellen)).split(",")
            self.q_verzeichnis.setChecked("verzeichnis" in gewaehlt)
            self.q_internet.setChecked("internet" in gewaehlt)
            self.q_jobboerse.setChecked("jobboerse" in gewaehlt)
            self.q_arbeitnow.setChecked("arbeitnow" in gewaehlt)
            for name, feld in self._stellenquellen():
                feld.setChecked(name in gewaehlt)
            self.stichwoerter.setText(lies("stichwoerter"))
            self._internet_moeglich()
        if not self.ausschluss.hasFocus():
            self.ausschluss.setText(db.einstellung(self.conn, "suche.ausschluss", ""))
        abgeleitet = db.einstellung(self.conn, "suche.abgeleitet_stichwoerter", "")
        self.stichwoerter.setPlaceholderText(f"Stichwörter, leer: {abgeleitet}" if abgeleitet else "Stichwörter für Titel, leer: aus den Suchbegriffen")
        self._zeige()

    def _zeige(self):
        bekannt = suche.bekannte_firmen(self.conn)
        zeilen = []
        for t in self.treffer.values():
            t["bekannt"] = regeln.kurzname(t["firma"]).lower() in bekannt
            zeilen.append((t["naehe"] if t["naehe"] is not None else "", t["firma"], t["titel"], t["ort"],
                           km_text(t), t.get("quelle", ""),
                           "in Liste" if t["bekannt"] else "Konzern" if t.get("konzern") else ""))
        fuelle(self.tab, zeilen, list(self.treffer))

    def _stellenquellen(self):
        return (("postfach", self.q_postfach), ("portale", self.q_portale), ("adzuna", self.q_adzuna), ("jooble", self.q_jooble))

    def postfaecher(self):
        dialog = PostfachDialog(self, self.conn)
        if dialog.exec() == QDialog.Accepted:
            try:
                dialog.speichere(self.conn)
            except quellen.SucheFehler as fehler:
                return self.f.melde(str(fehler))
            self.q_postfach.setChecked(bool(postfach.gewaehlte(self.conn)))

    def schluessel(self):
        dialog = SchluesselDialog(self)
        if dialog.exec() == QDialog.Accepted:
            try:
                dialog.speichere()
            except quellen.SucheFehler as fehler:
                self.f.melde(str(fehler))

    def _auftrag(self) -> suche.Auftrag:
        woerter = dict.fromkeys(w.strip() for w in self.ausschluss.text().split(",") if w.strip())
        db.setze_einstellung(self.conn, "suche.ausschluss", ", ".join(woerter))
        db.setze_einstellung(self.conn, "suche.stichwoerter", ", ".join(suche.bereinige_stichwoerter(self.stichwoerter.text().split(","))))
        gewaehlt = tuple(name for name, feld in (("verzeichnis", self.q_verzeichnis), ("internet", self.q_internet),
                                                 ("jobboerse", self.q_jobboerse), ("arbeitnow", self.q_arbeitnow),
                                                 *self._stellenquellen()) if feld.isChecked())
        auftrag = suche.Auftrag(self.was.text(), self.wo.text().strip(), self.land.currentData(), self.umkreis.currentData(),
                                self.branche.currentData(), gewaehlt, self.searxng.text().strip(), self.arbeitszeit.currentData())
        for name, wert in (("begriffe", auftrag.begriffe), ("ort", auftrag.ort), ("land", auftrag.land),
                           ("umkreis", str(auftrag.umkreis)), ("branche", auftrag.branche),
                           ("quellen", ",".join(gewaehlt)), ("searxng", auftrag.searxng), ("arbeitszeit", auftrag.arbeitszeit),
                           ("bundesland", self.gebiet.currentData() or "")):
            db.setze_einstellung(self.conn, f"suche.{name}", wert)
        return auftrag

    def passende_finden(self):
        modell, auftrag = self.f.modell(), self._auftrag()
        if not auftrag.quellen:
            return self.f.melde("Wähle mindestens eine Quelle.")
        self.ausfaelle.clear()
        self.f.starte("Passende Firmen finden", lambda conn, fort: suche.finde_passende(conn, modell, auftrag, fort),
                      nach=self.passende_gefunden)

    def passende_gefunden(self, ergebnis):
        self.was.setText(", ".join(ergebnis["begriffe"]))
        self.wo.setText(ergebnis["ort"])
        self.gefunden(ergebnis)

    def suchen(self):
        if not self.was.text().strip():  # Ohne Berufe kommen sie aus den Fakten, statt dass die Suche scheitert.
            self.was.setText(db.einstellung(self.conn, "suche.abgeleitet", ""))
            if not self.was.text():
                return self.passende_finden()
        auftrag = self._auftrag()
        if not auftrag.quellen:
            return self.f.melde("Wähle mindestens eine Quelle.")
        self.ausfaelle.clear()
        self.f.starte("Firmen suchen", lambda conn, fort: suche.suche_ueberall(conn, auftrag, fort), nach=self.gefunden)

    def gefunden(self, ergebnis):
        self.treffer = dict(enumerate(ergebnis["treffer"]))
        self.ausfaelle.setText("\n".join(ergebnis.get("hinweise", [])))
        self._zeige()
        if not ergebnis["treffer"]:
            self.f.melde("Keine Treffer. Versuche andere Begriffe, eine andere Branche oder ein größeres Gebiet.")

    def _gewaehlt(self):
        zeile = zeilen_id(self.tab)
        if zeile is None:
            self.f.melde("Wähle zuerst einen Treffer.")
            return None
        return self.treffer[zeile]

    def uebernehmen(self):
        stelle = self._gewaehlt()
        if stelle:
            self.f.starte("Firma übernehmen", lambda conn, _: suche.uebernimm(conn, stelle))

    def inhaber_finden(self):
        stelle = self._gewaehlt()
        if stelle:
            land, searxng = self.land.currentData(), self.searxng.text().strip()
            self.f.starte("Inhaber finden", lambda conn, _: suche.uebernimm_und_finde(conn, stelle, land, searxng),
                          nach=self.inhaber_gefunden)

    def inhaber_gefunden(self, ergebnis):
        self.f.melde(inhaber_text(ergebnis))

    def sofort_bewerben(self):
        stelle = self._gewaehlt()
        if stelle:
            modell, land, searxng = self.f.modell(), self.land.currentData(), self.searxng.text().strip()
            self.f.starte("Sofort bewerben", lambda conn, fort: suche.sofort_bewerben(conn, stelle, modell, land, searxng, fort,
                                                                                     mit_entwurf=False),
                          nach=lambda ergebnis: self._sofort_geprueft(ergebnis, modell))

    def _sofort_geprueft(self, ergebnis, modell):
        """Erst fragt die App nach, was in den Unterlagen fehlt, dann schreibt sie die Mail."""
        fid = ergebnis["firma_id"]
        if ergebnis["eignung"]:
            ergebnis["eignung"] = ergaenze_mit_rueckfrage(self.f, self.conn, fid, ergebnis["eignung"])
        eignung, hinweis = ergebnis["eignung"], ergebnis["hinweis"]

        def schreiben(conn, fort):
            neu = suche.schreibe_bewerbung(conn, fid, modell, eignung, fort)
            return {**ergebnis, **neu, "hinweis": neu.get("hinweis") or hinweis}
        self.f.starte("Mail schreiben", schreiben, nach=self.beworben)

    def beworben(self, ergebnis):
        teile = [ki.eignung_text(ergebnis["eignung"])] if ergebnis["eignung"] else []
        if ergebnis["hinweis"]:
            teile.append(ergebnis["hinweis"])
        if ergebnis["entwurf_id"]:
            anhaenge = ", ".join(ergebnis.get("anhaenge") or []) or "keine, lege sie unter „Unterlagen“ fest"
            teile.append(f"Die Mail liegt unter „Entwürfe“. Prüfe sie und gib sie frei, erst dann wird gesendet. "
                         f"Anhänge: {anhaenge}.")
        self.f.melde("\n\n".join(teile))
        if ergebnis["entwurf_id"]:
            self.f.zeige_seite(EntwurfSeite)

    def zeige_details(self):
        zeile = zeilen_id(self.tab)
        if zeile is None or zeile not in self.treffer:
            return
        self.aktive_zeile = zeile
        self.knopf_sofort.setEnabled(True)
        self.knopf_inhaber.setEnabled(True)
        self.knopf_nur.setEnabled(True)
        self._male_details(self.treffer[zeile])
        if "vorschau" not in self.treffer[zeile]:
            self.verzoegerung.start()

    def _lade_vorschau(self):
        zeile = self.aktive_zeile
        if zeile is None or zeile not in self.treffer:
            return
        stelle = self.treffer[zeile]
        land, searxng = self.land.currentData(), self.searxng.text().strip()
        self.f.starte("Firma ansehen", lambda conn, _: suche.vorschau(stelle, land, searxng),
                      nach=lambda v: self._vorschau_da(stelle, v), still=True)

    def _vorschau_da(self, stelle, vorschau):
        stelle["vorschau"] = vorschau
        if self.aktive_zeile is not None and self.treffer.get(self.aktive_zeile) is stelle:
            self._male_details(stelle)

    def _male_details(self, t):
        """Alles von fremden Seiten wird maskiert: Es ist Text, kein HTML."""
        e = html.escape
        v = t.get("vorschau")
        farbe = theme.AKTUELL["akzent"]
        link = lambda url, text: f"<a style='color:{farbe}' href='{e(url, quote=True)}'>{e(text)}</a>"
        teile = [f"<b style='font-size:15px'>{e(t['firma'])}</b>", e(t["titel"])]
        weg = "" if t["entfernung"] is None else (f"≈ {t['entfernung']} km Luftlinie" if t.get("luftlinie", True) else
                                                   f"{t['entfernung']} km mit dem Auto")
        ort = " · ".join(x for x in (t["ort"], weg) if x)
        if ort:
            teile.append(e(ort))
        teile.append(f"Quelle: {e(t.get('quelle', ''))}")
        konzern = (v or {}).get("konzern") or t.get("konzern", "")
        if konzern:
            teile.append(f"<b>Eher Konzern:</b> {e(konzern)}")
        links = []
        firmenquelle = str(t.get("referenz", "")).startswith(("web:", "osm:"))
        if t.get("url", "").startswith(("http://", "https://")) and t["url"] != t.get("website"):
            if not firmenquelle:
                art = "Stellenanzeige öffnen"
            else:
                art = "Impressum öffnen" if firmen.IMPRESSUM.search(t["url"]) else "Fundstelle öffnen"
            links.append(link(t["url"], art))
        website = (v or {}).get("website") or t.get("website", "")
        if website.startswith(("http://", "https://")):
            links.append(link(website, "Website der Firma"))
        else:
            suchlink = f"https://duckduckgo.com/?q={quote_plus(t['firma'] + ' ' + t['ort'] + ' Impressum')}"
            links.append(link(suchlink, "Im Browser suchen"))
        teile.append(" &nbsp;·&nbsp; ".join(links))
        if v is None:
            teile.append("<i>Lese die Firmenseite …</i>")
        else:
            if v["problem"]:
                teile.append(e(v["problem"]))
            if v.get("geraten"):
                teile.append("Website über den Firmennamen ermittelt. Prüfe, ob es wirklich diese Firma ist.")
            if v["kurztext"]:
                teile.append(e(v["kurztext"]))
            if not v["problem"]:
                teile.append(f"<b>Inhaber:</b> {e(v['personen']) or 'nicht gefunden'}")
                adresse = f"{link('mailto:' + v['email'], v['email'])} ({e(v['art'])})" if v["email"] else "keine veröffentlicht"
                teile.append(f"<b>Adresse:</b> {adresse}")
            if v["stellen"]:
                teile.append("<b>Auf der eigenen Website:</b><br>" + "<br>".join(e(x) for x in v["stellen"][:5]))
            elif firmenquelle and not v["problem"]:
                teile.append("Keine offenen Stellen auf der Website gefunden. Für eine Initiativanfrage kann sie trotzdem passen.")
        self.details.setText("<br><br>".join(teile))


class BerichtSeite(Seite):
    titel = "Morgenbericht"
    untertitel = "Was der Nachtlauf gefunden hat. Gesendet wird nichts, die Mails warten auf deine Freigabe"

    def __init__(self, f):
        super().__init__(f)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        karte = Karte("Nachtlauf")
        karte.lage.addWidget(label("Sucht mit den Angaben der Suchseite. Der Rechner muss laufen und du angemeldet sein. War er aus, "
                                   "holt die App den Lauf beim nächsten Anmelden nach. Die Aussicht bewertet das lokale Modell, "
                                   "das kann danebenliegen.", "gedimmt", True))
        reihe = QHBoxLayout()
        self.uhrzeit = QComboBox()
        for stunde in range(24):
            self.uhrzeit.addItem(f"{stunde:02d}:00")
        self.uhrzeit.setCurrentText("01:00")
        self.schalter = knopf("", self.umschalten, True)
        reihe.addWidget(self.uhrzeit)
        reihe.addWidget(self.schalter)
        reihe.addWidget(knopf("Jetzt laufen lassen", self.jetzt))
        reihe.addStretch()
        karte.lage.addLayout(reihe)
        lage.addWidget(karte)
        ergebnis = Karte("Aussichtsreichste Firmen")
        self.stand = label("Noch kein Bericht.", "gedimmt", True)
        ergebnis.lage.addWidget(self.stand)
        self.hinweise = label("", "hinweis", True)
        ergebnis.lage.addWidget(self.hinweise)
        self.tab = tabelle(["Score", "Firma", "Inhaber", "Adresse", "Art", "Weg", "Stellen", "Mail"], [55, None, 140, 190, 75, 70, 60, 60])
        self.tab.itemSelectionChanged.connect(self.zeige_grund)
        ergebnis.lage.addWidget(self.tab, 1)
        self.grund = label("", "gedimmt", True)
        ergebnis.lage.addWidget(self.grund)
        ergebnis.lage.addWidget(knopf("Zu den Mailvorschlägen", lambda: self.f.zeige_seite(EntwurfSeite)))
        lage.addWidget(ergebnis, 1)
        self.firmen = {}

    def aktualisieren(self):
        an = nacht.ist_eingeschaltet()
        self.schalter.setText("Nachtlauf ausschalten" if an else "Nachtlauf einschalten")
        self.schalter.setEnabled(not config.DEMO)
        self.uhrzeit.setEnabled(not an)
        bericht = nacht.letzter_bericht(self.conn)
        if not bericht:
            return
        self.stand.setText(f"Lauf vom {bericht['datum'][:16].replace('T', ' ')}: {bericht['gefunden']} gefunden, {bericht.get('direkt', 0)} davon nicht aus Stellenbörsen, {bericht['geprueft']} "
                           f"Firmenseiten gelesen, {bericht['ohne_adresse']} ohne veröffentlichte Adresse und {bericht['zu_niedrig']} mit "
                           f"zu niedrigem Score aussortiert.")
        self.hinweise.setText("\n".join(([f"Fehler: {bericht['fehler']}"] if bericht["fehler"] else []) + bericht["hinweise"]))
        self.firmen = {z["firma_id"]: z for z in bericht["firmen"]}
        fuelle(self.tab, [(z["score"], z["name"], z["personen"], z["email"], z["art"], "direkt" if z.get("direkt") else "Börse",
                           len(z.get("stellen", [])) or "", "fertig" if z["entwurf_id"] else "")
                          for z in bericht["firmen"]], list(self.firmen))

    def zeige_grund(self):
        zeile = self.firmen.get(zeilen_id(self.tab))
        if not zeile:
            return self.grund.setText("")
        stellen = zeile.get("stellen", [])
        vorspann = f"Auf der eigenen Website: {'; '.join(stellen)}\n" if stellen else ""
        self.grund.setText(vorspann + zeile["begruendung"])

    def umschalten(self):
        try:
            if nacht.ist_eingeschaltet():
                nacht.ausschalten()
            else:
                nacht.einschalten(config.DB_PFAD, self.uhrzeit.currentText())
        except (OSError, RuntimeError, subprocess.CalledProcessError) as fehler:
            return self.f.melde(f"Der Nachtlauf ließ sich nicht umschalten: {fehler}")
        self.f.neu_laden()

    def jetzt(self):
        modell = self.f.modell()
        self.f.starte("Nachtlauf", lambda conn, fort: nacht.lauf(conn, modell, fort))


class MailkontoSeite(Seite):
    titel = "Mailkonto"
    untertitel = "Absender für den Versand. Gefunden in deinen Mailprogrammen, das Passwort fragt die App selbst ab"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Gefundene Konten")
        links.lage.addWidget(label("Aus Thunderbird, Evolution, Geary, KMail, Claws Mail und msmtp. "
                                   "Passwörter liest die App dort nicht aus. Prüfe, ob das Konto wirklich deins ist.",
                                   "gedimmt", True))
        self.tab = tabelle(["Programm", "Adresse", "Server", "Port", "Sicherheit"], [95, None, 170, 50, 80])
        links.lage.addWidget(self.tab, 1)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Dieses Konto verwenden", self.verwenden, True))
        reihe.addWidget(knopf("Neu suchen", self.suchen))
        links.lage.addLayout(reihe)
        lage.addWidget(links, 3)

        rechts = QVBoxLayout()
        rechts.setSpacing(18)
        aktuell = Karte("Aktuelles Konto")
        self.aktuell = label("Noch keins gewählt.", "gedimmt", True)
        aktuell.lage.addWidget(self.aktuell)
        aktuell.lage.addWidget(knopf("Testmail an mich senden", self.testmail, True))
        self.vergessen_knopf = knopf("Gespeichertes Passwort löschen", self.vergessen)
        aktuell.lage.addWidget(self.vergessen_knopf)
        rechts.addWidget(aktuell)
        hand = Karte("Von Hand eintragen")
        form = QFormLayout()
        self.felder = {}
        for schluessel, text, hinweis in (("email", "Adresse", ""), ("host", "Server", "smtp.example.org"),
                                          ("port", "Port", "587"), ("benutzer", "Benutzer", "Meist die Adresse")):
            self.felder[schluessel] = QLineEdit()
            self.felder[schluessel].setPlaceholderText(hinweis)
            form.addRow(text, self.felder[schluessel])
        self.sicherheit = QComboBox()
        self.sicherheit.addItem("STARTTLS", "starttls")
        self.sicherheit.addItem("SSL/TLS", "ssl")
        form.addRow("Sicherheit", self.sicherheit)
        hand.lage.addLayout(form)
        hand.lage.addWidget(knopf("Speichern", self.von_hand))
        rechts.addWidget(hand)
        rechts.addStretch()
        lage.addLayout(rechts, 2)
        self.konten: list[mailkonten.Konto] = []
        self.gesucht = False

    def aktualisieren(self):
        if config.DEMO:
            self.gesucht = True
            self.konten = []
            self.aktuell.setText("In der Demo gibt es kein Mailkonto, und nichts wird gesendet. "
                                 "Deine echten Mailprogramme bleiben außen vor.")
            self.vergessen_knopf.setVisible(False)
            return
        if not self.gesucht:
            self.suchen()
        konto = versand.lade_konto(self.conn)
        if konto:
            gespeichert = versand.gespeichertes_passwort(konto) is not None
            self.aktuell.setText(f"{konto.email}\n{konto.host}:{konto.port} ({versand.SICHERHEIT[konto.sicherheit]})\n"
                                 f"Benutzer: {konto.benutzer}\n"
                                 + ("Passwort im Schlüsselbund gespeichert." if gespeichert else "Passwort wird beim Senden abgefragt."))
            self.vergessen_knopf.setVisible(gespeichert)
        else:
            self.aktuell.setText("Noch keins gewählt.")
            self.vergessen_knopf.setVisible(False)

    def suchen(self):
        if config.DEMO:
            return
        self.gesucht = True
        self.konten = mailkonten.finde_alle(eigene_adresse=person.lade(self.conn)["email"])
        fuelle(self.tab, [(k.quelle, k.email, k.host, k.port, versand.SICHERHEIT.get(k.sicherheit, k.sicherheit))
                          for k in self.konten], list(range(len(self.konten))))

    def verwenden(self):
        zeile = zeilen_id(self.tab)
        if zeile is None:
            return self.f.melde("Wähle zuerst ein Konto.")
        self._speichere(self.konten[zeile])

    def von_hand(self):
        w = {k: f.text().strip() for k, f in self.felder.items()}
        sicherheit = self.sicherheit.currentData()
        try:
            port = int(w["port"] or versand.mailkonten._standardport(sicherheit))
        except ValueError:
            return self.f.melde("Der Port muss eine Zahl sein.")
        self._speichere(mailkonten.Konto("von Hand", w["email"], w["host"], port, sicherheit, w["benutzer"] or w["email"]))

    def _speichere(self, konto):
        try:
            versand.speichere_konto(self.conn, konto)
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.f.neu_laden()

    def testmail(self):
        konto = versand.lade_konto(self.conn)
        if not konto:
            return self.f.melde("Wähle zuerst ein Konto.")
        self.f.mit_passwort(konto, "Testmail senden", lambda conn, _, pw: versand.sende_test(conn, pw),
                            erfolg=lambda adresse: self.f.melde(f"Testmail an {adresse} gesendet. Schau in dein Postfach."))

    def vergessen(self):
        konto = versand.lade_konto(self.conn)
        if konto:
            versand.loesche_passwort(konto)
            self.f.neu_laden()


class ProfilSeite(Seite):
    titel = "Profil"
    untertitel = "Wer du bist, wie deine Mails klingen und was nie darin stehen darf"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = QVBoxLayout()
        links.setSpacing(18)
        kontakt = Karte("Kontakt")
        form = QFormLayout()
        self.felder = {}
        for schluessel, text in (("name", "Name"), ("email", "Mail"), ("telefon", "Telefon"), ("ort", "Ort"), ("links", "Links")):
            self.felder[schluessel] = QLineEdit()
            form.addRow(text, self.felder[schluessel])
        self.signatur = QPlainTextEdit()
        self.signatur.setPlaceholderText("Mail-Signatur, z. B. Name, Rolle, Telefon, Web, GitHub. Leer: nur „Viele Grüße“ und dein Name.")
        self.signatur.setFixedHeight(120)
        form.addRow("Signatur", self.signatur)
        self.gehalt = QLineEdit()
        self.gehalt.setPlaceholderText("z. B. 65.000 € brutto im Jahr")
        self.gehalt.setToolTip("Steht nur in einer Mail, wenn die Stellenanzeige ausdrücklich nach der Gehaltsvorstellung "
                               "fragt, und dann genau als ein fester Satz. Sonst bleibt jede Gehaltsangabe gesperrt.")
        form.addRow("Gehaltsvorstellung", self.gehalt)
        kontakt.lage.addLayout(form)
        kontakt.lage.addWidget(knopf("Speichern", self.speichern, True))
        links.addWidget(kontakt)
        stil = Karte("Mailvorlage")
        stil.lage.addWidget(label("Die KI schreibt die komplette Mail für jede Firma und nimmt diesen Text als Stilbeispiel. "
                                  "Ist der Haken gesetzt, bleibt er wörtlich stehen und die KI ergänzt nur einen Satz zur Firma. "
                                  "Das ist sicherer, aber weniger persönlich.", "gedimmt", True))
        self.woertlich = Schalter("Vorlage wörtlich verwenden")
        stil.lage.addWidget(self.woertlich)
        self.ohne_alter = Schalter("Alter nicht über Berufsjahre verraten")
        self.ohne_alter.setToolTip("Mail und Qualifikationsprofil nennen keine Gesamtjahre ab 15 und keine Jahreszahlen "
                                   "vor 2005, sondern „langjährige Erfahrung“ und die jüngsten Stationen.")
        self.ohne_alter.toggled.connect(lambda an: db.setze_einstellung(self.conn, "mail.ohne_alter", "1" if an else "0"))
        stil.lage.addWidget(self.ohne_alter)
        self.betreff = QLineEdit()
        self.betreff.setPlaceholderText("Betreff der Mails")
        stil.lage.addWidget(self.betreff)
        self.vorlage = QPlainTextEdit()
        stil.lage.addWidget(self.vorlage, 1)
        stil.lage.addWidget(knopf("Vorlage speichern", self.vorlage_speichern))
        links.addWidget(stil, 1)
        lage.addLayout(links, 1)

        rechts = Karte("Eigene Regeln")
        rechts.lage.addWidget(label("Ein Verstoß sperrt die Freigabe. Gehalt, Gesundheit, Platzhalter und unbelegte Zahlen "
                                    "sind immer gesperrt.", "gedimmt", True))
        self.tab = tabelle(["Art", "Regel", "Meldung"], [130, None, 170])
        rechts.lage.addWidget(self.tab, 1)
        rechts.lage.addWidget(knopf("Regel löschen", self.loeschen))
        self.art = QComboBox()
        for art, text in regeln.REGEL_ARTEN.items():
            self.art.addItem(text, art)
        rechts.lage.addWidget(self.art)
        self.wert = QLineEdit()
        self.wert.setPlaceholderText("Begriff oder Suchmuster, zum Beispiel Machine Learning")
        rechts.lage.addWidget(self.wert)
        self.zweiter = QLineEdit()
        self.zweiter.setPlaceholderText("Nur bei „Nur zusammen nennen“: die zweite Angabe")
        rechts.lage.addWidget(self.zweiter)
        self.meldung = QLineEdit()
        self.meldung.setPlaceholderText("Hinweis bei einem Verstoß (optional)")
        rechts.lage.addWidget(self.meldung)
        rechts.lage.addWidget(knopf("Regel anlegen", self.anlegen))
        lage.addWidget(rechts, 1)

    def aktualisieren(self):
        daten = person.lade(self.conn)
        for schluessel, feld in self.felder.items():
            feld.setText(daten[schluessel])
        self.vorlage.setPlainText(db.einstellung(self.conn, "vorlage", ""))
        self.signatur.setPlainText(db.einstellung(self.conn, "mail.signatur", ""))
        self.gehalt.setText(db.einstellung(self.conn, "mail.gehalt", ""))
        self.woertlich.setChecked(db.einstellung(self.conn, "mail.modus", "ki") == "vorlage")
        self.ohne_alter.blockSignals(True)
        self.ohne_alter.setChecked(ki.ohne_alter(self.conn))
        self.ohne_alter.blockSignals(False)
        self.betreff.setText(db.einstellung(self.conn, "betreff", ""))
        eigene = regeln.lade_eigene(self.conn)
        fuelle(self.tab, [(regeln.REGEL_ARTEN[r["art"]], f"{r['wert']} + {r['zweiter']}" if r["zweiter"] else r["wert"],
                           r["meldung"]) for r in eigene], [r["id"] for r in eigene])

    def speichern(self):
        try:
            person.speichere(self.conn, **{k: f.text() for k, f in self.felder.items()})
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        signatur = self.signatur.toPlainText().strip()
        if regeln.ist_sensibel(signatur):
            return self.f.melde(profil.SENSIBEL_MELDUNG)
        db.setze_einstellung(self.conn, "mail.signatur", signatur)
        db.setze_einstellung(self.conn, "mail.gehalt", self.gehalt.text().strip())
        self.f.neu_laden()

    def vorlage_speichern(self):
        text = self.vorlage.toPlainText().strip()
        if regeln.ist_sensibel(text):
            return self.f.melde(profil.SENSIBEL_MELDUNG)
        db.setze_einstellung(self.conn, "vorlage", text)
        db.setze_einstellung(self.conn, "betreff", self.betreff.text().strip())
        db.setze_einstellung(self.conn, "mail.modus", "vorlage" if self.woertlich.isChecked() else "ki")

    def anlegen(self):
        try:
            regeln.lege_regel_an(self.conn, self.art.currentData(), self.wert.text(), self.zweiter.text(), self.meldung.text())
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        for feld in (self.wert, self.zweiter, self.meldung):
            feld.clear()
        self.f.neu_laden()

    def loeschen(self):
        rid = zeilen_id(self.tab)
        if rid is None:
            return self.f.melde("Wähle zuerst eine Regel.")
        regeln.loesche_regel(self.conn, rid)
        self.f.neu_laden()


class CVSeite(Seite):
    titel = "CV-Assistent"
    untertitel = "Kein Lebenslauf zur Hand? Der Assistent fragt nach und erstellt ihn, ohne etwas zu erfinden"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Der Assistent fragt")
        self.frage_text = label("", umbruch=True)
        links.lage.addWidget(self.frage_text)
        self.stapel = QStackedWidget()
        self.stapel.addWidget(self._baue_kontakt())
        self.stapel.addWidget(self._baue_station())
        self.stapel.addWidget(self._baue_text())
        self.stapel.addWidget(self._baue_fertig())
        links.lage.addWidget(self.stapel, 1)
        lage.addWidget(links, 3)
        rechts = Karte("Deine Antworten")
        self.tab = tabelle(["Bereich", "Inhalt"], [95, None])
        rechts.lage.addWidget(self.tab, 1)
        rechts.lage.addWidget(knopf("Verwerfen und neu fragen", self.verwerfen))
        lage.addWidget(rechts, 2)
        self.schritt = None
        self.pfad = None

    def _formular(self, felder):
        seite = QWidget()
        lage = QVBoxLayout(seite)
        lage.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        ergebnis = {}
        for schluessel, text, hinweis in felder:
            feld = QLineEdit()
            feld.setPlaceholderText(hinweis)
            ergebnis[schluessel] = feld
            form.addRow(text, feld)
        lage.addLayout(form)
        return seite, lage, ergebnis

    def _baue_kontakt(self):
        seite, lage, self.kontakt = self._formular([
            ("name", "Name", ""), ("email", "Mail", ""), ("telefon", "Telefon", "Optional"),
            ("ort", "Ort", "Optional"), ("links", "Links", "Optional, zum Beispiel GitHub oder LinkedIn")])
        lage.addWidget(knopf("Weiter", self.kontakt_speichern, True))
        lage.addStretch()
        return seite

    def _baue_station(self):
        seite, lage, self.station = self._formular([
            ("titel", "Bezeichnung", "Zum Beispiel Systemadministrator oder Fachinformatiker"),
            ("organisation", "Firma / Schule", ""), ("ort", "Ort", ""),
            ("von", "Von", "Zum Beispiel 03/2018"), ("bis", "Bis", "Zum Beispiel heute")])
        lage.addWidget(label("Was hast du dort gemacht? Eine Sache pro Zeile, möglichst mit Zahlen.", "gedimmt", True))
        self.station_details = QPlainTextEdit()
        lage.addWidget(self.station_details, 1)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Speichern, weitere eintragen", self.station_speichern, True))
        reihe.addWidget(knopf("Das war alles", self.station_fertig))
        self.claude_station = knopf("Mit Claude formulieren", lambda: self.formulieren(self.station_details))
        reihe.addWidget(self.claude_station)
        lage.addLayout(reihe)
        return seite

    def _baue_text(self):
        seite = QWidget()
        lage = QVBoxLayout(seite)
        lage.setContentsMargins(0, 0, 0, 0)
        self.antwort = QPlainTextEdit()
        lage.addWidget(self.antwort, 1)
        reihe = QHBoxLayout()
        reihe.addWidget(knopf("Weiter", self.text_speichern, True))
        reihe.addWidget(knopf("Überspringen", self.text_ueberspringen))
        self.claude_text = knopf("Mit Claude formulieren", lambda: self.formulieren(self.antwort))
        reihe.addWidget(self.claude_text)
        lage.addLayout(reihe)
        return seite

    def _baue_fertig(self):
        seite = QWidget()
        lage = QVBoxLayout(seite)
        lage.setContentsMargins(0, 0, 0, 0)
        self.fertig_text = label("", "gedimmt", True)
        lage.addWidget(self.fertig_text)
        lage.addWidget(knopf("Lebenslauf erstellen", self.erstellen, True))
        self.oeffnen_knopf = knopf("PDF öffnen", self.oeffnen)
        lage.addWidget(self.oeffnen_knopf)
        self.uebernehmen_knopf = knopf("Als meinen Lebenslauf übernehmen", self.uebernehmen)
        lage.addWidget(self.uebernehmen_knopf)
        lage.addStretch()
        return seite

    def formulieren(self, feld):
        """Claude glättet die Antwort im Feld. Der Vorschlag ersetzt den Text erst im Feld, gespeichert wird mit „Weiter“."""
        if not self.schritt:
            return
        text, modell, frage = feld.toPlainText(), self.f.modell(), self.schritt.frage
        if not text.strip():
            return self.f.melde("Schreib erst etwas, dann kann Claude es formulieren.")
        kontext = " · ".join(t for t in (self.station["titel"].text(), self.station["organisation"].text(),
                                         self.station["von"].text(), self.station["bis"].text()) if t.strip()) \
            if feld is self.station_details else ""
        self.f.starte("Mit Claude formulieren", lambda _c, _f: cv.formuliere(modell, frage, text, kontext),
                      nach=lambda neu: feld.setPlainText(neu))

    def aktualisieren(self):
        mit_claude = llm.ist_claude(self.f.modell())
        self.claude_text.setVisible(mit_claude)
        self.claude_station.setVisible(mit_claude)
        self.schritt = cv.naechster_schritt(self.conn)
        s = self.schritt
        if s is None:
            self.frage_text.setText("Alle Fragen sind beantwortet.")
            self.stapel.setCurrentIndex(3)
            self.fertig_text.setText(f"Erstellt: {self.pfad}" if self.pfad else
                                     "Erst das PDF ansehen, dann übernehmen. Übernehmen ersetzt den Lebenslauf "
                                     "im Mailanhang und legt deine Antworten als Fakten an.")
            self.oeffnen_knopf.setVisible(self.pfad is not None)
            self.uebernehmen_knopf.setVisible(self.pfad is not None)
        else:
            self.frage_text.setText(s.frage)
            self.stapel.setCurrentIndex({"kontakt": 0, "station": 1, "text": 2}[s.art])
            if s.art == "kontakt":
                daten = person.lade(self.conn)
                for schluessel, feld in self.kontakt.items():
                    feld.setText(daten[schluessel])
            elif s.art == "text":
                self.antwort.setPlaceholderText(s.beispiel)
        reihen = cv.uebersicht(self.conn)
        self.reihen = {i: r for i, r in enumerate(reihen)}
        fuelle(self.tab, [(r["bereich"], r["inhalt"]) for r in reihen], list(self.reihen))

    def _versuche(self, funktion):
        try:
            funktion()
        except ValueError as fehler:
            self.f.melde(str(fehler))
            return False
        self.f.neu_laden()
        return True

    def kontakt_speichern(self):
        self._versuche(lambda: person.speichere(self.conn, **{k: f.text() for k, f in self.kontakt.items()}))

    def _station_merken(self):
        cv.fuege_station_hinzu(self.conn, self.schritt.schluessel, details=self.station_details.toPlainText(),
                               **{k: f.text() for k, f in self.station.items()})
        for feld in self.station.values():
            feld.clear()
        self.station_details.clear()

    def station_speichern(self):
        self._versuche(self._station_merken)

    def station_fertig(self):
        def arbeit():
            if self.station["titel"].text().strip():
                self._station_merken()
            cv.station_abschliessen(self.conn, self.schritt.schluessel)
        self._versuche(arbeit)

    def text_speichern(self):
        text = self.antwort.toPlainText()
        if not text.strip():
            return self.f.melde("Schreib eine Antwort oder überspringe die Frage.")
        if self._versuche(lambda: cv.beantworte(self.conn, self.schritt.schluessel, text)):
            self.antwort.clear()

    def text_ueberspringen(self):
        self.antwort.clear()
        self._versuche(lambda: cv.beantworte(self.conn, self.schritt.schluessel, ""))

    def verwerfen(self):
        zeile = zeilen_id(self.tab)
        if zeile is None:
            return self.f.melde("Wähle zuerst eine Antwort.")
        cv.verwerfe(self.conn, self.reihen[zeile]["schluessel"])
        self.pfad = None
        self.f.neu_laden()

    def erstellen(self):
        self._versuche(lambda: setattr(self, "pfad", cv.erstelle(self.conn)))

    def oeffnen(self):
        if self.pfad:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.pfad)))

    def uebernehmen(self):
        if not self.pfad or not self.f.frage(
                "Diesen Lebenslauf für Mails verwenden und deine Antworten als Fakten übernehmen? "
                "Andere Lebensläufe bleiben in der App, gehen aber nicht mehr als Anhang raus."):
            return
        pfad = self.pfad
        self.f.starte("Lebenslauf übernehmen", lambda conn, _: cv.uebernehmen(conn, pfad), nach=self._uebernommen)

    def _uebernommen(self, uebersprungen):
        if uebersprungen:
            self.f.melde("Nicht als Fakt übernommen, weil interne Angaben darin stehen:\n" + "\n".join(uebersprungen))


class AgendaSeite(Seite):
    titel = "Agenda"
    untertitel = "Wird aus Fakten und deinen Antworten erzeugt. Für die Zeit nach dem ersten Gespräch"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Deine Angaben")
        links.lage.addWidget(label("Nur für die Agenda und die lokale Bewertung, nie für Mails.", "gedimmt", True))
        self.tab = tabelle(["Angabe", "Wert", "Quelle"], [170, None, 90])
        links.lage.addWidget(self.tab)
        links.lage.addWidget(knopf("Angabe ändern", self.aendern))
        lage.addWidget(links, 3)
        rechts = QVBoxLayout()
        rechts.setSpacing(18)
        karte = Karte("Erzeugen")
        self.stand = label("", "gedimmt", True)
        karte.lage.addWidget(self.stand)
        karte.lage.addWidget(knopf("Agenda erstellen", self.erstellen, True))
        self.oeffnen_knopf = knopf("PDF öffnen", self.oeffnen)
        karte.lage.addWidget(self.oeffnen_knopf)
        rechts.addWidget(karte)
        inhalt = Karte("Inhalt")
        self.inhalt = label("", "gedimmt", True)
        inhalt.lage.addWidget(self.inhalt)
        rechts.addWidget(inhalt)
        rechts.addStretch()
        lage.addLayout(rechts, 2)
        self.pfad = None

    def aktualisieren(self):
        reihen = angaben.uebersicht(self.conn)
        self.reihen = {i: r for i, r in enumerate(reihen)}
        fuelle(self.tab, [(r["titel"], r["wert"], r["quelle"]) for r in reihen], list(self.reihen))
        offen = len(angaben.offen(self.conn))
        self.stand.setText(f"Noch {offen} Fragen offen, siehe „Fakten“." if offen else "Alle Fragen sind erledigt.")
        self.inhalt.setText("\n".join(f"{titel}: {len(p)} Punkte" for titel, p in agenda.abschnitte(self.conn)))
        self.oeffnen_knopf.setVisible(self.pfad is not None)

    def aendern(self):
        zeile = zeilen_id(self.tab)
        if zeile is None:
            return self.f.melde("Wähle zuerst eine Angabe.")
        r = self.reihen[zeile]
        neu, ok = QInputDialog.getMultiLineText(self, r["titel"], r["frage"], r["wert"])
        if ok and neu.strip() and neu.strip() != r["wert"]:
            self.f.starte("Angabe ändern", lambda conn, _: angaben.beantworte(conn, r["schluessel"], neu))

    def erstellen(self):
        self.f.starte("Agenda erstellen", lambda conn, _: agenda.erstelle(conn), nach=self._erstellt)

    def _erstellt(self, pfad):
        self.pfad = pfad
        self.stand.setText(f"Erstellt: {pfad}")

    def oeffnen(self):
        if self.pfad:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.pfad)))


class GelerntSeite(Seite):
    titel = "Gelernt"
    untertitel = "Was die lokale KI aus deinen Rückmeldungen und Ergebnissen mitnimmt"

    def __init__(self, f):
        super().__init__(f)
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        links = Karte("Lektionen")
        self.tab = tabelle(["Inhalt", "Art", "Ergebnis", "Datum"], [None, 85, 110, 95])
        links.lage.addWidget(self.tab)
        links.lage.addWidget(knopf("Lektion vergessen", self.vergessen))
        lage.addWidget(links, 3)
        rechts = QVBoxLayout()
        rechts.setSpacing(18)
        regel = Karte("Regel beibringen")
        self.regel = QLineEdit()
        self.regel.setPlaceholderText("Zum Beispiel: Kürzer schreiben, keine Floskeln")
        regel.lage.addWidget(self.regel)
        regel.lage.addWidget(knopf("Merken", self.merken, True))
        rechts.addWidget(regel)
        info = Karte("So lernt sie")
        self.info = label("", "gedimmt", True)
        info.lage.addWidget(self.info)
        rechts.addWidget(info)
        rechts.addStretch()
        lage.addLayout(rechts, 2)

    def aktualisieren(self):
        reihen = self.conn.execute("SELECT * FROM lektionen ORDER BY id DESC").fetchall()
        art = {"regel": "Regel", "korrektur": "Korrektur", "bewertung": "Bewertung", "vorbild": "Vorbild"}
        inhalt = lambda r: f"{r['nachher']}/100: {r['notiz']}" if r["art"] == "bewertung" else r["nachher"][:200]
        fuelle(self.tab, [(inhalt(r), art[r["art"]], STATUS_TEXT.get(r["ergebnis"], ""), r["erstellt"][:10]) for r in reihen],
               [r["id"] for r in reihen])
        s = lernen.statistik(self.conn)
        self.info.setText(
            f"{s['regeln']} Regeln, {s['korrekturen']} Schreibbeispiele, {s['bewertungen']} Bewertungen.\n\n"
            "Die passendsten Lektionen werden per RAG in jeden neuen Prompt eingespielt. Mails mit Antwort zählen stärker. "
            "Die Gewichte des Modells ändern sich dabei nicht.")

    def merken(self):
        text = self.regel.text()
        try:
            lernen.merke_regel(self.conn, text)
        except ValueError as fehler:
            return self.f.melde(str(fehler))
        self.regel.clear()
        self.f.neu_laden()

    def vergessen(self):
        lid = zeilen_id(self.tab)
        if lid is None:
            return self.f.melde("Wähle zuerst eine Lektion.")
        self.conn.execute("DELETE FROM lektionen WHERE id = ?", (lid,))
        self.conn.commit()
        self.f.neu_laden()


class SystemSeite(Seite):
    titel = "System"
    untertitel = "Sprachmodell und Datenablage"

    def __init__(self, f):
        super().__init__(f)
        raster = QGridLayout(self)
        raster.setContentsMargins(0, 0, 0, 0)
        raster.setSpacing(18)
        modell = Karte("Sprachmodell")
        self.status = label("", "gedimmt", True)
        modell.lage.addWidget(self.status)
        self.auswahl = QComboBox()
        self.auswahl.activated.connect(self.waehle)
        modell.lage.addWidget(self.auswahl)
        raster.addWidget(modell, 0, 0)
        ablage = Karte("Daten")
        ablage.lage.addWidget(label(f"Datenbank: {config.DB_PFAD}\nEmbeddings: {config.EMBEDDING_MODELL}\n"
                                    f"Analyse: jupyter lab analyse.ipynb", "gedimmt", True))
        raster.addWidget(ablage, 0, 1)
        raster.setRowStretch(1, 1)

    def aktualisieren(self):
        aktuell = self.f.modell()
        self.auswahl.clear()
        try:
            namen = llm.modelle()
            self.status.setText(f"Ollama läuft, {len(namen)} Sprachmodelle installiert.")
        except llm.LLMFehler as fehler:
            namen = []
            self.status.setText(str(fehler))
        claude = llm.claude_status()
        if not claude:
            namen = [*llm.CLAUDE_MODELLE, *namen]
            stand = llm.claude_nutzung()
            werte = ", ".join(f"{n} {round((stand.get(k) or {}).get('utilization', 0) * 100)} %"
                              for k, n in (("five_hour", "5 Stunden"), ("seven_day", "Woche")) if k in stand)
            self.status.setText(
                self.status.text() + " Claude Code ist angemeldet: Bei „claude:“ gehen Unterlagen, Fakten und Anzeigen an "
                f"Anthropic. Ab {round(llm.CLAUDE_GRENZE * 100)} % Kontingent nimmt die App das lokale Modell"
                + (f", zuletzt {werte}." if werte else ".") + (f" {llm.claude_gesperrt()}" if llm.claude_gesperrt() else ""))
        else:
            self.status.setText(self.status.text() + f" {claude}")
        self.auswahl.addItems(namen or [aktuell])
        self.auswahl.setCurrentText(aktuell)
        if namen and aktuell not in namen:
            self.status.setText(f"Modell {aktuell} fehlt: " + ("Claude Code anmelden" if llm.ist_claude(aktuell)
                                                                else f"ollama pull {aktuell}"))

    def waehle(self):
        db.setze_einstellung(self.conn, "modell", self.auswahl.currentText())
        self.f.neu_laden()


class GesendetSeite(Seite):
    titel = "Gesendet"
    untertitel = "Was rausging, an wen, mit welchen Anhängen und was daraus wurde"

    def __init__(self, f):
        super().__init__(f)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(18)
        oben = Karte("Gesendete Mails")
        self.tab = tabelle(["Gesendet", "Firma", "An", "Betreff", "Status", "Prüfung"], [130, 190, 210, None, 110, 150])
        self.tab.itemSelectionChanged.connect(self.lade)
        oben.lage.addWidget(self.tab)
        lage.addWidget(oben, 2)
        unten = Karte("Mail")
        self.kopf = label("", "gedimmt", True)
        unten.lage.addWidget(self.kopf)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        unten.lage.addWidget(self.text, 1)
        self.befunde = QVBoxLayout()
        unten.lage.addLayout(self.befunde)
        self.anhang = label("", "gedimmt", True)
        self.anhang.setTextFormat(Qt.RichText)
        self.anhang.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.anhang.linkActivated.connect(lambda pfad: QDesktopServices.openUrl(QUrl.fromLocalFile(pfad)))
        unten.lage.addWidget(self.anhang)
        reihe = QHBoxLayout()
        self.neu_knopf = knopf("Als neuen Entwurf an die aktuelle Adresse", self.neu_entwurf, True)
        self.neu_knopf.setToolTip("Kopiert die Mail in einen Entwurf an die Adresse, die jetzt bei der Firma steht. "
                                  "Er muss wie jeder Entwurf geprüft und freigegeben werden.")
        reihe.addWidget(self.neu_knopf)
        reihe.addStretch()
        reihe.addWidget(label("Ergebnis:", "gedimmt"))
        for status, text in (("antwort", "Antwort"), ("absage", "Absage"), ("keine_antwort", "Keine Antwort")):
            reihe.addWidget(knopf(text, lambda _=False, s=status: self.status(s)))
        unten.lage.addLayout(reihe)
        lage.addWidget(unten, 3)
        self.zeilen: dict[int, dict] = {}

    def aktualisieren(self):
        mails = ki.gesendete(self.conn)
        self.zeilen = {m["id"]: m for m in mails}

        def pruefung(m):
            if any(b.stufe == "sperre" for b in m["befunde"]):
                return "✕ falsche Adresse?"
            return "! Adresse prüfen" if m["befunde"] else "✓"
        fuelle(self.tab, [((m["gesendet_am"] or m["erstellt"])[:16].replace("T", " "), m["firma"], m["empfaenger"], m["betreff"],
                           STATUS_TEXT.get(m["status"], m["status"]), pruefung(m)) for m in mails], list(self.zeilen))
        if self.tab.rowCount() and zeilen_id(self.tab) is None:
            self.tab.selectRow(0)
        self.lade()

    def lade(self):
        m = self.zeilen.get(zeilen_id(self.tab))
        leere(self.befunde)
        self.neu_knopf.setEnabled(bool(m))
        if not m:
            self.kopf.setText("Noch nichts gesendet.")
            self.text.clear()
            self.anhang.clear()
            return
        self.kopf.setText(f"An: {m['empfaenger']}\nBetreff: {m['betreff']}\n"
                          f"Gesendet: {(m['gesendet_am'] or '')[:16].replace('T', ' ')} · Status: {STATUS_TEXT.get(m['status'], m['status'])}"
                          + (f"\nAktuelle Adresse der Firma: {m['firma_email']}" if m["firma_email"]
                             and m["firma_email"].lower() != m["empfaenger"].lower() else ""))
        self.text.setPlainText(m["text"])
        self.anhang.setText(anhang_links(profil.anhaenge_des_entwurfs(self.conn, m["id"])))
        for b in m["befunde"]:
            self.befunde.addWidget(label(("✕ " if b.stufe == "sperre" else "! ") + b.text, b.stufe, True))

    def neu_entwurf(self):
        eid = zeilen_id(self.tab)
        if eid is None:
            return
        try:
            ki.als_neuer_entwurf(self.conn, eid)
        except ki.NichtMoeglich as fehler:
            return self.f.melde(str(fehler))
        self.f.melde("Neuer Entwurf liegt unter „Entwürfe“. Prüfe ihn und gib ihn frei.")
        self.f.zeige_seite(EntwurfSeite)

    def status(self, status):
        eid = zeilen_id(self.tab)
        if eid is None:
            return
        self.f.starte("Status speichern", lambda conn, _: ki.setze_status(conn, eid, status))


class ProtokollSeite(Seite):
    titel = "Protokoll"
    untertitel = "Was die App wann getan hat"

    def __init__(self, f):
        super().__init__(f)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(0, 0, 0, 0)
        karte = Karte("Ereignisse")
        self.tab = tabelle(["Zeit", "Aktion", "Firma", "Detail"], [170, 170, 210, None])
        karte.lage.addWidget(self.tab)
        lage.addWidget(karte)

    def aktualisieren(self):
        reihen = self.conn.execute(
            "SELECT e.*, f.name FROM ereignisse e LEFT JOIN firmen f ON f.id = e.firma_id ORDER BY e.id DESC LIMIT 500").fetchall()
        fuelle(self.tab, [(r["zeit"], r["aktion"], r["name"], r["detail"]) for r in reihen], [r["id"] for r in reihen])


class Hauptfenster(QMainWindow):
    NAVIGATION = [
        ("ARBEIT", [StartSeite, BerichtSeite, SucheSeite, FirmenSeite, EntwurfSeite, GesendetSeite]),
        ("PROFIL", [ProfilSeite, FaktenSeite, UnterlagenSeite, CVSeite, AgendaSeite, MailkontoSeite]),
        ("KI", [GelerntSeite, SystemSeite]),
        ("ARCHIV", [ProtokollSeite]),
    ]

    def __init__(self, einrichten=True, sitzung_konto=None, sitzung_passwort=None, beim_schliessen=None, beim_abmelden=None):
        super().__init__()
        self.sitzung_konto = sitzung_konto
        self.sitzung_passwort = sitzung_passwort
        self.beim_schliessen = beim_schliessen
        self.beim_abmelden = beim_abmelden
        self.abmelden_gewuenscht = False
        self.setWindowTitle("Job Agent")
        self.resize(1320, 840)
        self.conn = db.verbinden()
        self.einstellungen = QSettings("jobsuche", "jobsuche")
        self.hell = self.einstellungen.value("hell", False, type=bool)
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)  # Die CPU ist knapp: immer nur eine Arbeit.
        self.warteschlange = 0
        self.naechste_nummer = 0
        self.auftraege: dict[int, tuple] = {}
        self.signale = Signale()
        self.signale.fertig.connect(self._fertig)
        self.signale.fehler.connect(self._fehler)
        self.signale.fortschritt.connect(self._fortschritt)
        self.probleme: list[str] = []
        self._baue()
        self._wende_thema_an()
        self.zeige(0)
        if config.DEMO:
            self.starte("Demo vorbereiten", lambda conn, _: demo.befuellen(conn))
        elif einrichten:
            self.starte("Einrichten", self._einrichten, nach=self.setze_probleme)

    @staticmethod
    def _einrichten(conn, fort):
        fort("Unterlagen")
        return lies_und_uebernimm(conn)

    def _baue(self):
        wurzel = QWidget()
        wurzel.setObjectName("inhalt")
        self.setCentralWidget(wurzel)
        lage = QHBoxLayout(wurzel)
        lage.setContentsMargins(0, 0, 0, 0)
        lage.setSpacing(0)

        leiste = QFrame()
        leiste.setObjectName("sidebar")
        leiste.setFixedWidth(240)
        seitenlage = QVBoxLayout(leiste)
        seitenlage.setContentsMargins(14, 18, 14, 14)
        seitenlage.setSpacing(2)
        seitenlage.addWidget(label("◆ Job Agent", "logo"))
        seitenlage.addWidget(label("Firmen · Fakten · Lokale KI", "sidebarUnter"))
        seitenlage.addSpacing(10)

        self.stapel = QStackedWidget()
        self.seiten: list[Seite] = []
        self.gruppe = QButtonGroup(self)
        for sektion, klassen in self.NAVIGATION:
            seitenlage.addWidget(label(sektion, "navSektion"))
            for klasse in klassen:
                seite = klasse(self)
                index = len(self.seiten)
                self.seiten.append(seite)
                self.stapel.addWidget(seite)
                b = QPushButton(klasse.titel)
                b.setObjectName("navEintrag")
                b.setCheckable(True)
                b.clicked.connect(lambda _=False, i=index: self.zeige(i))
                self.gruppe.addButton(b, index)
                seitenlage.addWidget(b)
        seitenlage.addStretch()

        fuss = QHBoxLayout()
        self.avatar = QLabel()
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(34, 34)
        self.avatar.setAlignment(Qt.AlignCenter)
        fuss.addWidget(self.avatar)
        self.fuss_text = QPushButton()
        self.fuss_text.setObjectName("fussProfil")
        self.fuss_text.setToolTip("Zum Profil")
        self.fuss_text.clicked.connect(lambda: self.zeige_seite(ProfilSeite))
        fuss.addWidget(self.fuss_text, 1)
        seitenlage.addLayout(fuss)
        self.abmelden_knopf = QPushButton("Demo beenden" if config.DEMO else "Abmelden")
        self.abmelden_knopf.clicked.connect(self.abmelden)
        seitenlage.addSpacing(6)
        seitenlage.addWidget(self.abmelden_knopf)
        lage.addWidget(leiste)

        rechts = QVBoxLayout()
        rechts.setContentsMargins(28, 22, 28, 22)
        rechts.setSpacing(20)
        kopf = QHBoxLayout()
        kopf.setSpacing(14)
        self.ring = Ring()
        kopf.addWidget(self.ring)
        titel = QVBoxLayout()
        titel.setSpacing(1)
        self.titel = label("", "titel")
        self.untertitel = label("", "gedimmt")
        titel.addWidget(self.titel)
        titel.addWidget(self.untertitel)
        kopf.addLayout(titel, 1)
        self.pill = label("", "pill")
        self.pill.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        pill_spalte = QVBoxLayout()
        pill_spalte.setSpacing(4)
        pill_spalte.addWidget(self.pill)
        self.balken = QProgressBar()
        self.balken.setObjectName("fortschritt")
        self.balken.setTextVisible(False)
        self.balken.setFixedHeight(6)
        self.balken.hide()
        pill_spalte.addWidget(self.balken)
        kopf.addLayout(pill_spalte)
        self.thema_knopf = QPushButton()
        self.thema_knopf.setObjectName("themaKnopf")
        self.thema_knopf.clicked.connect(self.thema_wechseln)
        kopf.addWidget(self.thema_knopf)
        rechts.addLayout(kopf)
        rechts.addWidget(self.stapel, 1)
        lage.addLayout(rechts, 1)

    def _wende_thema_an(self):
        t = theme.HELL if self.hell else theme.DUNKEL
        QApplication.instance().setStyleSheet(theme.stylesheet(t))
        self.thema_knopf.setText("☾ Dunkel" if self.hell else "☀ Hell")
        self.ring.farbe = QColor(t["akzent"])
        self.ring.update()
        # Farbe über die Palette, nicht übers Stylesheet: Eine ::chunk-Regel im App-Stylesheet ließ Qt beim Neupolieren abstürzen.
        palette = self.balken.palette()
        palette.setColor(QPalette.Highlight, QColor(t["warn"]))
        palette.setColor(QPalette.Base, QColor(t["warn_bg"]))
        palette.setColor(QPalette.Window, QColor(t["warn_bg"]))
        self.balken.setPalette(palette)

    def thema_wechseln(self):
        self.hell = not self.hell
        self.einstellungen.setValue("hell", self.hell)
        self._wende_thema_an()

    def modell(self) -> str:
        return db.einstellung(self.conn, "modell", config.STANDARD_MODELL)

    def biete_claude_an(self) -> None:
        """Einmal pro Person: Ist Claude Code angemeldet, fragt die App, ob sie es statt des lokalen Modells nutzen darf."""
        if config.DEMO or llm.ist_claude(self.modell()) or db.einstellung(self.conn, "claude.gefragt", ""):
            return
        if llm.claude_status():
            return
        db.setze_einstellung(self.conn, "claude.gefragt", db.jetzt())
        if self.frage("Claude Code ist auf diesem Rechner angemeldet. Soll Job Agent Claude für Eignungsprüfung, "
                      "Firmenbewertung und Mailtexte nutzen? Das ist deutlich genauer und schneller als das lokale Modell.\n\n"
                      "Dabei gehen Lebenslauf, Zeugnis, bestätigte Fakten und Stellenanzeigen an Anthropic, über dein Abo. "
                      "Gesendet wird weiter nur auf deinen Klick. Zurück zum lokalen Modell geht es jederzeit unter „System“."):
            db.setze_einstellung(self.conn, "modell", llm.CLAUDE_MODELLE[0])
            self.neu_laden()

    def zeige(self, index: int):
        self.stapel.setCurrentIndex(index)
        self.gruppe.button(index).setChecked(True)
        seite = self.seiten[index]
        self.titel.setText(seite.titel)
        self.untertitel.setText(seite.untertitel)
        self.neu_laden()

    def zeige_seite(self, klasse):
        self.zeige(next(i for i, s in enumerate(self.seiten) if isinstance(s, klasse)))

    def neu_laden(self):
        self.stapel.currentWidget().aktualisieren()
        name = person.name(self.conn)
        self.avatar.setText(person.initialen(name))
        self.fuss_text.setText(f"{name or 'Noch kein Profil'}\nJob Agent {__version__}\nKI: {self.modell()} · lokal")
        if not self.warteschlange:
            self._zeige_stand()

    def _zeige_stand(self):
        z = lambda sql: self.conn.execute(sql).fetchone()[0]
        self._setze_pill(
            f"{z('SELECT count(*) FROM firmen')} Firmen · "
            f"{z('''SELECT count(*) FROM fakten WHERE status = 'bestaetigt' ''')} Fakten · "
            f"{z('''SELECT count(*) FROM entwuerfe WHERE status = 'antwort' ''')} Antworten"
            + (" · Demo, nichts wird gesendet" if config.DEMO else ""), False)

    def _setze_pill(self, text, beschaeftigt):
        self.pill.setObjectName("pillBeschaeftigt" if beschaeftigt else "pill")
        self.pill.setText(text)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

    def starte(self, titel, funktion, nach=None, still=False):
        self.naechste_nummer += 1
        self.auftraege[self.naechste_nummer] = (titel, nach, still)
        self.warteschlange += 1
        if not still:
            self._setze_pill(f"Läuft: {titel}", True)
        self.pool.start(Aufgabe(self.naechste_nummer, funktion, self.signale))

    def _fortschritt(self, nummer, text):
        self._setze_pill(f"{self.auftraege[nummer][0]}: {text}", True)
        self._zeige_balken(text)

    def _zeige_balken(self, text):
        """„12 von 40“ im Fortschrittstext füllt den Balken. Ohne Zählung bleibt er weg: Der unbestimmte Modus läuft über
        eine Stil-Animation, die beim Schließen des Fensters abstürzen kann."""
        treffer = FORTSCHRITT.search(text or "")
        if not treffer or int(treffer.group(2)) <= 0:
            self.balken.hide()
            return
        self.balken.setRange(0, int(treffer.group(2)))
        self.balken.setValue(min(int(treffer.group(1)), int(treffer.group(2))))
        self.balken.show()

    def _fertig(self, nummer, ergebnis):
        _, nach, still = self.auftraege.pop(nummer)
        self.warteschlange -= 1
        if nach:
            nach(ergebnis)
        self._nach_aufgabe(still)

    def _fehler(self, nummer, meldung):
        _, _, still = self.auftraege.pop(nummer)
        self.warteschlange -= 1
        self.melde(meldung)
        self._nach_aufgabe(still)

    def _nach_aufgabe(self, still):
        if not self.warteschlange:
            self.balken.hide()
        # Eine stille Aufgabe darf die Seite nicht neu laden: Das würde die Auswahl erneut auslösen.
        if not still:
            self.neu_laden()
        elif not self.warteschlange:
            self._zeige_stand()

    def setze_probleme(self, probleme):
        self.probleme = probleme or []

    def melde(self, text):
        QMessageBox.warning(self, "Job Agent", text)

    def frage(self, text) -> bool:
        return QMessageBox.question(self, "Job Agent", text) == QMessageBox.Yes

    def nachfragen(self, punkte: list[str]) -> dict[str, str] | None:
        dialog = NachfrageDialog(punkte, self)
        return dialog.antworten() if dialog.exec() == QDialog.Accepted else None

    def frage_passwort(self, konto) -> str | None:
        passwort, ok = QInputDialog.getText(self, "Mailkonto", f"Passwort für {konto.benutzer} auf {konto.host}:",
                                            QLineEdit.Password)
        return passwort if ok and passwort else None

    def mit_passwort(self, konto, titel, arbeit, erfolg=None):
        """Nimmt das Passwort aus dem Schlüsselbund oder fragt danach. Nach Erfolg bietet sie das Speichern an."""
        aus_sitzung = self.sitzung_passwort if konto == self.sitzung_konto else None
        gespeichert = aus_sitzung or versand.gespeichertes_passwort(konto)
        passwort = gespeichert or self.frage_passwort(konto)
        if not passwort:
            return

        def fertig(ergebnis):
            if not gespeichert and self.frage("Das Passwort hat funktioniert. Im Schlüsselbund des Systems speichern?\n"
                                              "Ohne Speichern fragt die App beim nächsten Mal wieder."):
                try:
                    versand.speichere_passwort(konto, passwort)
                except versand.VersandFehler as fehler:
                    self.melde(str(fehler))
            if erfolg:
                erfolg(ergebnis)

        self.starte(titel, lambda conn, fort: arbeit(conn, fort, passwort), nach=fertig)

    def abmelden(self):
        if self.warteschlange:
            return self.melde("Es läuft noch eine Arbeit. Warte kurz, bis sie fertig ist.")
        self.abmelden_gewuenscht = True
        self.close()

    def closeEvent(self, ereignis):
        self.pool.clear()
        self.pool.waitForDone(2000)
        self.sitzung_passwort = None
        super().closeEvent(ereignis)
        if self.abmelden_gewuenscht and self.beim_abmelden:
            self.beim_abmelden()
        elif not self.abmelden_gewuenscht and self.beim_schliessen:
            self.beim_schliessen()


class Sitzung:
    """Führt durch Splash, Einrichtung, Anmelden, Arbeiten und Abmelden."""

    def __init__(self, app):
        self.app = app
        self.fenster = None
        self.splash = None
        self.pool = QThreadPool()
        self.signale = start._Signale()
        self.signale.fertig.connect(self._geprueft)
        self.signale.fehler.connect(self._pruefung_fehlgeschlagen)
        self.signale.meldung.connect(lambda t: self.splash and self.splash.melde(t))

    def los(self):
        hell = QSettings("jobsuche", "jobsuche").value("hell", False, type=bool)
        self.app.setStyleSheet(theme.stylesheet(theme.HELL if hell else theme.DUNKEL))
        self.app.setWindowIcon(start.pinguin_icon())
        self.splash = start.Splash()
        self.splash.show()
        self.splash.laufe()
        start.pruefe_im_hintergrund(self.pool, self.signale)

    def _geprueft(self, ergebnis):
        info, pruefungen = ergebnis
        # Nur Fehlendes unterbricht den Start. Der Splash läuft seine Mindestdauer zu Ende, damit man ihn sieht.
        pflicht_fehlt = not einrichtung.ist_startklar(pruefungen)
        erster_start = not QSettings("jobsuche", "jobsuche").value("eingerichtet", False, type=bool)
        if pflicht_fehlt or erster_start:
            self.splash.melde("Einrichtung nötig …")
            self.splash.fertig(lambda: self._einrichten(info, pruefungen))
        else:
            self.splash.melde("Alles bereit.")
            self.splash.fertig(self._nach_splash)

    def _pruefung_fehlgeschlagen(self, text):
        self.splash.melde(text)
        self.splash.fertig(self._nach_splash)

    def _einrichten(self, info, pruefungen):
        self.splash.close()
        fenster_einrichten = start.EinrichtFenster(info, pruefungen, start.speichere_icon())
        if fenster_einrichten.exec() != QDialog.Accepted:
            return self.app.quit()
        QSettings("jobsuche", "jobsuche").setValue("eingerichtet", True)
        self.starten()

    def _nach_splash(self):
        self.splash.close()
        self.starten()

    def starten(self):
        app = self.app
        anmelden = AnmeldeFenster()
        if anmelden.exec() != QDialog.Accepted:
            return app.quit()
        if anmelden.demo:
            demo.vorbereiten()
            self.fenster = Hauptfenster(beim_schliessen=self.beenden, beim_abmelden=self.abgemeldet)
        else:
            anmeldung.oeffne(anmelden.ergebnis, anmelden.email.text(), anmelden.passwort, anmelden.name.text())
            self.fenster = Hauptfenster(sitzung_konto=anmelden.ergebnis.konto, sitzung_passwort=anmelden.passwort,
                                        beim_schliessen=self.beenden, beim_abmelden=self.abgemeldet)
            if anmelden.ergebnis.offline:
                self.fenster.melde("Der Mailserver ist nicht erreichbar. Du arbeitest ohne Netz, senden geht gerade nicht.")
            elif anmelden.warnung:
                self.fenster.melde(anmelden.warnung)
        self.fenster.show()
        self.fenster.biete_claude_an()

    def abgemeldet(self):
        self.fenster.conn.close()
        demo.aufraeumen()
        config.DEMO = False
        QTimer.singleShot(0, self.starten)

    def beenden(self):
        demo.aufraeumen()
        self.app.quit()


def _stapel_bei_signal():
    """`kill -USR1 <pid>` schreibt die Aufrufstapel aller Threads nach ~/.cache/jobagent/stapel.log.
    So lässt sich ein Hänger ohne Root-Rechte untersuchen."""
    import faulthandler
    import signal
    from pathlib import Path as _P
    try:
        ordner = _P.home() / ".cache" / "jobagent"
        ordner.mkdir(parents=True, exist_ok=True)
        datei = open(ordner / "stapel.log", "a", buffering=1)
        faulthandler.register(signal.SIGUSR1, file=datei, all_threads=True)
        return datei
    except (OSError, AttributeError, ValueError):
        return None


def main() -> int:
    _stapel = _stapel_bei_signal()  # noqa: F841 – die Datei muss offen bleiben
    app = QApplication(sys.argv)
    app.setApplicationName("Job Agent")
    app.setQuitOnLastWindowClosed(False)  # Zwischen Splash, Einrichtung und Anmeldung ist kurz kein Fenster offen.
    sitzung = Sitzung(app)
    QTimer.singleShot(0, sitzung.los)
    return app.exec()
