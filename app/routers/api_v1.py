"""Read-only REST API v1.

Authenticate with either an ``X-API-Key`` header (generated in Settings) or a
normal WebUI session cookie / bearer token.  The API is intentionally
read-only: cameras, NVRs and health data only.  Mutations stay in the WebUI,
where role checks and audit logging apply.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..security import read_session_token
from ..services.health import health_summary
from ..settings_service import as_bool, get_setting

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


def _session_user(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get("cctv_session")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    data = read_session_token(token)
    if not data or not data.get("uid"):
        return None
    user = db.get(models.User, int(data["uid"]))
    return user if user and user.is_active else None


def api_access(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not as_bool(get_setting(db, "api.enabled"), True):
        raise HTTPException(status_code=403, detail="The camera API is disabled")
    supplied = (
        request.headers.get("x-api-key")
        or request.headers.get("x-api-token")
        or ""
    ).strip()
    stored = get_setting(db, "api.key").strip()
    if supplied and stored and hmac.compare_digest(supplied, stored):
        return {"kind": "api_key"}
    user = _session_user(request, db)
    if user:
        return {"kind": "session", "user": user.username, "role": user.role}
    raise HTTPException(
        status_code=401,
        detail="Provide a valid X-API-Key header or sign in to the WebUI",
    )


def _camera(camera: models.Camera) -> dict[str, Any]:
    nvr = camera.nvr
    return {
        "id": camera.id,
        "channel": camera.channel,
        "name": camera.name,
        "ip_address": camera.ip_address,
        "mac_address": camera.mac_address,
        "enabled": camera.enabled,
        "online": camera.online,
        "last_seen": camera.last_seen.isoformat() if camera.last_seen else None,
        "rtsp_path": camera.rtsp_path,
        "nvr": {
            "id": nvr.id if nvr else None,
            "name": nvr.name if nvr else None,
            "code": nvr.code if nvr else None,
            "campus": nvr.campus if nvr else None,
            "zone": nvr.zone if nvr else None,
            "host": nvr.host if nvr else None,
            "status": nvr.status if nvr else None,
        },
    }


@router.get("/cameras")
def cameras(
    campus: str | None = Query(default=None),
    zone: str | None = Query(default=None),
    nvr_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="Search name, IP, MAC or NVR"),
    online: bool | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _auth: dict = Depends(api_access),
    db: Session = Depends(get_db),
):
    query = db.query(models.Camera).join(models.NVR)
    if campus:
        query = query.filter(models.NVR.campus == campus.strip().lower())
    if zone:
        query = query.filter(models.NVR.zone == zone.strip().lower())
    if nvr_id:
        query = query.filter(models.Camera.nvr_id == nvr_id)
    if online is not None:
        query = query.filter(models.Camera.online.is_(online))
    if enabled is not None:
        query = query.filter(models.Camera.enabled.is_(enabled))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            models.Camera.name.ilike(like)
            | models.Camera.ip_address.ilike(like)
            | models.Camera.mac_address.ilike(like)
            | models.NVR.name.ilike(like)
        )
    total = query.count()
    rows = (
        query.order_by(models.NVR.campus, models.NVR.name, models.Camera.channel)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "count": len(rows),
        "offset": offset,
        "limit": limit,
        "cameras": [_camera(camera) for camera in rows],
    }


@router.get("/cameras/summary")
def camera_summary(
    _auth: dict = Depends(api_access),
    db: Session = Depends(get_db),
):
    total = (
        db.query(func.count(models.Camera.id))
        .filter(models.Camera.enabled.is_(True))
        .scalar()
        or 0
    )
    online = (
        db.query(func.count(models.Camera.id))
        .filter(models.Camera.enabled.is_(True), models.Camera.online.is_(True))
        .scalar()
        or 0
    )
    offline = (
        db.query(func.count(models.Camera.id))
        .filter(models.Camera.enabled.is_(True), models.Camera.online.is_(False))
        .scalar()
        or 0
    )
    by_campus = [
        {"campus": campus or "unassigned", "cameras": count}
        for campus, count in (
            db.query(models.NVR.campus, func.count(models.Camera.id))
            .join(models.Camera, models.Camera.nvr_id == models.NVR.id)
            .filter(models.Camera.enabled.is_(True))
            .group_by(models.NVR.campus)
            .order_by(models.NVR.campus)
            .all()
        )
    ]
    return {
        "total_cameras": total,
        "online": online,
        "offline": offline,
        "unknown": max(0, total - online - offline),
        "nvr_total": db.query(func.count(models.NVR.id)).scalar() or 0,
        "by_campus": by_campus,
    }


@router.get("/cameras/{camera_id}")
def camera_detail(
    camera_id: int,
    _auth: dict = Depends(api_access),
    db: Session = Depends(get_db),
):
    camera = db.get(models.Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"camera": _camera(camera)}


@router.get("/nvrs")
def nvrs(
    campus: str | None = Query(default=None),
    _auth: dict = Depends(api_access),
    db: Session = Depends(get_db),
):
    query = db.query(models.NVR)
    if campus:
        query = query.filter(models.NVR.campus == campus.strip().lower())
    rows = query.order_by(models.NVR.campus, models.NVR.name).all()
    return {
        "total": len(rows),
        "nvrs": [
            {
                "id": nvr.id,
                "name": nvr.name,
                "code": nvr.code,
                "campus": nvr.campus,
                "zone": nvr.zone,
                "protocol": nvr.protocol,
                "host": nvr.host,
                "port": nvr.port,
                "enabled": nvr.enabled,
                "status": nvr.status,
                "hdd_used_percent": nvr.hdd_used_percent,
                "last_seen": nvr.last_seen.isoformat() if nvr.last_seen else None,
                "cameras": len(nvr.cameras),
            }
            for nvr in rows
        ],
    }


@router.get("/health")
def health(
    _auth: dict = Depends(api_access),
    db: Session = Depends(get_db),
):
    return {"summary": health_summary(db)}
