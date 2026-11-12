"""Admin log viewers used by Settings -> Logs & diagnostics.

Two views are exposed:

* ``/api/logs/activity`` reads the persistent audit trail (user actions, job
  lifecycle, failures) so an operator can see what changed and whether it
  succeeded.
* ``/api/logs/system`` tails the rotating log file written by ``app.main`` so
  unexpected exceptions and warnings are available without shell access.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .. import models
from ..config import DATA_DIR
from ..database import get_db
from ..security import require_roles

router = APIRouter(prefix="/api", tags=["logs"])
ADMIN = require_roles("superadmin", "admin")

LOG_FILE = DATA_DIR / "logs" / "cctv.log"
LEVELS = {"success", "info", "warning", "error"}
_LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _tail_text(path, max_bytes: int = 1_500_000) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # The first line may be cut in half; drop it.
        text = text.split("\n", 1)[-1]
    return text


def _line_level(line: str) -> str:
    for token in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        if f"[{token}]" in line:
            return token
    return ""


@router.get("/logs/activity")
def activity_log(
    level: str | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    query = db.query(models.AuditLog)
    wanted = (level or "").strip().lower()
    if wanted and wanted not in {"all", *LEVELS}:
        raise HTTPException(status_code=400, detail="Unknown log level")
    if wanted and wanted != "all":
        if wanted == "info":
            # Rows created before the level column existed have NULL.
            query = query.filter(
                or_(
                    models.AuditLog.level == "info",
                    models.AuditLog.level.is_(None),
                )
            )
        else:
            query = query.filter(models.AuditLog.level == wanted)
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        query = query.filter(
            or_(
                models.AuditLog.action.ilike(like),
                models.AuditLog.detail.ilike(like),
                models.AuditLog.username.ilike(like),
            )
        )
    total = query.count()
    rows = (
        query.order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "entries": [
            {
                "id": row.id,
                "username": row.username or "system",
                "action": row.action,
                "level": row.level or "info",
                "detail": row.detail or "",
                "ip": row.ip,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "total": total,
    }


@router.get("/logs/system")
def system_log(
    level: str | None = None,
    q: str | None = None,
    lines: int = Query(300, ge=10, le=5000),
    user: models.User = Depends(ADMIN),
):
    if not LOG_FILE.exists():
        return {"lines": [], "path": str(LOG_FILE), "size": 0, "missing": True}

    text = _tail_text(LOG_FILE)
    rows = text.splitlines()
    wanted = (level or "").strip().lower()
    if wanted and wanted not in {"all", "info", "warning", "error", "debug"}:
        raise HTTPException(status_code=400, detail="Unknown log level")
    if wanted == "error":
        rows = [line for line in rows if _line_level(line) in {"ERROR", "CRITICAL"}]
    elif wanted == "warning":
        rows = [
            line
            for line in rows
            if _LEVEL_RANK.get(_line_level(line), 0) >= _LEVEL_RANK["WARNING"]
        ]
    elif wanted == "info":
        rows = [
            line
            for line in rows
            if _LEVEL_RANK.get(_line_level(line), 0) >= _LEVEL_RANK["INFO"]
        ]
    elif wanted == "debug":
        rows = list(rows)

    term = (q or "").strip().lower()
    if term:
        rows = [line for line in rows if term in line.lower()]

    selected = rows[-lines:]
    return {
        "lines": selected,
        "path": str(LOG_FILE),
        "size": LOG_FILE.stat().st_size,
        "missing": False,
    }


@router.get("/logs/system/download")
def download_system_log(user: models.User = Depends(ADMIN)):
    if not LOG_FILE.exists():
        raise HTTPException(status_code=404, detail="Log file not created yet")
    return FileResponse(
        str(LOG_FILE),
        media_type="text/plain",
        filename="cctv.log",
        headers={"Cache-Control": "private, no-store"},
    )
