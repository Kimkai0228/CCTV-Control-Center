"""Remote campus agents: registration, dispatch, heartbeats and progress.

The main host owns all configuration.  An agent container polls over HTTPS,
receives one job at a time with everything it needs (NVR credentials,
compression settings and, when uploading, Drive credentials), executes the
download locally and posts progress back.  Tokens are stored encrypted and
compared in constant time.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import secrets
import socket
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models
from ..security import audit, decrypt_secret, encrypt_secret
from ..settings_service import as_bool, as_int, get_setting

ACTIVE_STATUSES = {"dispatched", "running"}
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def agent_dict(agent: models.Agent, *, include_token: bool = False) -> dict[str, Any]:
    data = {
        "id": agent.id,
        "name": agent.name,
        "campus": agent.campus,
        "zones": agent.zones,
        "trigger_word": agent.trigger_word,
        "public_url": agent.public_url,
        "enabled": agent.enabled,
        "status": agent.status,
        "version": agent.version,
        "hostname": agent.hostname,
        "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        "last_job_at": agent.last_job_at.isoformat() if agent.last_job_at else None,
        "notes": agent.notes,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }
    if include_token:
        data["token"] = decrypt_secret(agent.token_enc)
    return data


def _normalise_zones(zones: Any) -> list[str]:
    if isinstance(zones, str):
        items = zones.replace(";", ",").split(",")
    else:
        items = zones or []
    result: list[str] = []
    for item in items:
        value = str(item).strip().lower().replace(" ", "_")
        if value and value not in result:
            result.append(value)
    return result


def create_agent(
    db: Session,
    *,
    name: str,
    campus: str = "tk",
    zones: Any = None,
    trigger_word: str = "tk",
    public_url: str | None = None,
    notes: str | None = None,
) -> tuple[models.Agent, str]:
    token = secrets.token_urlsafe(32)
    agent = models.Agent(
        name=name.strip()[:160] or "Campus agent",
        campus=(campus or "tk").strip().lower()[:40],
        zones_json=json.dumps(_normalise_zones(zones)),
        trigger_word=(trigger_word or "tk").strip().lower()[:32],
        public_url=(public_url or "").strip().rstrip("/") or None,
        token_enc=encrypt_secret(token),
        notes=(notes or "").strip()[:1000] or None,
        enabled=True,
        status="unknown",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent, token


def rotate_token(db: Session, agent: models.Agent) -> str:
    token = secrets.token_urlsafe(32)
    agent.token_enc = encrypt_secret(token)
    db.commit()
    return token


def find_by_token(db: Session, token: str) -> models.Agent | None:
    token = (token or "").strip()
    if not token:
        return None
    for agent in db.query(models.Agent).filter(models.Agent.enabled.is_(True)).all():
        candidate = decrypt_secret(agent.token_enc)
        if candidate and hmac.compare_digest(candidate, token):
            return agent
    return None


def touch(db: Session, agent: models.Agent, **info: Any) -> None:
    agent.last_heartbeat = _now()
    agent.status = "online"
    if info.get("version"):
        agent.version = str(info["version"])[:40]
    if info.get("hostname"):
        agent.hostname = str(info["hostname"])[:160]
    db.commit()


def mark_offline_agents(db: Session) -> None:
    minutes = max(1, as_int(get_setting(db, "agents.heartbeat_offline_minutes"), 3))
    cutoff = _now() - dt.timedelta(minutes=minutes)
    changed = False
    for agent in db.query(models.Agent).all():
        seen = _aware(agent.last_heartbeat)
        if agent.enabled and (seen is None or seen < cutoff) and agent.status != "offline":
            agent.status = "offline"
            changed = True
    if changed:
        db.commit()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def job_campus(db: Session, payload: dict[str, Any]) -> str:
    campus = str(payload.get("campus") or "").strip().lower()
    if campus:
        return campus
    for item in payload.get("cameras") or []:
        nvr = db.get(models.NVR, int(item.get("nvr_id") or 0))
        if nvr and nvr.campus:
            return nvr.campus.strip().lower()
    return ""


def _job_zones(db: Session, payload: dict[str, Any]) -> set[str]:
    zones: set[str] = set()
    for item in payload.get("cameras") or []:
        nvr = db.get(models.NVR, int(item.get("nvr_id") or 0))
        if nvr and nvr.zone:
            zones.add(nvr.zone.strip().lower())
    return zones


def choose_agent(
    db: Session,
    payload: dict[str, Any],
    preferred_id: int | None = None,
) -> models.Agent | None:
    """Pick an enabled agent for a job, or None to run on the main host."""
    if not as_bool(get_setting(db, "agents.enabled"), True):
        return None
    if preferred_id:
        agent = db.get(models.Agent, int(preferred_id))
        return agent if agent and agent.enabled else None

    campus = job_campus(db, payload)
    local_campus = (get_setting(db, "app.campus") or "tk").strip().lower()
    if not campus or campus == local_campus:
        return None
    zones = _job_zones(db, payload)
    candidates = (
        db.query(models.Agent)
        .filter(models.Agent.enabled.is_(True), models.Agent.campus == campus)
        .order_by(models.Agent.id)
        .all()
    )
    for agent in candidates:
        agent_zones = set(agent.zones)
        if not agent_zones or not zones or agent_zones & zones:
            return agent
    return None


def _retry_camera_keys(job: models.Job) -> set[tuple[int, int]] | None:
    """Camera keys queued by a retry request, or ``None`` for a normal run."""
    if not job.retry_json:
        return None
    try:
        data = json.loads(job.retry_json)
    except (TypeError, ValueError):
        return None
    keys: set[tuple[int, int]] = set()
    for item in data if isinstance(data, list) else []:
        try:
            keys.add((int(item.get("nvr_id") or 0), int(item.get("channel") or 0)))
        except (TypeError, ValueError):
            continue
    keys.discard((0, 0))
    return keys or None


def dispatch_job(db: Session, job: models.Job, agent: models.Agent) -> models.Job:
    job.agent_id = agent.id
    job.status = "dispatched"
    job.progress = 0
    job.message = f"Waiting for agent {agent.name}"
    job.dispatched_at = _now()
    agent.last_job_at = _now()
    db.commit()
    return job


def _nvr_payload(nvr: models.NVR) -> dict[str, Any]:
    """Everything the agent needs to reach an NVR (credentials decrypted)."""
    return {
        "id": nvr.id,
        "name": nvr.name,
        "code": nvr.code,
        "campus": nvr.campus,
        "zone": nvr.zone,
        "protocol": nvr.protocol,
        "host": nvr.host,
        "port": int(nvr.port or 80),
        "sdk_port": int(nvr.sdk_port or 8000),
        "username": nvr.username,
        "password": decrypt_secret(nvr.password_enc),
        "channel_offset": nvr.channel_offset,
        "track_multiplier": nvr.track_multiplier,
        "time_offset_hours": nvr.time_offset_hours,
    }


def job_payload_for_agent(db: Session, job: models.Job) -> dict[str, Any]:
    payload = json.loads(job.request_json or "{}")
    retry_keys = _retry_camera_keys(job)
    if retry_keys:
        payload["cameras"] = [
            item
            for item in (payload.get("cameras") or [])
            if (int(item.get("nvr_id") or 0), int(item.get("channel") or 0))
            in retry_keys
        ]
    cameras: list[dict[str, Any]] = []
    seen_nvrs: dict[int, dict[str, Any]] = {}
    for item in payload.get("cameras") or []:
        nvr = db.get(models.NVR, int(item.get("nvr_id") or 0))
        if not nvr:
            continue
        if nvr.id not in seen_nvrs:
            seen_nvrs[nvr.id] = _nvr_payload(nvr)
        camera = (
            db.query(models.Camera)
            .filter(models.Camera.nvr_id == nvr.id, models.Camera.channel == int(item.get("channel")))
            .first()
        )
        if camera is not None and not camera.enabled:
            continue
        cameras.append(
            {
                "nvr": seen_nvrs[nvr.id],
                "channel": int(item.get("channel")),
                "name": camera.name if camera else f"Channel {item.get('channel')}",
            }
        )

    settings = {
        "compression.enabled": get_setting(db, "compression.enabled"),
        "compression.backend": get_setting(db, "compression.backend"),
        "compression.qsv_preset": get_setting(db, "compression.qsv_preset"),
        "compression.qsv_quality": get_setting(db, "compression.qsv_quality"),
        "compression.cpu_preset": get_setting(db, "compression.cpu_preset"),
        "compression.cpu_crf": get_setting(db, "compression.cpu_crf"),
        "compression.max_parallel": get_setting(db, "compression.max_parallel"),
    }

    drive_payload: dict[str, Any] = {"enabled": False}
    if as_bool(payload.get("upload", True), True) and as_bool(
        get_setting(db, "google.upload_enabled"), True
    ):
        from . import drive as drive_service

        service_account_json = get_setting(db, "google.service_account_json")
        parent_id = drive_service.resolve_folder_id(
            db, job.drive_folder_key, payload.get("campus")
        )
        if service_account_json and parent_id:
            drive_payload = {
                "enabled": True,
                "parent_id": parent_id,
                "service_account_json": service_account_json,
                "impersonate_email": get_setting(db, "google.impersonate_email"),
                "folder_id": job.drive_folder_id if retry_keys else None,
                "folder_link": job.drive_link if retry_keys else None,
            }

    direct_download = bool(
        job.agent
        and job.agent.public_url
        and as_bool(get_setting(db, "agents.direct_download"), True)
    )
    return {
        "job_id": job.id,
        "ref": job.ref,
        "folder_name": job.folder_name,
        "cancel_requested": bool(job.cancel_requested),
        "request": payload,
        "cameras": cameras,
        "settings": settings,
        "drive": drive_payload,
        # New agents keep finished files under /agent-files and answer signed
        # browser downloads themselves.  Older agents ignore this block and
        # fall back to the upload endpoint exactly as before.
        "download": {
            "direct": direct_download,
            "keep_days": max(1, as_int(get_setting(db, "agents.keep_files_days"), 7)),
        },
    }


# ---------------------------------------------------------------------------
# Agent-facing queue
# ---------------------------------------------------------------------------


def claim_next_job(db: Session, agent: models.Agent) -> models.Job | None:
    query = db.query(models.Job).filter(
        models.Job.agent_id == agent.id,
        models.Job.status == "dispatched",
    )
    job = query.order_by(models.Job.created_at).first()
    if not job:
        return None
    job.status = "running"
    job.started_at = _now()
    job.progress = max(1, job.progress or 0)
    job.message = f"Running on agent {agent.name}"
    agent.last_job_at = _now()
    agent.status = "busy"
    db.commit()
    return job


def _notify_finished(db: Session, job: models.Job, text: str) -> None:
    from . import telegram

    if not job.telegram_chat_id:
        return
    try:
        telegram.send_message(db, text)
    except Exception:
        pass


def update_progress(
    db: Session,
    agent: models.Agent,
    job: models.Job,
    data: dict[str, Any],
) -> models.Job:
    # A cancelled job is final. A worker that was still mid-flight (or came
    # back after a restart) must not resurrect it with a late "completed".
    if job.status == "cancelled":
        agent.last_heartbeat = _now()
        agent.status = "online"
        db.commit()
        return job

    status = str(data.get("status") or "running").strip().lower()
    if status not in {"running", *TERMINAL_STATUSES}:
        status = "running"
    retry_keys = _retry_camera_keys(job)
    job.status = status
    job.progress = max(0, min(100, int(data.get("progress") or 0)))
    if data.get("message"):
        job.message = str(data["message"])[:590]
    if data.get("error"):
        job.error = str(data["error"])[:4000]
    if data.get("drive_link"):
        job.drive_link = str(data["drive_link"])
    if data.get("drive_folder_id"):
        job.drive_folder_id = str(data["drive_folder_id"])
    if data.get("result") is not None:
        if retry_keys:
            from .jobs import merge_retry_result

            previous = json.loads(job.result_json) if job.result_json else {}
            job.result_json = json.dumps(
                merge_retry_result(previous, data["result"], list(retry_keys))
            )
        else:
            job.result_json = json.dumps(data["result"])
        if status in {"completed", "partial", "failed"}:
            merged = json.loads(job.result_json or "{}")
            cameras = merged.get("cameras") or []
            failed = any(row.get("status") == "failed" for row in cameras)
            recovered = any(row.get("status") == "success" for row in cameras) or bool(
                merged.get("files")
            )
            if failed or merged.get("errors"):
                # A failed retry must not discard cameras that already
                # succeeded in the original run.
                status = "partial" if status == "failed" and recovered else (
                    "failed" if status == "failed" else "partial"
                )
            else:
                status = "completed"
            job.status = status
    if status in TERMINAL_STATUSES:
        job.progress = 100 if status in {"completed", "partial"} else job.progress
        job.finished_at = _now()
        if status in {"completed", "partial"}:
            job.error = job.error if status == "partial" else None
        job.retry_json = None
    agent.last_heartbeat = _now()
    agent.last_job_at = _now()
    agent.status = "busy" if status == "running" else "online"
    db.commit()

    if status in {"completed", "partial", "failed", "cancelled"}:
        level = {
            "completed": "success",
            "partial": "warning",
            "failed": "error",
            "cancelled": "warning",
        }[status]
        audit(
            db,
            job.user,
            f"agent_job_{status}",
            f"{job.ref} · {job.folder_name} · {agent.name}",
            level=level,
        )

    payload = json.loads(job.request_json or "{}")
    if status in {"completed", "partial"} and payload.get("notify_telegram"):
        lines = [f"✅ <b>Footage ready on {agent.campus.upper()} agent</b> · {job.folder_name}"]
        if job.drive_link:
            lines.append(f'🔗 <a href="{job.drive_link}">Open Google Drive folder</a>')
        _notify_finished(db, job, "\n".join(lines))
    elif status == "failed" and payload.get("notify_telegram"):
        _notify_finished(
            db,
            job,
            f"❌ <b>Agent job failed</b> · {job.folder_name}\n{job.message}",
        )
    return job


def recover_stalled_jobs(db: Session) -> int:
    """Re-queue or fail jobs that an agent never claimed or stopped reporting."""
    timeout = max(1, as_int(get_setting(db, "agents.claim_timeout_minutes"), 10))
    cutoff = _now() - dt.timedelta(minutes=timeout)
    fallback = as_bool(get_setting(db, "agents.fallback_local"), True)
    stalled = (
        db.query(models.Job)
        .filter(models.Job.status == "dispatched")
        .all()
    )
    recovered = 0
    for job in stalled:
        dispatched = _aware(job.dispatched_at) or _aware(job.created_at)
        if dispatched and dispatched > cutoff:
            continue
        if fallback:
            job.agent_id = None
            job.status = "queued"
            job.progress = 0
            job.message = "No agent claimed the job; running on the main host"
            db.commit()
            from .jobs import start_job

            start_job(job.id)
        else:
            job.status = "failed"
            job.error = "No campus agent claimed the job before the timeout"
            job.message = job.error
            job.finished_at = _now()
            db.commit()
        recovered += 1

    # A running agent job whose worker disappeared (container restart, power
    # loss, crash) never sends a terminal update. Expire it so it does not stay
    # "running" forever and cannot block the UI.
    job_timeout = max(1, as_int(get_setting(db, "agents.job_timeout_minutes"), 180))
    running_cutoff = _now() - dt.timedelta(minutes=job_timeout)
    running = (
        db.query(models.Job)
        .filter(models.Job.status == "running", models.Job.agent_id.isnot(None))
        .all()
    )
    for job in running:
        started = _aware(job.started_at) or _aware(job.created_at)
        if started and started > running_cutoff:
            continue
        if job.cancel_requested:
            job.status = "cancelled"
            job.message = "Cancelled; agent stopped reporting"
        else:
            job.status = "failed"
            job.error = f"Agent stopped reporting for more than {job_timeout} minutes"
            job.message = job.error
        job.finished_at = _now()
        db.commit()
        recovered += 1
    return recovered


def agent_hostname() -> str:
    try:
        return socket.gethostname()[:160]
    except Exception:
        return "agent"
