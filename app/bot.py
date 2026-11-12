"""Telegram bot for CCTV V3.

The bot is a thin control surface over the same database and job engine as
the WebUI. /tk keeps the reply-to-request workflow and gains an optional
folder-name argument, while /tkbaboy opens the Telegram MiniApp.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from . import models
from .config import RUN_BOT
from .database import SessionLocal
from .security import audit, decrypt_secret
from .services import jobs as job_service
from .services import mailer, telegram
from .services.drive import DriveService
from .services.health import health_summary
from .settings_service import as_bool, as_int, get_setting

try:  # Imported lazily so the web app still boots if the package is absent.
    import telebot
    from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
except Exception:  # pragma: no cover
    telebot = None
    InlineKeyboardButton = InlineKeyboardMarkup = WebAppInfo = None  # type: ignore

_bot = None
_bot_thread: threading.Thread | None = None
_stop_event = threading.Event()
_bot_lock = threading.Lock()
_bot_generation = 0
_sessions: dict[int, dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _db() -> Session:
    return SessionLocal()


def _group_id(db: Session) -> str:
    return get_setting(db, "telegram.group_id").strip()


def _allowed(message_or_call) -> bool:
    with _db() as db:
        group = _group_id(db)
        if not group:
            return True
        chat = getattr(getattr(message_or_call, "chat", None), "id", None)
        if chat is None:
            chat = getattr(getattr(message_or_call, "message", None), "chat", None)
            chat = getattr(chat, "id", None)
        return str(chat) == str(group)


def _say(chat_id, text: str, markup=None):
    try:
        return _bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as exc:
        print(f"[bot] send failed: {exc}")
        return None


def _edit(chat_id, message_id, text: str, markup=None):
    try:
        return _bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            print(f"[bot] edit failed: {exc}")
        return None


def _answer(call, text: str | None = None, alert: bool = False):
    try:
        _bot.answer_callback_query(call.id, text or "", show_alert=alert)
    except Exception:
        pass


def _menu(rows: list[list[tuple[str, str]]], add_close: bool = True) -> Any:
    markup = InlineKeyboardMarkup()
    for row in rows:
        if not row:
            continue
        markup.row(*[InlineKeyboardButton(label, callback_data=data) for label, data in row])
    if add_close:
        markup.add(InlineKeyboardButton("✖️ Close", callback_data="close"))
    return markup


# ---------------------------------------------------------------------------
# /tk request parsing
# ---------------------------------------------------------------------------


def _parse_request_text(text: str) -> dict[str, Any] | None:
    def grab(label: str) -> str:
        match = re.search(rf"{label}\s*:\s*(.+)", text or "", re.IGNORECASE)
        return match.group(1).strip() if match else ""

    raw_date = grab("Date")
    start = grab("Start Time")
    end = grab("End Time")
    incident = grab("Incident")
    requester = grab("Requester")
    campus = grab("Campus")
    if not (raw_date and start and end):
        return None
    try:
        clean_date = re.sub(r"(?<=\d)(st|nd|rd|th)", "", raw_date)
        parsed = dt.datetime.strptime(clean_date.strip(), "%a %b %d %Y")
        formatted_date = parsed.strftime("%d-%m-%Y")
    except ValueError:
        try:
            parsed = dt.datetime.strptime(raw_date.strip(), "%d-%m-%Y")
            formatted_date = parsed.strftime("%d-%m-%Y")
        except ValueError:
            return None
    return {
        "date": formatted_date,
        "start_time": start,
        "end_time": end,
        "incident": incident or "CCTV_Request",
        "requester": requester or "Unknown",
        "requester_email": requester,
        "campus": (campus or "").lower(),
    }


def _folder_name_for(session: dict[str, Any]) -> str:
    override = (session.get("folder_override") or "").strip()
    if override:
        return override
    incident = re.sub(r'[\\/*?:"<>|]', "", session.get("incident") or "CCTV_Request").strip()
    return f"{incident} {session.get('date') or ''}".strip()


def _campus_menu(session: dict[str, Any] | None = None) -> Any:
    """Campus picker. Agents pin this list to their own campus only."""
    with _db() as db:
        campuses = [
            row[0]
            for row in db.query(models.NVR.campus)
            .filter(models.NVR.enabled.is_(True))
            .distinct()
            .order_by(models.NVR.campus)
            .all()
            if row[0]
        ]
    pinned = (session or {}).get("agent_campus")
    if pinned:
        campuses = [campus for campus in campuses if campus == pinned] or [pinned]
    rows = [[(f"🏫 {campus.upper()}", f"campus|{campus}")] for campus in campuses]
    return _menu(rows)


def _trigger_words(db: Session) -> tuple[str, dict[str, models.Agent]]:
    """Return ``(global_trigger, {word: agent})`` from the database."""
    global_word = (get_setting(db, "telegram.trigger_word") or "tk").strip().lower() or "tk"
    agents: dict[str, models.Agent] = {}
    try:
        rows = (
            db.query(models.Agent)
            .filter(models.Agent.enabled.is_(True))
            .order_by(models.Agent.id)
            .all()
        )
    except Exception:
        rows = []
    for agent in rows:
        word = (agent.trigger_word or "").strip().lower()
        if word and word not in agents:
            agents[word] = agent
    return global_word, agents


def _zone_menu(campus: str) -> Any:
    with _db() as db:
        zones = [
            row[0]
            for row in db.query(models.NVR.zone)
            .filter(models.NVR.enabled.is_(True), models.NVR.campus == campus)
            .distinct()
            .order_by(models.NVR.zone)
            .all()
            if row[0]
        ]
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for zone in zones:
        pair.append((f"📍 {zone.replace('_', ' ').title()}", f"zone|{campus}|{zone}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("🔙 Campus", "back|campus")])
    return _menu(rows)


def _nvr_menu(campus: str, zone: str) -> Any:
    with _db() as db:
        nvrs = (
            db.query(models.NVR)
            .filter(models.NVR.enabled.is_(True), models.NVR.campus == campus, models.NVR.zone == zone)
            .order_by(models.NVR.name)
            .all()
        )
    rows = []
    for nvr in nvrs:
        dot = "🟢" if nvr.status == "online" else "⚪"
        rows.append([(f"{dot} 🗄️ {nvr.name}", f"nvr|{nvr.id}")])
    rows.append([("🔙 Zones", f"campus|{campus}")])
    return _menu(rows)


def _camera_menu(user_id: int, nvr_id: int) -> Any:
    session = _sessions.get(user_id) or {}
    selected = {item["channel"] for item in session.get("selected", []) if item["nvr_id"] == nvr_id}
    with _db() as db:
        nvr = db.get(models.NVR, nvr_id)
        cameras = [c for c in (nvr.cameras if nvr else []) if c.enabled]
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for camera in cameras:
        prefix = "✅ " if camera.channel in selected else ""
        pair.append((f"{prefix}{camera.channel}. {camera.name}"[:60], f"cam|{nvr_id}|{camera.channel}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if session.get("selected"):
        rows.append([(f"🚀 Continue ({len(session['selected'])} cams)", "confirm")])
    rows.append([("🔙 NVRs", f"zone|{session.get('campus')}|{session.get('zone')}")])
    return _menu(rows)


def _folder_menu(user_id: int) -> Any:
    session = _sessions.get(user_id) or {}
    # ``selected_campus`` stores plain campus strings (see the nvr/cam
    # callbacks); older builds stored dicts, so accept both shapes.
    campuses = {
        (item.get("campus") if isinstance(item, dict) else str(item)).strip().lower()
        for item in session.get("selected_campus", [])
        if item
    }
    campuses.discard("")
    with _db() as db:
        query = db.query(models.DriveFolder).filter(models.DriveFolder.enabled.is_(True))
        folders = query.order_by(models.DriveFolder.key).all()
        if campuses:
            folders = [f for f in folders if f.campus in campuses] or folders
    rows: list[list[tuple[str, str]]] = []
    pair: list[tuple[str, str]] = []
    for folder in folders:
        pair.append((f"📁 {folder.name}", f"folder|{folder.key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([("🖥 Server only (no upload)", "folder|LOCAL")])
    rows.append([("✏️ Rename folder", "rename")])
    rows.append([("🔙 Cameras", f"nvr|{session.get('last_nvr')}")])
    return _menu(rows)


def _review_text(user_id: int) -> str:
    session = _sessions.get(user_id) or {}
    selected = session.get("selected", [])
    with _db() as db:
        lines = []
        for item in selected:
            camera = (
                db.query(models.Camera)
                .filter(models.Camera.nvr_id == item["nvr_id"], models.Camera.channel == item["channel"])
                .first()
            )
            nvr = db.get(models.NVR, item["nvr_id"])
            lines.append(f"• {_esc(nvr.name if nvr else 'NVR')} · CH{item['channel']} {_esc(camera.name if camera else '')}")
    destination = session.get("drive_folder_key") or "LOCAL"
    return (
        "🎬 <b>Ready to extract footage</b>\n\n"
        f"📅 <b>Date:</b> {_esc(session.get('date'))}\n"
        f"⏰ <b>Time:</b> {_esc(session.get('start_time'))} – {_esc(session.get('end_time'))}\n"
        f"📁 <b>Folder name:</b> {_esc(_folder_name_for(session))}\n"
        f"☁️ <b>Destination:</b> {_esc('Server only' if destination == 'LOCAL' else destination)}\n"
        f"📹 <b>Cameras ({len(selected)}):</b>\n" + "\n".join(lines) + "\n\n"
        "<i>Tap start to download, compress and upload.</i>"
    )


def _review_menu() -> Any:
    return _menu(
        [
            [("🚀 Start extraction", "start")],
            [("📁 Change destination", "confirm")],
            [("✏️ Rename folder", "rename")],
            [("✖️ Cancel", "close")],
        ]
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_start(message):
    if not _allowed(message):
        return
    with _db() as db:
        global_word, agents = _trigger_words(db)
    lines = [
        "👋 <b>CCTV Control Center</b>\n",
        f"• <b>/{_esc(global_word)}</b> — reply to a CCTV request to start extraction",
        f"• <b>/{_esc(global_word)} folder_name</b> — same, with a custom folder name",
    ]
    for word, agent in agents.items():
        if word == global_word:
            continue
        lines.append(f"• <b>/{_esc(word)}</b> — extract on the {_esc(agent.campus.upper())} campus agent")
    lines += [
        "• <b>/tkbaboy</b> — open the CCTV MiniApp",
        "• <b>/health</b> — NVR health summary",
        "• <b>/id</b> — show this chat ID",
    ]
    _say(message.chat.id, "\n".join(lines))


def _command_word(message) -> str:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return ""
    return text[1:].split()[0].split("@")[0].strip().lower()


def _cmd_tk(message, word: str = ""):
    if not _allowed(message):
        return
    args = (message.text or "").split(maxsplit=1)
    override = args[1].strip() if len(args) > 1 else ""
    with _db() as db:
        global_word, agents = _trigger_words(db)
        approver_email = get_setting(db, "smtp.approver_email")
        agent = agents.get(word) if word and word != global_word else None
        if agent is not None and not agent.enabled:
            agent = None
        agent_id = agent.id if agent else None
        agent_name = agent.name if agent else ""
        agent_campus = (agent.campus or "").strip().lower() if agent else None
        agent_zones = list(agent.zones) if agent else []
    label = f"/{word or global_word}"
    if not message.reply_to_message or not message.reply_to_message.text:
        _say(
            message.chat.id,
            f"↩️ <b>Reply</b> to a CCTV request message with <code>{_esc(label)}</code> "
            f"or <code>{_esc(label)} folder_name</code>.",
        )
        return
    parsed = _parse_request_text(message.reply_to_message.text)
    if not parsed:
        _say(message.chat.id, "⚠️ Could not find <b>Date</b>, <b>Start Time</b> and <b>End Time</b> in the replied message.")
        return
    user_id = message.from_user.id
    with _sessions_lock:
        _sessions[user_id] = {
            **parsed,
            "folder_override": override,
            "selected": [],
            "selected_campus": [],
            "drive_folder_key": None,
            "upload": True,
            "nvr_codes": [],
            "boss_email": approver_email,
            "trigger_word": word or global_word,
            "agent_id": agent_id,
            "agent_campus": agent_campus,
            "agent_zones": agent_zones,
        }
    session = _sessions[user_id]
    agent_note = ""
    if agent_id:
        agent_note = f"\n🤖 Routed to <b>{_esc(agent_name)}</b> ({_esc((agent_campus or '').upper())})"
    _say(
        message.chat.id,
        "🚨 <b>Footage extraction</b>\n\n"
        f"📅 {_esc(parsed['date'])} · ⏰ {_esc(parsed['start_time'])}–{_esc(parsed['end_time'])}\n"
        f"📁 Folder: <code>{_esc(_folder_name_for(session))}</code>{agent_note}\n\n"
        "<b>Step 1</b> — choose a campus.",
        _campus_menu(session),
    )


def _cmd_tkbaboy(message):
    if not _allowed(message):
        return
    with _db() as db:
        miniapp = as_bool(get_setting(db, "telegram.miniapp_enabled"), True)
        public_url = (get_setting(db, "app.public_url") or "").rstrip("/")
        token_ok = bool(get_setting(db, "telegram.bot_token"))
    url = f"{public_url}/miniapp" if public_url else ""
    if not url or not token_ok or not miniapp:
        _say(message.chat.id, "⚙️ MiniApp is not configured yet. Open the WebUI integrations tab first.")
        return
    if url.startswith("https://"):
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎬 Open CCTV MiniApp", web_app=WebAppInfo(url=url)))
        markup.add(InlineKeyboardButton("🌐 Open in browser", url=url))
        _say(
            message.chat.id,
            "🎬 <b>CCTV MiniApp</b>\n\n"
            "Manual footage download, camera selection and live job progress — all inside Telegram.\n"
            "Tap below to launch.",
            markup,
        )
    else:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🌐 Open CCTV WebUI", url=url))
        _say(message.chat.id, "🌐 <b>CCTV WebUI</b>\n\nThe MiniApp needs HTTPS. Open the WebUI instead.", markup)


def _cmd_health(message):
    if not _allowed(message):
        return
    with _db() as db:
        summary = health_summary(db)
        nvrs = db.query(models.NVR).order_by(models.NVR.campus, models.NVR.name).all()
        lines = []
        for nvr in nvrs:
            icon = "🟢" if nvr.status == "online" else "🔴"
            extra = []
            if nvr.hdd_used_percent is not None:
                extra.append(f"HDD {nvr.hdd_used_percent:.0f}%")
            offline = sum(1 for c in nvr.cameras if c.online is False)
            if offline:
                extra.append(f"{offline} cam offline")
            lines.append(f"{icon} <b>{_esc(nvr.name)}</b>" + (f" — {_esc(' · '.join(extra))}" if extra else ""))
        text = (
            "🩺 <b>CCTV Health</b>\n\n"
            f"🗄️ NVR online: <b>{summary['nvr_online']}/{summary['nvr_total']}</b>\n"
            f"📹 Cameras: <b>{summary['cameras']}</b> · offline <b>{summary['cameras_offline']}</b>\n"
            f"🚨 Open alerts: <b>{summary['open_alerts']}</b> (critical {summary['critical_alerts']})\n"
            f"💽 Server disk: <b>{summary['system']['disk_used_percent']}%</b>\n\n"
            + ("\n".join(lines) if lines else "<i>No NVRs configured.</i>")
        )
    _say(message.chat.id, text)


def _cmd_id(message):
    _say(message.chat.id, f"🆔 Chat ID: <code>{message.chat.id}</code>\n👤 User ID: <code>{message.from_user.id}</code>")


# ---------------------------------------------------------------------------
# Callback routing
# ---------------------------------------------------------------------------


def _on_callback(call):
    if not _allowed(call):
        _answer(call)
        return
    user_id = call.from_user.id
    data = call.data or ""
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if data == "close":
        _answer(call)
        try:
            _bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        return

    with _sessions_lock:
        session = _sessions.get(user_id)
    if session is None and data not in {"close"} and not data.startswith(("cancel|", "email|")):
        _answer(call, "Session expired. Send /tk again.", alert=True)
        return

    try:
        if data == "back|campus" or data.startswith("campus|"):
            campus = data.split("|", 1)[1] if "|" in data else None
            pinned = session.get("agent_campus")
            if pinned and campus and campus != pinned:
                _answer(call, "This command only serves the assigned campus.", alert=True)
                return
            if campus:
                session["campus"] = campus
                _edit(chat_id, message_id, f"📍 <b>{_esc(campus.upper())}</b> — choose a zone.", _zone_menu(campus))
            else:
                _edit(chat_id, message_id, "🏫 <b>Step 1</b> — choose a campus.", _campus_menu(session))
            _answer(call)
            return

        if data.startswith("zone|"):
            _, campus, zone = data.split("|", 2)
            pinned = session.get("agent_campus")
            zones = session.get("agent_zones") or []
            if pinned and campus != pinned:
                _answer(call, "This command only serves the assigned campus.", alert=True)
                return
            if zones and zone not in zones:
                _answer(call, "This command only serves the assigned zones.", alert=True)
                return
            session["campus"] = campus
            session["zone"] = zone
            _edit(
                chat_id,
                message_id,
                f"🗄️ <b>{_esc(zone.replace('_', ' ').title())}</b> — choose an NVR.",
                _nvr_menu(campus, zone),
            )
            _answer(call)
            return

        if data.startswith("nvr|"):
            nvr_id = int(data.split("|", 1)[1])
            with _db() as db:
                nvr = db.get(models.NVR, nvr_id)
                if nvr and session.get("agent_campus") and nvr.campus != session["agent_campus"]:
                    _answer(call, "This command only serves the assigned campus.", alert=True)
                    return
                nvr_zones = session.get("agent_zones") or []
                if nvr and nvr_zones and nvr.zone not in nvr_zones:
                    _answer(call, "This command only serves the assigned zones.", alert=True)
                    return
            session["last_nvr"] = nvr_id
            with _db() as db:
                nvr = db.get(models.NVR, nvr_id)
                campus = nvr.campus if nvr else session.get("campus")
                zone = nvr.zone if nvr else session.get("zone")
                name = nvr.name if nvr else "NVR"
                nvr_code = nvr.code if nvr else ""
            session["last_nvr_code"] = nvr_code
            if campus and campus not in session.get("selected_campus", []):
                session.setdefault("selected_campus", []).append(campus)
            session["campus"] = campus
            session["zone"] = zone
            _edit(
                chat_id,
                message_id,
                f"📹 <b>{_esc(name)}</b> — select cameras.\n<i>Tap again to unselect. You can pick cameras from multiple NVRs.</i>",
                _camera_menu(user_id, nvr_id),
            )
            _answer(call)
            return

        if data.startswith("cam|"):
            _, nvr_id_raw, channel_raw = data.split("|", 2)
            nvr_id, channel = int(nvr_id_raw), int(channel_raw)
            selected = session.setdefault("selected", [])
            entry = {"nvr_id": nvr_id, "channel": channel}
            if entry in selected:
                selected.remove(entry)
            else:
                selected.append(entry)
            with _db() as db:
                nvr = db.get(models.NVR, nvr_id)
                if nvr and nvr.campus not in session.setdefault("selected_campus", []):
                    session["selected_campus"].append(nvr.campus)
            _edit(chat_id, message_id, "📹 <b>Select cameras</b>\n<i>Tap again to unselect.</i>", _camera_menu(user_id, nvr_id))
            _answer(call)
            return

        if data == "confirm":
            if not session.get("selected"):
                _answer(call, "Select at least one camera first.", alert=True)
                return
            _edit(
                chat_id,
                message_id,
                f"📂 <b>Step 5</b> — destination for <b>{len(session['selected'])}</b> camera(s).",
                _folder_menu(user_id),
            )
            _answer(call)
            return

        if data.startswith("folder|"):
            key = data.split("|", 1)[1]
            session["drive_folder_key"] = None if key == "LOCAL" else key
            session["upload"] = key != "LOCAL"
            _edit(chat_id, message_id, _review_text(user_id), _review_menu())
            _answer(call)
            return

        if data == "rename":
            prompt = _say(
                chat_id,
                "✏️ Send the new folder name (or /cancel).\n"
                f"Current: <code>{_esc(_folder_name_for(session))}</code>",
            )
            if prompt:
                _bot.register_next_step_handler(prompt, _process_rename, user_id, chat_id, message_id)
            _answer(call)
            return

        if data == "start":
            _answer(call, "Starting…")
            _start_bot_job(user_id, chat_id, message_id)
            return

        if data.startswith("email|"):
            _, target, job_id_raw = data.split("|", 2)
            _send_job_email(call, int(job_id_raw), target)
            return

        if data.startswith("cancel|"):
            job_id = int(data.split("|", 1)[1])
            with _db() as db:
                job_service.request_cancel(db, job_id)
            _answer(call, "Cancellation requested")
            return

        if data.startswith("retry|"):
            _answer(call, "Open /tkbaboy to retry in the MiniApp.", alert=True)
            return
    except Exception as exc:
        print(f"[bot] callback error: {exc}")
        _answer(call, f"Error: {exc}", alert=True)


def _process_rename(message, user_id: int, chat_id: int, message_id: int):
    if not message.text:
        return
    if message.text.strip().lower() in {"/cancel", "cancel"}:
        _say(chat_id, "Rename cancelled.")
        return
    with _sessions_lock:
        session = _sessions.get(user_id)
        if not session:
            return
        session["folder_override"] = message.text.strip()[:150]
    try:
        _bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    _edit(chat_id, message_id, _review_text(user_id), _review_menu())


def _start_bot_job(user_id: int, chat_id: int, message_id: int):
    with _sessions_lock:
        session = _sessions.get(user_id)
    if not session or not session.get("selected"):
        _say(chat_id, "Session expired. Send /tk again.")
        return
    folder_name = _folder_name_for(session)
    with _db() as db:
        nvr_codes = []
        for item in session["selected"]:
            nvr = db.get(models.NVR, item["nvr_id"])
            if nvr:
                nvr_codes.append(nvr.code)
        emails = []
        if session.get("requester_email"):
            emails = [e.strip() for e in str(session["requester_email"]).replace(";", ",").split(",") if e.strip()]
        payload = {
            "cameras": [
                {"nvr_id": item["nvr_id"], "channel": item["channel"]} for item in session["selected"]
            ],
            "date": session["date"],
            "start_time": session["start_time"],
            "end_time": session["end_time"],
            "folder_name": folder_name,
            "incident": session.get("incident"),
            "campus": session.get("campus"),
            "requester": session.get("requester"),
            "requester_email": session.get("requester_email"),
            "drive_folder_key": session.get("drive_folder_key"),
            "upload": session.get("upload", True),
            "notify_emails": [],
            "notify_approver": False,
            "agent_id": session.get("agent_id"),
        }
        try:
            job = job_service.create_job(
                db,
                payload,
                source="bot",
                telegram_chat_id=str(chat_id),
                telegram_message_id=str(message_id),
            )
        except Exception as exc:
            _say(chat_id, f"❌ Could not start job: {_esc(exc)}")
            return
        audit(
            db,
            None,
            "job_created",
            f"bot {job.ref} · {job.folder_name} · {', '.join(nvr_codes)}",
            level="success",
        )
    with _sessions_lock:
        _sessions.pop(user_id, None)
    _edit(chat_id, message_id, "⏳ <b>Starting extraction…</b>", _menu([[("✖️ Cancel job", f"cancel|{job.id}")]]))
    threading.Thread(target=_watch_job, args=(job.id, chat_id, message_id), daemon=True).start()


def _watch_job(job_id: int, chat_id: int, message_id: int):
    last_text = ""
    while True:
        try:
            with _db() as db:
                job = db.get(models.Job, job_id)
                if not job:
                    return
                status = job.status
                progress = job.progress
                folder = job.folder_name
                message = job.message or ""
                drive_link = job.drive_link
                payload = json.loads(job.request_json or "{}")
                error = job.error
        except Exception:
            time.sleep(5)
            continue

        bar = "▰" * int(progress / 10) + "▱" * (10 - int(progress / 10))
        if status in {"queued", "dispatched", "running"}:
            waiting = status == "dispatched"
            text = (
                f"⏳ <b>{'Waiting for campus agent' if waiting else 'Extracting footage'}</b>\n\n"
                f"📁 {_esc(folder)}\n"
                f"{bar} <b>{progress}%</b>\n"
                f"<i>{_esc(message)}</i>"
            )
            markup = _menu([[("✖️ Cancel job", f"cancel|{job_id}")]])
        elif status in {"completed", "partial"}:
            text = f"✅ <b>Footage ready</b>\n\n📁 {_esc(folder)}\n📹 {len(payload.get('cameras', []))} camera(s)\n"
            if drive_link:
                text += f'🔗 <a href="{drive_link}">Open Google Drive folder</a>\n'
            text += "\nChoose who should receive access:"
            rows = []
            if payload.get("requester_email"):
                rows.append([("📧 Send to requester", f"email|req|{job_id}")])
            rows.append([("👔 Send to approver", f"email|boss|{job_id}")])
            rows.append([("🎬 New request", "close")])
            markup = _menu(rows, add_close=False)
        elif status == "cancelled":
            text = f"🛑 <b>Cancelled</b>\n\n📁 {_esc(folder)}"
            markup = _menu([[("🎬 New request", "close")]], add_close=False)
        else:
            text = f"❌ <b>Job failed</b>\n\n📁 {_esc(folder)}\n<code>{_esc(error or message)[:700]}</code>"
            markup = _menu([[("🎬 New request", "close")]], add_close=False)

        if text != last_text:
            _edit(chat_id, message_id, text, markup)
            last_text = text
        if status not in {"queued", "dispatched", "running"}:
            return
        time.sleep(4)


def _send_job_email(call, job_id: int, target: str):
    with _db() as db:
        job = db.get(models.Job, job_id)
        if not job or not job.drive_link:
            _answer(call, "Job or Drive link not found.", alert=True)
            return
        payload = json.loads(job.request_json or "{}")
        if target == "req":
            emails = [
                e.strip()
                for e in str(payload.get("requester_email") or "").replace(";", ",").split(",")
                if e.strip()
            ]
            if not emails:
                _answer(call, "No requester email in the original request.", alert=True)
                return
        else:
            emails = [get_setting(db, "smtp.approver_email")]
        emails = [email for email in emails if email]
        if not emails:
            _answer(call, "No email address configured.", alert=True)
            return
        try:
            service = DriveService.from_settings(db)
            days = as_int(get_setting(db, "google.share_expiry_days"), 30)
            request_data = {
                "incident": payload.get("incident") or job.folder_name,
                "campus": payload.get("campus") or job.drive_folder_key,
                "date": payload.get("date"),
                "requester": payload.get("requester"),
                "requester_email": payload.get("requester_email"),
                "expiry_days": days,
            }
            results = []
            for email in emails:
                try:
                    service.grant_access(job.drive_folder_id, email, days)
                    ok, message = mailer.send_footage_email(
                        db, email, job.drive_link, request_data, approver=(target == "boss")
                    )
                    results.append(f"{email}: {'✅' if ok else '❌ ' + message}")
                except Exception as exc:
                    results.append(f"{email}: ❌ {exc}")
            _edit(
                call.message.chat.id,
                call.message.message_id,
                "📧 <b>Access email results</b>\n\n" + "\n".join(_esc(row) for row in results),
            )
            _answer(call, "Done")
        except Exception as exc:
            _answer(call, f"Error: {exc}", alert=True)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _build_bot(token: str, triggers: set[str] | None = None):
    if telebot is None:
        raise RuntimeError("pyTelegramBotAPI is not installed")
    bot = telebot.TeleBot(token, threaded=False, parse_mode="HTML")
    triggers = set(triggers or {"tk"})
    # Always keep /tk working so an existing group workflow never breaks after
    # the trigger word is changed in the WebUI.
    triggers.add("tk")

    @bot.message_handler(commands=["start", "help"])
    def _start(message):
        _cmd_start(message)

    @bot.message_handler(commands=["tkbaboy", "badboytk", "miniapp"])
    def _tkbaboy(message):
        _cmd_tkbaboy(message)

    @bot.message_handler(commands=["health", "status"])
    def _health(message):
        _cmd_health(message)

    @bot.message_handler(commands=["id", "chatid"])
    def _id(message):
        _cmd_id(message)

    @bot.message_handler(func=lambda message: _command_word(message) in triggers)
    def _trigger(message):
        _cmd_tk(message, _command_word(message))

    @bot.callback_query_handler(func=lambda call: True)
    def _callback(call):
        _on_callback(call)

    return bot


def _launch(token: str, triggers: set[str]) -> None:
    global _bot, _bot_thread
    with _bot_lock:
        generation = _bot_generation
        _bot = _build_bot(token, triggers)
        _stop_event.clear()
        target = _bot

    def _poll():
        try:
            # A shorter long-poll makes token/trigger reloads take effect
            # within a few seconds instead of waiting up to 25 seconds.
            target.infinity_polling(none_stop=True, timeout=30, long_polling_timeout=10)
        except Exception as exc:
            with _bot_lock:
                stale = generation != _bot_generation
            if not stale and not _stop_event.is_set():
                print(f"[bot] polling stopped: {exc}")

    with _bot_lock:
        _bot_thread = threading.Thread(target=_poll, name="telegram-bot", daemon=True)
        _bot_thread.start()


def start_bot() -> None:
    if not RUN_BOT:
        return
    with _db() as db:
        token = get_setting(db, "telegram.bot_token").strip()
        if not token:
            print("[bot] no Telegram token configured; bot thread not started")
            return
        global_word, agents = _trigger_words(db)
    _launch(token, {global_word, "tk"} | set(agents))


def reload_bot() -> None:
    """Reload token and trigger words without restarting the container.

    The old polling thread is asked to stop and the replacement starts only
    after it exits, so two getUpdates loops never race (Telegram answers 409
    when they do). Reloading happens in a background thread so saving the
    Integrations form never blocks on a long poll.
    """
    global _bot, _bot_thread, _bot_generation
    if not RUN_BOT:
        return
    with _bot_lock:
        _bot_generation += 1
        generation = _bot_generation
        old = _bot
        old_thread = _bot_thread
        _bot = None
        _bot_thread = None
        _stop_event.set()
    if old is not None:
        try:
            old.stop_polling()
        except Exception:
            pass

    def _restart():
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=30)
        with _bot_lock:
            if generation != _bot_generation:
                return
        with _db() as db:
            token = get_setting(db, "telegram.bot_token").strip()
            global_word, agents = _trigger_words(db)
        if not token:
            print("[bot] token removed; polling stopped")
            return
        _launch(token, {global_word, "tk"} | set(agents))

    threading.Thread(target=_restart, name="bot-reload", daemon=True).start()


def stop_bot() -> None:
    global _bot, _bot_thread, _bot_generation
    with _bot_lock:
        _bot_generation += 1
        _stop_event.set()
        bot = _bot
        thread = _bot_thread
        _bot = None
        _bot_thread = None
    if bot:
        try:
            bot.stop_polling()
        except Exception:
            pass
    if thread and thread.is_alive():
        thread.join(timeout=5)
