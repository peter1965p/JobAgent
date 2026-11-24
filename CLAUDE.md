# Job Agent

Peter schreibt Deutsch und nennt Claude „Krebsi“. Er ist ITler: knapp und technisch antworten, Grundlagen weglassen, offene Risiken und Ungeprüftes klar sagen.

Lies zuerst `GESPRAECHSPROTOKOLL.md`. Dort stehen Stand, Fakten für Bewerbungen, offene Punkte, Firmen und die Nachrichten.

Die App heißt Job Agent (früher Türöffner) und liegt in `jobsuche/` (der Modulname bleibt), siehe `README.md`. Das LLM läuft lokal über Ollama oder, wenn die Person es wählt, über Claude Code (ihr Abo, per CLI). Die App fragt dafür einmal nach Zustimmung, weil dann Unterlagen an Anthropic gehen. Dritter Anbieter ist Cloudflare Workers AI (`cf:` in `llm.py`) über einen eigenen Worker mit Token (URL und Token aus Umgebung bzw. Schlüsselbund). Weitere Cloud-Anbieter sind ausgeschlossen, Tests rufen weder Claude noch Workers AI auf. Die App ist für beliebige Personen gebaut: Im Code stehen keine persönlichen Daten, ein Test prüft das. Ohne PostgreSQL meldet sich jede Person mit Mailadresse und Mailpasswort an und hat eine eigene SQLite-Datei. Mit PostgreSQL gibt es Konten und Mandanten (`mandanten.py`, Schema `js_zentral`, je Mandant eigenes Schema und eigene Rolle per SET ROLE) und dazu die Web-Oberfläche `jobsuche/web` (CT 106, öffentlich über Cloudflare Tunnel, nur mit Einladung). Peters echte Daten stehen in `daten/`: Bestätigen, Freigeben und Ergebnisse gehören ihm, Tests laufen auf einer Kopie.

Der Versand spricht echte Server an: Tests laufen nur mit dem Fake-Server, echte Mails löst nur Peter aus.

Das Protokoll enthält persönliche Daten wie die Gehaltsvorstellung. Nichts committen oder pushen, ohne Peter zu fragen, und nichts an Firmen schicken, ohne dass er es freigegeben hat.
