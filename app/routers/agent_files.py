"""Serve finished agent files directly from the campus host.

Only the agent container has ``AGENT_TOKEN`` set, so on the main host this
router always answers 404.  On the agent, a signed URL issued by the main host
is verified before anything is opened.  Starlette's FileResponse provides
``Accept-Ranges`` and single/multi-range responses, which lets browsers and
download managers resume a large MP4 instead of starting over.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..config import AGENT_FILES_DIR, AGENT_TOKEN
from ..services.download_links import verify_download

router = APIRouter(prefix="/agent-files", tags=["agent-files"])

_CHUNK_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".ts"}


def _job_dir(job_id: int):
    return AGENT_FILES_DIR / f"job_{int(job_id)}"


@router.api_route("/{job_id}/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
def download_agent_file(
    job_id: int,
    filename: str,
    request: Request,
    expires: int = Query(...),
    sig: str = Query(...),
):
    # The main host has no AGENT_TOKEN, so it never serves agent files itself.
    if not AGENT_TOKEN:
        raise HTTPException(status_code=404, detail="Agent file serving is not enabled")

    if expires < int(time.time()):
        raise HTTPException(status_code=410, detail="This download link has expired")

    if not verify_download(AGENT_TOKEN, job_id, filename, expires, sig):
        raise HTTPException(status_code=403, detail="Invalid download signature")

    # The signature covers the exact filename.  Reject anything that could
    # escape the per-job directory even if a signature was somehow forged.
    if (
        not filename
        or filename in {".", ".."}
        or filename != os.path.basename(filename)
        or "/" in filename
        or "\\" in filename
    ):
        raise HTTPException(status_code=404, detail="File not found")

    job_dir = _job_dir(job_id)
    try:
        root = job_dir.resolve()
        target = (job_dir / filename).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Keep the retention clock fresh while a large file is being downloaded so
    # cleanup cannot remove a job directory mid-transfer.
    try:
        os.utime(target, None)
    except OSError:
        pass

    suffix = target.suffix.lower()
    media_type = "video/mp4" if suffix in _CHUNK_SUFFIXES else "application/octet-stream"
    headers = {
        "Cache-Control": "private, max-age=0, no-store",
        "X-Content-Type-Options": "nosniff",
        # Ask nginx and similar proxies not to buffer a multi-gigabyte response.
        "X-Accel-Buffering": "no",
    }
    return FileResponse(
        str(target),
        media_type=media_type,
        filename=filename,
        headers=headers,
    )
