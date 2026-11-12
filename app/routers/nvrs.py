from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import audit, decrypt_secret, encrypt_secret, get_current_user, require_roles
from ..services.nvr import NVRClient, apply_camera_address, nvr_public_dict
from ..services.health import HealthChecker

router = APIRouter(prefix="/api", tags=["nvrs"])
ADMIN = require_roles("superadmin", "admin")
OPERATOR = require_roles("superadmin", "admin", "operator")


def camera_dict(camera: models.Camera) -> dict[str, Any]:
    nvr = camera.nvr
    return {
        "id": camera.id,
        "nvr_id": camera.nvr_id,
        "nvr_name": nvr.name if nvr else None,
        "nvr_code": nvr.code if nvr else None,
        "campus": nvr.campus if nvr else None,
        "zone": nvr.zone if nvr else None,
        "channel": camera.channel,
        "name": camera.name,
        "ip_address": camera.ip_address,
        "mac_address": camera.mac_address,
        "enabled": camera.enabled,
        "online": camera.online,
        "last_seen": camera.last_seen.isoformat() if camera.last_seen else None,
        "snapshot_url": f"/api/cameras/{camera.id}/snapshot",
        "live_url": f"/api/cameras/{camera.id}/live.mjpg",
    }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (value or "").lower()).strip("_")[:70] or "nvr"


def _tree(db: Session) -> dict[str, Any]:
    nvrs = db.query(models.NVR).order_by(models.NVR.campus, models.NVR.zone, models.NVR.name).all()
    campuses: dict[str, dict[str, Any]] = {}
    nvr_rows: list[dict[str, Any]] = []
    for nvr in nvrs:
        row = nvr_public_dict(nvr)
        row["cameras"] = [camera_dict(camera) for camera in nvr.cameras if camera.enabled]
        nvr_rows.append(row)
        campus = campuses.setdefault(
            nvr.campus or "general",
            {"key": nvr.campus or "general", "name": (nvr.campus or "general").upper(), "zones": {}},
        )
        zone = campus["zones"].setdefault(
            nvr.zone or "general",
            {"key": nvr.zone or "general", "name": (nvr.zone or "general").replace("_", " ").title(), "nvrs": []},
        )
        zone["nvrs"].append(
            {
                "id": nvr.id,
                "name": nvr.name,
                "code": nvr.code,
                "status": nvr.status,
                "cameras": row["cameras"],
            }
        )
    return {
        "campuses": [
            {**campus, "zones": list(campus["zones"].values())} for campus in campuses.values()
        ],
        "nvrs": nvr_rows,
    }


@router.get("/tree")
def get_tree(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _tree(db)


@router.get("/nvrs")
def list_nvrs(
    campus: str | None = None,
    zone: str | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(models.NVR).order_by(models.NVR.campus, models.NVR.zone, models.NVR.name)
    if campus:
        query = query.filter(models.NVR.campus == campus)
    if zone:
        query = query.filter(models.NVR.zone == zone)
    rows = query.all()
    return {"nvrs": [{**nvr_public_dict(nvr), "cameras": [camera_dict(c) for c in nvr.cameras]} for nvr in rows]}


@router.post("/nvrs")
def create_nvr(
    payload: schemas.NVRInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    code = _slug(payload.code or payload.code or payload.name)
    if db.query(models.NVR).filter(models.NVR.code == code).first():
        raise HTTPException(status_code=409, detail="NVR code already exists")
    if not payload.host.strip():
        raise HTTPException(status_code=400, detail="Host / IP is required")
    nvr = models.NVR(
        name=payload.name.strip(),
        code=code,
        campus=(payload.campus or "tk").lower().strip(),
        zone=_slug(payload.zone or "general"),
        protocol=payload.protocol,
        host=payload.host.strip(),
        port=payload.port,
        sdk_port=payload.sdk_port,
        username=payload.username.strip() or "admin",
        password_enc=encrypt_secret(payload.password or ""),
        channel_offset=payload.channel_offset,
        track_multiplier=payload.track_multiplier,
        time_offset_hours=payload.time_offset_hours,
        enabled=payload.enabled,
        auto_discover=payload.auto_discover,
    )
    db.add(nvr)
    db.commit()
    db.refresh(nvr)
    audit(db, user, "nvr_created", nvr.code, request.client.host if request.client else None)
    return {"nvr": nvr_public_dict(nvr)}


@router.patch("/nvrs/{nvr_id}")
def update_nvr(
    nvr_id: int,
    payload: schemas.NVRInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    if payload.code and _slug(payload.code) != nvr.code:
        new_code = _slug(payload.code)
        if db.query(models.NVR).filter(models.NVR.code == new_code, models.NVR.id != nvr.id).first():
            raise HTTPException(status_code=409, detail="NVR code already exists")
        nvr.code = new_code
    nvr.name = payload.name.strip() or nvr.name
    nvr.campus = (payload.campus or nvr.campus).lower().strip()
    nvr.zone = _slug(payload.zone or nvr.zone)
    nvr.protocol = payload.protocol
    nvr.host = payload.host.strip() or nvr.host
    nvr.port = payload.port
    nvr.sdk_port = payload.sdk_port
    nvr.username = payload.username.strip() or nvr.username
    if payload.password and payload.password != "********":
        nvr.password_enc = encrypt_secret(payload.password)
    nvr.channel_offset = payload.channel_offset
    nvr.track_multiplier = payload.track_multiplier
    nvr.time_offset_hours = payload.time_offset_hours
    nvr.enabled = payload.enabled
    nvr.auto_discover = payload.auto_discover
    db.commit()
    db.refresh(nvr)
    audit(db, user, "nvr_updated", nvr.code, request.client.host if request.client else None)
    return {"nvr": nvr_public_dict(nvr)}


@router.delete("/nvrs/{nvr_id}")
def delete_nvr(
    nvr_id: int,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    code = nvr.code
    db.delete(nvr)
    db.commit()
    audit(db, user, "nvr_deleted", code, request.client.host if request.client else None)
    return {"ok": True}


@router.post("/nvrs/{nvr_id}/test")
def test_nvr(
    nvr_id: int,
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    ok, message = NVRClient(nvr).test()
    nvr.status = "online" if ok else "offline"
    if ok:
        import datetime as dt

        nvr.last_seen = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return {"ok": ok, "message": message, "status": nvr.status}


def _upsert_cameras(db: Session, nvr: models.NVR, discovered: list[dict[str, Any]]) -> list[models.Camera]:
    existing = {camera.channel: camera for camera in nvr.cameras}
    for item in discovered:
        channel = int(item["channel"])
        camera = existing.get(channel)
        if camera is None:
            camera = models.Camera(nvr_id=nvr.id, channel=channel, name=item.get("name") or f"Channel {channel}")
            db.add(camera)
            existing[channel] = camera
        elif item.get("name") and (not camera.name or camera.name.startswith("Channel ")):
            camera.name = item["name"]
        apply_camera_address(camera, item)
        if item.get("online") is not None and camera.enabled:
            camera.online = bool(item["online"])
    db.commit()
    return list(existing.values())


def _fill_macs(db: Session, client: NVRClient, cameras: list[models.Camera], force: bool = False) -> None:
    try:
        client.fill_missing_macs([camera for camera in cameras if camera.enabled], force=force)
        db.commit()
    except Exception as exc:  # MAC lookup is best effort only
        db.rollback()
        print(f"[nvrs] MAC lookup failed: {exc}")


@router.post("/nvrs/{nvr_id}/discover")
def discover_nvr(
    nvr_id: int,
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    client = NVRClient(nvr)
    cameras = client.discover_cameras()
    if not cameras:
        ok, message = client.test()
        return {"ok": False, "message": f"No cameras returned by ISAPI. {message}", "cameras": []}
    rows = _upsert_cameras(db, nvr, cameras)
    # A manual discover retries cameras whose earlier MAC lookup failed.
    _fill_macs(db, client, rows, force=True)
    nvr.status = "online"
    db.commit()
    db.refresh(nvr)
    with_ip = sum(1 for camera in rows if camera.ip_address)
    with_mac = sum(1 for camera in rows if camera.mac_address)
    detail = f" · {with_ip} IP / {with_mac} MAC" if with_ip else ""
    return {
        "ok": True,
        "message": f"Discovered {len(cameras)} camera(s){detail}",
        "cameras": [camera_dict(camera) for camera in nvr.cameras],
    }


@router.post("/nvrs/discover-all")
def discover_all(
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    rows = db.query(models.NVR).filter(models.NVR.enabled.is_(True)).all()
    summary = []
    for nvr in rows:
        try:
            client = NVRClient(nvr)
            cameras = client.discover_cameras()
            if cameras:
                _fill_macs(db, client, _upsert_cameras(db, nvr, cameras))
                nvr.status = "online"
                summary.append({"nvr": nvr.name, "ok": True, "cameras": len(cameras)})
            else:
                summary.append({"nvr": nvr.name, "ok": False, "cameras": 0, "message": "No cameras"})
        except Exception as exc:
            summary.append({"nvr": nvr.name, "ok": False, "cameras": 0, "message": str(exc)})
    db.commit()
    return {"results": summary}


@router.get("/nvrs/{nvr_id}/health")
def nvr_health(
    nvr_id: int,
    user: models.User = Depends(OPERATOR),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    checker = HealthChecker(db)
    result = checker.check_nvr(nvr)
    db.commit()
    db.refresh(nvr)
    health = result["health"]
    return {
        "ok": result["ok"],
        "nvr": nvr_public_dict(nvr),
        "storage": health.storage,
        "cameras": [camera_dict(camera) for camera in nvr.cameras],
        "summary": result["summary"],
        "message": health.message,
    }


@router.get("/cameras")
def list_cameras(
    campus: str | None = None,
    zone: str | None = None,
    nvr_id: int | None = None,
    q: str | None = None,
    online: bool | None = None,
    enabled: bool | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(models.Camera).join(models.NVR)
    if campus:
        query = query.filter(models.NVR.campus == campus.lower())
    if zone:
        query = query.filter(models.NVR.zone == zone)
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
    rows = query.order_by(models.NVR.campus, models.NVR.name, models.Camera.channel).all()
    return {
        "total": len(rows),
        "cameras": [camera_dict(camera) for camera in rows],
    }


@router.get("/cameras/{camera_id}")
def get_camera(
    camera_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    camera = db.get(models.Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"camera": camera_dict(camera)}


@router.post("/nvrs/{nvr_id}/cameras")
def add_camera(
    nvr_id: int,
    payload: schemas.CameraCreate,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    nvr = db.get(models.NVR, nvr_id)
    if not nvr:
        raise HTTPException(status_code=404, detail="NVR not found")
    existing = (
        db.query(models.Camera)
        .filter(models.Camera.nvr_id == nvr.id, models.Camera.channel == payload.channel)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="That channel already exists")
    camera = models.Camera(
        nvr_id=nvr.id,
        channel=payload.channel,
        name=payload.name.strip(),
        ip_address=(payload.ip_address or "").strip() or None,
        mac_address=(payload.mac_address or "").strip() or None,
    )
    db.add(camera)
    db.commit()
    db.refresh(camera)
    return {"camera": camera_dict(camera)}


@router.patch("/cameras/{camera_id}")
def update_camera(
    camera_id: int,
    payload: schemas.CameraUpdate,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    camera = db.get(models.Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    if payload.name is not None:
        camera.name = payload.name.strip()
    if payload.enabled is not None:
        camera.enabled = payload.enabled
    if payload.ip_address is not None:
        camera.ip_address = payload.ip_address.strip() or None
    if payload.mac_address is not None:
        camera.mac_address = payload.mac_address.strip() or None
    if not camera.enabled:
        # A disabled port is ignored by discovery, health and all download
        # pickers. Clear its last known state and close stale offline alerts.
        import datetime as dt

        camera.online = None
        camera.last_seen = None
        (
            db.query(models.Alert)
            .filter(
                models.Alert.camera_id == camera.id,
                models.Alert.type == "camera_offline",
                models.Alert.resolved.is_(False),
            )
            .update(
                {
                    "resolved": True,
                    "resolved_at": dt.datetime.now(dt.timezone.utc),
                    "resolve_note": "Camera disabled",
                },
                synchronize_session=False,
            )
        )
    db.commit()
    db.refresh(camera)
    return {"camera": camera_dict(camera)}


@router.delete("/cameras/{camera_id}")
def delete_camera(
    camera_id: int,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    camera = db.get(models.Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    db.delete(camera)
    db.commit()
    return {"ok": True}


@router.get("/cameras/{camera_id}/snapshot")
def camera_snapshot(
    camera_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    camera = db.get(models.Camera, camera_id)
    if not camera or not camera.nvr:
        raise HTTPException(status_code=404, detail="Camera not found")
    image = NVRClient(camera.nvr, timeout=5).snapshot(camera.channel)
    if not image:
        raise HTTPException(status_code=404, detail="Snapshot not available")
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/cameras/{camera_id}/live.mjpg")
def camera_live(
    camera_id: int,
    frames: int = Query(0, ge=0, le=1000, description="Optional frame limit (0 = continuous)"),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """MJPEG live view made from the NVR's ISAPI snapshot stream.

    Hikvision/TruVision expose a JPEG per channel over ISAPI. Repainting those
    frames is far more portable than requiring HLS/WebRTC on every campus
    network, and works in every browser with a plain ``<img>`` tag.
    """
    camera = db.get(models.Camera, camera_id)
    if not camera or not camera.nvr:
        raise HTTPException(status_code=404, detail="Camera not found")
    channel = int(camera.channel)
    client = NVRClient(camera.nvr, timeout=4)
    boundary = "cctvframe"

    def generate():
        sent = 0
        misses = 0
        while frames <= 0 or sent < frames:
            image = client.snapshot(channel, timeout=4)
            if image:
                misses = 0
                sent += 1
                yield (
                    b"--" + boundary.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(image)).encode() + b"\r\n\r\n"
                    + image + b"\r\n"
                )
            else:
                misses += 1
                # Give a briefly busy camera a couple of chances, then end the
                # stream so the browser can show an error instead of hanging.
                if misses >= 2:
                    break
            time.sleep(0.35)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
