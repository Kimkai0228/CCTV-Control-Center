"""Password hashing, secret encryption, sessions and authorization helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import models
from .config import SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE
from .database import get_db

# ---------------------------------------------------------------------------
# Passwords (PBKDF2-SHA256, no external build dependencies)
# ---------------------------------------------------------------------------

_PBKDF2_ROUNDS = 260_000


def hash_password(password: str) -> str:
    if not password or len(password) < 4:
        raise ValueError("Password must be at least 4 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Secret storage (Fernet, key derived from the app SECRET_KEY)
# ---------------------------------------------------------------------------


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str:
    if value is None:
        value = ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Tolerate plain-text values written by an older deployment.
        return value


# ---------------------------------------------------------------------------
# Signed session cookies
# ---------------------------------------------------------------------------

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="cctv-v3-session")


def create_session_token(user: models.User) -> str:
    return _serializer.dumps({"uid": user.id, "ph": user.password_hash[-20:]})


def read_session_token(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def _token_from_request(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    token = _token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    data = read_session_token(token)
    if not data or "uid" not in data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    user = db.get(models.User, int(data["uid"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled")
    if not hmac.compare_digest(user.password_hash[-20:], str(data.get("ph", ""))):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
    return user


def require_roles(*roles: str):
    allowed = set(roles)

    def _checker(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return _checker


def is_admin(user: models.User | None) -> bool:
    return bool(user and user.role in {"superadmin", "admin"})


def can_manage_user(actor: models.User, target: models.User) -> bool:
    if actor.role == "superadmin":
        return True
    if actor.role == "admin":
        return target.role != "superadmin"
    return False


def validate_email(value: str | None) -> bool:
    if not value:
        return True
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def validate_username(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_.-]{3,40}$", value or ""))


def _audit_level(action: str) -> str:
    """Infer a useful level when a caller does not pass one explicitly."""
    text = (action or "").lower()
    if "fail" in text or "error" in text:
        return "error"
    if any(token in text for token in ("delete", "cancel", "rotate", "reset")):
        return "warning"
    if text == "login" or text == "logout":
        return "info"
    return "success"


def audit(
    db: Session,
    user: models.User | None,
    action: str,
    detail: str = "",
    ip: str | None = None,
    level: str | None = None,
) -> None:
    chosen = level if level in {"success", "info", "warning", "error"} else _audit_level(action)
    db.add(
        models.AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "system",
            action=action,
            level=chosen,
            detail=detail[:4000] if detail else "",
            ip=ip,
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()


def roles_with_access(roles: Iterable[str]) -> str:
    return ",".join(sorted(roles))
