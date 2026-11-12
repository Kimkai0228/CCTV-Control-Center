"""Low-level Telegram Bot API helpers (notifications, MiniApp buttons)."""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

import requests
from sqlalchemy.orm import Session

from ..settings_service import as_bool, get_setting


def _hhmm(value: Any, fallback: str) -> tuple[int, int] | None:
    """Parse a HH:MM setting without letting bad data break notifications."""
    raw = str(value or "").strip()
    for candidate in (raw, fallback):
        try:
            hour, minute = candidate.split(":", 1)
            hour_n, minute_n = int(hour), int(minute)
        except (TypeError, ValueError):
            continue
        if 0 <= hour_n <= 23 and 0 <= minute_n <= 59:
            return hour_n, minute_n
    return None


def alert_muted(db: Session, now: dt.datetime | None = None) -> bool:
    """True when quiet hours are enabled and Telegram alerts are paused.

    Alerts are still written to the WebUI by the health checker before this
    function is consulted; only the outbound Telegram message is suppressed.
    """
    if not as_bool(get_setting(db, "telegram.alert_mute_enabled"), False):
        return False
    start = _hhmm(get_setting(db, "telegram.alert_mute_start"), "17:00")
    end = _hhmm(get_setting(db, "telegram.alert_mute_end"), "07:00")
    if start is None or end is None or start == end:
        return False

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(get_setting(db, "app.timezone") or "UTC")
    except Exception:
        tz = dt.timezone.utc
    current = now or dt.datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    minute = current.hour * 60 + current.minute
    start_minute = start[0] * 60 + start[1]
    end_minute = end[0] * 60 + end[1]
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    # Overnight window such as 17:00 → 07:00.
    return minute >= start_minute or minute < end_minute


def get_token(db: Session) -> str:
    return get_setting(db, "telegram.bot_token").strip()


def get_group_id(db: Session) -> str:
    return get_setting(db, "telegram.group_id").strip()


def get_alert_chat(db: Session) -> str:
    return get_setting(db, "telegram.alert_chat_id").strip() or get_group_id(db)


def configured(db: Session) -> bool:
    return bool(get_token(db) and get_alert_chat(db))


def api_call(db: Session, method: str, payload: dict[str, Any] | None = None, timeout: int = 15) -> tuple[bool, Any]:
    token = get_token(db)
    if not token:
        return False, "Telegram bot token is not configured"
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        data = response.json()
    except Exception as exc:
        return False, str(exc)
    if not data.get("ok"):
        return False, data.get("description") or f"Telegram error {response.status_code}"
    return True, data.get("result")


def send_message(
    db: Session,
    text: str,
    chat_id: str | None = None,
    *,
    parse_mode: str = "HTML",
    reply_markup: dict[str, Any] | None = None,
    disable_preview: bool = True,
) -> tuple[bool, Any]:
    target = chat_id or get_alert_chat(db)
    if not target:
        return False, "No Telegram chat configured"
    payload: dict[str, Any] = {
        "chat_id": target,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api_call(db, "sendMessage", payload)


def send_alert(db: Session, text: str, *, chat_id: str | None = None) -> tuple[bool, Any]:
    if not as_bool(get_setting(db, "telegram.alerts_enabled"), True):
        return False, "Telegram alerts are disabled"
    if alert_muted(db):
        return False, "Telegram alerts are paused during quiet hours"
    return send_message(db, text, chat_id or get_alert_chat(db))


def send_webapp_button(db: Session, url: str, text: str, chat_id: str | None = None, label: str = "Open CCTV MiniApp") -> tuple[bool, Any]:
    markup = {"inline_keyboard": [[{"text": label, "web_app": {"url": url}}]]}
    return send_message(db, text, chat_id or get_group_id(db), reply_markup=markup)


def send_test(db: Session, chat_id: str | None = None) -> tuple[bool, str]:
    token = get_token(db)
    if not token:
        return False, "Bot token is empty"
    ok, result = api_call(db, "getMe", {})
    if not ok:
        return False, f"getMe failed: {result}"
    username = result.get("username") if isinstance(result, dict) else "bot"
    ok, message = send_message(db, f"✅ <b>CCTV V3 connected</b>\nBot @{username} can send alerts here.", chat_id)
    if not ok:
        return False, f"Message failed: {message}"
    return True, f"Connected as @{username}"


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def format_alert(title: str, lines: list[str], severity: str = "warning") -> str:
    icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(severity, "⚠️")
    body = "\n".join(lines)
    return f"{icon} <b>{esc(title)}</b>\n\n{body}"
