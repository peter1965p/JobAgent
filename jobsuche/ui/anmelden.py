"""Das Fenster beim Start: anmelden oder die Demo ansehen."""

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import anmeldung, mailkonten, versand
from .schalter import Schalter


class _Signale(QObject):
    fertig = Signal(object)
    fehlt = Signal(str)
    fehler = Signal(str)


class _Pruefung(QRunnable):
    def __init__(self, email, passwort, konto, signale):
        super().__init__()
        self.email, self.passwort, self.konto, self.signale = email, passwort, konto, signale

    def run(self):
        try:
            self.signale.fertig.emit(anmeldung.pruefe(self.email, self.passwort, self.konto))
        except anmeldung.KontoFehlt as fehler:
            self.signale.fehlt.emit(str(fehler))
        except (versand.VersandFehler, ValueError) as fehler:
            self.signale.fehler.emit(str(fehler))


class _KontoPruefung(QRunnable):
    def __init__(self, email, passwort, signale):
        super().__init__()
        self.email, self.passwort, self.signale = email, passwort, signale

    def run(self):
        try:
            self.signale.fertig.emit(anmeldung.pruefe_konto(self.email, self.passwort))
        except (versand.VersandFehler, ValueError) as fehler:
            self.signale.fehler.emit(str(fehler))


class AnmeldeFenster(QDialog):
    """Nach `exec()`: `demo` ist wahr, wenn die Demo gewählt wurde, sonst stehen die Anmeldedaten bereit."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Job Agent")
        self.setMinimumWidth(460)
        self.demo = False
        self.ergebnis = None
        # Mit Mandanten-Datenbank (PostgreSQL + js_zentral): eigenes Konto statt Mailserver-Anmeldung
        self.kontomodus = anmeldung.konten_verfuegbar()
        self.passwort = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.einstellungen = QSettings("jobsuche", "jobsuche")
        self.signale = _Signale()
        self.signale.fertig.connect(self._erfolg)
        self.signale.fehlt.connect(self._server_fehlt)
        self.signale.fehler.connect(self._fehlgeschlagen)

        lage = QVBoxLayout(self)
        lage.setContentsMargins(28, 26, 28, 24)
        lage.setSpacing(10)
        self.logo = QLabel("Job Agent")
        self.logo.setObjectName("logo")
        lage.addWidget(self.logo)
        unter = QLabel("Dein Mailkonto ist dein Zugang: Mailadresse und Mailpasswort genügen, dein Mailserver prüft das Passwort. "
                       "Beim ersten Mal legt die App dein Konto auf diesem Rechner an. Jede Person hat ihre eigenen Daten.")
        unter.setObjectName("gedimmt")
        unter.setWordWrap(True)
        if self.kontomodus:
            unter.setText("Melde dich mit deinem Job-Agent-Konto an (dasselbe wie im Web). Dein Mailkonto brauchst du "
                          "nur noch zum Senden. Neue Konten gibt es per Einladung über die Web-Verwaltung.")
        lage.addWidget(unter)
        self.email = QLineEdit(self.einstellungen.value("letzte_adresse", "", type=str))
        self.email.setPlaceholderText("Mailadresse")
        self.email.editingFinished.connect(self._fuelle_passwort)
        self.email.textChanged.connect(self._konto_neu)
        lage.addWidget(self.email)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Dein Name, für Absender und Lebenslauf")
        self.name.hide()
        lage.addWidget(self.name)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("Konto-Passwort" if self.kontomodus else "Mailpasswort oder App-Passwort")
        self.pw.returnPressed.connect(self.anmelden)
        lage.addWidget(self.pw)

        self.server = QWidget()
        form = QFormLayout(self.server)
        form.setContentsMargins(0, 0, 0, 0)
        self.host = QLineEdit()
        self.host.setPlaceholderText("smtp.example.org")
        self.port = QLineEdit()
        self.port.setPlaceholderText("587")
        self.sicherheit = QComboBox()
        self.sicherheit.addItem("STARTTLS", "starttls")
        self.sicherheit.addItem("SSL/TLS", "ssl")
        self.benutzer = QLineEdit()
        self.benutzer.setPlaceholderText("Meist die Mailadresse")
        form.addRow("Server", self.host)
        form.addRow("Port", self.port)
        form.addRow("Sicherheit", self.sicherheit)
        form.addRow("Benutzer", self.benutzer)
        self.server.hide()
        lage.addWidget(self.server)

        self.merken = Schalter("Passwort im Schlüsselbund des Systems merken")
        lage.addWidget(self.merken)
        self.meldung = QLabel("")
        self.meldung.setObjectName("sperre")
        self.meldung.setWordWrap(True)
        self.meldung.hide()
        lage.addWidget(self.meldung)

        reihe = QHBoxLayout()
        self.anmelden_knopf = QPushButton("Anmelden")
        self.anmelden_knopf.setObjectName("primaer")
        self.anmelden_knopf.setDefault(True)
        self.anmelden_knopf.clicked.connect(self.anmelden)
        self.demo_knopf = QPushButton("Demo ansehen")
        self.demo_knopf.setAutoDefault(False)
        self.demo_knopf.clicked.connect(self.demo_starten)
        reihe.addWidget(self.anmelden_knopf, 2)
        reihe.addWidget(self.demo_knopf, 1)
        lage.addLayout(reihe)
        hinweis = QLabel("Die Demo zeigt erfundene Daten, speichert nichts und sendet nichts.")
        hinweis.setObjectName("gedimmt")
        hinweis.setWordWrap(True)
        lage.addWidget(hinweis)
        self._fuelle_passwort()
        self._konto_neu()
        (self.pw if self.email.text() else self.email).setFocus()

    def _konto_neu(self):
        if self.kontomodus:
            self.anmelden_knopf.setText("Anmelden")
            return
        neu = anmeldung.ist_neu(self.email.text())
        self.name.setVisible(neu)
        self.logo.setText("Konto anlegen" if neu else "Job Agent")
        self.anmelden_knopf.setText("Konto anlegen" if neu else "Anmelden")

    def _fuelle_passwort(self):
        gemerkt = anmeldung.gemerktes_passwort(self.email.text(), self.kontomodus) if "@" in self.email.text() else ""
        if gemerkt and not self.pw.text():
            self.pw.setText(gemerkt)
            self.merken.setChecked(True)

    def _konto_von_hand(self):
        if not self.server.isVisible():
            return None
        email = self.email.text().strip()
        sicherheit = self.sicherheit.currentData()
        try:
            port = int(self.port.text() or (465 if sicherheit == "ssl" else 587))
        except ValueError:
            raise ValueError("Der Port muss eine Zahl sein.") from None
        if not self.host.text().strip():
            raise ValueError("Trage den Postausgangsserver ein.")
        return mailkonten.Konto("von Hand", email, self.host.text().strip(), port, sicherheit,
                                self.benutzer.text().strip() or email)

    def _beschaeftigt(self, ja):
        for w in (self.anmelden_knopf, self.demo_knopf, self.email, self.pw):
            w.setEnabled(not ja)
        if ja:
            self.anmelden_knopf.setText("Prüfe …")
        else:
            self._konto_neu()

    def _zeige_meldung(self, text):
        self.meldung.setText(text)
        self.meldung.setVisible(bool(text))

    def anmelden(self):
        self._zeige_meldung("")
        try:
            konto = self._konto_von_hand()
        except ValueError as fehler:
            return self._zeige_meldung(str(fehler))
        self.passwort = self.pw.text()
        self._beschaeftigt(True)
        if self.kontomodus:
            self.pool.start(_KontoPruefung(self.email.text(), self.passwort, self.signale))
            return
        self.pool.start(_Pruefung(self.email.text(), self.passwort, konto, self.signale))

    def _erfolg(self, ergebnis):
        self._beschaeftigt(False)
        self.ergebnis = ergebnis
        self.einstellungen.setValue("letzte_adresse", self.email.text().strip())
        self.warnung = anmeldung.merke_passwort(self.email.text(), self.passwort, self.merken.isChecked(), self.kontomodus)
        self.accept()

    def _server_fehlt(self, text):
        self._beschaeftigt(False)
        self.server.show()
        self._zeige_meldung(text + " Trage ihn bitte ein.")
        self.host.setFocus()

    def _fehlgeschlagen(self, text):
        self._beschaeftigt(False)
        self.pw.clear()
        self.pw.setFocus()
        self._zeige_meldung(text)

    def demo_starten(self):
        self.demo = True
        self.accept()

    def closeEvent(self, ereignis):
        self.pool.waitForDone(100)
        super().closeEvent(ereignis)


