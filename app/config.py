"""Application configuration loaded from environment variables.

Only bootstrap secrets live in .env. Everything else (telegram, google,
NVR credentials, folders, branding, compression and alert thresholds) lives
in the database and is managed from the WebUI.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

WORK_DIR = Path(os.getenv("WORK_DIR", str(DATA_DIR / "work"))).resolve()
WORK_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(DATA_DIR / "uploads"))).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Finished files that a campus agent serves directly to the browser.  The main
# host creates the directory too, but only the agent writes files into it.
AGENT_FILES_DIR = Path(os.getenv("AGENT_FILES_DIR", str(DATA_DIR / "agent-files"))).resolve()
AGENT_FILES_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "cctv.db"
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{DB_PATH}"


def _load_or_create_secret() -> str:
    """Keep sessions/encryption stable across restarts without git leaks."""
    env_secret = os.getenv("SECRET_KEY")
    if env_secret and len(env_secret) >= 16:
        return env_secret
    secret_file = DATA_DIR / ".secret_key"
    if secret_file.exists():
        value = secret_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    secret_file.write_text(value, encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return value


SECRET_KEY = _load_or_create_secret()

SUPER_ADMIN_USER = os.getenv("SUPER_ADMIN_USER", "superadmin")
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "ChangeMe!2026")
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "cctv@example.com")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5555"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://cctv.footprints.work").rstrip("/")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Phnom_Penh")

RUN_BOT = os.getenv("RUN_BOT", "true").lower() in {"1", "true", "yes", "on"}
RUN_SCHEDULER = os.getenv("RUN_SCHEDULER", "true").lower() in {"1", "true", "yes", "on"}
# Remote campus agent mode. A container started with RUN_AGENT=true polls the
# main host for jobs instead of exposing an independent control plane.
RUN_AGENT = os.getenv("RUN_AGENT", os.getenv("AGENT_MODE", "false")).lower() in {"1", "true", "yes", "on"}
AGENT_MAIN_URL = os.getenv("AGENT_MAIN_URL", "").rstrip("/")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
AGENT_NAME = os.getenv("AGENT_NAME", "")
AGENT_CAMPUS = os.getenv("AGENT_CAMPUS", "").lower()
AGENT_ZONES = os.getenv("AGENT_ZONES", "")
AGENT_TRIGGER_WORD = os.getenv("AGENT_TRIGGER_WORD", "")
AGENT_POLL_SECONDS = int(os.getenv("AGENT_POLL_SECONDS", "20"))
AGENT_VERIFY_TLS = os.getenv("AGENT_VERIFY_TLS", "true").lower() in {"1", "true", "yes", "on"}
AGENT_WORKERS = int(os.getenv("AGENT_WORKERS", "2"))
AGENT_KEEP_FILES = os.getenv("AGENT_KEEP_FILES", "false").lower() in {"1", "true", "yes", "on"}
# Fallback retention for direct-download files.  The main WebUI sends its
# Settings value on every heartbeat, so this is only used before the first one.
AGENT_KEEP_FILES_DAYS = max(1, int(os.getenv("AGENT_KEEP_FILES_DAYS", "7")))
AGENT_UPLOAD_CHUNK_MB = max(1, int(os.getenv("AGENT_UPLOAD_CHUNK_MB", "8")))
AGENT_UPLOAD_RETRIES = max(1, int(os.getenv("AGENT_UPLOAD_RETRIES", "4")))
DEBUG = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}
# One-time migration of the v2 hard-coded NVR_DATA module into the database.
# Opt-in only: public deployments must never ship the private nvr_data.py file.
SEED_LEGACY_NVRS = os.getenv("SEED_LEGACY_NVRS", "false").lower() in {"1", "true", "yes", "on"}

SESSION_COOKIE = "cctv_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(60 * 60 * 24 * 7)))

# Optional bootstrap values. The WebUI/database is the source of truth once set.
BOOTSTRAP_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOOTSTRAP_TELEGRAM_GROUP = os.getenv("TELEGRAM_GROUP_ID", "")
BOOTSTRAP_GOOGLE_FILE = os.getenv("GOOGLE_KEYS_FILE", "google_keys.json")
