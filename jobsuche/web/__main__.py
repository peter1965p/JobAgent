"""Start: python -m jobsuche.web            -> Server (Einstellungen aus der Umgebung, siehe deploy/jobagent-web.service)
       python -m jobsuche.web schluessel -> neuen AES-Schlüssel ausgeben (nur bei der Einrichtung, direkt in die 600-Datei)

Umgebung: JOBAGENT_SCHLUESSEL (Pflicht), JOBAGENT_PG_HOST/PORT/DB/USER/PASSWORT, JOBAGENT_DATEN, JOBAGENT_HOSTS,
          JOBAGENT_OLLAMA_URL, JOBAGENT_CLAUDE_BRUECKE_URL/TOKEN, JOBAGENT_BIND (127.0.0.1), JOBAGENT_PORT (8090)."""

import base64
import logging
import os
import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["schluessel"]:
        print(base64.urlsafe_b64encode(os.urandom(32)).decode())
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import uvicorn
    from .app import erstelle_app
    app = erstelle_app(nachtlauf=os.environ.get("JOBAGENT_NACHTLAUF", "1") == "1")
    # Hinter nginx: Proxy-Kopfzeilen nur von 127.0.0.1 übernehmen. Direkt erreichbar ist der Port nicht (bind 127.0.0.1).
    uvicorn.run(app, host=os.environ.get("JOBAGENT_BIND", "127.0.0.1"), port=int(os.environ.get("JOBAGENT_PORT", "8090")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", server_header=False, date_header=False,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
