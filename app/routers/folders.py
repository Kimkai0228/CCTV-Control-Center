from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import audit, get_current_user, require_roles

router = APIRouter(prefix="/api/folders", tags=["folders"])
ADMIN = require_roles("superadmin", "admin")


def folder_dict(folder: models.DriveFolder) -> dict:
    return {
        "id": folder.id,
        "key": folder.key,
        "name": folder.name,
        "folder_id": folder.folder_id,
        "campus": folder.campus,
        "enabled": folder.enabled,
    }


@router.get("")
def list_folders(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.DriveFolder).order_by(models.DriveFolder.key).all()
    return {"folders": [folder_dict(row) for row in rows]}


@router.post("")
def create_folder(
    payload: schemas.FolderInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    key = re.sub(r"[^A-Za-z0-9_]+", "_", payload.key.strip().upper())
    if not key:
        raise HTTPException(status_code=400, detail="Folder key is required")
    if db.query(models.DriveFolder).filter(models.DriveFolder.key == key).first():
        raise HTTPException(status_code=409, detail="Folder key already exists")
    folder = models.DriveFolder(
        key=key,
        name=payload.name.strip() or key.replace("_", " ").title(),
        folder_id=payload.folder_id.strip(),
        campus=(payload.campus or key.split("_")[0]).lower(),
        enabled=payload.enabled,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    audit(db, user, "folder_created", key, request.client.host if request.client else None)
    return {"folder": folder_dict(folder)}


@router.patch("/{folder_id}")
def update_folder(
    folder_id: int,
    payload: schemas.FolderInput,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    folder = db.get(models.DriveFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    folder.name = payload.name.strip() or folder.name
    folder.folder_id = payload.folder_id.strip() or folder.folder_id
    folder.campus = (payload.campus or folder.campus or "").lower() or None
    folder.enabled = payload.enabled
    db.commit()
    db.refresh(folder)
    audit(db, user, "folder_updated", folder.key, request.client.host if request.client else None)
    return {"folder": folder_dict(folder)}


@router.delete("/{folder_id}")
def delete_folder(
    folder_id: int,
    request: Request,
    user: models.User = Depends(ADMIN),
    db: Session = Depends(get_db),
):
    folder = db.get(models.DriveFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    key = folder.key
    db.delete(folder)
    db.commit()
    audit(db, user, "folder_deleted", key, request.client.host if request.client else None)
    return {"ok": True}
