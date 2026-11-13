"""Settings storage, defaults and first-run seeding."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from . import models
from .config import (
    BASE_DIR,
    BOOTSTRAP_GOOGLE_FILE,
    BOOTSTRAP_TELEGRAM_GROUP,
    BOOTSTRAP_TELEGRAM_TOKEN,
    PUBLIC_URL,
    SEED_LEGACY_NVRS,
    SUPER_ADMIN_EMAIL,
    SUPER_ADMIN_PASSWORD,
    SUPER_ADMIN_USER,
    TIMEZONE,
)
from .database import SessionLocal
from .security import decrypt_secret, encrypt_secret, hash_password

# key: (default, is_secret)
DEFAULT_SETTINGS: dict[str, tuple[str, bool]] = {
    # Branding / general
    "app.name": ("CCTV Control Center", False),
    "app.organization": ("Footprints International School", False),
    "app.public_url": (PUBLIC_URL, False),
    "app.timezone": (TIMEZONE, False),
    "app.logo_url": ("", False),
    "app.theme": ("light", False),
    # The campus where this host physically lives. Used by agent routing.
    "app.campus": (os.getenv("APP_CAMPUS", "tk").lower(), False),
    "app.primary_color": ("#424874", False),
    "app.secondary_color": ("#A6B1E1", False),
    "app.accent_color": ("#DCD6F7", False),
    "app.background_color": ("#F4EEFF", False),
    # Telegram
    "telegram.bot_token": ("", True),
    "telegram.group_id": ("", False),
    "telegram.alert_chat_id": ("", False),
    "telegram.alerts_enabled": ("true", False),
    "telegram.alert_mute_enabled": ("false", False),
    "telegram.alert_mute_start": ("17:00", False),
    "telegram.alert_mute_end": ("07:00", False),
    "telegram.miniapp_enabled": ("true", False),
    "telegram.allowed_user_ids": ("", False),
    # Global command, e.g. /tk. Agents can override this per campus.
    "telegram.trigger_word": ("tk", False),
    # Google Drive
    "google.service_account_json": ("", True),
    "google.impersonate_email": ("cctv@footprintsschool.edu.kh", False),
    "google.fallback_folder_id": ("", False),
    "google.share_expiry_days": ("30", False),
    "google.upload_enabled": ("true", False),
    # SMTP / email
    "smtp.enabled": ("true", False),
    "smtp.host": ("smtp.gmail.com", False),
    "smtp.port": ("587", False),
    # Credentials and addresses are supplied in the WebUI, never hard-coded.
    "smtp.username": ("", False),
    "smtp.password": ("", True),
    "smtp.from_email": ("", False),
    "smtp.from_name": ("Footprints CCTV", False),
    "smtp.cc_emails": ("", False),
    "smtp.approver_email": ("", False),
    # Compression
    "compression.enabled": ("true", False),
    "compression.backend": ("auto", False),  # auto|qsv|software
    "compression.qsv_preset": ("faster", False),
    "compression.qsv_quality": ("25", False),
    "compression.cpu_preset": ("ultrafast", False),
    "compression.cpu_crf": ("30", False),
    "compression.max_parallel": ("2", False),
    # Live view: RTSP is transcoded to MJPEG in the browser.
    "live.quality": ("balanced", False),  # low|balanced|high
    "live.stream": ("main", False),  # main|sub
    # Download / jobs
    "jobs.max_workers": ("2", False),
    "jobs.keep_local_days": ("7", False),
    "jobs.default_folder_name": ("CCTV_Request", False),
    # Health monitoring
    "health.enabled": ("true", False),
    "health.interval_minutes": ("10", False),
    "health.hdd_threshold_percent": ("90", False),
    "health.alert_cooldown_minutes": ("60", False),
    "health.offline_camera_alert": ("true", False),
    "health.nvr_offline_alert": ("true", False),
    # NVR/DVR disks are normally always full. Alert on corruption/error only
    # unless full-capacity alerting is explicitly enabled.
    "health.hdd_capacity_alert": ("false", False),
    # Camera REST API
    "api.enabled": ("true", False),
    "api.key": ("", True),
    # Remote campus agents
    "agents.enabled": ("true", False),
    "agents.strict_campus": ("false", False),
    "agents.fallback_local": ("true", False),
    "agents.claim_timeout_minutes": ("10", False),
    "agents.job_timeout_minutes": ("180", False),
    "agents.heartbeat_offline_minutes": ("3", False),
    "agents.poll_seconds": ("20", False),
    # Direct download: finished files stay on the campus agent and the browser
    # downloads them over the agent's public HTTPS URL.  The main host only
    # signs short-lived links and never stores a second copy.
    "agents.direct_download": ("true", False),
    "agents.keep_files_days": ("7", False),
    # Optional in-process agent mode (also settable through env/container).
    "agents.mode": ("false", False),
    "agents.main_url": ("", False),
    "agents.name": ("", False),
    "agents.campus": ("", False),
    "agents.zones": ("", False),
    "agents.trigger_word": ("", False),
    "agents.token": ("", True),
    # Security
    "security.session_hours": ("168", False),
}

SECRET_KEYS = {key for key, (_, secret) in DEFAULT_SETTINGS.items() if secret}

# Settings that belonged to features removed in v3.1. Delete them on startup.
REMOVED_SETTINGS = {
    "health.temp_threshold_c",
    "health.snmp_enabled",
    "health.snmp_community",
    "health.snmp_timeout_seconds",
}

# Keys that must never be returned to a non-superadmin session.
SUPERADMIN_ONLY_KEYS = {
    "telegram.bot_token",
    "google.service_account_json",
    "smtp.password",
    "api.key",
    "agents.token",
}


def get_setting(db: Session, key: str, default: str | None = None) -> str:
    row = db.get(models.Setting, key)
    if not row:
        if key in DEFAULT_SETTINGS:
            return DEFAULT_SETTINGS[key][0]
        return default if default is not None else ""
    if row.is_secret:
        return decrypt_secret(row.value)
    return row.value or ""


def set_setting(db: Session, key: str, value: Any, is_secret: bool | None = None) -> models.Setting:
    if is_secret is None:
        is_secret = key in SECRET_KEYS
    text = "" if value is None else str(value)
    stored = encrypt_secret(text) if is_secret else text
    row = db.get(models.Setting, key)
    if row:
        row.value = stored
        row.is_secret = is_secret
    else:
        row = models.Setting(key=key, value=stored, is_secret=is_secret)
        db.add(row)
    db.commit()
    return row


def get_all_settings(db: Session, include_secrets: bool = True) -> dict[str, Any]:
    values = {key: default for key, (default, _) in DEFAULT_SETTINGS.items()}
    for row in db.query(models.Setting).all():
        values[row.key] = decrypt_secret(row.value) if row.is_secret else (row.value or "")
    if not include_secrets:
        for key in SECRET_KEYS:
            if values.get(key):
                values[key] = "********"
    return values


def masked_settings(db: Session, user: models.User) -> dict[str, Any]:
    values = get_all_settings(db, include_secrets=False)
    if user.role != "superadmin":
        for key in SUPERADMIN_ONLY_KEYS:
            values.pop(key, None)
    return values


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def public_config(db: Session) -> dict[str, Any]:
    """Values the login page and MiniApp need before authentication."""
    return {
        "app_name": get_setting(db, "app.name"),
        "organization": get_setting(db, "app.organization"),
        "public_url": get_setting(db, "app.public_url") or PUBLIC_URL,
        "logo_url": get_setting(db, "app.logo_url"),
        "theme": get_setting(db, "app.theme"),
        "colors": {
            "primary": get_setting(db, "app.primary_color"),
            "secondary": get_setting(db, "app.secondary_color"),
            "accent": get_setting(db, "app.accent_color"),
            "background": get_setting(db, "app.background_color"),
        },
    }


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_superadmin(db: Session) -> None:
    user = db.query(models.User).filter(models.User.username == SUPER_ADMIN_USER).first()
    if not user:
        user = models.User(
            username=SUPER_ADMIN_USER,
            email=SUPER_ADMIN_EMAIL,
            password_hash=hash_password(SUPER_ADMIN_PASSWORD),
            role="superadmin",
            is_active=True,
        )
        db.add(user)
    else:
        user.role = "superadmin"
        user.is_active = True
        # .env is the source of truth for the bootstrap super-admin password.
        from .security import verify_password

        if not verify_password(SUPER_ADMIN_PASSWORD, user.password_hash):
            user.password_hash = hash_password(SUPER_ADMIN_PASSWORD)
    db.commit()


def _seed_settings(db: Session) -> None:
    existing = {row[0] for row in db.query(models.Setting.key).all()}
    for key, (default, secret) in DEFAULT_SETTINGS.items():
        if key not in existing:
            db.add(models.Setting(key=key, value=encrypt_secret(default) if secret else default, is_secret=secret))
    db.commit()


def _purge_removed_settings(db: Session) -> None:
    """Drop settings rows left behind by features removed from the product."""
    db.query(models.Setting).filter(models.Setting.key.in_(REMOVED_SETTINGS)).delete(
        synchronize_session=False
    )
    db.commit()


def _seed_telegram(db: Session) -> None:
    if BOOTSTRAP_TELEGRAM_TOKEN and not get_setting(db, "telegram.bot_token"):
        set_setting(db, "telegram.bot_token", BOOTSTRAP_TELEGRAM_TOKEN)
    if BOOTSTRAP_TELEGRAM_GROUP and not get_setting(db, "telegram.group_id"):
        set_setting(db, "telegram.group_id", BOOTSTRAP_TELEGRAM_GROUP)


def _seed_google(db: Session) -> None:
    if get_setting(db, "google.service_account_json"):
        return
    path = BASE_DIR / BOOTSTRAP_GOOGLE_FILE
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            json.loads(raw)
            set_setting(db, "google.service_account_json", raw)
        except Exception:
            pass


def _seed_drive_folders(db: Session) -> None:
    if db.query(models.DriveFolder).count() > 0:
        return
    for key, value in os.environ.items():
        if not key.startswith("DRIVE_") or key == "DRIVE_FALLBACK" or not value:
            continue
        folder_key = key.replace("DRIVE_", "")
        campus = folder_key.split("_")[0].lower()
        display = folder_key.replace("_", " ").title()
        db.add(
            models.DriveFolder(
                key=folder_key,
                name=display,
                folder_id=value,
                campus=campus,
                enabled=True,
            )
        )
    fallback = os.getenv("DRIVE_FALLBACK")
    if fallback and not get_setting(db, "google.fallback_folder_id"):
        set_setting(db, "google.fallback_folder_id", fallback)
    db.commit()


def _seed_legacy_nvrs(db: Session) -> None:
    """Import the v2 hard-coded NVR_DATA once, so existing cameras keep working."""
    if not SEED_LEGACY_NVRS:
        return
    if db.query(models.NVR).count() > 0:
        return
    try:
        from nvr_data import NVR_DATA  # type: ignore
    except Exception:
        return

    for campus_key, zones in NVR_DATA.items():
        for zone_key, nvr_list in zones.items():
            for item in nvr_list:
                parsed = urlparse(item.get("base_url", ""))
                code = item.get("code", "").lower()
                if "tru" in code:
                    protocol = "truvision"
                elif code.startswith("dvr_") or "dvr" in code:
                    protocol = "dvr"
                else:
                    protocol = "hikvision"
                nvr = models.NVR(
                    name=item.get("name", code),
                    code=code,
                    campus=campus_key,
                    zone=zone_key,
                    protocol=protocol,
                    host=parsed.hostname or "",
                    port=80,
                    sdk_port=8000,
                    username=unquote(parsed.username or "admin"),
                    password_enc=encrypt_secret(unquote(parsed.password or "")),
                    channel_offset=0 if protocol == "dvr" else 32,
                    track_multiplier=100,
                    enabled=True,
                    auto_discover=True,
                )
                db.add(nvr)
                db.flush()
                for channel, cam_name in (item.get("cameras") or {}).items():
                    db.add(
                        models.Camera(
                            nvr_id=nvr.id,
                            channel=int(channel),
                            name=cam_name,
                            enabled=True,
                        )
                    )
    db.commit()


def seed_database() -> None:
    db = SessionLocal()
    try:
        _purge_removed_settings(db)
        _seed_superadmin(db)
        _seed_settings(db)
        _seed_telegram(db)
        _seed_google(db)
        _seed_drive_folders(db)
        _seed_legacy_nvrs(db)
    finally:
        db.close()
