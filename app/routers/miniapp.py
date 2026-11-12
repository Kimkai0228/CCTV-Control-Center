from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import audit, read_session_token
from ..settings_service import as_bool, get_setting, public_config
from ..telegram_webapp import validate_init_data
from ..services import jobs
from ..services.jobs import _serialize_job
from .nvrs import _tree

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])


def _resolve_user(request: Request, db: Session) -> dict:
    init_data = request.headers.get("x-telegram-init-data", "")
    linked_user: models.User | None = None
    if init_data:
        if not as_bool(get_setting(db, "telegram.miniapp_enabled"), True):
            raise HTTPException(status_code=403, detail="Telegram MiniApp is disabled")
        token = get_setting(db, "telegram.bot_token")
        payload = validate_init_data(init_data, token)
        tg_user = payload.get("user") or {}
        tg_id = str(tg_user.get("id", ""))
        allowed_raw = get_setting(db, "telegram.allowed_user_ids")
        allowed = {item.strip() for item in allowed_raw.replace(";", ",").split(",") if item.strip()}
        if allowed and tg_id not in allowed:
            raise HTTPException(status_code=403, detail="Your Telegram account is not allowed")
        linked_user = (
            db.query(models.User).filter(models.User.telegram_user_id == tg_id, models.User.is_active.is_(True)).first()
        )
        return {"telegram_user": tg_user, "telegram_id": tg_id, "user": linked_user, "source": "miniapp"}

    # Allow an authenticated browser session for previewing/testing the MiniApp.
    token = request.cookies.get("cctv_session")
    if token:
        data = read_session_token(token)
        if data and data.get("uid"):
            user = db.get(models.User, int(data["uid"]))
            if user and user.is_active:
                return {"telegram_user": {}, "telegram_id": user.telegram_user_id or "", "user": user, "source": "web"}
    raise HTTPException(status_code=401, detail="Open this page from Telegram or sign in first")


def _can_access_job(identity: dict, job: models.Job) -> bool:
    """Allow the Telegram owner, the linked web user, or an admin."""
    user = identity.get("user")
    if user and user.role in {"superadmin", "admin"}:
        return True
    if job.telegram_user_id and job.telegram_user_id == identity.get("telegram_id"):
        return True
    if user and job.user_id is not None and job.user_id == user.id:
        return True
    return False


@router.get("/bootstrap")
def bootstrap(request: Request, db: Session = Depends(get_db)):
    identity = _resolve_user(request, db)
    tg_user = identity["telegram_user"]
    linked_user = identity["user"]
    telegram_id = identity["telegram_id"]
    if telegram_id:
        recent = (
            db.query(models.Job)
            .filter(models.Job.telegram_user_id == telegram_id)
            .order_by(models.Job.created_at.desc())
            .limit(10)
            .all()
        )
    elif linked_user:
        recent = (
            db.query(models.Job)
            .filter(models.Job.user_id == linked_user.id)
            .order_by(models.Job.created_at.desc())
            .limit(10)
            .all()
        )
    else:
        recent = []
    folders = (
        db.query(models.DriveFolder)
        .filter(models.DriveFolder.enabled.is_(True))
        .order_by(models.DriveFolder.key)
        .all()
    )
    return {
        "config": public_config(db),
        "identity": {
            "first_name": tg_user.get("first_name") or (linked_user.username if linked_user else "Operator"),
            "last_name": tg_user.get("last_name") or "",
            "username": tg_user.get("username") or (linked_user.username if linked_user else ""),
            "telegram_id": telegram_id,
            "role": linked_user.role if linked_user else "telegram",
        },
        "tree": _tree(db),
        "folders": [
            {"key": folder.key, "name": folder.name, "campus": folder.campus} for folder in folders
        ],
        "jobs": [_serialize_job(job) for job in recent],
        "settings": {
            "default_folder": get_setting(db, "jobs.default_folder_name"),
            "upload_enabled": as_bool(get_setting(db, "google.upload_enabled"), True),
            "compression_enabled": as_bool(get_setting(db, "compression.enabled"), True),
        },
    }


@router.post("/jobs")
def create_miniapp_job(
    payload: schemas.JobCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _resolve_user(request, db)
    data = payload.model_dump()
    emails = list(data.get("notify_emails") or [])
    if data.get("requester_email"):
        for email in str(data["requester_email"]).replace(";", ",").split(","):
            email = email.strip()
            if email and email not in emails:
                emails.append(email)
    data["notify_emails"] = emails
    try:
        job = jobs.create_job(
            db,
            data,
            user=identity["user"],
            source="miniapp",
            telegram_user_id=identity["telegram_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        db,
        identity["user"],
        "job_created",
        f"miniapp {job.ref} · {job.folder_name}",
        request.client.host if request.client else None,
        level="success",
    )
    return {"job": _serialize_job(job)}


@router.get("/jobs/{job_id}")
def get_miniapp_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _resolve_user(request, db)
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access_job(identity, job):
        raise HTTPException(status_code=403, detail="Not your job")
    return {"job": _serialize_job(job)}


@router.post("/jobs/{job_id}/cancel")
def cancel_miniapp_job(job_id: int, request: Request, db: Session = Depends(get_db)):
    identity = _resolve_user(request, db)
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access_job(identity, job):
        raise HTTPException(status_code=403, detail="Not your job")
    if job.status not in {"queued", "dispatched", "running"}:
        raise HTTPException(status_code=400, detail="Job is not running")
    jobs.request_cancel(db, job_id)
    return {"ok": True}


@router.get("/jobs/{job_id}/files/{file_id}")
def download_miniapp_file(
    job_id: int,
    file_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Download a local file from inside Telegram (initData header, no cookies)."""
    identity = _resolve_user(request, db)
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access_job(identity, job):
        raise HTTPException(status_code=403, detail="Not your job")
    job_file = db.get(models.JobFile, file_id)
    if not job_file or job_file.job_id != job.id:
        raise HTTPException(status_code=404, detail="File not found")
    if not job_file.local_path or not os.path.exists(job_file.local_path):
        raise HTTPException(status_code=410, detail="The local file has expired")
    return FileResponse(job_file.local_path, media_type="video/mp4", filename=job_file.filename)
