"""NVR health monitoring: HDD, offline cameras and alerting."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
import time
from collections import defaultdict
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..database import session_scope
from ..settings_service import as_bool, as_float, as_int, get_setting
from . import telegram
from .nvr import NVRClient, apply_camera_address


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def system_metrics() -> dict[str, Any]:
    usage = shutil.disk_usage(str(DATA_DIR))
    memory: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                memory[key] = int(rest.strip().split()[0]) * 1024
    except Exception:
        pass
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return {
        "disk_total": usage.total,
        "disk_used": usage.used,
        "disk_free": usage.free,
        "disk_used_percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "memory_total": memory.get("MemTotal", 0),
        "memory_available": memory.get("MemAvailable", 0),
        "memory_used_percent": round(
            (1 - memory.get("MemAvailable", 0) / memory["MemTotal"]) * 100, 1
        )
        if memory.get("MemTotal")
        else 0,
        "load_1": round(load[0], 2),
        "load_5": round(load[1], 2),
        "load_15": round(load[2], 2),
        "cpu_count": os.cpu_count() or 1,
    }


class HealthChecker:
    def __init__(self, db: Session):
        self.db = db
        self.notifications: list[str] = []
        cooldown = as_int(get_setting(db, "health.alert_cooldown_minutes"), 60)
        self.cooldown_seconds = max(0, cooldown) * 60
        self.hdd_capacity_alert = as_bool(
            get_setting(db, "health.hdd_capacity_alert"), False
        )
        self.exceptions = (
            db.query(models.AlertException)
            .filter(models.AlertException.enabled.is_(True))
            .all()
        )

    # ------------------------------------------------------------------
    def _is_excepted(
        self,
        alert_type: str,
        fingerprint: str,
        nvr: models.NVR | None = None,
        camera: models.Camera | None = None,
    ) -> bool:
        """True when an admin exception suppresses this alert."""
        for item in self.exceptions:
            if item.fingerprint and item.fingerprint != fingerprint:
                continue
            if item.alert_type and item.alert_type != alert_type:
                continue
            if item.nvr_id and (not nvr or item.nvr_id != nvr.id):
                continue
            if item.camera_id and (not camera or item.camera_id != camera.id):
                continue
            # A completely empty exception must not silently mute everything.
            if not any((item.fingerprint, item.alert_type, item.nvr_id, item.camera_id)):
                continue
            return True
        return False

    def _alert(
        self,
        *,
        fingerprint: str,
        alert_type: str,
        severity: str,
        message: str,
        nvr: models.NVR | None = None,
        camera: models.Camera | None = None,
    ) -> bool:
        """Create/update an alert. Returns True when it is due to be notified."""
        if self._is_excepted(alert_type, fingerprint, nvr=nvr, camera=camera):
            # Make sure an alert created before the exception is not left open.
            self._resolve(fingerprint, "Suppressed by alert exception")
            return False
        now = _now()
        existing = (
            self.db.query(models.Alert)
            .filter(models.Alert.fingerprint == fingerprint, models.Alert.resolved.is_(False))
            .order_by(models.Alert.created_at.desc())
            .first()
        )
        if existing:
            existing.message = message
            existing.severity = severity
            last = existing.last_notified_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            if self.cooldown_seconds and last is not None:
                if (now - last).total_seconds() < self.cooldown_seconds:
                    return False
            existing.last_notified_at = now
            return True
        alert = models.Alert(
            nvr_id=nvr.id if nvr else None,
            camera_id=camera.id if camera else None,
            type=alert_type,
            severity=severity,
            fingerprint=fingerprint,
            message=message,
            created_at=now,
            last_notified_at=now,
        )
        self.db.add(alert)
        self.db.flush()
        return True

    def _resolve(self, fingerprint: str, note: str | None = None) -> bool:
        rows = (
            self.db.query(models.Alert)
            .filter(models.Alert.fingerprint == fingerprint, models.Alert.resolved.is_(False))
            .all()
        )
        changed = False
        for row in rows:
            row.resolved = True
            row.resolved_at = _now()
            row.resolve_note = note or "Recovered automatically"
            changed = True
        return changed

    # ------------------------------------------------------------------
    def check_nvr(self, nvr: models.NVR) -> dict[str, Any]:
        cfg = {
            key: get_setting(self.db, key)
            for key in (
                "health.hdd_threshold_percent",
                "health.offline_camera_alert",
                "health.nvr_offline_alert",
            )
        }
        hdd_threshold = as_float(cfg["health.hdd_threshold_percent"], 90)
        offline_cam_enabled = as_bool(cfg["health.offline_camera_alert"], True)
        nvr_offline_enabled = as_bool(cfg["health.nvr_offline_alert"], True)

        now = _now()
        client = NVRClient(nvr)
        health = client.get_health(include_cameras=nvr.auto_discover)

        nvr.last_checked = now
        summary: list[str] = []

        if not health.ok:
            nvr.status = "offline"
            if nvr_offline_enabled and self._alert(
                fingerprint=f"nvr:{nvr.id}:offline",
                alert_type="nvr_offline",
                severity="critical",
                message=f"NVR {nvr.name} ({nvr.host}) is offline: {health.message}",
                nvr=nvr,
            ):
                summary.append(f"🔴 <b>{telegram.esc(nvr.name)}</b> is offline — {telegram.esc(health.message)}")
            self.db.flush()
            return {"ok": False, "summary": summary, "health": health}

        nvr.status = "online"
        nvr.last_seen = now
        if self._resolve(f"nvr:{nvr.id}:offline", "NVR back online"):
            summary.append(f"🟢 <b>{telegram.esc(nvr.name)}</b> is back online")
        if health.device:
            nvr.model = health.device.get("model") or nvr.model
            nvr.firmware = health.device.get("firmware") or nvr.firmware
            nvr.serial = health.device.get("serial") or nvr.serial

        # --- Storage ---------------------------------------------------
        if health.storage:
            nvr.hdd_json = json.dumps(health.storage)
            nvr.hdd_used_percent = health.hdd_used_percent
            for index, disk in enumerate(health.storage):
                disk_id = disk.get("id") or disk.get("name") or str(index)
                fp = f"nvr:{nvr.id}:hdd:{disk_id}"
                status_text = str(disk.get("status") or "unknown").lower()
                used = disk.get("used_percent")
                bad_status = status_text not in {"ok", "normal", "healthy", "online", "rw", "readwrite", "good", ""}
                # NVR/DVR disks are almost always near-full by design; only
                # alert on capacity when the admin explicitly opts in.
                over_capacity = (
                    self.hdd_capacity_alert and used is not None and used >= hdd_threshold
                )
                if bad_status or over_capacity:
                    severity = "critical" if bad_status or (used is not None and used >= 98) else "warning"
                    reasons = []
                    if bad_status:
                        reasons.append(f"status {disk.get('status')}")
                    if over_capacity:
                        reasons.append(f"{used}% full")
                    message = f"HDD {disk.get('name') or disk_id} on {nvr.name}: " + ", ".join(reasons)
                    if self._alert(
                        fingerprint=fp,
                        alert_type="hdd",
                        severity=severity,
                        message=message,
                        nvr=nvr,
                    ):
                        icon = "🚨" if severity == "critical" else "⚠️"
                        summary.append(f"{icon} <b>{telegram.esc(nvr.name)}</b> HDD {telegram.esc(disk.get('name') or disk_id)} — {telegram.esc(', '.join(reasons))}")
                else:
                    if self._resolve(fp, "Storage recovered"):
                        summary.append(f"✅ <b>{telegram.esc(nvr.name)}</b> HDD {telegram.esc(disk.get('name') or disk_id)} recovered")

        # --- Cameras ---------------------------------------------------
        if health.cameras:
            existing = {camera.channel: camera for camera in nvr.cameras}
            discovered_channels = set()
            for item in health.cameras:
                channel = item["channel"]
                discovered_channels.add(channel)
                camera = existing.get(channel)
                if camera is None:
                    camera = models.Camera(nvr_id=nvr.id, channel=channel, name=item.get("name") or f"Channel {channel}")
                    self.db.add(camera)
                    self.db.flush()
                    existing[channel] = camera
                elif not camera.name or camera.name.startswith("Channel "):
                    camera.name = item.get("name") or camera.name
                if not camera.enabled:
                    # Disabled ports are ignored completely: no state, no alerts.
                    camera.online = None
                    continue
                apply_camera_address(camera, item)
                online = item.get("online")
                camera.online = online
                if online is not False:
                    camera.last_seen = now
                if online is False and offline_cam_enabled:
                    if self._alert(
                        fingerprint=f"nvr:{nvr.id}:cam:{channel}:offline",
                        alert_type="camera_offline",
                        severity="warning",
                        message=f"Camera {channel} ({camera.name}) on {nvr.name} is offline",
                        nvr=nvr,
                        camera=camera,
                    ):
                        summary.append(f"📵 <b>{telegram.esc(nvr.name)}</b> cam {channel} {telegram.esc(camera.name)} offline")
                elif online is True:
                    if self._resolve(f"nvr:{nvr.id}:cam:{channel}:offline", "Camera back online"):
                        summary.append(f"📹 <b>{telegram.esc(nvr.name)}</b> cam {channel} {telegram.esc(camera.name)} back online")
            # Cameras that no longer exist on the NVR are marked offline.
            for channel, camera in existing.items():
                if not camera.enabled:
                    continue
                if channel not in discovered_channels and camera.online is not False:
                    camera.online = False
            try:
                client.fill_missing_macs([camera for camera in existing.values() if camera.enabled])
            except Exception as exc:  # MAC lookup is best effort only
                print(f"[health] MAC lookup failed for {nvr.name}: {exc}")

        self.db.flush()
        return {"ok": True, "summary": summary, "health": health}

    # ------------------------------------------------------------------
    def run(self, nvr_ids: list[int] | None = None, notify: bool = True) -> dict[str, Any]:
        query = self.db.query(models.NVR).filter(models.NVR.enabled.is_(True))
        if nvr_ids:
            query = query.filter(models.NVR.id.in_(nvr_ids))
        nvrs = query.all()
        results: list[dict[str, Any]] = []
        for nvr in nvrs:
            try:
                results.append(self.check_nvr(nvr))
            except Exception as exc:
                self.db.rollback()
                nvr = self.db.get(models.NVR, nvr.id)
                if nvr:
                    nvr.status = "error"
                    nvr.last_checked = _now()
                results.append({"ok": False, "summary": [f"⚠️ {nvr.name if nvr else 'NVR'} check error: {exc}"], "error": str(exc)})
        self.db.commit()

        notifications: list[str] = []
        for result in results:
            notifications.extend(result.get("summary") or [])
        if notify and notifications and as_bool(get_setting(self.db, "telegram.alerts_enabled"), True):
            # Send one digest instead of a storm of messages.
            chunks: list[str] = []
            current = ""
            for line in notifications:
                if len(current) + len(line) > 3500:
                    chunks.append(current)
                    current = ""
                current += line + "\n"
            if current:
                chunks.append(current)
            for chunk in chunks:
                telegram.send_alert(self.db, "🩺 <b>CCTV Health Report</b>\n\n" + chunk)

        online = sum(1 for r in results if r.get("ok"))
        return {
            "checked": len(results),
            "online": online,
            "offline": len(results) - online,
            "notifications": len(notifications),
            "results": [
                {
                    "ok": r.get("ok"),
                    "summary": r.get("summary"),
                    "error": r.get("error"),
                }
                for r in results
            ],
        }


def health_summary(db: Session) -> dict[str, Any]:
    total = db.query(func.count(models.NVR.id)).scalar() or 0
    online = db.query(func.count(models.NVR.id)).filter(models.NVR.status == "online").scalar() or 0
    cameras = db.query(func.count(models.Camera.id)).filter(models.Camera.enabled.is_(True)).scalar() or 0
    offline_cameras = (
        db.query(func.count(models.Camera.id))
        .filter(models.Camera.enabled.is_(True), models.Camera.online.is_(False))
        .scalar()
        or 0
    )
    open_alerts = (
        db.query(func.count(models.Alert.id))
        .filter(models.Alert.resolved.is_(False))
        .scalar()
        or 0
    )
    critical_alerts = (
        db.query(func.count(models.Alert.id))
        .filter(models.Alert.resolved.is_(False), models.Alert.severity == "critical")
        .scalar()
        or 0
    )
    return {
        "nvr_total": total,
        "nvr_online": online,
        "nvr_offline": total - online,
        "cameras": cameras,
        "cameras_offline": offline_cameras,
        "open_alerts": open_alerts,
        "critical_alerts": critical_alerts,
        "system": system_metrics(),
    }


def cleanup_old_files(db: Session, keep_days: int | None = None) -> int:
    days = keep_days if keep_days is not None else as_int(get_setting(db, "jobs.keep_local_days"), 7)
    cutoff = _now() - dt.timedelta(days=max(1, days))
    removed = 0
    for job_file in db.query(models.JobFile).filter(models.JobFile.created_at < cutoff).all():
        if job_file.local_path and os.path.exists(job_file.local_path):
            try:
                os.remove(job_file.local_path)
                removed += 1
            except OSError:
                pass
        job_file.local_path = None
        job_file.status = "expired"
    db.commit()
    # Remove orphaned job work folders.
    work_root = DATA_DIR / "work"
    if work_root.exists():
        for folder in work_root.iterdir():
            try:
                if folder.is_dir() and folder.stat().st_mtime < cutoff.timestamp():
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass
    return removed


class HealthScheduler(threading.Thread):
    def __init__(self, stop_event: threading.Event | None = None):
        super().__init__(name="health-scheduler", daemon=True)
        self.stop_event = stop_event or threading.Event()
        self.last_run: dt.datetime | None = None
        self.last_result: dict[str, Any] | None = None

    def run(self) -> None:
        # Small delay lets the web app finish booting.
        if self.stop_event.wait(5):
            return
        last_cleanup = 0.0
        last_agent_tick = 0.0
        while not self.stop_event.is_set():
            try:
                with session_scope() as db:
                    # Agent housekeeping runs more often than the NVR sweep so
                    # a dead campus worker is noticed quickly.
                    if time.time() - last_agent_tick > 60:
                        from . import agents as agent_service

                        agent_service.mark_offline_agents(db)
                        agent_service.recover_stalled_jobs(db)
                        last_agent_tick = time.time()
                    if as_bool(get_setting(db, "health.enabled"), True):
                        checker = HealthChecker(db)
                        self.last_result = checker.run(notify=True)
                        self.last_run = _now()
                    if time.time() - last_cleanup > 3600:
                        cleanup_old_files(db)
                        last_cleanup = time.time()
            except Exception as exc:  # a broken NVR must never kill the scheduler
                print(f"[health] scheduler error: {exc}")
            interval = 10
            try:
                with session_scope() as db:
                    interval = max(1, as_int(get_setting(db, "health.interval_minutes"), 10))
            except Exception:
                pass
            if self.stop_event.wait(interval * 60):
                break


_scheduler: HealthScheduler | None = None


def start_scheduler() -> HealthScheduler:
    global _scheduler
    if _scheduler and _scheduler.is_alive():
        return _scheduler
    _scheduler = HealthScheduler()
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.stop_event.set()
        _scheduler = None
