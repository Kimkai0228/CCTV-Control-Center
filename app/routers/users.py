from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import (
    audit,
    can_manage_user,
    get_current_user,
    hash_password,
    is_admin,
    require_roles,
    validate_email,
    validate_username,
)
from .auth import user_dict

router = APIRouter(prefix="/api/users", tags=["users"])

VALID_ROLES = {"admin", "operator", "viewer"}


def _admin(user: models.User = Depends(require_roles("superadmin", "admin"))) -> models.User:
    return user


@router.get("")
def list_users(user: models.User = Depends(_admin), db: Session = Depends(get_db)):
    rows = db.query(models.User).order_by(models.User.created_at.asc()).all()
    return {"users": [user_dict(row) for row in rows]}


@router.post("")
def create_user(
    payload: schemas.UserCreate,
    request: Request,
    actor: models.User = Depends(_admin),
    db: Session = Depends(get_db),
):
    username = (payload.username or "").strip()
    if not validate_username(username):
        raise HTTPException(status_code=400, detail="Username must be 3-40 characters (letters, numbers, . _ -)")
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    if actor.role == "admin" and payload.role == "admin":
        raise HTTPException(status_code=403, detail="Only a super admin can create admins")
    if not validate_email(payload.email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    if len(payload.password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(status_code=409, detail="Username already exists")

    new_user = models.User(
        username=username,
        email=(payload.email or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=payload.role,
        telegram_user_id=(payload.telegram_user_id or "").strip() or None,
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    audit(
        db,
        actor,
        "user_created",
        f"Created {username} with role {payload.role}",
        request.client.host if request.client else None,
    )
    return {"user": user_dict(new_user)}


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    payload: schemas.UserUpdate,
    request: Request,
    actor: models.User = Depends(_admin),
    db: Session = Depends(get_db),
):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(actor, target):
        raise HTTPException(status_code=403, detail="You cannot modify this account")
    if payload.role is not None:
        if payload.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="Invalid role")
        if actor.role == "admin" and payload.role == "admin":
            raise HTTPException(status_code=403, detail="Only a super admin can assign admin")
        target.role = payload.role
    if payload.email is not None:
        if not validate_email(payload.email):
            raise HTTPException(status_code=400, detail="Invalid email address")
        target.email = payload.email.strip() or None
    if payload.is_active is not None:
        if target.id == actor.id and not payload.is_active:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        target.is_active = payload.is_active
    if payload.telegram_user_id is not None:
        target.telegram_user_id = payload.telegram_user_id.strip() or None
    db.commit()
    db.refresh(target)
    audit(db, actor, "user_updated", f"Updated {target.username}", request.client.host if request.client else None)
    return {"user": user_dict(target)}


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: schemas.PasswordReset,
    request: Request,
    actor: models.User = Depends(_admin),
    db: Session = Depends(get_db),
):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not can_manage_user(actor, target):
        raise HTTPException(status_code=403, detail="You cannot modify this account")
    if len(payload.password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    target.password_hash = hash_password(payload.password)
    db.commit()
    audit(db, actor, "password_reset", f"Reset password for {target.username}", request.client.host if request.client else None)
    return {"ok": True, "message": f"Password updated for {target.username}"}


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    request: Request,
    actor: models.User = Depends(_admin),
    db: Session = Depends(get_db),
):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if target.role == "superadmin":
        raise HTTPException(status_code=403, detail="The bootstrap super admin cannot be deleted")
    if not is_admin(actor):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    username = target.username
    db.delete(target)
    db.commit()
    audit(db, actor, "user_deleted", f"Deleted {username}", request.client.host if request.client else None)
    return {"ok": True}
