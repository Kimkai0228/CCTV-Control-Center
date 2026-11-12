from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import SESSION_COOKIE, SESSION_MAX_AGE
from ..database import get_db
from ..security import (
    audit,
    create_session_token,
    get_current_user,
    hash_password,
    verify_password,
)
from ..settings_service import get_setting, public_config

router = APIRouter(prefix="/api/auth", tags=["auth"])


def user_dict(user: models.User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "telegram_user_id": user.telegram_user_id,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/public-config")
def get_public_config(db: Session = Depends(get_db)):
    return public_config(db)


@router.post("/login")
def login(payload: schemas.LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == payload.username.strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        audit(db, None, "login_failed", f"username={payload.username}", request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account is disabled")

    user.last_login = dt.datetime.now(dt.timezone.utc)
    db.commit()
    token = create_session_token(user)
    # Trust the proxy-forwarded scheme (uvicorn --proxy-headers) instead of the
    # public URL, so local HTTP testing still receives the session cookie.
    secure = request.url.scheme == "https"
    session_max_age = max(1, int(get_setting(db, "security.session_hours", "168") or 168)) * 3600
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=session_max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    audit(db, user, "login", "WebUI login", request.client.host if request.client else None)
    return {"user": user_dict(user)}


@router.post("/logout")
def logout(response: Response, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    response.delete_cookie(SESSION_COOKIE, path="/")
    audit(db, user, "logout")
    return {"ok": True}


@router.get("/me")
def me(user: models.User = Depends(get_current_user)):
    return {"user": user_dict(user)}


@router.post("/change-password")
def change_password(
    payload: schemas.PasswordReset,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if len(payload.password or "") < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user.password_hash = hash_password(payload.password)
    db.commit()
    audit(db, user, "password_changed", "Changed own password")
    return {"ok": True}
