from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .config import BASE_DIR, PUBLIC_URL
from .database import get_db
from .security import is_admin, read_session_token
from .settings_service import get_setting, public_config

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _asset_version() -> str:
    """Cache-buster so a rebuilt image never serves stale JS/CSS."""
    try:
        js = (BASE_DIR / "app" / "static" / "js" / "app.js").stat().st_mtime_ns
        css = (BASE_DIR / "app" / "static" / "css" / "app.css").stat().st_mtime_ns
        return str(max(js, css))
    except OSError:
        return "3"


ASSET_VERSION = _asset_version()


def _optional_user(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get("cctv_session")
    if not token:
        return None
    data = read_session_token(token)
    if not data or not data.get("uid"):
        return None
    user = db.get(models.User, int(data["uid"]))
    if not user or not user.is_active:
        return None
    return user


def _context(request: Request, db: Session, user: models.User | None, page: str, **extra):
    ctx = {
        "request": request,
        "user": user,
        "page": page,
        "config": public_config(db),
        "public_url": get_setting(db, "app.public_url") or PUBLIC_URL,
        "is_admin": is_admin(user),
        "is_superadmin": bool(user and user.role == "superadmin"),
        "asset_version": ASSET_VERSION,
    }
    ctx.update(extra)
    return ctx


def _require_page(request: Request, db: Session, page: str):
    user = _optional_user(request, db)
    if not user:
        return None, RedirectResponse(url="/login", status_code=303)
    return user, None


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if _optional_user(request, db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _context(request, db, None, "login"))


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "dashboard")
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "dashboard.html", _context(request, db, user, "dashboard"))


@router.get("/footage", response_class=HTMLResponse)
def footage_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "footage")
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "footage.html", _context(request, db, user, "footage"))


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "jobs")
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "jobs.html", _context(request, db, user, "jobs"))


@router.get("/nvrs", response_class=HTMLResponse)
def nvrs_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "nvrs")
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "nvrs.html", _context(request, db, user, "nvrs"))


@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "integrations")
    if redirect:
        return redirect
    if not is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "integrations.html", _context(request, db, user, "integrations"))


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "users")
    if redirect:
        return redirect
    if not is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "users.html", _context(request, db, user, "users"))


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "settings")
    if redirect:
        return redirect
    if not is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "settings.html", _context(request, db, user, "settings"))


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "alerts")
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "alerts.html", _context(request, db, user, "alerts"))


@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, db: Session = Depends(get_db)):
    user, redirect = _require_page(request, db, "agents")
    if redirect:
        return redirect
    if not is_admin(user):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "agents.html", _context(request, db, user, "agents"))


@router.get("/miniapp", response_class=HTMLResponse)
def miniapp_page(request: Request, db: Session = Depends(get_db)):
    # Telegram supplies initData to the JS; browser sessions are also accepted.
    return templates.TemplateResponse(request, "miniapp.html", _context(request, db, None, "miniapp"))
