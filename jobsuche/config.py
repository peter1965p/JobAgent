"""Technische Einstellungen. Alles über die Person steht in der Datenbank."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATEN = ROOT / "daten"
DB_PFAD = DATEN / "jobsuche.db"  # Nach der Anmeldung: die Datenbank der angemeldeten Person.
DEMO = False  # In der Demo gehen keine Mails raus.
MODELLE = DATEN / "modelle"

OLLAMA_URL = "http://localhost:11434"
STANDARD_MODELL = "qwen2.5:3b"
EMBEDDING_MODELL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIM = 384

MAX_FREIGABEN_PRO_TAG = 5
USER_AGENT = "jobsuche-recherche/0.1"
