from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import audit, get_current_user, is_admin, require_roles
from ..services import jobs
from ..services.jobs import _serialize_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
OPERATOR = require_roles("superadmin", "admin", "operator")


def _job_row(job: models.Job) -> dict:
    row = _serialize_job(job)
    row["username"] = job.user.username if job.user else "telegram"
    return row


def _can_access(user: models.User, job: models.Job) -> bool:
    return is_admin(user) or job.user_id == user.id


@router.get("")
def list_jobs(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(models.Job).order_by(models.Job.created_at.desc())
    if status:
        query = query.filter(models.Job.status == status)
    if not is_admin(user):
        query = query.filter(models.Job.user_id == user.id)
    rows = query.limit(limit).all()
    return {"jobs": [_job_row(row) for row in rows]}


@router.post("")
def create_job(
    payload: schemas.JobCreate,
    request: Request,
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    data = payload.model_dump()
    emails = list(data.get("notify_emails") or [])
    if data.get("requester_email"):
        for email in str(data["requester_email"]).replace(";", ",").split(","):
            email = email.strip()
            if email and email not in emails:
                emails.append(email)
    data["notify_emails"] = emails
    if not data.get("drive_folder_key"):
        folder = (
            db.query(models.DriveFolder)
            .filter(models.DriveFolder.campus == (data.get("campus") or "").lower())
            .first()
        )
        if folder:
            data["drive_folder_key"] = folder.key
    try:
        job = jobs.create_job(db, data, user=user, source="web")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        db,
        user,
        "job_created",
        f"{job.ref} {job.folder_name}",
        request.client.host if request.client else None,
    )
    return {"job": _job_row(job)}


@router.get("/{job_id}")
def get_job(
    job_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access(user, job):
        raise HTTPException(status_code=403, detail="You cannot view this job")
    return {"job": _job_row(job)}


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access(user, job):
        raise HTTPException(status_code=403, detail="You cannot cancel this job")
    if job.status == "cancelled":
        # Cancelling twice (double click, stale modal) is harmless.
        return {"ok": True, "job": _job_row(job)}
    if job.status not in {"queued", "dispatched", "running"}:
        raise HTTPException(status_code=400, detail="This job is no longer running")
    jobs.request_cancel(db, job_id)
    audit(db, user, "job_cancelled", job.ref)
    return {"ok": True, "job": _job_row(db.get(models.Job, job_id))}


@router.post("/{job_id}/retry")
def retry_job(
    job_id: int,
    payload: schemas.JobRetryInput | None = None,
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    """Retry only the failed cameras, keeping successful downloads intact."""
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access(user, job):
        raise HTTPException(status_code=403, detail="You cannot retry this job")
    if job.status in {"queued", "dispatched", "running"}:
        raise HTTPException(status_code=400, detail="This job is still running")

    result = json.loads(job.result_json) if job.result_json else {}
    failed_rows = [
        row for row in (result.get("cameras") or []) if row.get("status") == "failed"
    ]
    failed_keys = {
        (int(row.get("nvr_id") or 0), int(row.get("channel") or 0))
        for row in failed_rows
    }
    failed_keys.discard((0, 0))

    requested: list[tuple[int, int]] = []
    for camera in (payload.cameras if payload else []):
        key = (int(camera.nvr_id), int(camera.channel))
        if key not in failed_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Camera CH{camera.channel} did not fail in this job",
            )
        if key not in requested:
            requested.append(key)
    if not requested:
        requested = sorted(failed_keys)

    if not requested:
        # Older jobs only stored error strings. Fall back to the original
        # camera list, but only for a job that actually finished with errors.
        if job.status not in {"failed", "partial"}:
            raise HTTPException(status_code=400, detail="No failed cameras to retry")
        request_data = json.loads(job.request_json or "{}")
        requested = [
            (int(item.get("nvr_id") or 0), int(item.get("channel") or 0))
            for item in (request_data.get("cameras") or [])
        ]
        requested = [key for key in requested if key != (0, 0)]
    if not requested:
        raise HTTPException(status_code=400, detail="No failed cameras to retry")

    job.retry_json = json.dumps(
        [{"nvr_id": nvr_id, "channel": channel} for nvr_id, channel in requested]
    )
    job.status = "queued"
    job.progress = 0
    job.message = f"Queued to retry {len(requested)} camera(s)"
    job.error = None
    job.finished_at = None
    job.started_at = None
    job.cancel_requested = False
    db.commit()

    from ..services import agents as agent_service

    agent = job.agent if job.agent and job.agent.enabled else None
    if agent is not None:
        agent_service.dispatch_job(db, job, agent)
    else:
        if job.agent_id is not None:
            job.agent_id = None
            db.commit()
        jobs.start_job(job.id)

    audit(db, user, "job_retry", f"{job.ref} · {len(requested)} camera(s)")
    return {"ok": True, "job": _job_row(db.get(models.Job, job_id))}


@router.delete("/{job_id}")
def delete_job(
    job_id: int,
    user: models.User = Depends(require_roles("superadmin", "admin")),
    db: Session = Depends(get_db),
):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in {"queued", "dispatched", "running"}:
        raise HTTPException(status_code=400, detail="Cancel the job before deleting it")
    for job_file in job.files:
        if job_file.local_path and os.path.exists(job_file.local_path):
            try:
                os.remove(job_file.local_path)
            except OSError:
                pass
    ref = job.ref
    db.delete(job)
    db.commit()
    audit(db, user, "job_deleted", ref)
    return {"ok": True}


@router.get("/{job_id}/files/{file_id}")
def download_job_file(
    job_id: int,
    file_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(models.Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _can_access(user, job):
        raise HTTPException(status_code=403, detail="You cannot download this file")
    job_file = db.get(models.JobFile, file_id)
    if not job_file or job_file.job_id != job.id:
        raise HTTPException(status_code=404, detail="File not found")
    if not job_file.local_path or not os.path.exists(job_file.local_path):
        raise HTTPException(status_code=410, detail="The local file has expired (Drive link may still work)")
    return FileResponse(
        job_file.local_path,
        media_type="video/mp4",
        filename=job_file.filename,
    )
