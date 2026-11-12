"""Remote campus agent runner.

Enabled with ``RUN_AGENT=true`` on a lightweight container.  The worker polls
the main host over HTTPS, downloads footage from NVRs on its own LAN, compresses
it locally, uploads to Google Drive and posts progress back.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace
from typing import Any

import requests

from .config import (
    AGENT_CAMPUS,
    AGENT_FILES_DIR,
    AGENT_KEEP_FILES,
    AGENT_KEEP_FILES_DAYS,
    AGENT_MAIN_URL,
    AGENT_NAME,
    AGENT_POLL_SECONDS,
    AGENT_TOKEN,
    AGENT_UPLOAD_CHUNK_MB,
    AGENT_UPLOAD_RETRIES,
    AGENT_VERIFY_TLS,
    AGENT_WORKERS,
    WORK_DIR,
)
from .services import drive
from .services.compressor import compress_file
from .services.downloader import download_camera, parse_boundary, safe_filename

_stop = threading.Event()
_thread: threading.Thread | None = None
_pool: ThreadPoolExecutor | None = None
_active: set[int] = set()
_lock = threading.Lock()
_keep_days = AGENT_KEEP_FILES_DAYS
_last_cleanup = 0.0
_CLEANUP_INTERVAL = 10 * 60


def _apply_server_settings(data: dict[str, Any]) -> None:
    global _keep_days
    try:
        days = int(data.get("keep_files_days") or 0)
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        _keep_days = max(1, min(365, days))


def _cleanup_results(force: bool = False) -> None:
    """Delete direct-download job folders older than the main-host retention."""
    global _last_cleanup
    now = time.time()
    if not force and now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - max(1, _keep_days) * 86400
    try:
        folders = list(AGENT_FILES_DIR.glob("job_*"))
    except OSError:
        return
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
            newest = folder.stat().st_mtime
            for item in folder.rglob("*"):
                if item.is_file():
                    newest = max(newest, item.stat().st_mtime)
            if newest < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                print(f"[agent] removed expired download folder {folder.name}")
        except OSError:
            continue


def configured() -> bool:
    return bool(AGENT_MAIN_URL and AGENT_TOKEN)


def _headers() -> dict[str, str]:
    return {"X-Agent-Token": AGENT_TOKEN, "Accept": "application/json"}


def _api(method: str, path: str, *, json_body: Any = None, timeout: float = 30.0) -> Any:
    url = f"{AGENT_MAIN_URL}{path}"
    response = requests.request(
        method,
        url,
        json=json_body,
        headers=_headers(),
        timeout=timeout,
        verify=AGENT_VERIFY_TLS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
    if not response.content:
        return {}
    return response.json()


def _post_progress(job_id: int, **data: Any) -> dict[str, Any]:
    try:
        return _api("POST", f"/api/agents/jobs/{job_id}/progress", json_body=data)
    except Exception as exc:
        print(f"[agent] progress update failed: {exc}")
        return {}


class _UploadCancelled(Exception):
    """Raised when the operator cancels while a file is uploading."""


def _upload_result(job_id: int, entry: dict[str, Any], on_progress=None) -> dict[str, Any]:
    """Upload a finished file in resumable chunks.

    A single multi-hundred-megabyte POST can be dropped by a WAN link or a
    reverse proxy. Sending small sequential chunks lets each retry resume from
    the last confirmed offset instead of starting the whole file again.
    """
    path = entry["path"]
    filename = os.path.basename(path)
    size = os.path.getsize(path)
    chunk_size = AGENT_UPLOAD_CHUNK_MB * 1024 * 1024
    offset = 0
    response = None

    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            final = offset + len(chunk) >= size
            data = {
                "offset": str(offset),
                "total": str(size),
                "final": "true" if final else "false",
                "channel": str(entry.get("channel") or ""),
                "camera": str(entry.get("camera") or ""),
                "nvr_id": str(entry.get("nvr_id") or ""),
            }
            last_error: Exception | None = None
            for attempt in range(1, AGENT_UPLOAD_RETRIES + 1):
                try:
                    response = requests.post(
                        f"{AGENT_MAIN_URL}/api/agents/jobs/{job_id}/files/chunk",
                        headers=_headers(),
                        files={"file": (filename, chunk, "video/mp4")},
                        data=data,
                        timeout=(30, 600),
                        verify=AGENT_VERIFY_TLS,
                    )
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"chunk @{offset} -> {response.status_code}: {response.text[:300]}"
                        )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= AGENT_UPLOAD_RETRIES:
                        raise
                    wait = min(20, 2 ** attempt)
                    print(
                        f"[agent] upload retry {attempt}/{AGENT_UPLOAD_RETRIES - 1} "
                        f"for {filename} @{offset}: {exc} (waiting {wait}s)"
                    )
                    time.sleep(wait)
            if last_error is not None:
                raise last_error
            offset += len(chunk)
            if on_progress:
                state = on_progress(offset, size)
                if state and state.get("cancel"):
                    raise _UploadCancelled()

    if response is None:
        # Zero-byte file: still create the row so the UI shows the result.
        response = requests.post(
            f"{AGENT_MAIN_URL}/api/agents/jobs/{job_id}/files/chunk",
            headers=_headers(),
            files={"file": (filename, b"", "video/mp4")},
            data={
                "offset": "0",
                "total": "0",
                "final": "true",
                "channel": str(entry.get("channel") or ""),
                "camera": str(entry.get("camera") or ""),
                "nvr_id": str(entry.get("nvr_id") or ""),
            },
            timeout=(30, 300),
            verify=AGENT_VERIFY_TLS,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"upload -> {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else {}


def _as_nvr(data: dict[str, Any]) -> SimpleNamespace:
    # download_camera() calls decrypt_secret(nvr.password_enc); plain text is
    # returned unchanged when Fernet decryption fails, which is exactly what we
    # want for credentials delivered over the authenticated agent channel.
    return SimpleNamespace(
        id=data.get("id"),
        name=data.get("name") or "NVR",
        code=data.get("code") or "NVR",
        campus=data.get("campus"),
        zone=data.get("zone"),
        protocol=data.get("protocol") or "hikvision",
        host=data.get("host"),
        port=int(data.get("port") or 80),
        sdk_port=int(data.get("sdk_port") or 8000),
        username=data.get("username"),
        password_enc=data.get("password") or "",
        channel_offset=int(data.get("channel_offset") or 0),
        track_multiplier=int(data.get("track_multiplier") or 100),
        time_offset_hours=float(data.get("time_offset_hours") or 0),
    )


def _run_job(payload: dict[str, Any]) -> None:
    global _keep_days
    job_id = int(payload["job_id"])
    ref = payload.get("ref") or str(job_id)
    folder_name = payload.get("folder_name") or "CCTV Request"
    request_payload = payload.get("request") or {}
    entries_payload = payload.get("cameras") or []
    settings = payload.get("settings") or {}
    drive_payload = payload.get("drive") or {}
    download_settings = payload.get("download") or {}
    direct_download = bool(download_settings.get("direct"))
    try:
        keep_days = int(download_settings.get("keep_days") or 0)
    except (TypeError, ValueError):
        keep_days = 0
    if keep_days > 0:
        _keep_days = max(1, min(365, keep_days))
    work_dir = WORK_DIR / f"agent_{job_id}_{safe_filename(ref, str(job_id))}"
    errors: list[str] = []
    failed_rows: list[dict[str, Any]] = []

    try:
        if payload.get("cancel_requested"):
            _post_progress(job_id, status="cancelled", progress=0, message="Cancelled before start")
            return
        os.makedirs(work_dir, exist_ok=True)
        start_dt = parse_boundary(request_payload["date"], request_payload["start_time"])
        end_dt = parse_boundary(request_payload["date"], request_payload["end_time"])
        total = max(1, len(entries_payload))
        downloaded: list[dict[str, Any]] = []
        cancel_state = {"cancel": False}
        # Same naming as the main host, so files never overwrite each other
        # when two NVRs share a channel number.
        multiple_nvrs = len({(e.get("nvr") or {}).get("id") for e in entries_payload}) > 1
        used_names: set[str] = set()

        for index, item in enumerate(entries_payload):
            nvr_data = item.get("nvr") or {}
            nvr = _as_nvr(nvr_data)
            channel = int(item.get("channel"))
            prefix = f"{folder_name}_{nvr.code}" if multiple_nvrs else folder_name
            base_name = safe_filename(f"{prefix}_CH{channel}", f"CH{channel}")
            candidate = f"{base_name}.mp4"
            counter = 2
            while candidate.lower() in used_names:
                candidate = f"{base_name}_{counter}.mp4"
                counter += 1
            used_names.add(candidate.lower())
            path = str(work_dir / candidate)
            state = _post_progress(
                job_id,
                status="running",
                progress=3 + int((index / total) * 42),
                message=f"Downloading {item.get('name') or ('CH' + str(channel))} from {nvr.name}",
            )
            if state.get("cancel"):
                _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                return

            def progress(value: int, text: str, index=index) -> None:
                state = _post_progress(
                    job_id,
                    status="running",
                    progress=3 + int(((index + value / 100) / total) * 42),
                    message=text,
                )
                if state.get("cancel"):
                    cancel_state["cancel"] = True

            ok, message = download_camera(nvr, channel, start_dt, end_dt, path, progress)
            if cancel_state["cancel"]:
                _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                return
            if ok and os.path.exists(path) and os.path.getsize(path) > 0:
                downloaded.append(
                    {
                        "path": path,
                        "channel": channel,
                        "camera": item.get("name") or f"Channel {channel}",
                        "nvr": nvr.name,
                        "nvr_id": nvr.id,
                    }
                )
            else:
                failed_rows.append(
                    {
                        "nvr_id": nvr.id,
                        "nvr": nvr.name,
                        "channel": channel,
                        "camera": item.get("name") or f"Channel {channel}",
                        "status": "failed",
                        "message": message or "Download failed",
                        "filename": None,
                        "size_bytes": None,
                    }
                )

        def _camera_rows() -> list[dict[str, Any]]:
            rows = [
                {
                    "nvr_id": e.get("nvr_id"),
                    "nvr": e.get("nvr"),
                    "channel": e.get("channel"),
                    "camera": e.get("camera"),
                    "status": "success",
                    "message": (e.get("compression") or {}).get("message") or "Ready",
                    "filename": os.path.basename(e["path"]),
                    "size_bytes": (
                        os.path.getsize(e["path"]) if os.path.exists(e["path"]) else 0
                    ),
                }
                for e in downloaded
            ]
            return rows + failed_rows

        if not downloaded:
            failure = " | ".join(
                f"{row['nvr']} CH{row['channel']}: {row['message']}"
                for row in failed_rows[-3:]
            ) or "All camera downloads failed"
            _post_progress(
                job_id,
                status="failed",
                progress=100,
                message="All selected camera downloads failed",
                error=failure[:4000],
                result={"files": [], "cameras": failed_rows, "errors": errors},
            )
            return

        state = _post_progress(job_id, status="running", progress=45, message="Compressing locally")
        if state.get("cancel"):
            _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
            return
        if str(settings.get("compression.enabled", "true")).lower() in {"1", "true", "yes", "on"}:
            workers = max(1, min(4, int(settings.get("compression.max_parallel") or 2)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-compress") as pool:
                futures = {pool.submit(compress_file, e["path"], settings): e for e in downloaded}
                done = 0
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        result = future.result()
                        if result.compressed and result.path != entry["path"]:
                            try:
                                if os.path.exists(entry["path"]):
                                    os.remove(entry["path"])
                                os.replace(result.path, entry["path"])
                            except OSError:
                                entry["path"] = result.path
                        entry["compression"] = {
                            "backend": result.backend,
                            "compressed": result.compressed,
                            "message": result.message,
                            "original_size": result.original_size,
                            "final_size": result.final_size,
                        }
                    except Exception as exc:
                        errors.append(f"Compression {entry['camera']}: {exc}")
                    done += 1
                    _post_progress(
                        job_id,
                        status="running",
                        progress=45 + int((done / len(downloaded)) * 28),
                        message=f"Compressed {done}/{len(downloaded)}",
                    )

        drive_result: dict[str, Any] | None = None
        if drive_payload.get("enabled"):
            state = _post_progress(job_id, status="running", progress=76, message="Uploading to Google Drive")
            if state.get("cancel"):
                _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                return
            try:
                service = drive.DriveService.from_credentials_json(
                    drive_payload.get("service_account_json") or "",
                    drive_payload.get("impersonate_email") or "",
                )
                drive_result = drive.upload_job_files(
                    None,  # type: ignore[arg-type]
                    [{"path": e["path"], "filename": os.path.basename(e["path"])} for e in downloaded],
                    folder_name,
                    None,
                    None,
                    progress=lambda pct, text: _post_progress(
                        job_id, status="running", progress=76 + int(pct * 0.20), message=text
                    ),
                    service=service,
                    parent_id=drive_payload.get("parent_id"),
                    existing_folder_id=drive_payload.get("folder_id"),
                    existing_folder_link=drive_payload.get("folder_link"),
                )
            except Exception as exc:
                errors.append(f"Drive upload failed: {exc}")

        # --- Either keep files here for direct download, or upload them ----
        if direct_download:
            state = _post_progress(
                job_id,
                status="running",
                progress=96,
                message="Preparing direct download from agent",
            )
            if state.get("cancel"):
                _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                return
            try:
                results_dir = AGENT_FILES_DIR / f"job_{job_id}"
                os.makedirs(results_dir, exist_ok=True)
                for entry in downloaded:
                    target = results_dir / os.path.basename(entry["path"])
                    if os.path.abspath(target) != os.path.abspath(entry["path"]):
                        if os.path.exists(target):
                            os.remove(target)
                        shutil.move(entry["path"], target)
                    entry["path"] = str(target)
                    entry["agent_file"] = True
                _post_progress(
                    job_id,
                    status="running",
                    progress=99,
                    message="Files are ready for direct download",
                )
            except Exception as exc:
                errors.append(f"Store for agent download: {exc}")
                # Fall back to copying to the main host so the job is still
                # downloadable if the local results directory is unavailable.
                direct_download = False

        if not direct_download:
            state = _post_progress(
                job_id, status="running", progress=96, message="Uploading files to the main host"
            )
            if state.get("cancel"):
                _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                return
            for index, entry in enumerate(downloaded):
                try:
                    def chunk_progress(uploaded, total, current=index):
                        pct = 96 + int(
                            ((current + (uploaded / max(1, total))) / max(1, len(downloaded))) * 3
                        )
                        return _post_progress(
                            job_id,
                            status="running",
                            progress=min(99, pct),
                            message=(
                                f"Uploading {current + 1}/{len(downloaded)} · "
                                f"{int((uploaded / max(1, total)) * 100)}%"
                            ),
                        )

                    info = _upload_result(job_id, entry, chunk_progress)
                    stored = info.get("file") or {}
                    entry["main_file_id"] = stored.get("id")
                    entry["download_url"] = stored.get("download_url")
                except _UploadCancelled:
                    _post_progress(job_id, status="cancelled", progress=0, message="Cancelled by operator")
                    return
                except Exception as exc:
                    errors.append(f"Copy to main host {entry['camera']}: {exc}")
                _post_progress(
                    job_id,
                    status="running",
                    progress=min(99, 96 + int(((index + 1) / max(1, len(downloaded))) * 3)),
                    message=f"Uploaded {index + 1}/{len(downloaded)} file(s) to main host",
                )

        result = {
            "files": [
                {
                    "filename": os.path.basename(e["path"]),
                    "size_bytes": os.path.getsize(e["path"]) if os.path.exists(e["path"]) else 0,
                    "channel": e["channel"],
                    "camera": e["camera"],
                    "nvr": e["nvr"],
                    "compression": e.get("compression"),
                    "file_id": e.get("main_file_id"),
                    "download_url": e.get("download_url"),
                    "agent_file": bool(e.get("agent_file")),
                }
                for e in downloaded
            ],
            "cameras": _camera_rows(),
            "drive": drive_result,
            "errors": errors,
        }
        status = "completed" if not errors and not failed_rows else "partial"
        _post_progress(
            job_id,
            status=status,
            progress=100,
            message=f"{len(downloaded)} camera(s) processed" + (f" · {len(errors)} warning(s)" if errors else ""),
            drive_link=(drive_result or {}).get("folder_link"),
            drive_folder_id=(drive_result or {}).get("folder_id"),
            result=result,
            error="; ".join(errors[:5]) if errors else None,
        )
    except Exception as exc:
        _post_progress(job_id, status="failed", progress=0, message=str(exc)[:590], error=str(exc))
    finally:
        if not AGENT_KEEP_FILES:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass
        with _lock:
            _active.discard(job_id)


def _poll_once() -> bool:
    """Claim and start one job. Returns True when a job was received."""
    data = _api("GET", "/api/agents/jobs/next", timeout=40)
    job = data.get("job")
    if not job:
        return False
    job_id = int(job["job_id"])
    with _lock:
        if job_id in _active:
            return False
        _active.add(job_id)
    assert _pool is not None
    _pool.submit(_run_job, job)
    return True


def _loop() -> None:
    print(f"[agent] polling {AGENT_MAIN_URL} as {AGENT_NAME or AGENT_CAMPUS or 'agent'}")
    while not _stop.is_set():
        try:
            heartbeat = _api(
                "POST",
                "/api/agents/heartbeat",
                json_body={
                    "name": AGENT_NAME,
                    "campus": AGENT_CAMPUS,
                    "version": "v3",
                },
                timeout=20,
            )
            _apply_server_settings(heartbeat or {})
            # Drain the queue until empty.
            while not _stop.is_set() and _poll_once():
                pass
        except Exception as exc:
            print(f"[agent] poll failed: {exc}")
        _cleanup_results()
        _stop.wait(max(5, AGENT_POLL_SECONDS))


def start_agent_runner() -> bool:
    global _thread, _pool
    if not configured():
        print("[agent] AGENT_MAIN_URL / AGENT_TOKEN missing; agent runner not started")
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _cleanup_results(force=True)
    _pool = ThreadPoolExecutor(max_workers=max(1, AGENT_WORKERS), thread_name_prefix="agent-job")
    _thread = threading.Thread(target=_loop, name="campus-agent", daemon=True)
    _thread.start()
    return True


def stop_agent_runner() -> None:
    global _thread, _pool
    _stop.set()
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=False)
    _pool = None
    _thread = None
