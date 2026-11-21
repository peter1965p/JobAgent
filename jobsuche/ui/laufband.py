"""Hinweise als eine Zeile: Jeder Hinweis fährt von unten herein und nach oben wieder hinaus."""

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


class Laufband(QWidget):
    """Ersatz für ein mehrzeiliges Hinweis-Label: `setText` mit Zeilen, `text` und `clear` wie bei QLabel.
    Ohne Hinweise ist das Band unsichtbar. Mit der Maus darüber hält es an, der Tooltip zeigt alle Hinweise."""

    TAKT_MS = 4500
    FAHRT_MS = 380

    def __init__(self, titel: str = "System Probs:", name: str = "hinweis"):
        super().__init__()
        self._zeilen: list[str] = []
        self._index = 0
        lage = QHBoxLayout(self)
        lage.setContentsMargins(0, 2, 0, 2)
        lage.setSpacing(8)
        self.titel = QLabel(titel)
        self.titel.setObjectName(name)
        schrift = self.titel.font()
        schrift.setBold(True)
        self.titel.setFont(schrift)
        self.fenster = QWidget()  # schneidet die Zeile beim Hinein- und Hinausfahren ab
        self.fenster.setFixedHeight(self.fontMetrics().height() + 6)
        self.zeile = QLabel(self.fenster)
        self.zeile.setObjectName(name)
        self.zaehler = QLabel()
        self.zaehler.setObjectName("gedimmt")
        lage.addWidget(self.titel)
        lage.addWidget(self.fenster, 1)
        lage.addWidget(self.zaehler)
        self.takt = QTimer(self)
        self.takt.timeout.connect(self.weiter)
        self.raus = QPropertyAnimation(self.zeile, b"pos", self)
        self.rein = QPropertyAnimation(self.zeile, b"pos", self)
        for anim, kurve in ((self.raus, QEasingCurve.InCubic), (self.rein, QEasingCurve.OutCubic)):
            anim.setDuration(self.FAHRT_MS)
            anim.setEasingCurve(kurve)
        self.raus.finished.connect(self._naechste_setzen)
        self.hide()

    # --- wie QLabel ---------------------------------------------------------------------------------------------------
    def setText(self, text: str) -> None:
        self._zeilen = [z.strip() for z in (text or "").splitlines() if z.strip()]
        self._index = 0
        self.raus.stop()
        self.rein.stop()
        self.takt.stop()
        self.setVisible(bool(self._zeilen))
        self.setToolTip("\n".join(self._zeilen))
        if not self._zeilen:
            self.zeile.clear()
            return
        self._setze_zeile()
        self._reinfahren()
        if len(self._zeilen) > 1:
            self.takt.start(self.TAKT_MS)

    def text(self) -> str:
        return "\n".join(self._zeilen)

    def clear(self) -> None:
        self.setText("")

    # --- Ablauf -------------------------------------------------------------------------------------------------------
    def weiter(self) -> None:
        if len(self._zeilen) < 2:
            return
        self.rein.stop()
        self.raus.setStartValue(QPoint(0, 0))
        self.raus.setEndValue(QPoint(0, -self.fenster.height()))
        self.raus.start()

    def _naechste_setzen(self) -> None:
        if not self._zeilen:
            return
        self._index = (self._index + 1) % len(self._zeilen)
        self._setze_zeile()
        self._reinfahren()

    def _reinfahren(self) -> None:
        self.rein.setStartValue(QPoint(0, self.fenster.height()))
        self.rein.setEndValue(QPoint(0, 0))
        self.rein.start()

    def _setze_zeile(self) -> None:
        if not self._zeilen:
            return
        zeile = self._zeilen[self._index]
        breite = max(self.fenster.width(), 50)
        self.zeile.setText(self.zeile.fontMetrics().elidedText(zeile, Qt.ElideRight, breite))
        self.zeile.resize(breite, self.fenster.height())
        self.zaehler.setText(f"{self._index + 1}/{len(self._zeilen)}" if len(self._zeilen) > 1 else "")

    def aktuell(self) -> str:
        return self._zeilen[self._index] if self._zeilen else ""

    def resizeEvent(self, ereignis) -> None:
        super().resizeEvent(ereignis)
        self._setze_zeile()

    def enterEvent(self, ereignis) -> None:
        self.takt.stop()
        super().enterEvent(ereignis)

    def leaveEvent(self, ereignis) -> None:
        if len(self._zeilen) > 1:
            self.takt.start(self.TAKT_MS)
        super().leaveEvent(ereignis)
