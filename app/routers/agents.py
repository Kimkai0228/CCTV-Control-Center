"""Admin agent management plus the token-authenticated agent API."""

from __future__ import annotations

import json
import os
import re
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import WORK_DIR
from ..database import get_db
from ..security import audit, get_current_user, require_roles
from ..services import agents as agent_service
from ..services.downloader import safe_filename
from ..services.jobs import _serialize_job
from ..settings_service import as_bool, as_int, get_setting

router = APIRouter(prefix="/api", tags=["agents"])
ADMIN = require_roles("superadmin", "admin")


def _agent_auth(request: Request, db: Session = Depends(get_db)) -> models.Agent:
    token = (
        request.headers.get("x-agent-token")
        or request.headers.get("x-agent-key")
        or ""
    ).strip()
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    agent = agent_service.find_by_token(db, token)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return agent


def _normalise_trigger(value: str, fallback: str = "tk") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", value or "").lower()[:32]
    return cleaned or fallback


def _normalise_public_url(value: str | None) -> str | None:
    url = (value or "").strip().rstrip("/")
    if not url:
        return None
    if not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="Agent public URL must start with http:// or https://",
        )
    if len(url) > 400:
        raise HTTPException(status_code=400, detail="Agent public URL is too long")
    return url


@router.get("/agents")
def list_agents(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(models.Agent).order_by(models.Agent.campus, models.Agent.name).all()
    stats = {
        status: count
        for status, count in (
            db.query(models.Agent.status, func.count(models.Agent.id))
            .group_by(models.Agent.status)
            .all()
        )
    }
    job_counts = {
        status: count
        for status, count in (
            db.query(models.Job.status, func.count(models.Job.id))
            .filter(models.Job.agent_id.isnot(None))
            .group_by(models.Job.status)
            .all()
        )
    }
    recent = (
        db.query(models.Job)
        .filter(models.Job.agent_id.isnot(None))
        .order_by(models.Job.created_at.desc())
        .limit(30)
        .all()
    )
    return {
        "agents": [agent_service.agent_dict(row) for row in rows],
        "stats": {
            "total": len(rows),
            "online": stats.get("online", 0) + stats.get("busy", 0),
            "busy": stats.get("busy", 0),
            "offline": stats.get("offline", 0)
            + stats.get("unknown", 0)
            + max(0, len(rows) - sum(stats.values())),
            "jobs": job_counts,
        },
        "settings": {
            "enabled": as_bool(get_setting(db, "agents.enabled"), True),
            "strict_campus": as_bool(get_setting(db, "agents.strict_campus"), False),
            "fallback_local": as_bool(get_setting(db, "agents.fallback_local"), True),
            "claim_timeout_minutes": int(get_setting(db, "agents.claim_timeout_minutes") or 10),
            "local_campus": get_setting(db, "app.campus") or "tk",
        },
        "jobs": [_serialize_job(job) for job in recent],
    }


@router.post("/agents")
def create_agent(
    payload: schemas.AgentInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    existing = (
        db.query(models.Agent)
        .filter(models.Agent.trigger_word == _normalise_trigger(payload.trigger_word))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Trigger word /{existing.trigger_word} is already used by {existing.name}",
        )
    if _normalise_trigger(payload.trigger_word) == (
        get_setting(db, "telegram.trigger_word") or "tk"
    ).strip().lower():
        raise HTTPException(
            status_code=400,
            detail="The agent trigger word must differ from the global Telegram trigger word",
        )
    agent, token = agent_service.create_agent(
        db,
        name=payload.name,
        campus=payload.campus,
        zones=payload.zones,
        trigger_word=_normalise_trigger(payload.trigger_word),
        public_url=_normalise_public_url(payload.public_url),
        notes=payload.notes,
    )
    agent.enabled = payload.enabled
    db.commit()
    audit(db, user, "agent_created", f"Created agent {agent.name}", request.client.host if request.client else None)
    return {"ok": True, "agent": agent_service.agent_dict(agent), "token": token}


@router.patch("/agents/{agent_id}")
def update_agent(
    agent_id: int,
    payload: schemas.AgentUpdate,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    agent = db.get(models.Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if payload.name is not None:
        agent.name = payload.name.strip()[:160] or agent.name
    if payload.campus is not None:
        agent.campus = payload.campus.strip().lower()[:40] or agent.campus
    if payload.zones is not None:
        agent.zones_json = json.dumps(agent_service._normalise_zones(payload.zones))
    if payload.trigger_word is not None:
        word = _normalise_trigger(payload.trigger_word)
        clash = (
            db.query(models.Agent)
            .filter(models.Agent.trigger_word == word, models.Agent.id != agent.id)
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=400,
                detail=f"Trigger word /{word} is already used by {clash.name}",
            )
        if word == (get_setting(db, "telegram.trigger_word") or "tk").strip().lower():
            raise HTTPException(
                status_code=400,
                detail="The agent trigger word must differ from the global Telegram trigger word",
            )
        agent.trigger_word = word
    if payload.public_url is not None:
        agent.public_url = _normalise_public_url(payload.public_url)
    if payload.enabled is not None:
        agent.enabled = payload.enabled
    if payload.notes is not None:
        agent.notes = payload.notes.strip()[:1000] or None
    db.commit()
    audit(db, user, "agent_updated", f"Updated agent {agent.name}", request.client.host if request.client else None)
    return {"ok": True, "agent": agent_service.agent_dict(agent)}


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: int,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    agent = db.get(models.Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    audit(db, user, "agent_deleted", f"Deleted agent {agent.name}", request.client.host if request.client else None)
    db.delete(agent)
    db.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/rotate-token")
def rotate_agent_token(
    agent_id: int,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    agent = db.get(models.Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    token = agent_service.rotate_token(db, agent)
    audit(db, user, "agent_token_rotated", f"Rotated token for {agent.name}", request.client.host if request.client else None)
    return {"ok": True, "agent": agent_service.agent_dict(agent), "token": token}


# ---------------------------------------------------------------------------
# Agent-facing endpoints (token auth, no session)
# ---------------------------------------------------------------------------


@router.post("/agents/heartbeat")
def agent_heartbeat(
    payload: dict,
    agent: models.Agent = Depends(_agent_auth),
    db: Session = Depends(get_db),
):
    agent_service.touch(db, agent, **payload)
    return {
        "ok": True,
        "agent": {"id": agent.id, "name": agent.name, "campus": agent.campus},
        "poll_seconds": max(5, int(get_setting(db, "agents.poll_seconds") or 20)),
        # The agent uses this for its local cleanup even when no job is queued.
        "keep_files_days": max(1, as_int(get_setting(db, "agents.keep_files_days"), 7)),
        "direct_download": as_bool(get_setting(db, "agents.direct_download"), True),
    }


@router.get("/agents/jobs/next")
def agent_next_job(
    agent: models.Agent = Depends(_agent_auth),
    db: Session = Depends(get_db),
):
    job = agent_service.claim_next_job(db, agent)
    if not job:
        return {"job": None}
    return {"job": agent_service.job_payload_for_agent(db, job)}


@router.post("/agents/jobs/{job_id}/progress")
def agent_job_progress(
    job_id: int,
    payload: schemas.AgentJobProgress,
    agent: models.Agent = Depends(_agent_auth),
    db: Session = Depends(get_db),
):
    job = db.get(models.Job, job_id)
    if not job or job.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Job not assigned to this agent")
    agent_service.update_progress(db, agent, job, payload.model_dump())
    return {"ok": True, "cancel": bool(job.cancel_requested)}


def _upload_target(job: models.Job, filename: str) -> tuple[str, str]:
    work_dir = WORK_DIR / f"job_{job.id}_{safe_filename(job.ref, str(job.id))}"
    os.makedirs(work_dir, exist_ok=True)
    return str(work_dir), os.path.join(str(work_dir), filename)


def _resolve_camera_id(
    db: Session,
    job: models.Job,
    channel: int | None,
    nvr_id: str | None,
) -> int | None:
    if channel is None:
        return None
    payload = json.loads(job.request_json or "{}")
    for item in payload.get("cameras") or []:
        try:
            item_channel = int(item.get("channel"))
        except (TypeError, ValueError):
            continue
        if item_channel != int(channel):
            continue
        if nvr_id and str(nvr_id).isdigit() and int(item.get("nvr_id") or 0) != int(nvr_id):
            continue
        found = (
            db.query(models.Camera)
            .filter(
                models.Camera.nvr_id == int(item.get("nvr_id") or 0),
                models.Camera.channel == item_channel,
            )
            .first()
        )
        return found.id if found else None
    return None


def _store_job_file(
    db: Session,
    job: models.Job,
    *,
    filename: str,
    target: str,
    channel: int | None,
    nvr_id: str | None,
) -> models.JobFile:
    camera_id = _resolve_camera_id(db, job, channel, nvr_id)
    size = os.path.getsize(target) if os.path.exists(target) else 0
    job_file = (
        db.query(models.JobFile)
        .filter(models.JobFile.job_id == job.id, models.JobFile.filename == filename)
        .first()
    )
    if job_file is None:
        job_file = models.JobFile(job_id=job.id, filename=filename, status="ready")
        db.add(job_file)
    job_file.camera_id = camera_id
    job_file.channel = int(channel) if channel is not None else None
    job_file.local_path = os.path.abspath(target)
    job_file.size_bytes = size
    job_file.status = "ready"
    db.commit()
    db.refresh(job_file)
    return job_file


@router.post("/agents/jobs/{job_id}/files")
def agent_upload_file(
    job_id: int,
    file: UploadFile = File(...),
    channel: int | None = Form(None),
    camera: str | None = Form(None),
    nvr_id: str | None = Form(None),
    agent: models.Agent = Depends(_agent_auth),
    db: Session = Depends(get_db),
):
    """Copy a finished agent output onto the main host in one request.

    Kept for older agent containers. New agents use the chunked endpoint below
    so a large MP4 can be resumed instead of failing in one long request.
    """
    job = db.get(models.Job, job_id)
    if not job or job.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Job not assigned to this agent")
    if job.status == "cancelled":
        # The operator already discarded this job; accept the request so the
        # agent can finish cleanly without resurrecting any data.
        return {"ok": True, "stored": False, "message": "Job was cancelled"}

    filename = safe_filename(os.path.basename(file.filename or "footage.mp4"), "footage.mp4")
    _, target = _upload_target(job, filename)
    try:
        with open(target, "wb") as handle:
            shutil.copyfileobj(file.file, handle)
    finally:
        try:
            file.file.close()
        except Exception:
            pass
    job_file = _store_job_file(
        db, job, filename=filename, target=target, channel=channel, nvr_id=nvr_id
    )
    return {
        "ok": True,
        "stored": True,
        "file": {
            "id": job_file.id,
            "filename": job_file.filename,
            "size_bytes": job_file.size_bytes,
            "channel": job_file.channel,
            "download_url": f"/api/jobs/{job.id}/files/{job_file.id}",
        },
    }


@router.post("/agents/jobs/{job_id}/files/chunk")
def agent_upload_chunk(
    job_id: int,
    file: UploadFile = File(...),
    offset: int = Form(0),
    total: int | None = Form(None),
    final: bool = Form(False),
    channel: int | None = Form(None),
    camera: str | None = Form(None),
    nvr_id: str | None = Form(None),
    agent: models.Agent = Depends(_agent_auth),
    db: Session = Depends(get_db),
):
    """Append one resumable chunk of a finished agent file.

    The agent sends sequential chunks with ``offset``/``total`` and marks the
    last chunk ``final=true``. Re-sending a chunk at the same offset is safe,
    which lets the agent retry after a dropped connection without restarting a
    multi-hundred-megabyte upload.
    """
    job = db.get(models.Job, job_id)
    if not job or job.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Job not assigned to this agent")
    if job.status == "cancelled":
        return {"ok": True, "stored": False, "message": "Job was cancelled"}

    if offset < 0 or (total is not None and total < 0):
        raise HTTPException(status_code=400, detail="Invalid upload offset or size")
    if total is not None and offset > total:
        raise HTTPException(status_code=409, detail="Upload offset is beyond the declared size")

    filename = safe_filename(os.path.basename(file.filename or "footage.mp4"), "footage.mp4")
    _, target = _upload_target(job, filename)
    current = os.path.getsize(target) if os.path.exists(target) else 0
    if offset > current:
        raise HTTPException(
            status_code=409,
            detail=f"Upload gap: have {current} bytes but chunk starts at {offset}",
        )
    mode = "wb" if offset == 0 else "r+b"
    try:
        with open(target, mode) as handle:
            handle.seek(offset)
            shutil.copyfileobj(file.file, handle)
            handle.truncate()
    finally:
        try:
            file.file.close()
        except Exception:
            pass
    size = os.path.getsize(target)

    if final:
        if total is not None and size != total:
            raise HTTPException(
                status_code=409,
                detail=f"Incomplete upload: expected {total} bytes, stored {size}",
            )
        job_file = _store_job_file(
            db, job, filename=filename, target=target, channel=channel, nvr_id=nvr_id
        )
        return {
            "ok": True,
            "stored": True,
            "file": {
                "id": job_file.id,
                "filename": job_file.filename,
                "size_bytes": job_file.size_bytes,
                "channel": job_file.channel,
                "download_url": f"/api/jobs/{job.id}/files/{job_file.id}",
            },
        }

    return {"ok": True, "stored": False, "size_bytes": size, "offset": offset, "received": size}
