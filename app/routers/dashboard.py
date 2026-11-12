from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from .. import models, schemas
from ..database import get_db
from ..security import audit, get_current_user, is_admin, require_roles
from ..services.compressor import qsv_info
from ..services.dashboard_reports import (
    default_reports,
    get_reports,
    normalize_reports,
)
from ..services.health import health_summary, system_metrics
from ..services.jobs import _serialize_job
from ..settings_service import as_bool, as_int, get_setting, set_setting

router = APIRouter(prefix="/api", tags=["dashboard"])
ADMIN = require_roles("superadmin", "admin")


@router.get("/dashboard")
def dashboard(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = dt.datetime.now(dt.timezone.utc)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    summary = health_summary(db)

    job_query = db.query(models.Job)
    if not is_admin(user):
        job_query = job_query.filter(models.Job.user_id == user.id)
    recent_jobs = [_serialize_job(job) for job in job_query.order_by(models.Job.created_at.desc()).limit(8).all()]
    jobs_today = job_query.filter(models.Job.created_at >= start_today).count()
    active_jobs = job_query.filter(models.Job.status.in_(["queued", "dispatched", "running"])).count()

    alerts = (
        db.query(models.Alert)
        .filter(models.Alert.resolved.is_(False))
        .order_by(models.Alert.created_at.desc())
        .limit(8)
        .all()
    )
    nvrs = db.query(models.NVR).order_by(models.NVR.campus, models.NVR.name).all()
    latest_health = (
        db.query(func.max(models.NVR.last_checked)).scalar()
    )
    return {
        "summary": {
            **summary,
            "jobs_today": jobs_today,
            "active_jobs": active_jobs,
            "jobs_total": job_query.count(),
        },
        "recent_jobs": recent_jobs,
        "alerts": [
            {
                "id": alert.id,
                "type": alert.type,
                "severity": alert.severity,
                "message": alert.message,
                "nvr": alert.nvr.name if alert.nvr else None,
                "created_at": alert.created_at.isoformat() if alert.created_at else None,
            }
            for alert in alerts
        ],
        "nvrs": [
            {
                "id": nvr.id,
                "name": nvr.name,
                "campus": nvr.campus,
                "zone": nvr.zone,
                "status": nvr.status,
                "hdd_used_percent": nvr.hdd_used_percent,
                "camera_count": len(nvr.cameras),
                "offline_cameras": sum(1 for c in nvr.cameras if c.online is False),
                "last_checked": nvr.last_checked.isoformat() if nvr.last_checked else None,
            }
            for nvr in nvrs
        ],
        "compression": qsv_info(),
        "health": {
            "enabled": as_bool(get_setting(db, "health.enabled"), True),
            "interval_minutes": as_int(get_setting(db, "health.interval_minutes"), 10),
            "last_checked": latest_health.isoformat() if latest_health else None,
        },
        "system": system_metrics(),
        "reports": get_reports(db),
        "report_defaults": default_reports(),
        "can_customize": is_admin(user),
    }


@router.put("/dashboard/reports")
def update_dashboard_reports(
    payload: schemas.DashboardReportsInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    try:
        reports = normalize_reports([item.model_dump() for item in payload.reports])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_setting(db, "dashboard.reports", json.dumps(reports))
    audit(
        db,
        user,
        "dashboard_updated",
        f"Saved {len(reports)} report chart(s)",
        request.client.host if request.client else None,
        level="success",
    )
    # Re-read so every user sees exactly the same saved layout.
    return {"ok": True, "reports": get_reports(db), "defaults": default_reports()}


_SOURCE_LABELS = {
    "web": "Web",
    "miniapp": "MiniApp",
    "bot": "Telegram bot",
    "system": "System",
}
_STATUS_LABELS = {
    "completed": "Completed",
    "partial": "Partial",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "running": "Running",
    "queued": "Queued",
    "dispatched": "Waiting for agent",
}
_AGENT_LABELS = {"local": "Main host", "agent": "Campus agent"}


def _dimension_label(metric: str, key: str) -> str:
    value = str(key or "unknown")
    if metric == "campus":
        return value.upper()
    if metric == "source":
        return _SOURCE_LABELS.get(value.lower(), value.replace("_", " ").title())
    if metric == "status":
        return _STATUS_LABELS.get(value.lower(), value.replace("_", " ").title())
    if metric == "agent":
        return _AGENT_LABELS.get(value.lower(), value.replace("_", " ").title())
    return value


def _job_campus(job: models.Job, local_campus: str) -> str:
    if job.agent and job.agent.campus:
        return str(job.agent.campus).strip().lower() or "unknown"
    try:
        payload = json.loads(job.request_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    campus = str(payload.get("campus") or local_campus or "unknown").strip().lower()
    return campus or "unknown"


def _job_file_stats(job: models.Job) -> tuple[int, int]:
    count = 0
    size = 0
    seen: set[str] = set()
    for row in job.files:
        count += 1
        size += int(row.size_bytes or 0)
        seen.add(row.filename)
    try:
        result = json.loads(job.result_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    for entry in result.get("files") or []:
        name = entry.get("filename")
        if not name or name in seen:
            continue
        seen.add(name)
        count += 1
        size += int(entry.get("size_bytes") or 0)
    return count, size


@router.get("/dashboard/analytics")
def dashboard_analytics(
    days: int = Query(90, ge=1, le=366),
    start: str | None = Query(None),
    end: str | None = Query(None),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    now = dt.datetime.now(dt.timezone.utc)
    if start or end:
        if not start or not end:
            raise HTTPException(status_code=400, detail="Provide both start and end dates")
        try:
            start_date = dt.date.fromisoformat(start.strip())
            end_date = dt.date.fromisoformat(end.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
        if start_date > end_date:
            raise HTTPException(
                status_code=400, detail="Start date must be on or before end date"
            )
        days = (end_date - start_date).days + 1
        if days > 366:
            raise HTTPException(status_code=400, detail="Date range cannot exceed 366 days")
    else:
        days = max(1, min(366, int(days)))
        end_date = now.date()
        start_date = end_date - dt.timedelta(days=days - 1)
    start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    end_dt = dt.datetime.combine(
        end_date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc
    )

    query = (
        db.query(models.Job)
        .options(joinedload(models.Job.agent), selectinload(models.Job.files))
        .filter(models.Job.created_at >= start_dt, models.Job.created_at < end_dt)
    )
    if not is_admin(user):
        query = query.filter(models.Job.user_id == user.id)
    jobs = query.all()

    local_campus = (get_setting(db, "app.campus") or "tk").strip().lower()
    labels = [(start_date + dt.timedelta(days=index)).isoformat() for index in range(days)]
    day_index = {label: index for index, label in enumerate(labels)}
    metrics = ("source", "campus", "status", "agent")
    daily: dict[str, dict[str, list[int]]] = {
        metric: defaultdict(lambda: [0] * days) for metric in metrics
    }
    totals_by_dimension: dict[str, Counter] = {metric: Counter() for metric in metrics}
    total_series = [0] * days
    totals = {
        "requests": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "cancelled": 0,
        "files": 0,
        "bytes": 0,
    }

    for job in jobs:
        created = job.created_at.date() if job.created_at else None
        index = day_index.get(created.isoformat()) if created else None
        if index is None:
            continue

        source = str(job.source or "web").strip().lower()
        campus = _job_campus(job, local_campus)
        status = str(job.status or "unknown").strip().lower()
        agent_key = "agent" if job.agent_id else "local"
        values = {
            "source": source,
            "campus": campus,
            "status": status,
            "agent": agent_key,
        }

        total_series[index] += 1
        totals["requests"] += 1
        if status in totals:
            totals[status] += 1
        file_count, file_bytes = _job_file_stats(job)
        totals["files"] += file_count
        totals["bytes"] += file_bytes

        for metric, key in values.items():
            daily[metric][key][index] += 1
            totals_by_dimension[metric][key] += 1

    dimensions = {
        metric: [
            {
                "key": key,
                "label": _dimension_label(metric, key),
                "value": value,
            }
            for key, value in totals_by_dimension[metric].most_common()
        ]
        for metric in metrics
    }
    timeline: dict[str, object] = {"labels": labels, "total": total_series}
    for metric in metrics:
        timeline[metric] = {
            key: series for key, series in daily[metric].items()
        }

    return {
        "days": days,
        "start": labels[0] if labels else start_date.isoformat(),
        "end": labels[-1] if labels else start_date.isoformat(),
        "totals": totals,
        "dimensions": dimensions,
        "timeline": timeline,
    }


@router.get("/alerts")
def list_alerts(
    resolved: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(models.Alert).order_by(models.Alert.created_at.desc())
    if resolved is not None:
        query = query.filter(models.Alert.resolved.is_(resolved))
    rows = query.limit(limit).all()
    return {
        "alerts": [
            {
                "id": alert.id,
                "type": alert.type,
                "severity": alert.severity,
                "message": alert.message,
                "fingerprint": alert.fingerprint,
                "acknowledged": alert.acknowledged,
                "resolved": alert.resolved,
                "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
                "resolve_note": alert.resolve_note,
                "nvr": alert.nvr.name if alert.nvr else None,
                "nvr_id": alert.nvr_id,
                "created_at": alert.created_at.isoformat() if alert.created_at else None,
                "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
            }
            for alert in rows
        ]
    }


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(
    alert_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alert = db.get(models.Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.acknowledged = True
    db.commit()
    return {"ok": True}


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    payload: schemas.AlertResolveInput | None = None,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    alert = db.get(models.Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.resolved = True
    alert.resolved_at = dt.datetime.now(dt.timezone.utc)
    note = (payload.note if payload else "") or ""
    alert.resolve_note = note.strip() or None
    db.commit()
    return {"ok": True}


@router.post("/alerts/resolve-all")
def resolve_all(
    payload: schemas.AlertResolveInput | None = None,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    rows = db.query(models.Alert).filter(models.Alert.resolved.is_(False)).all()
    now = dt.datetime.now(dt.timezone.utc)
    note = ((payload.note if payload else "") or "").strip() or "Resolved all from Alerts"
    for alert in rows:
        alert.resolved = True
        alert.resolved_at = now
        alert.resolve_note = note
    db.commit()
    return {"ok": True, "resolved": len(rows)}


# ---------------------------------------------------------------------------
# Alert exceptions
# ---------------------------------------------------------------------------


def _exception_dict(item: models.AlertException, db: Session) -> dict:
    nvr = db.get(models.NVR, item.nvr_id) if item.nvr_id else None
    camera = db.get(models.Camera, item.camera_id) if item.camera_id else None
    return {
        "id": item.id,
        "alert_type": item.alert_type,
        "nvr_id": item.nvr_id,
        "nvr": nvr.name if nvr else None,
        "camera_id": item.camera_id,
        "camera": camera.name if camera else None,
        "fingerprint": item.fingerprint,
        "reason": item.reason,
        "enabled": item.enabled,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/alert-exceptions")
def list_alert_exceptions(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(models.AlertException)
        .order_by(models.AlertException.created_at.desc())
        .all()
    )
    return {"exceptions": [_exception_dict(row, db) for row in rows]}


@router.post("/alert-exceptions")
def create_alert_exception(
    payload: schemas.AlertExceptionInput,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    if not any((payload.alert_type, payload.nvr_id, payload.camera_id, payload.fingerprint)):
        raise HTTPException(
            status_code=400,
            detail="Choose at least one of alert type, NVR, camera or fingerprint",
        )
    item = models.AlertException(
        alert_type=(payload.alert_type or "").strip() or None,
        nvr_id=payload.nvr_id,
        camera_id=payload.camera_id,
        fingerprint=(payload.fingerprint or "").strip() or None,
        reason=(payload.reason or "").strip()[:300] or None,
        enabled=payload.enabled,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"ok": True, "exception": _exception_dict(item, db)}


@router.patch("/alert-exceptions/{exception_id}")
def update_alert_exception(
    exception_id: int,
    payload: schemas.AlertExceptionInput,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    item = db.get(models.AlertException, exception_id)
    if not item:
        raise HTTPException(status_code=404, detail="Exception not found")
    item.alert_type = (payload.alert_type or "").strip() or item.alert_type
    item.nvr_id = payload.nvr_id if payload.nvr_id is not None else item.nvr_id
    item.camera_id = payload.camera_id if payload.camera_id is not None else item.camera_id
    if payload.fingerprint is not None:
        item.fingerprint = payload.fingerprint.strip() or None
    if payload.reason is not None:
        item.reason = payload.reason.strip()[:300] or None
    item.enabled = payload.enabled
    db.commit()
    return {"ok": True, "exception": _exception_dict(item, db)}


@router.delete("/alert-exceptions/{exception_id}")
def delete_alert_exception(
    exception_id: int,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    item = db.get(models.AlertException, exception_id)
    if not item:
        raise HTTPException(status_code=404, detail="Exception not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.post("/alerts/{alert_id}/ignore")
def ignore_alert(
    alert_id: int,
    payload: schemas.AlertIgnoreInput | None = None,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    alert = db.get(models.Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    mode = (payload.mode if payload else "fingerprint") or "fingerprint"
    item = models.AlertException(
        reason=(payload.reason if payload else None) or f"Ignored from alert #{alert.id}",
        enabled=True,
    )
    if mode == "camera" and alert.camera_id:
        item.camera_id = alert.camera_id
        item.alert_type = alert.type
    elif mode == "nvr" and alert.nvr_id:
        item.nvr_id = alert.nvr_id
    elif mode == "type":
        item.alert_type = alert.type
    else:
        item.fingerprint = alert.fingerprint
        item.alert_type = alert.type
    db.add(item)
    alert.resolved = True
    alert.resolved_at = dt.datetime.now(dt.timezone.utc)
    alert.resolve_note = "Ignored and suppressed"
    db.commit()
    db.refresh(item)
    return {"ok": True, "exception": _exception_dict(item, db)}
