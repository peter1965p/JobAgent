# Änderungen

Versionen nach [Semantic Versioning](https://semver.org/lang/de/). Die laufende Version steht in `jobsuche/__init__.py` und unten links in der App.

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
