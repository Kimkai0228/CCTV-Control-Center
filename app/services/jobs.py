"""Background footage jobs: download -> compress -> upload -> notify."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..config import WORK_DIR
from ..database import SessionLocal
from ..security import audit, decrypt_secret
from ..settings_service import as_bool, as_int, get_all_settings, get_setting
from . import drive, mailer, telegram
from .compressor import compress_file
from .download_links import build_agent_download_url
from .downloader import download_camera, parse_boundary, safe_filename

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_cancel_events: dict[int, threading.Event] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ref() -> str:
    return f"{dt.datetime.now().strftime('%y%m%d')}-{secrets.token_hex(3).upper()}"


def init_job_manager() -> None:
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = 4
            try:
                db = SessionLocal()
                try:
                    workers = max(1, min(16, as_int(get_setting(db, "jobs.max_workers"), 4)))
                finally:
                    db.close()
            except Exception:
                pass
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cctv-job")


def shutdown_job_manager() -> None:
    global _executor
    with _executor_lock:
        if _executor:
            _executor.shutdown(wait=False, cancel_futures=False)
            _executor = None


def _agent_file_url(job: models.Job, filename: str) -> str | None:
    """Build a short-lived direct URL for a file kept on the campus agent."""
    if not job.agent or not job.agent.public_url or not job.agent.token_enc:
        return None
    token = decrypt_secret(job.agent.token_enc)
    if not token:
        return None
    return build_agent_download_url(job.agent.public_url, token, job.id, filename)


def _serialize_job(job: models.Job) -> dict[str, Any]:
    result = json.loads(job.result_json) if job.result_json else None
    files: list[dict[str, Any]] = [
        {
            "id": f.id,
            "filename": f.filename,
            "size_bytes": f.size_bytes,
            "status": f.status,
            "channel": f.channel,
            "download_url": f"/api/jobs/{job.id}/files/{f.id}" if f.local_path and os.path.exists(f.local_path) else None,
            "agent_download": False,
            "on_agent": False,
        }
        for f in job.files
    ]
    seen = {f["filename"] for f in files}
    for entry in (result or {}).get("files") or []:
        filename = entry.get("filename")
        if not filename or filename in seen:
            continue
        url = entry.get("download_url")
        agent_download = False
        if not url and entry.get("agent_file"):
            url = _agent_file_url(job, filename)
            agent_download = bool(url)
        files.append(
            {
                "id": entry.get("file_id"),
                "filename": filename,
                "size_bytes": entry.get("size_bytes"),
                "status": entry.get("status") or "ready",
                "channel": entry.get("channel"),
                "camera": entry.get("camera"),
                "nvr": entry.get("nvr"),
                "download_url": url,
                "agent_download": agent_download,
                "on_agent": bool(not url),
            }
        )
        seen.add(filename)

    return {
        "id": job.id,
        "ref": job.ref,
        "source": job.source,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "folder_name": job.folder_name,
        "drive_folder_key": job.drive_folder_key,
        "drive_link": job.drive_link,
        "agent_id": job.agent_id,
        "agent": job.agent.name if job.agent else None,
        "agent_campus": job.agent.campus if job.agent else None,
        "agent_public_url": job.agent.public_url if job.agent else None,
        "dispatched_at": job.dispatched_at.isoformat() if job.dispatched_at else None,
        "cancel_requested": bool(job.cancel_requested),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "retry_pending": bool(job.retry_json),
        "result": result,
        "files": files,
    }


def _camera_key(nvr_id: Any, channel: Any) -> tuple[int, int]:
    try:
        return int(nvr_id or 0), int(channel or 0)
    except (TypeError, ValueError):
        return 0, 0


def retry_targets(job: models.Job) -> list[tuple[int, int]]:
    """Cameras queued by a retry request, encoded in ``retry_json``."""
    if not job.retry_json:
        return []
    try:
        data = json.loads(job.retry_json)
    except (TypeError, ValueError):
        return []
    targets: list[tuple[int, int]] = []
    for item in data if isinstance(data, list) else []:
        key = _camera_key(item.get("nvr_id"), item.get("channel"))
        if key != (0, 0) and key not in targets:
            targets.append(key)
    return targets


def _retry_error_prefixes(previous: dict[str, Any], targets: set[tuple[int, int]]) -> set[str]:
    """Error strings produced by the camera rows being retried."""
    prefixes: set[str] = set()
    for row in previous.get("cameras") or []:
        if _camera_key(row.get("nvr_id"), row.get("channel")) not in targets:
            continue
        nvr_name = str(row.get("nvr") or "")
        channel = row.get("channel")
        if nvr_name:
            prefixes.add(f"{nvr_name} CH{channel}:")
        prefixes.add(f"Camera CH{channel}")
        prefixes.add(f"NVR {row.get('nvr_id')}")
    return prefixes


def merge_retry_result(
    previous: dict[str, Any] | None,
    fresh: dict[str, Any] | None,
    targets: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Merge a retry run into the job's earlier result.

    Successful cameras and their files from the first run are preserved while
    the retried camera rows replace the old failed rows.
    """
    previous = previous or {}
    fresh = fresh or {}
    target_set = set(targets or [])
    cameras: dict[tuple[int, int], dict[str, Any]] = {}
    for row in previous.get("cameras") or []:
        cameras[_camera_key(row.get("nvr_id"), row.get("channel"))] = dict(row)
    for row in fresh.get("cameras") or []:
        cameras[_camera_key(row.get("nvr_id"), row.get("channel"))] = dict(row)

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in (previous.get("files") or []) + (fresh.get("files") or []):
        filename = entry.get("filename")
        if filename and filename in seen:
            continue
        if filename:
            seen.add(filename)
        files.append(entry)

    drop = _retry_error_prefixes(previous, target_set)
    old_errors = [
        error
        for error in (previous.get("errors") or [])
        if not any(str(error).startswith(prefix) for prefix in drop)
    ]
    errors: list[str] = []
    for error in old_errors + (fresh.get("errors") or []):
        if error not in errors:
            errors.append(error)

    merged = dict(previous)
    merged.update(fresh)
    # Keep the original Drive receipt when a retry did not upload anything
    # (for example when Drive was disabled for the second run).
    if fresh.get("drive") is None and previous.get("drive") is not None:
        merged["drive"] = previous["drive"]
    merged["cameras"] = list(cameras.values())
    merged["files"] = files
    merged["errors"] = errors
    return merged


def _camera_row(
    *,
    nvr_id: int,
    nvr_name: str,
    channel: int,
    camera_name: str,
    status: str = "failed",
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "nvr_id": nvr_id,
        "nvr": nvr_name,
        "channel": channel,
        "camera": camera_name,
        "status": status,
        "message": message,
        "filename": None,
        "size_bytes": None,
    }


def _failed_camera_message(cameras: list[dict[str, Any]]) -> str:
    parts = []
    for row in cameras:
        if row.get("status") != "failed":
            continue
        parts.append(
            f"{row.get('nvr') or 'NVR'} CH{row.get('channel')}: {row.get('message') or 'download failed'}"
        )
    return " | ".join(parts[-3:])


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------


def _validate_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    cameras = payload.get("cameras") or []
    if not cameras:
        return False, "Select at least one camera"
    for item in cameras:
        try:
            int(item.get("nvr_id"))
            int(item.get("channel"))
        except (TypeError, ValueError):
            return False, "Invalid camera selection"
    date_str = str(payload.get("date") or "").strip()
    start = str(payload.get("start_time") or "").strip()
    end = str(payload.get("end_time") or "").strip()
    if not date_str or not start or not end:
        return False, "Date, start time and end time are required"
    try:
        start_dt = parse_boundary(date_str, start)
        end_dt = parse_boundary(date_str, end)
    except ValueError as exc:
        return False, str(exc)
    if end_dt <= start_dt:
        return False, "End time must be after start time"
    if (end_dt - start_dt).total_seconds() > 12 * 3600:
        return False, "A single request cannot exceed 12 hours"
    return True, ""


def _validate_camera_selection(db: Session, payload: dict[str, Any]) -> str:
    """Reject missing, disabled or incomplete camera selections server-side.

    The WebUI already hides disabled ports, but the API, bot and MiniApp can
    still submit a stale selection.  Disabled ports must never produce footage.
    """
    for item in payload.get("cameras") or []:
        try:
            nvr_id = int(item.get("nvr_id"))
            channel = int(item.get("channel"))
        except (TypeError, ValueError):
            return "Invalid camera selection"
        nvr = db.get(models.NVR, nvr_id)
        if not nvr or not nvr.enabled:
            return f"NVR {nvr_id} is missing or disabled"
        camera = (
            db.query(models.Camera)
            .filter(models.Camera.nvr_id == nvr_id, models.Camera.channel == channel)
            .first()
        )
        if camera is None:
            return f"Camera CH{channel} on {nvr.name} was not found"
        if not camera.enabled:
            return f"Camera CH{channel} ({camera.name}) on {nvr.name} is disabled"
    return ""


def create_job(
    db: Session,
    payload: dict[str, Any],
    *,
    user: models.User | None = None,
    source: str = "web",
    kind: str = "footage",
    telegram_chat_id: str | None = None,
    telegram_message_id: str | None = None,
    telegram_user_id: str | None = None,
    auto_start: bool = True,
) -> models.Job:
    ok, error = _validate_payload(payload)
    if not ok:
        raise ValueError(error)
    camera_error = _validate_camera_selection(db, payload)
    if camera_error:
        raise ValueError(camera_error)

    # Route the job to a remote campus agent when one is configured for the
    # requested campus/zone. The main host keeps running local jobs.
    from . import agents as agent_service

    preferred = payload.get("agent_id")
    agent = agent_service.choose_agent(
        db,
        payload,
        int(preferred) if preferred not in (None, "", 0, "0") else None,
    )
    campus = agent_service.job_campus(db, payload)
    local_campus = (get_setting(db, "app.campus") or "tk").strip().lower()
    if (
        agent is None
        and campus
        and campus != local_campus
        and as_bool(get_setting(db, "agents.enabled"), True)
        and as_bool(get_setting(db, "agents.strict_campus"), False)
    ):
        raise ValueError(f"No enabled agent serves the {campus.upper()} campus")

    date_str = str(payload.get("date")).strip()
    explicit_folder = str(payload.get("folder_name") or "").strip()
    if explicit_folder:
        # An explicit folder name (bot rename, /tk <name>, web/MiniApp field)
        # must always win over the incident + date convention.
        folder_name = safe_filename(explicit_folder, "CCTV_Request")
    else:
        base = str(payload.get("incident") or "").strip()
        if base:
            folder_name = safe_filename(f"{base} {date_str}".strip(), "CCTV_Request")
        else:
            folder_name = safe_filename(
                get_setting(db, "jobs.default_folder_name", "CCTV_Request"),
                "CCTV_Request",
            )

    job = models.Job(
        ref=_ref(),
        user_id=user.id if user else None,
        source=source,
        kind=kind,
        status="queued",
        progress=0,
        message="Queued",
        folder_name=folder_name,
        drive_folder_key=payload.get("drive_folder_key") or None,
        request_json=json.dumps(payload),
        telegram_chat_id=str(telegram_chat_id) if telegram_chat_id else None,
        telegram_message_id=str(telegram_message_id) if telegram_message_id else None,
        telegram_user_id=str(telegram_user_id) if telegram_user_id else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    if agent is not None:
        agent_service.dispatch_job(db, job, agent)
    elif auto_start:
        start_job(job.id)
    return job


def start_job(job_id: int) -> None:
    init_job_manager()
    assert _executor is not None
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job and job.agent_id and job.status in {"dispatched", "running"}:
            # Remote agent owns execution; it reports back to the main host.
            return
    finally:
        db.close()
    _cancel_events[job_id] = threading.Event()
    _executor.submit(_run_job, job_id)


def request_cancel(db: Session, job_id: int) -> models.Job | None:
    job = db.get(models.Job, job_id)
    if not job:
        return None
    event = _cancel_events.get(job_id)
    if event:
        event.set()
    job.cancel_requested = True
    if job.status in {"queued", "dispatched", "running"}:
        # Finalize immediately. For remote agents this also prevents a worker
        # that was restarted (and no longer knows about the job) from leaving
        # the job stuck forever waiting for a cancel acknowledgement.
        job.status = "cancelled"
        job.finished_at = _now()
        job.message = "Cancelled by operator"
    db.commit()
    return job


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class _Reporter:
    def __init__(self, db: Session, job: models.Job):
        self.db = db
        self.job = job
        self.last = 0.0

    def update(self, progress: int, message: str, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last < 1.0:
            return
        self.last = now
        self.job.progress = max(0, min(100, int(progress)))
        self.job.message = message[:590]
        self.db.commit()

    def cancel_requested(self) -> bool:
        self.db.refresh(self.job)
        return bool(self.job.cancel_requested)


def _run_job(job_id: int) -> None:
    db = SessionLocal()
    job: models.Job | None = None
    work_dir: str | None = None
    try:
        job = db.get(models.Job, job_id)
        if not job:
            return
        if job.status == "cancelled":
            return
        job.status = "running"
        job.started_at = _now()
        job.progress = 1
        job.message = "Preparing"
        db.commit()
        reporter = _Reporter(db, job)
        payload = json.loads(job.request_json or "{}")
        retry_keys = retry_targets(job)
        if retry_keys:
            wanted = set(retry_keys)
            payload["cameras"] = [
                item
                for item in (payload.get("cameras") or [])
                if _camera_key(item.get("nvr_id"), item.get("channel")) in wanted
            ]
        previous_result = json.loads(job.result_json) if job.result_json else {}
        settings = get_all_settings(db)
        entries: list[dict[str, Any]] = []
        camera_rows: list[dict[str, Any]] = []
        errors: list[str] = []

        for item in payload.get("cameras", []):
            try:
                nvr_id = int(item.get("nvr_id"))
                channel = int(item.get("channel"))
            except (TypeError, ValueError):
                camera_rows.append(
                    _camera_row(
                        nvr_id=0,
                        nvr_name=f"NVR {item.get('nvr_id')}",
                        channel=int(item.get("channel") or 0),
                        camera_name=f"Channel {item.get('channel')}",
                        message="Invalid camera selection",
                    )
                )
                continue
            nvr = db.get(models.NVR, nvr_id)
            if not nvr or not nvr.enabled:
                camera_rows.append(
                    _camera_row(
                        nvr_id=nvr_id,
                        nvr_name=nvr.name if nvr else f"NVR {nvr_id}",
                        channel=channel,
                        camera_name=f"Channel {channel}",
                        message=f"NVR {nvr_id} is missing or disabled",
                    )
                )
                continue
            camera = (
                db.query(models.Camera)
                .filter(models.Camera.nvr_id == nvr.id, models.Camera.channel == channel)
                .first()
            )
            camera_name = camera.name if camera else f"Channel {channel}"
            if camera is not None and not camera.enabled:
                camera_rows.append(
                    _camera_row(
                        nvr_id=nvr.id,
                        nvr_name=nvr.name,
                        channel=channel,
                        camera_name=camera_name,
                        message=f"Camera CH{channel} ({camera_name}) is disabled",
                    )
                )
                continue
            entries.append(
                {
                    "nvr": nvr,
                    "camera": camera,
                    "channel": channel,
                    "nvr_name": nvr.name,
                    "camera_name": camera_name,
                }
            )
            camera_rows.append(
                _camera_row(
                    nvr_id=nvr.id,
                    nvr_name=nvr.name,
                    channel=channel,
                    camera_name=camera_name,
                    status="pending",
                )
            )
        if not entries:
            fresh = {"files": [], "cameras": camera_rows, "errors": []}
            merged = (
                merge_retry_result(previous_result, fresh, retry_keys)
                if retry_keys
                else fresh
            )
            job.result_json = json.dumps(merged)
            job.retry_json = None
            previous_ok = bool(
                merged.get("files")
            ) or any(
                row.get("status") == "success"
                for row in (merged.get("cameras") or [])
            )
            if retry_keys and previous_ok:
                # The retried cameras are gone (NVR removed/disabled) but the
                # original successful downloads remain valid.
                job.status = "partial"
                job.progress = 100
                job.message = "Retry: no cameras available · previous files kept"
                job.error = _failed_camera_message(
                    merged.get("cameras") or []
                )[:4000]
                job.finished_at = _now()
                db.commit()
                audit(
                    db,
                    job.user,
                    "job_partial",
                    f"{job.ref} · {job.folder_name} · retry found no available cameras",
                    level="warning",
                )
                return
            raise RuntimeError("None of the selected cameras are available")

        work_dir = str(WORK_DIR / f"job_{job.id}_{job.ref}")
        os.makedirs(work_dir, exist_ok=True)

        start_dt = parse_boundary(payload["date"], payload["start_time"])
        end_dt = parse_boundary(payload["date"], payload["end_time"])
        multiple_nvrs = len({e["nvr"].id for e in entries}) > 1

        # Build a unique, human-friendly filename per camera.
        used_names: set[str] = set()
        for entry in entries:
            prefix = job.folder_name or "CCTV"
            if multiple_nvrs:
                prefix = f"{prefix}_{entry['nvr'].code}"
            base = safe_filename(f"{prefix}_CH{entry['channel']}", f"CH{entry['channel']}")
            candidate = f"{base}.mp4"
            counter = 2
            while candidate.lower() in used_names:
                candidate = f"{base}_{counter}.mp4"
                counter += 1
            used_names.add(candidate.lower())
            entry["filename"] = candidate
            entry["path"] = os.path.join(work_dir, candidate)

        # --- Phase 1: download -----------------------------------------
        downloaded: list[dict[str, Any]] = []
        total_cams = max(1, len(entries))
        row_index = {
            _camera_key(row.get("nvr_id"), row.get("channel")): row for row in camera_rows
        }
        for index, entry in enumerate(entries):
            if reporter.cancel_requested():
                _finish_cancelled(db, job)
                return
            base_pct = 3 + int((index / total_cams) * 42)
            reporter.update(base_pct, f"Downloading {entry['camera_name']} from {entry['nvr_name']}", force=True)

            def progress(value: int, text: str, index=index):
                reporter.update(3 + int(((index + value / 100) / total_cams) * 42), text)

            ok, message = download_camera(
                entry["nvr"],
                entry["channel"],
                start_dt,
                end_dt,
                entry["path"],
                progress,
            )
            row = row_index.get(_camera_key(entry["nvr"].id, entry["channel"]))
            if ok and os.path.exists(entry["path"]) and os.path.getsize(entry["path"]) > 0:
                downloaded.append(entry)
            elif row is not None:
                row["status"] = "failed"
                row["message"] = message or "Download failed"

        if not downloaded:
            fresh = {"files": [], "cameras": camera_rows, "errors": errors}
            merged = (
                merge_retry_result(previous_result, fresh, retry_keys)
                if retry_keys
                else fresh
            )
            all_rows = merged.get("cameras") or []
            failure = _failed_camera_message(all_rows) or "All camera downloads failed"
            # A retry that recovers nothing must not hide the successful
            # cameras collected by the original run.
            previous_ok = any(
                row.get("status") == "success" for row in all_rows
            ) or bool(merged.get("files"))
            still_failed = len(
                [row for row in all_rows if row.get("status") == "failed"]
            )
            job.progress = 100
            job.status = "partial" if retry_keys and previous_ok else "failed"
            if job.status == "partial":
                job.message = (
                    f"Retry: 0 camera(s) recovered · {still_failed} still failing"
                )
            else:
                job.message = "All selected camera downloads failed"
            job.error = failure[:4000]
            job.result_json = json.dumps(merged)
            job.retry_json = None
            job.finished_at = _now()
            db.commit()
            audit(
                db,
                job.user,
                "job_partial" if job.status == "partial" else "job_failed",
                f"{job.ref} · {job.folder_name} · {failure[:1000]}",
                level="warning" if job.status == "partial" else "error",
            )
            if job.telegram_chat_id:
                icon = "⚠️" if job.status == "partial" else "❌"
                title = "Retry still failing" if job.status == "partial" else "Job failed"
                telegram.send_message(
                    db,
                    f"{icon} <b>{title}</b> · {telegram.esc(job.folder_name)}\n{telegram.esc(failure[:500])}",
                    chat_id=job.telegram_chat_id,
                )
            return

        # --- Phase 2: compress -----------------------------------------
        compression_enabled = as_bool(settings.get("compression.enabled"), True)
        if compression_enabled and downloaded:
            reporter.update(45, f"Compressing {len(downloaded)} file(s)", force=True)
            max_parallel = max(1, min(4, as_int(settings.get("compression.max_parallel"), 2)))
            compressed_results: dict[int, dict[str, Any]] = {}

            def compress_entry(entry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
                result = compress_file(entry["path"], settings)
                return entry["channel"], {
                    "path": result.path,
                    "backend": result.backend,
                    "compressed": result.compressed,
                    "message": result.message,
                    "original_size": result.original_size,
                    "final_size": result.final_size,
                }

            done = 0
            with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="compress") as pool:
                future_map = {pool.submit(compress_entry, entry): entry for entry in downloaded}
                for future in as_completed(future_map):
                    if reporter.cancel_requested():
                        _finish_cancelled(db, job)
                        return
                    entry = future_map[future]
                    try:
                        _, result = future.result()
                    except Exception as exc:
                        result = {"path": entry["path"], "backend": "none", "compressed": False, "message": str(exc)}
                    # Move the compressed file back to the clean filename.
                    if result.get("compressed") and result["path"] != entry["path"]:
                        try:
                            if os.path.exists(entry["path"]):
                                os.remove(entry["path"])
                            os.replace(result["path"], entry["path"])
                        except OSError:
                            entry["path"] = result["path"]
                    entry["compression"] = result
                    done += 1
                    reporter.update(45 + int((done / len(downloaded)) * 28), f"Compressed {done}/{len(downloaded)}")

        success_info: dict[tuple[int, int], dict[str, Any]] = {}
        for entry in downloaded:
            filename = os.path.basename(entry["path"])
            size_bytes = os.path.getsize(entry["path"]) if os.path.exists(entry["path"]) else 0
            jf = models.JobFile(
                job_id=job.id,
                camera_id=entry["camera"].id if entry["camera"] else None,
                channel=entry["channel"],
                filename=filename,
                local_path=os.path.abspath(entry["path"]),
                size_bytes=size_bytes,
                status="ready",
            )
            db.add(jf)
            success_info[_camera_key(entry["nvr"].id, entry["channel"])] = {
                "filename": filename,
                "size_bytes": size_bytes,
                "status": "success",
                "message": (entry.get("compression") or {}).get("message") or "Ready",
            }
        for row in camera_rows:
            info = success_info.get(_camera_key(row.get("nvr_id"), row.get("channel")))
            if info:
                row.update(info)
        db.commit()

        # --- Phase 3: upload -------------------------------------------
        upload_enabled = as_bool(settings.get("google.upload_enabled"), True)
        drive_result: dict[str, Any] | None = None
        if upload_enabled and as_bool(payload.get("upload", True), True):
            if reporter.cancel_requested():
                _finish_cancelled(db, job)
                return
            reporter.update(76, "Uploading to Google Drive", force=True)
            files_for_upload = [
                {"path": entry["path"], "filename": os.path.basename(entry["path"])} for entry in downloaded
            ]
            try:
                drive_result = drive.upload_job_files(
                    db,
                    files_for_upload,
                    job.folder_name or "CCTV Request",
                    job.drive_folder_key,
                    payload.get("campus"),
                    progress=lambda pct, text: reporter.update(76 + int(pct * 0.20), text),
                    existing_folder_id=job.drive_folder_id if retry_keys else None,
                    existing_folder_link=job.drive_link if retry_keys else None,
                )
                job.drive_link = drive_result.get("folder_link")
                job.drive_folder_id = drive_result.get("folder_id")
                db.commit()
            except Exception as exc:
                errors.append(f"Drive upload failed: {exc}")
                reporter.update(88, f"Upload skipped: {exc}", force=True)

        # --- Phase 4: share / email ------------------------------------
        notify_emails = [e for e in (payload.get("notify_emails") or []) if e]
        notify_approver = bool(payload.get("notify_approver"))
        if drive_result and (notify_emails or notify_approver):
            reporter.update(96, "Granting access and sending email", force=True)
            expiry_days = as_int(settings.get("google.share_expiry_days"), 30)
            try:
                service = drive.DriveService.from_settings(db)
                request_data = {
                    "incident": payload.get("incident") or job.folder_name,
                    "campus": payload.get("campus") or job.drive_folder_key or "",
                    "date": payload.get("date"),
                    "requester": payload.get("requester"),
                    "requester_email": ", ".join(notify_emails) if notify_emails else None,
                    "expiry_days": expiry_days,
                }
                for email in notify_emails:
                    try:
                        service.grant_access(job.drive_folder_id, email, expiry_days)
                        mailer.send_footage_email(db, email, job.drive_link, request_data)
                    except Exception as exc:
                        errors.append(f"Email {email}: {exc}")
                if notify_approver:
                    approver = get_setting(db, "smtp.approver_email")
                    if approver:
                        try:
                            service.grant_access(job.drive_folder_id, approver, expiry_days)
                            mailer.send_footage_email(db, approver, job.drive_link, request_data, approver=True)
                        except Exception as exc:
                            errors.append(f"Approver email: {exc}")
            except Exception as exc:
                errors.append(f"Sharing failed: {exc}")

        # --- Finish -----------------------------------------------------
        fresh = {
            "files": [
                {
                    "filename": os.path.basename(entry["path"]),
                    "size_bytes": os.path.getsize(entry["path"]) if os.path.exists(entry["path"]) else 0,
                    "channel": entry["channel"],
                    "camera": entry["camera_name"],
                    "nvr": entry["nvr_name"],
                    "compression": entry.get("compression"),
                }
                for entry in downloaded
            ],
            "cameras": camera_rows,
            "drive": drive_result,
            "errors": errors,
        }
        merged = (
            merge_retry_result(previous_result, fresh, retry_keys)
            if retry_keys
            else fresh
        )
        merged_cameras = merged.get("cameras") or []
        failed_cameras = [row for row in merged_cameras if row.get("status") == "failed"]
        warnings_list = merged.get("errors") or []
        ready_count = len(
            [row for row in merged_cameras if row.get("status") == "success"]
        ) or len(merged.get("files") or [])
        if failed_cameras and ready_count == 0:
            job.status = "failed"
        elif failed_cameras or warnings_list:
            job.status = "partial"
        else:
            job.status = "completed"
        job.progress = 100
        if retry_keys:
            job.message = (
                f"Retry: {len(downloaded)} camera(s) recovered · "
                f"{len(failed_cameras)} still failing"
                if failed_cameras
                else f"Retry: {len(downloaded)} camera(s) recovered"
            )
        else:
            job.message = (
                f"{len(downloaded)} camera(s) processed"
                + (f" · {len(failed_cameras)} failed" if failed_cameras else "")
                + (f" · {len(warnings_list)} warning(s)" if warnings_list else "")
            )
        job.finished_at = _now()
        if job.status == "failed":
            job.error = (
                _failed_camera_message(failed_cameras)
                or "; ".join(warnings_list)
                or "Job failed"
            )[:4000]
        else:
            job.error = None
        job.result_json = json.dumps(merged)
        job.retry_json = None
        db.commit()
        detail = f"{job.ref} · {job.folder_name}"
        if failed_cameras:
            detail += f" · {len(failed_cameras)} failed: {failed_cameras[0].get('message')}"
        elif warnings_list:
            detail += f" · {len(warnings_list)} warning(s): {warnings_list[0]}"
        audit(
            db,
            job.user,
            "job_partial"
            if job.status == "partial"
            else ("job_failed" if job.status == "failed" else "job_completed"),
            detail,
            level=(
                "error"
                if job.status == "failed"
                else ("warning" if job.status == "partial" else "success")
            ),
        )

        if payload.get("notify_telegram"):
            lines = [
                f"✅ <b>Footage ready</b> · {telegram.esc(job.folder_name)}",
                f"📹 {len(downloaded)} camera(s)",
            ]
            if job.drive_link:
                lines.append(f'🔗 <a href="{job.drive_link}">Open Google Drive folder</a>')
            if failed_cameras:
                lines.append(
                    f"⚠️ {len(failed_cameras)} camera(s) still failing: "
                    f"{telegram.esc(str(failed_cameras[0].get('message') or ''))}"
                )
            elif warnings_list:
                lines.append(f"⚠️ {len(warnings_list)} warning(s): {telegram.esc(warnings_list[0])}")
            telegram.send_message(db, "\n".join(lines))
    except Exception as exc:
        try:
            if job:
                job.status = "failed"
                job.error = str(exc)[:4000]
                job.message = str(exc)[:590]
                job.retry_json = None
                job.finished_at = _now()
                db.commit()
                audit(
                    db,
                    job.user,
                    "job_failed",
                    f"{job.ref} · {job.folder_name} · {str(exc)[:1000]}",
                    level="error",
                )
                if job.telegram_chat_id:
                    telegram.send_message(
                        db,
                        f"❌ <b>Job failed</b> · {telegram.esc(job.folder_name)}\n{telegram.esc(str(exc)[:500])}",
                        chat_id=job.telegram_chat_id,
                    )
        except Exception:
            pass
    finally:
        if work_dir and os.path.isdir(work_dir):
            try:
                # Keep files for 7 days only if the job produced nothing usable.
                if not os.listdir(work_dir):
                    os.rmdir(work_dir)
            except OSError:
                pass
        _cancel_events.pop(job_id, None)
        db.close()


def _finish_cancelled(db: Session, job: models.Job) -> None:
    job.status = "cancelled"
    job.message = "Cancelled"
    job.finished_at = _now()
    db.commit()
