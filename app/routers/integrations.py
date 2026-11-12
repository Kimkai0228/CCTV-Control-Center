from __future__ import annotations

import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import audit, get_current_user, require_roles
from ..settings_service import (
    DEFAULT_SETTINGS,
    SUPERADMIN_ONLY_KEYS,
    as_bool,
    as_int,
    get_all_settings,
    get_setting,
    masked_settings,
    set_setting,
)
from ..services import drive, health, mailer, telegram
from ..services.compressor import qsv_info

router = APIRouter(prefix="/api", tags=["settings"])

ADMIN = require_roles("superadmin", "admin")
SUPER = require_roles("superadmin")

INT_KEYS = {
    "smtp.port",
    "google.share_expiry_days",
    "compression.qsv_quality",
    "compression.cpu_crf",
    "compression.max_parallel",
    "jobs.max_workers",
    "jobs.keep_local_days",
    "health.interval_minutes",
    "health.hdd_threshold_percent",
    "health.alert_cooldown_minutes",
    "agents.claim_timeout_minutes",
    "agents.job_timeout_minutes",
    "agents.heartbeat_offline_minutes",
    "agents.poll_seconds",
    "agents.keep_files_days",
    "security.session_hours",
}
FLOAT_KEYS = {"compression.qsv_quality", "compression.cpu_crf"}
BOOL_KEYS = {
    "telegram.alerts_enabled",
    "telegram.alert_mute_enabled",
    "telegram.miniapp_enabled",
    "google.upload_enabled",
    "smtp.enabled",
    "compression.enabled",
    "health.enabled",
    "health.offline_camera_alert",
    "health.nvr_offline_alert",
    "health.hdd_capacity_alert",
    "api.enabled",
    "agents.enabled",
    "agents.strict_campus",
    "agents.fallback_local",
    "agents.direct_download",
    "agents.mode",
}
PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _normalise(key: str, value: Any) -> str:
    if key in BOOL_KEYS:
        return "true" if as_bool(value) else "false"
    if key in INT_KEYS:
        number = as_int(value, 0)
        if key == "health.hdd_threshold_percent" and not 50 <= number <= 100:
            raise ValueError("HDD threshold must be between 50 and 100")
        if key in {"jobs.max_workers", "compression.max_parallel"} and not 1 <= number <= 8:
            raise ValueError("Worker count must be between 1 and 8")
        if key in {"jobs.keep_local_days", "agents.keep_files_days"} and not 1 <= number <= 365:
            raise ValueError("Retention must be between 1 and 365 days")
        if key in {"compression.qsv_quality", "compression.cpu_crf"}:
            return str(max(1, min(51, number)))
        return str(number)
    if key in FLOAT_KEYS:
        return str(value)
    if key in {"compression.qsv_preset", "compression.cpu_preset"}:
        value = str(value)
        if value not in PRESETS:
            raise ValueError(f"Invalid preset: {value}")
        return value
    if key == "compression.backend":
        value = str(value)
        if value not in {"auto", "qsv", "vaapi", "software"}:
            raise ValueError("Compression backend must be auto, qsv, vaapi or software")
        return value
    if key in {"telegram.alert_mute_start", "telegram.alert_mute_end"}:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
        if not match:
            raise ValueError("Quiet-hours times must use HH:MM (for example 17:00)")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Quiet-hours times must be a valid 24-hour time")
        return f"{hour:02d}:{minute:02d}"
    if key in {"telegram.trigger_word", "agents.trigger_word"}:
        value = re.sub(r"[^a-zA-Z0-9_]", "", str(value or "")).lower()[:32]
        return value or "tk"
    if key in {"app.campus", "agents.campus"}:
        return str(value or "").strip().lower()[:40]
    if key == "agents.zones":
        return ",".join(
            re.sub(r"[^a-z0-9_]+", "_", part.strip().lower()).strip("_")
            for part in re.split(r"[,\s]+", str(value or ""))
            if part.strip()
        )
    if key == "agents.main_url":
        return str(value or "").rstrip("/")
    if key in {"app.primary_color", "app.secondary_color", "app.accent_color", "app.background_color"}:
        value = str(value)
        if not COLOR_RE.match(value):
            raise ValueError("Colors must be hex values like #424874")
        return value
    if key == "app.public_url":
        return str(value).rstrip("/")
    if key == "app.theme":
        value = str(value)
        return value if value in {"light", "dark", "auto"} else "light"
    return str(value)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings")
def get_settings(user: models.User = Depends(ADMIN), db: Session = Depends(get_db)):
    return {
        "values": masked_settings(db, user),
        "compression": qsv_info(),
        "defaults": {key: default for key, (default, secret) in DEFAULT_SETTINGS.items() if not secret},
    }


@router.put("/settings")
def update_settings(
    payload: schemas.SettingsUpdate,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    changed: list[str] = []
    errors: list[str] = []
    for key, value in (payload.values or {}).items():
        if key not in DEFAULT_SETTINGS:
            errors.append(f"Unknown setting: {key}")
            continue
        if key in SUPERADMIN_ONLY_KEYS and user.role != "superadmin":
            errors.append(f"Only the super admin can change {key}")
            continue
        is_secret = DEFAULT_SETTINGS[key][1]
        if is_secret and (value is None or str(value) in {"", "********"}):
            continue
        try:
            set_setting(db, key, _normalise(key, value), is_secret)
            changed.append(key)
        except ValueError as exc:
            errors.append(f"{key}: {exc}")
    db.commit()
    audit(db, user, "settings_updated", ", ".join(changed), request.client.host if request.client else None)
    return {"ok": not errors, "changed": changed, "errors": errors, "values": masked_settings(db, user)}


# ---------------------------------------------------------------------------
# Integration status
# ---------------------------------------------------------------------------


@router.get("/integrations")
def integration_status(user: models.User = Depends(ADMIN), db: Session = Depends(get_db)):
    google_raw = get_setting(db, "google.service_account_json")
    google_email = ""
    if google_raw:
        try:
            google_email = json.loads(google_raw).get("client_email", "")
        except ValueError:
            google_email = ""
    return {
        "google": {
            "configured": bool(google_raw),
            "client_email": google_email,
            "impersonate_email": get_setting(db, "google.impersonate_email"),
            "fallback_folder_id": get_setting(db, "google.fallback_folder_id"),
            "share_expiry_days": as_int(get_setting(db, "google.share_expiry_days"), 30),
            "upload_enabled": as_bool(get_setting(db, "google.upload_enabled"), True),
        },
        "telegram": {
            "configured": bool(get_setting(db, "telegram.bot_token")),
            "group_id": get_setting(db, "telegram.group_id"),
            "alert_chat_id": get_setting(db, "telegram.alert_chat_id"),
            "alerts_enabled": as_bool(get_setting(db, "telegram.alerts_enabled"), True),
            "alert_mute_enabled": as_bool(get_setting(db, "telegram.alert_mute_enabled"), False),
            "alert_mute_start": get_setting(db, "telegram.alert_mute_start") or "17:00",
            "alert_mute_end": get_setting(db, "telegram.alert_mute_end") or "07:00",
            "alert_mute_active": telegram.alert_muted(db),
            "miniapp_enabled": as_bool(get_setting(db, "telegram.miniapp_enabled"), True),
            "allowed_user_ids": get_setting(db, "telegram.allowed_user_ids"),
            "trigger_word": get_setting(db, "telegram.trigger_word") or "tk",
        },
        "smtp": {
            "configured": bool(get_setting(db, "smtp.username") and get_setting(db, "smtp.password")),
            "host": get_setting(db, "smtp.host"),
            "port": as_int(get_setting(db, "smtp.port"), 587),
            "username": get_setting(db, "smtp.username"),
            "from_email": get_setting(db, "smtp.from_email"),
            "from_name": get_setting(db, "smtp.from_name"),
            "cc_emails": get_setting(db, "smtp.cc_emails"),
            "approver_email": get_setting(db, "smtp.approver_email"),
            "enabled": as_bool(get_setting(db, "smtp.enabled"), True),
        },
        "counts": {
            "nvrs": db.query(models.NVR).count(),
            "cameras": db.query(models.Camera).count(),
            "folders": db.query(models.DriveFolder).count(),
        },
    }


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


@router.put("/integrations/google")
def update_google(
    payload: schemas.GoogleSettings,
    request: Request,
    user: models.User = Depends(SUPER),
    db: Session = Depends(get_db),
):
    if payload.service_account_json:
        ok, message = drive.validate_credentials_json(payload.service_account_json)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        set_setting(db, "google.service_account_json", payload.service_account_json)
    if payload.impersonate_email is not None:
        set_setting(db, "google.impersonate_email", payload.impersonate_email.strip())
    if payload.fallback_folder_id is not None:
        set_setting(db, "google.fallback_folder_id", payload.fallback_folder_id.strip())
    set_setting(db, "google.share_expiry_days", max(1, payload.share_expiry_days))
    set_setting(db, "google.upload_enabled", "true" if payload.upload_enabled else "false")
    audit(db, user, "integration_google", "Updated Google Drive integration", request.client.host if request.client else None)
    return {"ok": True}


@router.post("/integrations/google/upload")
async def upload_google_credentials(
    request: Request,
    file: UploadFile = File(...),
    user: models.User = Depends(SUPER),
    db: Session = Depends(get_db),
):
    raw = (await file.read()).decode("utf-8", "ignore")
    ok, message = drive.validate_credentials_json(raw)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    set_setting(db, "google.service_account_json", raw)
    audit(db, user, "integration_google", f"Uploaded {file.filename}", request.client.host if request.client else None)
    return {"ok": True, "message": message}


@router.post("/integrations/google/test")
def test_google(user: models.User = Depends(ADMIN), db: Session = Depends(get_db)):
    try:
        service = drive.DriveService.from_settings(db)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    ok, message = service.test()
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


@router.put("/integrations/telegram")
def update_telegram(
    payload: schemas.TelegramSettings,
    request: Request,
    user: models.User = Depends(SUPER),
    db: Session = Depends(get_db),
):
    if payload.bot_token and payload.bot_token != "********":
        set_setting(db, "telegram.bot_token", payload.bot_token.strip())
    if payload.group_id is not None:
        set_setting(db, "telegram.group_id", payload.group_id.strip())
    if payload.alert_chat_id is not None:
        set_setting(db, "telegram.alert_chat_id", payload.alert_chat_id.strip())
    set_setting(db, "telegram.alerts_enabled", "true" if payload.alerts_enabled else "false")
    if payload.alert_mute_enabled is not None:
        set_setting(db, "telegram.alert_mute_enabled", "true" if payload.alert_mute_enabled else "false")
    if payload.alert_mute_start is not None or payload.alert_mute_end is not None:
        try:
            mute_start = _normalise(
                "telegram.alert_mute_start",
                payload.alert_mute_start
                if payload.alert_mute_start is not None
                else get_setting(db, "telegram.alert_mute_start"),
            )
            mute_end = _normalise(
                "telegram.alert_mute_end",
                payload.alert_mute_end
                if payload.alert_mute_end is not None
                else get_setting(db, "telegram.alert_mute_end"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if mute_start == mute_end:
            raise HTTPException(status_code=400, detail="Quiet-hours start and end must be different")
        set_setting(db, "telegram.alert_mute_start", mute_start)
        set_setting(db, "telegram.alert_mute_end", mute_end)
    set_setting(db, "telegram.miniapp_enabled", "true" if payload.miniapp_enabled else "false")
    if payload.allowed_user_ids is not None:
        set_setting(db, "telegram.allowed_user_ids", payload.allowed_user_ids.strip())
    if payload.trigger_word is not None:
        word = _normalise("telegram.trigger_word", payload.trigger_word)
        if db.query(models.Agent).filter(models.Agent.trigger_word == word).first():
            raise HTTPException(
                status_code=400,
                detail=f"/{word} is already assigned to a campus agent",
            )
        set_setting(db, "telegram.trigger_word", word)
    audit(db, user, "integration_telegram", "Updated Telegram integration", request.client.host if request.client else None)
    # Token changes need a fresh Telegram client, without restarting the app.
    try:
        from ..bot import reload_bot

        reload_bot()
    except Exception as exc:
        print(f"[bot] reload failed: {exc}")
    return {"ok": True}


@router.post("/integrations/telegram/test")
def test_telegram(user: models.User = Depends(ADMIN), db: Session = Depends(get_db)):
    ok, message = telegram.send_test(db)
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


@router.put("/integrations/smtp")
def update_smtp(
    payload: schemas.SMTPSettings,
    request: Request,
    user: models.User = Depends(SUPER),
    db: Session = Depends(get_db),
):
    set_setting(db, "smtp.enabled", "true" if payload.enabled else "false")
    set_setting(db, "smtp.host", payload.host.strip())
    set_setting(db, "smtp.port", max(1, payload.port))
    set_setting(db, "smtp.username", payload.username.strip())
    if payload.password and payload.password != "********":
        set_setting(db, "smtp.password", payload.password)
    set_setting(db, "smtp.from_email", payload.from_email.strip())
    set_setting(db, "smtp.from_name", payload.from_name.strip())
    set_setting(db, "smtp.cc_emails", payload.cc_emails.strip())
    set_setting(db, "smtp.approver_email", payload.approver_email.strip())
    audit(db, user, "integration_smtp", "Updated SMTP integration", request.client.host if request.client else None)
    return {"ok": True}


@router.post("/integrations/smtp/test")
def test_smtp(
    payload: schemas.TestConnection,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    recipient = (payload.recipient or get_setting(db, "smtp.username")).strip()
    if not recipient:
        return {"ok": False, "message": "Provide a test recipient"}
    ok, message = mailer.send_test_email(db, recipient)
    return {"ok": ok, "message": message}


# ---------------------------------------------------------------------------
# Health / system
# ---------------------------------------------------------------------------


@router.post("/health/run")
def run_health(
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    checker = health.HealthChecker(db)
    result = checker.run(notify=True)
    return result


@router.post("/system/api-key/rotate")
def rotate_api_key(
    request: Request,
    user: models.User = Depends(SUPER),
    db: Session = Depends(get_db),
):
    key = secrets.token_urlsafe(36)
    set_setting(db, "api.key", key)
    audit(db, user, "api_key_rotated", "Camera REST API key rotated", request.client.host if request.client else None)
    return {"ok": True, "key": key}


@router.get("/system/compression")
def compression_info(user: models.User = Depends(get_current_user)):
    return qsv_info()


@router.post("/system/compression/probe")
def compression_probe(user: models.User = Depends(ADMIN)):
    from ..services.compressor import probe_qsv

    available, detail = probe_qsv(force=True)
    return {**qsv_info(), "available": available, "detail": detail}
