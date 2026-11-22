# Änderungen

Versionen nach [Semantic Versioning](https://semver.org/lang/de/). Die laufende Version steht in `jobsuche/__init__.py` und unten links in der App.

## 0.10.0 – 2026-11-22

### Neu
- Wohnungen: Klick auf ein Angebot klappt animiert eine Vorschau auf, die die echte Portal-Anzeige mit Fotos und allen Daten im eingebauten Browser zeigt (Doppelklick öffnet sie weiterhin im System-Browser). Ohne QtWebEngine bleibt die App ohne Vorschau lauffähig.

## 0.9.1 – 2026-11-22

### Geändert
- Wohnungen: WBS-Angebote werden nicht mehr ausgeblendet. Neuer Schalter „Nur mit WBS“; ausgeblendet wird nur noch Tausch.

## 0.9.0 – 2026-11-22

### Neu
- **Wohnungen (Bereich „Umzug“):** Angebote aus den Suchauftrags-Mails von Immowelt, ImmoScout24, meinestadt und Kleinanzeigen, mit Kalt-/Warmmiete, Zimmern, Fläche, Ort und Portal. Eigene Postfach-Auswahl (z. B. ein zweites Konto nur für Kleinanzeigen), schreibgeschützt, Mails der letzten 30 Tage.
- Filter: Warmmiete (ohne Angabe geschätzt: Kaltmiete + 2,80 €/m²), Zimmer, Fläche, Bundesländer (Vorauswahl NRW bis Norden), Arbeitsweg in km, Balkon/Terrasse/Garten/Haustiere/Einbauküche, Tausch- und WBS-Angebote ausblenden. Runde Ampel: grün passt, rot passt nicht (Grund im Tooltip), orange gemerkt.
- Arbeitsorte eintragen: Fahrstrecke mit dem Auto zum nächsten Arbeitsort (OSRM); Ort und Bundesland über Nominatim, zwischengespeichert.
- Merken, Verwerfen, Anzeige öffnen, „Netz prüfen“ (Breitbandatlas, Adresse liegt in der Zwischenablage).

### Behoben
- Zahlenfelder (z. B. „höchstens … Tage alt“) hatten kein Theme und erschienen hell/grau; jetzt wie die übrigen Eingabefelder, mit eigenen Pfeilen.

## 0.8.0 – 2026-11-22

### Neu
- **Job-Alerts: Glassdoor und JobLeads.** Glassdoor-Mails (Firma, Bewertung, Titel, Ort) und JobLeads-Mails (Titel, Firma und Ort stehen vor „Job ansehen“) werden ausgewertet, mit fester Kennung gegen Doppelte; JobLeads „Deutschland | Remote“ zählt als Remote.
- **XING-Jobmails** (jobs@mail.xing.com) mit ihren Kurzlinks werden gelesen; Geburtstags- und Kontaktmails von XING bleiben außen vor.

### Behoben
- Job-Alerts: Echte Mails setzen jede Zeile einer Karte (Titel, „Firma · Ort“, „Einfach bewerben“) in einen eigenen Link. Bisher wurde jede Zeile zur eigenen „Stelle“ (LinkedIn: „Firma · Ort“ als Titel). Jetzt gehört alles mit derselben Kennung zu einer Stelle.
- Job-Alerts: Outlook-Kommentare (`[if mso]…`) landeten als Firmen- und Ortszeilen in den Stellen.
- Job-Alerts Indeed: Die Links sind in cts.indeed.com verpackt (gzip + base64) und werden ausgepackt. Profil-Links („Pendelzeit“, „Arbeitsort“, „Mindestgrundgehalt“) sind keine Stellen mehr; gesponserte Stellen bekommen ihre Anzeigen-Kennung.
- Job-Alerts StepStone/XING: „I'm interested“, „Because you viewed“, „Report this job“, „Im Browser ansehen“, „Alle Vorschläge ansehen“ und Fußzeilen werden nicht mehr als Stellen gelesen.

## 0.7.0 – 2026-11-21

### Neu
- **Splash zeigt live, was er prüft und verbindet:** System, Speicher, Ollama und Dienst, Sprach- und Suchmodell, Claude Code, Schlüsselbund, Datenbank, eigene Suchmaschine (SearXNG) und Jobbörse. Die laufende Zeile steht orange mit „…“, das Ergebnis ersetzt sie (✓ grün, ✗ rot, • Hinweis); neue Zeilen fahren von unten herein.
- **Suche: Hinweise als Laufband „System Probs:“** statt eines Kastens mit vielen Zeilen. Jeder Hinweis fährt von unten herein und nach oben hinaus, rechts steht „2/8“; Maus darüber hält an, der Tooltip zeigt alle.

### Behoben
- Entwurf: Wer in der Anzeige unterschreibt („Alex & Inga – Personalteam“) oder als Ansprechpartner genannt ist, geht der Person aus dem Impressum vor – in Anrede und „z. Hd.“.
- Entwurf: Duzt die Anzeige, beginnt die Mail mit „Hallo Vorname“ (bei mehreren „Hallo Alex, hallo Inga“) statt „Guten Tag Vor- und Nachname“.
- Entwurf: Nach der Anrede geht es klein weiter („Guten Tag …,“ → „dass …“), außer bei Substantiven und „Sie“.

## 0.6.0 – 2026-11-21

### Neu
- **Remote** in der Suche (Auswahl neben „Aufgefrischte behalten“, gilt auch nachts):
  - *Remote markieren* (Standard): 🏠 in der Spalte Ort, wenn die Anzeige Remote oder Homeoffice nennt; Tooltip und Details zeigen, wie viel („Homeoffice 40 %“, „nach Vereinbarung“, „Remote“). Bei der Jobbörse aus ihren Angaben zum Homeoffice, sonst aus Ort, Titel und Text.
  - *+ Remote bundesweit*: dazu Jobbörsen-Stellen aus ganz Deutschland, die ganz aus der Ferne gehen (ab 80 % Homeoffice oder „100 % remote“). Sie bleiben trotz Gebiet und Umkreis in der Liste.
  - *nur Remote/Homeoffice*: nur Stellen mit 🏠.

## 0.5.2 – 2026-11-21

### Behoben
- Prüfung: Fragt die Anzeige nach dem Eintrittstermin, sperrt ein künftiges Startdatum im Satz dazu („Ich kann ab 01.12.2026 anfangen“) die Mail nicht mehr als „Zahl ohne Beleg“. Vergangene Daten und Daten ohne Frage in der Anzeige müssen weiter belegt sein.

## 0.5.1 – 2026-11-21

### Behoben
- get in IT: Anzeigen mit mehreren Standorten (Berlin, Hannover, Leipzig …) hatten keinen Ort. Jetzt wird jeder Standort verortet, und es zählt der im Gebiet bzw. der nächste – wie bei der Jobbörse.

## 0.5.0 – 2026-11-21

### Neu
- **get in IT** als weiteres Portal (Schalter „Portale“, neben LinkedIn und StepStone): gefunden über die eigene SearXNG-Instanz mit `site:get-in-it.de/jobsuche`. Stelle und Firma stehen im Seitentitel. Ort, Datum (für Ampel und Altersfilter), Website der Firma und Anzeigentext liest die App aus der öffentlichen Anzeigenseite, höchstens 20 je Suche. Die interne Such-Schnittstelle von get in IT wird nicht benutzt.

## 0.4.0 – 2026-11-21

### Neu
- **Alter der Anzeigen:** In der Suche „höchstens 15 Tage alt“ (einstellbar von 1 bis 100, 0 = jedes Alter). Ältere Anzeigen fallen raus, der Hinweis nennt, wie viele. Die Jobbörse filtert schon bei der Abfrage, Arbeitnow, Adzuna und Jooble über ihr Datum. Quellen ohne Datum (Verzeichnis, Internet, Alerts, Portale) bleiben ungefiltert.
- **„Aufgefrischte behalten“:** Eine alte Anzeige, die die Firma in dieser Zeit bearbeitet hat, bleibt. In den Details steht dann zum Beispiel „Online seit 37 Tagen, zuletzt bearbeitet vor 15 Tagen“. Ist der Schalter aus, zählt streng die erste Veröffentlichung.
- **Ampel-Spalte „Alter“** in den Treffern: grün unter 15 Tagen, gelb ab 15, rot ab 30 – gezählt ab der ersten Veröffentlichung. Die Zahl ist sortierbar, der Tooltip nennt auch die letzte Bearbeitung.

## 0.3.1 – 2026-11-21

### Behoben
- Ein bloßes „ja“ bei der Nachfrage zur Eignung wurde ein Fakt aus dem wörtlichen Anzeigentext. Die Mail versprach dann genau, was gefordert war. Jetzt bleibt der Punkt offen, bis du schreibst, wo und wie. Anforderungen in Du-Form („Du automatisierst …“) werden nicht mehr wie ein Satz über dich gespeichert.
- Menüpunkte unter „Geschäftsführung“ (etwa „Track Record“) galten als Name der Ansprechperson.
- Ein Satzzeichen direkt hinter einer Adresse („www.plexora.eu:“) zählte zur Adresse, die Prüfung meldete dann „Adresse ohne Beleg“.

## 0.3.0 – 2026-11-20

### Neu
- **Schalter „KI erwünscht“** in der Suche: Die App liest jede Anzeige und behält nur Stellen, die KI-Arbeit ausdrücklich nennen (Claude, Cursor, Copilot, ChatGPT/OpenAI, Codex, Gemini, LLM, RAG, Prompting, Agenten, generative KI, KI-gestützte Entwicklung, n8n/LangChain, MCP). Gelesene Anzeigen werden gemerkt.
- Neue Spalte **„KI“** in den Treffern und „KI in der Anzeige: …“ in den Details.
- **„Route anzeigen“** in den Details: Autoroute vom Startpunkt zur Firma auf OpenStreetMap, im Browser.

## 0.2.3 – 2026-11-20

### Geändert
- **Profil:** Signatur und Gehaltsvorstellung stehen in einer eigenen Karte „Signatur & Gehalt“ mit eigenem Speichern-Knopf, statt in die Kontaktkarte gequetscht.
- Eine Gehaltsvorstellung als bloße Zahl („65000“) steht in der Mail als „65.000 € brutto im Jahr“.

## 0.2.2 – 2026-11-20

### Neu
- **Firmenliste:** mehrere Firmen markieren (Strg/Umschalt) und mit Entf, Rechtsklick oder „Entfernen“ löschen. Firmen, an die schon eine Mail ging, bleiben erhalten.
- **„Entwurf löschen“** unter der Mail-Liste: Der Entwurf wird verworfen und protokolliert, gesendet wird er nie.
- **Gehaltsvorstellung** im Profil: Fragt eine Anzeige ausdrücklich danach, hängt die App genau einen festen Satz an. Jede andere Gehaltsangabe bleibt gesperrt.
- **Kennziffer** aus der Anzeige („Kennziffer ERP-2026“) landet automatisch im Betreff.

### Behoben
- „Invalid IPv6 URL“: Kaputte Links auf Firmenseiten (etwa aus Cookie-Bannern) brachen die Suche nach dem Inhaber ab. Sie werden jetzt übersprungen.
- Umbrochene Listenpunkte in Anzeigen („idealerweise ↵ proALPHA“) beendeten das Lesen der Anforderungen nach dem ersten Punkt. Die Eignung stand dann fälschlich bei 100 %.

## 0.2.1 – 2026-11-20

### Geändert
- Die Mail endet nicht mehr fest mit „15 Minuten“. Die KI formuliert die Bitte um ein Gespräch selbst und duzt, wenn die Anzeige duzt.

### Behoben
- Der Hinweis „lang für eine Erstmail“ zählte Gruß und Signatur mit. Jetzt zählt nur der Text bis zum Gruß.

## 0.2.0 – 2026-11-20

### Neu
- **Suche nach Bundesland:** Die Auswahl „Ganz Niedersachsen“, „Ganz Nordrhein-Westfalen“ usw. filtert über die echte Landesgrenze, nicht über ein Rechteck.
- **Fahrstrecke statt Luftlinie:** Die Spalte km zeigt die Strecke mit dem Auto (OpenStreetMap/OSRM). Beim Umkreis zählt die Straße. ≈ markiert die Luftlinie, wenn keine Route bekannt ist.
- **Fortschrittsbalken** oben rechts für alle Schritte, die zählen („12 von 40“).
- **Ausschlusswörter** in der Suche, zum Beispiel TYPO3 oder Zeitarbeit. Treffer damit fliegen raus. Steht ein Wort im Anzeigentext, entsteht keine Mail.
- **Entwürfe:** Anhänge pro Mail hinzufügen oder per ✕ entfernen. Der Empfänger ist änderbar (genau eine Adresse, wird protokolliert). Speichern zeigt eine Bestätigung. Jede Änderung hebt die Freigabe auf.
- **Mail auf eine Anzeige:** Die Fakten werden Punkt für Punkt den Anforderungen und Aufgaben der Anzeige zugeordnet. „Mail vorschlagen“ prüft vorher die Eignung und fragt nach, was nicht belegt ist.
- **Signatur** aus dem Profil steht unter jeder Mail, Gruß dann mit Vornamen. Telefon und Links darin gelten als belegt.
- **Bessere Mailtexte:** Das Ergebnis der Eignungsprüfung mit Belegen aus Lebenslauf und Zeugnis fließt in die Mail ein. Das Modell plant erst die zwei bis drei stärksten Übereinstimmungen und schreibt dann frei formuliert, Zahlen bleiben exakt. Mit Claude schreibt Sonnet die Mail, Prüfungen bleiben bei Haiku. Floskeln werden angemerkt.
- **Varianten …** in den Entwürfen: Drei Fassungen derselben Mail (sachlich-knapp, persönlich, projektstark), jede mit denselben Fakten und derselben Prüfung. Die gewählte Fassung kommt in den Editor, gespeichert und freigegeben wird wie immer von dir.
- **Nur-Portal-Anzeigen:** Verlangt die Anzeige die Bewerbung über einen „Jetzt bewerben“-Button oder ein Bewerberportal, schreibt die App keine Mail an info@ oder den Vorstand, sondern nennt den Weg über die Anzeige.
- Ortsnamen aus Portalen, Job-Alerts und Arbeitnow werden verortet. Damit greifen Landesgrenze und Fahrstrecke auch dort. Bei Anzeigen mit mehreren Standorten zählt der, der im Gebiet liegt bzw. am nächsten ist.
- `kill -USR1 <pid>` schreibt die Aufrufstapel aller Threads nach `~/.cache/jobagent/stapel.log`.

### Behoben
- Vorteile der Stelle wie „Darauf können Sie sich freuen“ (Bikeleasing, Weihnachtsgeld …) galten als Anforderungen. „Damit überzeugen Sie uns“ wird jetzt als Profil erkannt.
- Die Suche hing beim Prüfen des Firmensitzes. Jetzt gibt es höchstens 6 Linkversuche je Website, 20 s je Seite und 240 s für alle Websites zusammen.
- Bei der Nachfrage zur Eignung galt jede Antwort als „erfüllt“, auch „nö“. Verneinungen lassen den Punkt jetzt offen und stehen als Hinweis dabei. Aus kurzen Antworten wie „ja“ werden Fakten, die für sich verständlich sind.
- Eine verworfene fremde Website hinterließ Namen aus ihrem Impressum als Ansprechperson.
- Links auf die eigene Absenderdomain galten als „Adresse ohne Beleg“.
- Links auf Hosts ohne Domainnamen (http://intranet/) brachen die Internet-Suche ab.

## 0.1.0 – 2026-11-09

Erste Version: lokale Firmensuche (Verzeichnis, Internet, Jobbörse, Arbeitnow, Job-Alerts, Portale, Adzuna, Jooble), Inhaber und Mailadresse von den Firmenseiten, Eignungsprüfung mit Rückfrage, Lebenslauf je Stelle, Mailentwürfe mit Regelprüfung und Freigabe, Versand per SMTP, Nachtlauf mit Morgenbericht. Das LLM läuft lokal über Ollama oder auf Wunsch über die Claude-Code-CLI.
