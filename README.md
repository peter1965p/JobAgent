# Job Agent

Lokale Desktop-App für Linux zur Direktansprache von Firmen: Sie sucht auf Wunsch jede Nacht passende Firmen, findet den Inhaber auf deren Seiten, bewertet die Passung und legt dir morgens einen Bericht mit Mailvorschlägen hin. Jede Mail prüfst und gibst du selbst frei. Lebenslauf und Agenda erstellt sie bei Bedarf mit dir.

Datenbank, Suche und LLM laufen auf dem eigenen Rechner. Nach außen gehen nur Suchanfragen (Gebiet, Branche, Berufe), Abrufe öffentlicher Firmenseiten und Modell-Downloads. Jede Person hat ihre eigene Datenbank.

## Einrichten

Voraussetzung ist Python ab 3.10. Alles andere richtet die App ein:

```bash
python3 start.py
```

Das Startskript legt beim ersten Mal die Python-Umgebung an und installiert die Pakete (rund 1,3 GB). Danach prüft die App hinter dem Splash dein System und zeigt, was fehlt: Ollama, Dienst, Modelle, Schlüsselbund, Eintrag im Startmenü. Jeden geplanten Schritt nennt sie vorher, erst nach deinem Klick auf „Alles einrichten“ läuft er. Für Ollama nutzt sie den üblichen Weg:

| System | Weg |
|---|---|
| Arch und Ableger | `pacman` über `pkexec` |
| Andere Linux-Distributionen | offizielles Skript von ollama.com über `pkexec` |
| NixOS | Hinweis auf die Systemkonfiguration |
| Sonst | Link zum Download |

Auf einer CPU dauert ein KI-Schritt mehrere Minuten.

### Getestete Systeme

Entwickelt und getestet ist die App auf CachyOS (Arch). Die Installationswege für Ollama prüfen die Tests nur als Befehl, wirklich gelaufen ist noch keiner, auch der über `pacman` nicht.

Windows und macOS werden nicht unterstützt. Im Code gibt es Ansätze dafür (`winget`, `brew`, Outlook-Konten), sie sind aber auf keinem der beiden Systeme gelaufen. Unter Windows ist außerdem ein Fehler bekannt: Nach der Installation findet die App Ollama erst nach einem Neustart.

Für die Entwicklung:

```bash
source venv/bin/activate
python -m pytest tests        # Ohne Modell, ohne Netz, ohne Eingriffe ins System
PYTHONPATH=. jupyter lab analyse.ipynb
```

## Anmeldung und Demo

Beim Start meldest du dich mit Mailadresse und Mailpasswort an, oder du schaust dir die Demo an. Dein Mailserver prüft das Passwort, die App sendet dabei nichts. Findet sie den Server nicht in deinen Mailprogrammen, fragt sie danach. Ohne Netz gilt die letzte erfolgreiche Anmeldung, dann ist nur das Senden gesperrt.

Jede Adresse bekommt eine eigene Datenbank unter `daten/personen/`. Das trennt die Daten, schützt sie aber nicht: Die Dateien sind unverschlüsselt, wer direkt an sie kommt, kann sie lesen. Das Passwort steht nicht in der Datenbank, nur ein Prüfwert für die Anmeldung ohne Netz.

Die Demo läuft mit erfundenen Daten in einem temporären Ordner, der beim Beenden verschwindet. Sie sendet nichts und liest deine Mailprogramme nicht. Die Suche funktioniert auch dort, der Nachtlauf nicht.

## Ablauf

1. **Profil:** Name und Mailadresse eintragen. Optional eine Stilvorlage und eigene Regeln.
2. **Unterlagen oder CV-Assistent:** Lebenslauf und Zeugnis hinzufügen. Ohne Lebenslauf fragt der CV-Assistent nach und erstellt einen.
3. **Fakten:** Prüfen und die Fragen der App beantworten. Eigene Eingaben gelten sofort, KI-Vorschläge erst nach Bestätigung.
4. **Suche:** Land, Ort oder Region, Umkreis, Branche, Arbeitszeit und Quellen wählen. „Passende Firmen finden“ leitet Berufe und Branche aus deinen bestätigten Fakten ab. Ein Klick auf „Übernehmen und Inhaber finden“ liest die Firmenseiten aus.
5. **Firmen:** Bewerten lassen und bei Bedarf eine eigene Bewertung ergänzen. „Mail vorschlagen“ sucht bei Bedarf den Empfänger und schreibt die komplette Mail.
6. **Mailkonto:** Die App findet Konten aus deinen Mailprogrammen. Wähle deins und schicke dir eine Testmail.
7. **Morgenbericht (optional):** Nachtlauf einschalten. Er nutzt die Angaben der Suchseite und sendet nichts.
8. **Entwürfe:** Text prüfen, ändern, freigeben, senden. Danach das Ergebnis festhalten.
9. **Agenda:** Für die Zeit nach dem ersten Gespräch als PDF erzeugen.

## Mailtext

Das LLM schreibt die komplette Mail für jede Firma. Empfänger, Anrede und Gruß setzt das Programm. Ein Text unter „Profil“ dient als Stilbeispiel. Mit „Vorlage wörtlich verwenden“ bleibt er unverändert stehen und das LLM schreibt nur einen Satz zur Firma; das ist sicherer, aber weniger persönlich. Die Prüfungen fangen unbelegte Zahlen und Adressen ab, aber keine falsch verstandenen Aussagen.

## Versand

Gefundene Konten stammen aus Thunderbird, Evolution, Geary, KMail, Claws Mail und msmtp. Mit echten Daten geprüft ist bisher nur Thunderbird und Evolution, die übrigen nur mit Beispieldateien. Gelesen werden nur Serverdaten, nie Passwörter. Das Passwort fragt die App selbst ab und bietet nach erfolgreichem Senden an, es im Schlüsselbund des Systems zu speichern. In der Datenbank steht es nie.

Die App sendet nur freigegebene Mails, jede nur einmal, nach einer Rückfrage und nur verschlüsselt mit geprüftem Zertifikat. Eine Kopie geht per Bcc an dich, weil SMTP nichts im Ordner „Gesendet“ ablegt. Konten mit OAuth (etwa Gmail) brauchen ein App-Passwort des Anbieters.

Mails an Firmen ohne vorherigen Kontakt können rechtlich heikel sein (§ 7 UWG, DSGVO). Das Tageslimit und die Freigabe jeder einzelnen Mail halten das Risiko klein, ersetzen aber keine Prüfung. Das ist keine Rechtsberatung.

## Firmensuche

| Quelle | Was sie liefert | Grenzen |
|---|---|---|
| Firmenverzeichnis (OpenStreetMap) | Firmen mit Website, nach Gebiet und Branche | Lückenhaft, Server oft überlastet. Der letzte Ersatzserver gehört VK (Russland), er sieht Gebiet und Branche |
| Jobbörse der Arbeitsagentur | Aktuelle Stellen, nur Deutschland | Nicht offiziell dokumentiert, keine Websites |
| Arbeitnow | Aktuelle Stellen über eine freie Schnittstelle | Nur die neuesten 300, Ort als Textvergleich |
| Internet (eigene SearXNG-Instanz) | Firmenseiten und Impressen zu deinen Berufen | Musst du selbst betreiben; SearXNG fragt dafür öffentliche Suchmaschinen ab, deren Bedingungen das teils untersagen |

Die App liest Stellen nur über offene Schnittstellen. StepStone, Indeed, LinkedIn und Kununu sind deshalb keine Quelle.

Der Filter für Voll- und Teilzeit gilt für Jobbörse und Arbeitnow. Das Verzeichnis und die Internetsuche liefern Firmen statt Stellen und kennen keine Arbeitszeit. Bei Arbeitnow bleiben Anzeigen ohne Angabe in der Liste.

Suchmaschinen kennen keinen Umkreis. Darum liest die App bei jedem Internet-Treffer den Sitz aus dem Impressum, nimmt die Koordinaten der Postleitzahl von OpenStreetMap und sortiert aus, was außerhalb des Gebiets liegt oder keine lesbare Adresse hat. Es zählt die erste Adresse im Impressum, eine Firma mit Sitz anderswo und Niederlassung vor Ort fällt also raus. Regionen gelten über ihr umschließendes Rechteck, am Rand ist das ungenau.

**Ansprechperson aus Anzeigen:** Beim Übernehmen lädt die App den vollen Anzeigentext. Findet „Inhaber finden“ keine persönliche Adresse der Geschäftsführung, sucht es bei der Jobbörse laufende Anzeigen der Firma (bis zu drei) und nimmt die dort genannte Person mit Adresse. Ihren Namen übernimmt die App nur, wenn er zur Adresse passt und im Text steht. Fehlt im Impressum jede Adresse, zählt auch eine Bewerbungsadresse der eigenen Domain, etwa `jobs@`.

**Mittelstand vor Konzern:** Gesucht und angeboten wird alles, aber Firmen mit Konzernmerkmalen stehen hinten und tragen den Status „Konzern“; auch der Nachtlauf nimmt den Mittelstand zuerst. Eine Größenangabe liefert keine Quelle, die App schätzt über Rechtsform AG, SE oder KGaA, Gruppe oder Holding im Namen, Aufsichtsrat oder Gruppenzugehörigkeit im Impressum und viele Anzeigen. Kleine AGs gelten dabei als Konzern, große GmbHs ohne diese Merkmale nicht.

**Sofort bewerben:** Ein Klick in den Suchdetails übernimmt die Firma, sucht die Ansprechperson, prüft die Eignung und schreibt die Mail. Gesendet wird erst nach deiner Freigabe unter „Entwürfe“, die als Anhang markierten Unterlagen gehen mit. Die Eignungsprüfung nimmt die Aufzählung unter dem Profil der Stellenanzeige und prüft jede Anforderung einzeln gegen die vier passendsten Abschnitte aus Lebenslauf und Zeugnis, sonstige Unterlagen bleiben draußen. Steht keine Aufzählung in der Anzeige, nennt das Modell die Anforderungen. Erfüllt zählt eine Anforderung nur, wenn das Modell einen dieser Abschnitte als Beleg nennt; den Prozentwert rechnet die App selbst. „Von Vorteil“ und „idealerweise“ zählen halb, Führerschein und Reisebereitschaft stehen in keinen Unterlagen und landen unter „Selbst prüfen“. Dazu kommen Vorschläge für den CV: was nur das Zeugnis belegt, was nur teilweise belegt ist und was fehlt. Für jede belegte Anforderung soll das Modell die Stelle wörtlich zitieren. Die App prüft das Zitat gegen den Originaltext und verwirft Umformuliertes, Erfundenes und Sensibles wie das Geburtsdatum. Aus den geprüften Zitaten entsteht der **Lebenslauf für die Stelle**: vorn eine Seite „Qualifikationsprofil“ mit den Anforderungen der Anzeige und den Nachweisen, dahinter dein Lebenslauf unverändert. Die Datei liegt im Ordner `cv` neben deiner Datenbank, das Original bleibt unangetastet. Jede danach erstellte Mail an diese Firma bekommt ihn statt des allgemeinen Lebenslaufs als Anhang, dazu alle übrigen Anhänge wie das Zeugnis. Welche Dateien mitgehen, steht im Entwurf und in der Sendeabfrage. Der Nachtlauf prüft vor jeder Mail an eine Firma mit Stellenanzeige ebenfalls die Eignung und schreibt unter 40 % keine Mail. Unter 40 % schreibt sie keine Mail. Ohne Stellenanzeige gibt es nichts zu prüfen, dann wird es eine Initiativanfrage. Das Ergebnis siehst nur du, die Mail entsteht weiter allein aus bestätigten Fakten. Auf der CPU dauern Prüfung und Mail zusammen mehrere Minuten.

### SearXNG einrichten

Die Internetsuche braucht eine eigene SearXNG-Instanz. Läuft sie auf `127.0.0.1:18888`, erkennt die App sie selbst, sonst trägst du ihre Adresse auf der Suchseite ein. In der `settings.yml` muss unter `search.formats` auch `json` stehen. Sie sollte nur lokal lauschen und braucht einen eigenen `secret_key`.

## Inhaber und Empfänger

Die App liest Impressum, Kontakt- und Teamseiten der Firma, nur auf deren eigener Domain und unter Beachtung von `robots.txt`. Eine persönliche Adresse des Inhabers geht vor `info@`. Es zählen nur veröffentlichte Adressen: Die App rät keine und nutzt keine geleakten Daten. Die Erkennung arbeitet mit Textmustern für deutsche Seiten und kann danebenliegen, prüfe das Ergebnis.

## Nachtlauf

Der Nachtlauf ist ein systemd-Timer deines Benutzers, den du unter „Morgenbericht“ ein- und ausschaltest. Er liest pro Nacht die Websites von bis zu 60 neuen Firmen, solche ohne Stellenbörse zuerst, vergleicht deren Inhalt mit deinen Fakten und sucht dort offene Stellen. Die besten 10 bewertet das Modell, für die besten 3 schreibt es eine Mail. Firmen ohne veröffentlichte Adresse oder mit einem Score unter 50 fliegen raus und werden nicht erneut geprüft, der Rest kommt in den nächsten Nächten dran. Gefunden werden nur Firmen, die in einer Quelle stehen; was weder im Verzeichnis noch in der Internetsuche auftaucht, bleibt unsichtbar. Er sendet nichts. Der Rechner muss laufen und du angemeldet sein, ein verpasster Lauf wird nachgeholt. Der Score stammt vom lokalen Modell und ist nur eine Einschätzung.

## Was fest im Code liegt

| Regel | Ort |
|---|---|
| Ein Entwurf sieht nur bestätigte Fakten, Firmentexte und die Stilvorlage | `rag.py`, `ki.py` |
| Gehalt, Gesundheit, Platzhalter sowie unbelegte Zahlen und Adressen sperren die Freigabe | `regeln.py` |
| Empfänger, Anrede und Gruß setzt das Programm, nie das Modell | `ki.py` |
| Senden nur nach Freigabe, Prüfung und Rückfrage, jede Mail einmal, nur verschlüsselt | `versand.py` |
| Webseiten gelten als Daten; versteckter Text wird entfernt | `firmen.py`, `ki.py` |
| Höchstens 5 Freigaben am Tag | `config.py` |
| Lebenslauf und Agenda entstehen ohne LLM | `cv.py`, `agenda.py` |

Die Prüfungen erkennen nicht, ob eine Aussage inhaltlich stimmt. Lies jede Mail vor der Freigabe selbst.

## Was die App übernimmt und was sie fragt

Quellen sind Lebenslauf und Zeugnis. Was dort fehlt, fragt die App unter „Fakten“ ab. Gehalt, Verfügbarkeit und Einstieg nutzt nur die lokale Bewertung, Ziele und Bedingungen nur die Agenda. Nichts davon gelangt in Mailentwürfe. Die Agenda enthält das Gehalt und ist deshalb kein Mailanhang.

## So lernt die KI

Die Modellgewichte ändern sich nicht. Die App merkt sich Regeln, Korrekturen, Bewertungen und welche Mails Antworten bekamen, jeweils anonymisiert, und spielt die passendsten per RAG in neue Prompts ein. Unter „Gelernt“ lässt sich jede Lektion ansehen und löschen. Ob das wirkt, zeigt das Notebook über den Korrekturgrad.

## Noch nicht gebaut

- Antworten aus dem Postfach erkennen: Du hältst das Ergebnis von Hand fest.
- OCR für gescannte PDFs

## Daten

Alles Persönliche liegt in `daten/` und ist per `.gitignore` ausgeschlossen. Für die Messung der Suchqualität im Notebook kannst du dort `suchfragen.json` anlegen. Ein Notebook mit gespeicherten Ausgaben enthält ebenfalls Firmen und Bewertungen.
