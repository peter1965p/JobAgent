"""Splash mit Pinguin und das Einrichtungsfenster."""

from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import config, einrichtung, system

SPLASH_BILD = Path(__file__).with_name("splash_pinguin.jpg")


def male_pinguin(p: QPainter, rahmen: QRectF) -> None:
    """Ein eigener Pinguin mit Brille und Kapuzenpulli, leicht nach links gedreht. Alle Maße sind relativ, er skaliert also beliebig."""
    x, y, b, h = rahmen.x(), rahmen.y(), rahmen.width(), rahmen.height()

    def r(a, c, d, e):
        return QRectF(x + a * b, y + c * h, d * b, e * h)

    def pt(a, c):
        return QPointF(x + a * b, y + c * h)

    def aermel(mitte_x, mitte_y, winkel):
        p.save()
        p.translate(pt(mitte_x, mitte_y))
        p.rotate(winkel)
        p.setBrush(stoff)
        p.drawRoundedRect(QRectF(-0.075 * b, -0.17 * h, 0.15 * b, 0.32 * h), 0.07 * b, 0.07 * b)
        p.setBrush(schwarz)
        p.drawEllipse(QRectF(-0.07 * b, 0.11 * h, 0.14 * b, 0.11 * h))  # Handschuh
        p.restore()

    schwarz, weiss, gelb = QColor("#20232b"), QColor("#f6f4ee"), QColor("#f5a623")
    stoff, schatten = QColor("#8f9198"), QColor("#6f7178")
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor("#d68c12"), max(1.0, b * 0.012)))
    p.setBrush(gelb)
    p.drawEllipse(r(0.20, 0.89, 0.28, 0.10))  # Füße
    p.drawEllipse(r(0.52, 0.89, 0.28, 0.10))
    p.setPen(Qt.NoPen)
    p.setBrush(schatten)
    p.drawEllipse(r(0.24, 0.24, 0.62, 0.30))  # Kapuze hinter dem Kopf
    p.setBrush(stoff)
    p.drawRoundedRect(r(0.20, 0.38, 0.60, 0.55), 0.16 * b, 0.16 * b)  # Pulli
    aermel(0.19, 0.58, 14)
    aermel(0.81, 0.58, -14)
    p.setBrush(schatten)
    tasche = QPainterPath(pt(0.36, 0.68))
    tasche.lineTo(pt(0.64, 0.68))
    tasche.lineTo(pt(0.71, 0.84))
    tasche.lineTo(pt(0.29, 0.84))
    tasche.closeSubpath()
    p.drawPath(tasche)
    p.setPen(QPen(weiss, max(1.0, b * 0.012)))
    p.drawLine(pt(0.44, 0.44), pt(0.43, 0.60))  # Kordeln
    p.drawLine(pt(0.56, 0.44), pt(0.57, 0.60))
    p.setPen(Qt.NoPen)
    p.setBrush(schwarz)
    p.drawEllipse(r(0.24, 0.01, 0.54, 0.47))  # Kopf
    p.setBrush(weiss)
    p.drawEllipse(r(0.25, 0.14, 0.40, 0.32))  # Gesicht
    p.setBrush(schwarz)
    p.drawEllipse(r(0.345, 0.205, 0.055, 0.065))  # Augen
    p.drawEllipse(r(0.525, 0.205, 0.055, 0.065))
    p.setPen(QPen(QColor("#b8bac2"), max(1.0, b * 0.016)))
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawRoundedRect(r(0.285, 0.165, 0.175, 0.14), 0.035 * b, 0.035 * b)  # Brille
    p.drawRoundedRect(r(0.485, 0.165, 0.175, 0.14), 0.035 * b, 0.035 * b)
    p.drawLine(pt(0.46, 0.215), pt(0.485, 0.215))
    p.drawLine(pt(0.66, 0.215), pt(0.77, 0.20))
    p.setPen(Qt.NoPen)
    schnabel = QPainterPath(pt(0.32, 0.30))
    schnabel.quadTo(pt(0.46, 0.27), pt(0.54, 0.33))
    schnabel.quadTo(pt(0.40, 0.45), pt(0.12, 0.37))
    schnabel.quadTo(pt(0.20, 0.31), pt(0.32, 0.30))
    p.setBrush(gelb)
    p.drawPath(schnabel)
    p.setPen(QPen(QColor("#b9770e"), max(1.0, b * 0.01)))
    p.drawLine(pt(0.13, 0.37), pt(0.50, 0.35))
    p.restore()


def pinguin_bild(groesse: int = 256) -> QPixmap:
    bild = QPixmap(groesse, groesse)
    bild.fill(Qt.transparent)
    p = QPainter(bild)
    male_pinguin(p, QRectF(0, 0, groesse, groesse))
    p.end()
    return bild


def pinguin_icon() -> QIcon:
    return QIcon(pinguin_bild())


def speichere_icon() -> "config.Path":
    ziel = config.DATEN / "tueroeffner.png"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    pinguin_bild().save(str(ziel))
    return ziel


class Splash(QWidget):
    """Steht mindestens MIN_DAUER_MS. Der Balken läuft mit der Zeit bis 90 %, den Rest gibt erst die fertige Prüfung frei."""
    MIN_DAUER_MS = 4000
    TAKT_MS = 30

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(600, 340)
        self.bild = QPixmap(str(SPLASH_BILD))
        self.status = "Starte …"
        self.protokoll: list[str] = []  # die letzten Schritte, jede neue Zeile fährt von unten herein
        self._schub = 0.0
        self.fortschritt = 0.0
        self._danach = None
        self._uhr = QElapsedTimer()
        self._takt = QTimer(self)
        self._takt.setInterval(self.TAKT_MS)
        self._takt.timeout.connect(self._tick)

    def laufe(self) -> None:
        self._uhr.start()
        self._takt.start()

    def fertig(self, danach) -> None:
        """Füllt den Balken auf und ruft `danach`, sobald die Mindestdauer um ist."""
        self._danach = danach
        if not self._takt.isActive():
            self.laufe()

    def _tick(self) -> None:
        self._schub = max(0.0, self._schub * 0.75 - 0.3)
        zeit = self._uhr.elapsed() / self.MIN_DAUER_MS
        if self._danach is None:
            self.fortschritt = min(zeit, 0.9)
        else:
            self.fortschritt = min(1.0, max(self.fortschritt + 0.03, zeit))
            if self.fortschritt >= 1.0 and zeit >= 1.0:
                self._takt.stop()
                danach, self._danach = self._danach, None
                danach()
        self.update()

    ZEILEN = 5
    ZEILENHOEHE = 17

    def melde(self, text: str) -> None:
        """„… Verbinde …“ zeigt, was gerade läuft; „✓/✗/• Ergebnis“ ersetzt diese Zeile. Andere Texte sind Status."""
        self.status = text
        if text.startswith(("✓ ", "✗ ", "• ")) and self.protokoll and self.protokoll[-1].startswith("… "):
            self.protokoll[-1] = text  # Ergebnis ersetzt die laufende Zeile, ohne neue Fahrt
        else:
            self.protokoll = (self.protokoll + [text])[-(self.ZEILEN + 1):]
            self._schub = float(self.ZEILENHOEHE)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        rand = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        form = QPainterPath()
        form.addRoundedRect(rand, 18, 18)
        p.fillPath(form, QColor("#1a1c22"))
        # Der Pinguin sitzt rechts und schaut zum Schriftzug, sein Bild läuft nach links in den Hintergrund aus.
        p.save()
        p.setClipPath(form)
        bild = QRectF(290, 0, 310, 340)
        p.drawPixmap(bild, self.bild, QRectF(self.bild.rect()))
        verlauf = QLinearGradient(290, 0, 390, 0)
        verlauf.setColorAt(0, QColor(26, 28, 34, 255))
        verlauf.setColorAt(1, QColor(26, 28, 34, 0))
        p.fillRect(QRectF(290, 0, 100, 340), verlauf)
        p.restore()
        p.setPen(QPen(QColor("#383b47"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(form)

        p.setPen(QColor("#e7e8ec"))
        schrift = QFont()
        schrift.setPixelSize(36)
        schrift.setBold(True)
        p.setFont(schrift)
        p.drawText(QRectF(32, 84, 270, 50), Qt.AlignLeft | Qt.AlignVCenter, "Job Agent")
        schrift.setPixelSize(15)
        schrift.setBold(False)
        p.setFont(schrift)
        p.setPen(QColor("#9a9eab"))
        p.drawText(QRectF(34, 136, 250, 48), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, "Direkt zum Chef, lokal und mit Freigabe")
        # Was gerade geprüft und verbunden wird: unten die neueste Zeile, ältere wandern nach oben und verblassen.
        schrift.setPixelSize(12)
        p.setFont(schrift)
        feld = QRectF(34, 186, 252, self.ZEILEN * self.ZEILENHOEHE)
        p.save()
        p.setClipRect(feld)
        zeilen = self.protokoll[-(self.ZEILEN + 1):]
        for nummer, zeile in enumerate(reversed(zeilen)):
            y = feld.bottom() - (nummer + 1) * self.ZEILENHOEHE + self._schub
            farbe = QColor("#39ff14" if zeile.startswith("✓ ") else "#ff6b5b" if zeile.startswith("✗ ")
                           else "#9a9eab" if zeile.startswith("• ") else "#f5a623")
            farbe.setAlphaF(max(0.25, 1.0 - nummer * 0.17))
            p.setPen(farbe)
            text = p.fontMetrics().elidedText(zeile, Qt.ElideRight, int(feld.width()))
            p.drawText(QRectF(feld.x(), y, feld.width(), self.ZEILENHOEHE), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.restore()
        # Fortschrittsbalken mit Prozent
        balken = QRectF(34, 296, 210, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#383b47"))
        p.drawRoundedRect(balken, 3, 3)
        if self.fortschritt > 0:
            p.setBrush(QColor("#f5a623"))
            p.drawRoundedRect(QRectF(balken.x(), balken.y(), max(6.0, balken.width() * self.fortschritt), balken.height()), 3, 3)
        schrift.setPixelSize(11)
        p.setFont(schrift)
        p.setPen(QColor("#9a9eab"))
        p.drawText(QRectF(250, 288, 40, 20), Qt.AlignLeft | Qt.AlignVCenter, f"{round(self.fortschritt * 100)} %")


class _Signale(QObject):
    fertig = Signal(object)
    fehler = Signal(str)
    meldung = Signal(str)
    prozent = Signal(int)


class _Arbeit(QRunnable):
    def __init__(self, funktion, signale):
        super().__init__()
        self.funktion, self.signale = funktion, signale

    def run(self):
        try:
            self.signale.fertig.emit(self.funktion(self.signale))
        except einrichtung.EinrichtFehler as fehler:
            self.signale.fehler.emit(str(fehler))
        except Exception as fehler:  # Auch unerwartete Fehler dürfen das Setup nicht still abbrechen.
            self.signale.fehler.emit(f"{type(fehler).__name__}: {fehler}")


class EinrichtFenster(QDialog):
    """Zeigt System und Prüfungen, nennt jeden geplanten Schritt und legt erst nach dem Klick los."""

    def __init__(self, sys_info, pruefungen, icon_pfad):
        super().__init__()
        self.setWindowTitle("Job Agent einrichten")
        self.setWindowIcon(pinguin_icon())
        self.setMinimumSize(760, 620)
        self.sys_info, self.pruefungen, self.icon_pfad = sys_info, pruefungen, icon_pfad
        self.pool = QThreadPool(self)
        self.signale = _Signale()
        self.signale.fertig.connect(self._fertig)
        self.signale.fehler.connect(self._fehler)
        self.signale.meldung.connect(self._protokoll)
        self.signale.prozent.connect(self._prozent)
        lage = QVBoxLayout(self)
        lage.setContentsMargins(26, 22, 26, 22)
        lage.setSpacing(12)
        kopf = QHBoxLayout()
        bild = QLabel()
        bild.setPixmap(pinguin_bild(72))
        kopf.addWidget(bild)
        texte = QVBoxLayout()
        titel = QLabel("Einrichtung")
        titel.setObjectName("titel")
        self.system_text = QLabel(f"Erkannt: {sys_info.beschreibung()}")
        self.system_text.setObjectName("gedimmt")
        self.system_text.setWordWrap(True)
        texte.addWidget(titel)
        texte.addWidget(self.system_text)
        kopf.addLayout(texte, 1)
        lage.addLayout(kopf)
        self.tab = QTableWidget(0, 3)
        self.tab.setHorizontalHeaderLabels(["", "Prüfung", "Ergebnis"])
        self.tab.verticalHeader().hide()
        self.tab.setShowGrid(False)
        self.tab.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tab.setSelectionMode(QAbstractItemView.NoSelection)
        self.tab.setColumnWidth(0, 34)
        self.tab.setColumnWidth(1, 190)
        self.tab.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        lage.addWidget(self.tab, 2)
        self.plan_text = QLabel("")
        self.plan_text.setWordWrap(True)
        lage.addWidget(self.plan_text)
        self.balken = QProgressBar()
        self.balken.setRange(0, 100)
        self.balken.hide()
        lage.addWidget(self.balken)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.hide()
        lage.addWidget(self.log)
        reihe = QHBoxLayout()
        self.los = QPushButton("Alles einrichten")
        self.los.setObjectName("primaer")
        self.los.clicked.connect(self.einrichten)
        self.weiter = QPushButton("Weiter")
        self.weiter.clicked.connect(self.accept)
        reihe.addWidget(self.los, 2)
        reihe.addWidget(self.weiter, 1)
        lage.addLayout(reihe)
        self._zeige()

    def _zeige(self):
        self.tab.setRowCount(len(self.pruefungen))
        for zeile, p in enumerate(self.pruefungen):
            zeichen = QTableWidgetItem("✓" if p.ok else ("✕" if p.pflicht else "!"))
            zeichen.setForeground(QColor("#3ecf8e" if p.ok else ("#f0716a" if p.pflicht else "#f5a623")))
            self.tab.setItem(zeile, 0, zeichen)
            self.tab.setItem(zeile, 1, QTableWidgetItem(p.titel))
            self.tab.setItem(zeile, 2, QTableWidgetItem(p.detail))
        self.tab.resizeRowsToContents()
        self.schritte = einrichtung.plan(self.pruefungen, self.sys_info, self.icon_pfad)
        startklar = einrichtung.ist_startklar(self.pruefungen)
        if self.schritte:
            self.plan_text.setText("Das würde ich jetzt tun:\n" + "\n".join(f"• {s.titel}: {s.zeigt}" for s in self.schritte))
        else:
            self.plan_text.setText("Alles ist eingerichtet.")
        self.los.setVisible(bool(self.schritte))
        self.weiter.setText("Weiter" if startklar else "Ohne KI weiter (nur Demo und Verwaltung)")

    def einrichten(self):
        self.los.setEnabled(False)
        self.weiter.setEnabled(False)
        self.log.show()
        self.balken.show()
        schritte = self.schritte

        def arbeit(signale):
            for schritt in schritte:
                signale.meldung.emit(f"▶ {schritt.titel}")
                signale.prozent.emit(0)
                schritt.ausfuehren(signale.meldung.emit, signale.prozent.emit)
                signale.prozent.emit(100)
            return True

        self.pool.start(_Arbeit(arbeit, self.signale))

    def _protokoll(self, text):
        self.log.appendPlainText(text)

    def _prozent(self, wert):
        self.balken.setValue(wert)

    def _neu_pruefen(self):
        self.pruefungen = einrichtung.pruefe(self.sys_info)
        self._zeige()
        self.los.setEnabled(True)
        self.weiter.setEnabled(True)

    def _fertig(self, _):
        self._protokoll("Fertig.")
        self._neu_pruefen()

    def _fehler(self, text):
        self._protokoll(f"✕ {text}")
        self._neu_pruefen()

    def closeEvent(self, ereignis):
        if self.pool.activeThreadCount():
            ereignis.ignore()
            return
        super().closeEvent(ereignis)

    def reject(self):
        # Eine laufende Installation lässt sich nicht einfach wegklicken.
        if not self.pool.activeThreadCount():
            super().reject()


def verbindungen(melde) -> None:
    """Nur zur Anzeige im Splash: Was die App gleich braucht, ist es erreichbar? Nichts davon hält den Start auf."""
    import httpx
    from .. import quellen, suche
    melde("… Öffne die Datenbank")
    da = config.DATEN.exists() and any(config.DATEN.rglob("*.db"))
    melde("✓ Datenbank: bereit" if da else "• Datenbank: wird beim Anmelden angelegt")
    melde("… Verbinde mit der eigenen Suchmaschine (SearXNG)")
    melde("✓ SearXNG: läuft" if quellen.finde_lokale_instanz() else "• SearXNG: nicht gestartet, Internet und Portale fehlen dann")
    melde("… Verbinde mit der Jobbörse")
    try:
        antwort = httpx.get(suche.API, headers={"X-API-Key": suche.API_SCHLUESSEL},
                            params={"was": "Test", "wo": "Berlin", "size": 1}, timeout=4)
        melde("✓ Jobbörse: erreichbar" if antwort.status_code == 200 else f"• Jobbörse: meldet {antwort.status_code}")
    except httpx.HTTPError:
        melde("• Jobbörse: nicht erreichbar, offline?")


def pruefe_im_hintergrund(pool, signale) -> None:
    def arbeit(s):
        s.meldung.emit("… Erkenne dein System")
        info = system.erkenne(config.DATEN if config.DATEN.exists() else None)
        s.meldung.emit(f"✓ System: {info.name}")
        pruefungen = einrichtung.pruefe(info, s.meldung.emit)
        try:
            verbindungen(s.meldung.emit)
        except Exception as fehler:  # Anzeige, kein Startgrund
            s.meldung.emit(f"• Verbindungen: {type(fehler).__name__}")
        return info, pruefungen
    pool.start(_Arbeit(arbeit, signale))
