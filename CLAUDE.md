# Job Agent

Peter schreibt Deutsch und nennt Claude „Krebsi“. Er ist ITler: knapp und technisch antworten, Grundlagen weglassen, offene Risiken und Ungeprüftes klar sagen.

Lies zuerst `GESPRAECHSPROTOKOLL.md`. Dort stehen Stand, Fakten für Bewerbungen, offene Punkte, Firmen und die Nachrichten.

Die App heißt Job Agent (früher Türöffner) und liegt in `jobsuche/` (der Modulname bleibt), siehe `README.md`. Das LLM läuft nur lokal über Ollama, Cloud-Anbieter sind ausgeschlossen. Die App ist für beliebige Personen gebaut: Im Code stehen keine persönlichen Daten, ein Test prüft das. Jede Person meldet sich mit Mailadresse und Mailpasswort an und hat eine eigene SQLite-Datei. Peters echte Daten stehen in `daten/`: Bestätigen, Freigeben und Ergebnisse gehören ihm, Tests laufen auf einer Kopie.

Der Versand spricht echte Server an: Tests laufen nur mit dem Fake-Server, echte Mails löst nur Peter aus.

Das Protokoll enthält persönliche Daten wie die Gehaltsvorstellung. Nichts committen oder pushen, ohne Peter zu fragen, und nichts an Firmen schicken, ohne dass er es freigegeben hat.
