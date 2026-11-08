"""Findet Postausgangsserver in installierten Mailprogrammen. Liest nur Einstellungen, niemals Passwörter."""

import configparser
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Konto:
    quelle: str
    email: str
    host: str
    port: int
    sicherheit: str  # "ssl", "starttls" oder "keine"
    benutzer: str
    oauth: bool = False

    def als_dict(self) -> dict:
        return asdict(self)


def _standardport(sicherheit: str) -> int:
    return 465 if sicherheit == "ssl" else 587


def _zahl(wert, standard: int) -> int:
    try:
        return int(wert)
    except (TypeError, ValueError):
        return standard


def _ini(pfad: Path) -> configparser.RawConfigParser:
    ini = configparser.RawConfigParser(strict=False, interpolation=None)
    ini.optionxform = str
    ini.read(pfad, encoding="utf-8")
    return ini


def thunderbird(home: Path) -> list[Konto]:
    konten = []
    wurzeln = [".thunderbird", ".var/app/org.mozilla.Thunderbird/.thunderbird", "snap/thunderbird/common/.thunderbird",
               ".mozilla-thunderbird", "AppData/Roaming/Thunderbird/Profiles", "Library/Thunderbird/Profiles"]
    for wurzel in wurzeln:
        for prefs in sorted((home / wurzel).glob("*/prefs.js")):
            werte = dict(re.findall(r'user_pref\("([^"]+)",\s*"?([^")]*)"?\);', prefs.read_text("utf-8", errors="replace")))
            for schluessel, server in werte.items():
                treffer = re.fullmatch(r"mail\.identity\.([^.]+)\.smtpServer", schluessel)
                if not treffer:
                    continue
                email = werte.get(f"mail.identity.{treffer.group(1)}.useremail", "")
                praefix = f"mail.smtpserver.{server}" if f"mail.smtpserver.{server}.hostname" in werte else f"mail.outgoingserver.{server}"
                host = werte.get(f"{praefix}.hostname", "")
                if not email or not host:
                    continue
                sicherheit = {"3": "ssl", "2": "starttls"}.get(werte.get(f"{praefix}.try_ssl", ""), "keine")
                konten.append(Konto("Thunderbird", email, host, _zahl(werte.get(f"{praefix}.port"), _standardport(sicherheit)),
                                    sicherheit, werte.get(f"{praefix}.username", email),
                                    werte.get(f"{praefix}.authMethod") == "10"))
    return konten


def evolution(home: Path) -> list[Konto]:
    transporte, adressen = {}, {}
    for datei in sorted((home / ".config/evolution/sources").glob("*.source")):
        ini = _ini(datei)
        if ini.has_section("Mail Transport") and ini.get("Mail Transport", "BackendName", fallback="") == "smtp":
            transporte[datei.stem] = ini
        if ini.has_section("Mail Identity") and ini.has_section("Mail Submission"):
            adressen[ini.get("Mail Submission", "TransportUid", fallback="")] = ini.get("Mail Identity", "Address", fallback="")
    konten = []
    for uid, ini in transporte.items():
        host = ini.get("Authentication", "Host", fallback="")
        email = adressen.get(uid, "")
        if not host or not email:
            continue
        methode = ini.get("Security", "Method", fallback="none")
        sicherheit = {"starttls-on-standard-port": "starttls", "smtp-over-ssl": "ssl"}.get(methode, "keine")
        konten.append(Konto("Evolution", email, host, _zahl(ini.get("Authentication", "Port", fallback=""), _standardport(sicherheit)),
                            sicherheit, ini.get("Authentication", "User", fallback=email)))
    return konten


def geary(home: Path) -> list[Konto]:
    konten = []
    for wurzel in (".config/geary", ".local/share/geary"):
        for datei in sorted((home / wurzel).glob("account_*/geary.ini")):
            ini = _ini(datei)
            host = ini.get("Outgoing", "host", fallback="")
            email = ini.get("Account", "primary_email", fallback="")
            if not host or not email:
                continue
            sicherheit = {"tls": "ssl", "start-tls": "starttls"}.get(ini.get("Outgoing", "transport_security", fallback=""), "keine")
            konten.append(Konto("Geary", email, host, _zahl(ini.get("Outgoing", "port", fallback=""), _standardport(sicherheit)),
                                sicherheit, ini.get("Outgoing", "login", fallback=email)))
    return konten


def kmail(home: Path) -> list[Konto]:
    datei = home / ".config/mailtransports"
    if not datei.exists():
        return []
    ini, konten = _ini(datei), []
    for abschnitt in ini.sections():
        if not abschnitt.startswith("Transport") or not ini.get(abschnitt, "host", fallback=""):
            continue
        sicherheit = {"ssl": "ssl", "tls": "starttls"}.get(ini.get(abschnitt, "encryption", fallback="").lower(), "keine")
        benutzer = ini.get(abschnitt, "user", fallback="")
        konten.append(Konto("KMail", benutzer if "@" in benutzer else "", ini.get(abschnitt, "host"),
                            _zahl(ini.get(abschnitt, "port", fallback=""), _standardport(sicherheit)), sicherheit, benutzer))
    return konten


def claws(home: Path) -> list[Konto]:
    datei = home / ".claws-mail/accountrc"
    if not datei.exists():
        return []
    ini, konten = _ini(datei), []
    for abschnitt in ini.sections():
        host = ini.get(abschnitt, "smtp_server", fallback="")
        email = ini.get(abschnitt, "address", fallback="")
        if not host or not email:
            continue
        sicherheit = {"1": "ssl", "2": "starttls"}.get(ini.get(abschnitt, "ssl_smtp", fallback=""), "keine")
        konten.append(Konto("Claws Mail", email, host, _zahl(ini.get(abschnitt, "smtpport", fallback=""), _standardport(sicherheit)),
                            sicherheit, ini.get(abschnitt, "smtp_user_id", fallback="") or email))
    return konten


def msmtp(home: Path) -> list[Konto]:
    datei = home / ".msmtprc"
    if not datei.exists():
        return []
    konten, aktuell = [], {}

    def abschliessen():
        if aktuell.get("host") and aktuell.get("from"):
            if aktuell.get("tls") != "on":
                sicherheit = "keine"
            else:
                sicherheit = "starttls" if aktuell.get("tls_starttls", "on") == "on" else "ssl"
            konten.append(Konto("msmtp", aktuell["from"], aktuell["host"], _zahl(aktuell.get("port"), _standardport(sicherheit)),
                                sicherheit, aktuell.get("user", aktuell["from"])))

    for zeile in datei.read_text("utf-8", errors="replace").splitlines():
        teile = zeile.split(None, 1)
        if len(teile) != 2 or zeile.lstrip().startswith("#"):
            continue
        if teile[0] == "account":
            abschliessen()
            aktuell = {}
        elif teile[0] != "password":
            aktuell[teile[0]] = teile[1].strip()
    abschliessen()
    return konten


def _text(wert) -> str:
    """Outlook speichert Texte teils als UTF-16-Bytes."""
    if isinstance(wert, bytes):
        return wert.decode("utf-16-le", errors="ignore").rstrip("\x00")
    return str(wert or "")


def outlook_aus_werten(eintraege: list[dict]) -> list[Konto]:
    konten = []
    for werte in eintraege:
        host, email = _text(werte.get("SMTP Server")), _text(werte.get("Email"))
        if not host or not email:
            continue
        art = _zahl(werte.get("SMTP Use SSL"), 0)
        sicherheit = {1: "ssl", 2: "starttls", 3: "starttls"}.get(art, "keine")
        konten.append(Konto("Outlook", email, host, _zahl(werte.get("SMTP Port"), _standardport(sicherheit)), sicherheit,
                            _text(werte.get("SMTP User")) or email))
    return konten


def outlook() -> list[Konto]:
    """Nur unter Windows. Das neue Outlook und Exchange-Konten legen hier keine SMTP-Daten ab."""
    if sys.platform != "win32":
        return []
    import winreg  # noqa: PLC0415

    eintraege = []
    for version in ("16.0", "15.0"):
        wurzel = rf"Software\Microsoft\Office\{version}\Outlook\Profiles"
        try:
            profile = winreg.OpenKey(winreg.HKEY_CURRENT_USER, wurzel)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(profile)[0]):
            basis = rf"{wurzel}\{winreg.EnumKey(profile, i)}\9375CFF0413111d3B88A00104B2A6676"
            try:
                konten = winreg.OpenKey(winreg.HKEY_CURRENT_USER, basis)
            except OSError:
                continue
            for j in range(winreg.QueryInfoKey(konten)[0]):
                schluessel = winreg.OpenKey(konten, winreg.EnumKey(konten, j))
                werte = {}
                for k in range(winreg.QueryInfoKey(schluessel)[1]):
                    name, wert, _ = winreg.EnumValue(schluessel, k)
                    werte[name] = wert
                eintraege.append(werte)
    return outlook_aus_werten(eintraege)


def finde_alle(home: Path | None = None, eigene_adresse: str = "") -> list[Konto]:
    """Alle gefundenen Konten, die zur eigenen Adresse passenden zuerst. Ein defektes Programm stoppt die Suche nicht."""
    home = home or Path.home()
    gefunden: list[Konto] = []
    for suche in (lambda: thunderbird(home), lambda: outlook(), lambda: evolution(home), lambda: geary(home),
                  lambda: kmail(home), lambda: claws(home), lambda: msmtp(home)):
        try:
            gefunden.extend(suche())
        except (OSError, configparser.Error, UnicodeError, ValueError):
            continue
    eindeutig = list({(k.email.lower(), k.host.lower(), k.benutzer): k for k in gefunden}.values())
    return sorted(eindeutig, key=lambda k: k.email.lower() != eigene_adresse.lower())
