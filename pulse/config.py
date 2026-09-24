"""Runtime settings, all overridable through environment variables (or a .env file)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

BOT_NAME = os.getenv("BOT_NAME", "Pulse")
GROUP_NAME = os.getenv("GROUP_NAME", "METI UniPods AI Innovation Programme, Cohort 1")

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")  # low | medium | high | xhigh | max
USE_FALLBACKS = os.getenv("CLAUDE_FALLBACKS", "1") == "1"

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "pulse.db"))
UPLOAD_DIR = DATA_DIR / "uploads"

# Protects uploads / deletes / the ingest API. Leave empty only for local demos.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# If the whole knowledge base is smaller than this (characters), send all of it to Claude
# (prompt-cached) instead of doing keyword retrieval. ~4 chars per token.
FULL_CONTEXT_CHARS = int(os.getenv("FULL_CONTEXT_CHARS", "200000"))
RETRIEVAL_CHUNKS = int(os.getenv("RETRIEVAL_CHUNKS", "25"))
DIGEST_MAX_CHARS = int(os.getenv("DIGEST_MAX_CHARS", "400000"))

# WhatsApp exports: which way to read ambiguous dates like 03/04/2026 ("dmy" or "mdy").
DEFAULT_DATE_ORDER = os.getenv("DEFAULT_DATE_ORDER", "dmy")
TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "SAST")

# Connectors
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
