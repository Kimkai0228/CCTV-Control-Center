"""Database models. All operational configuration lives here."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    email = Column(String(200), nullable=True)
    password_hash = Column(String(300), nullable=False)
    role = Column(String(30), nullable=False, default="operator")  # superadmin|admin|operator|viewer
    is_active = Column(Boolean, nullable=False, default=True)
    telegram_user_id = Column(String(40), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)

    jobs = relationship("Job", back_populates="user")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DriveFolder(Base):
    __tablename__ = "drive_folders"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    name = Column(String(160), nullable=False)
    folder_id = Column(String(200), nullable=False)
    campus = Column(String(40), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class NVR(Base):
    __tablename__ = "nvrs"

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    code = Column(String(80), unique=True, nullable=False, index=True)
    campus = Column(String(40), nullable=False, default="tk")
    zone = Column(String(80), nullable=False, default="general")
    protocol = Column(String(30), nullable=False, default="hikvision")  # hikvision|truvision|dvr|rtsp
    host = Column(String(190), nullable=False)
    port = Column(Integer, nullable=False, default=80)  # ISAPI / web port
    sdk_port = Column(Integer, nullable=False, default=8000)  # Hikvision SDK port
    username = Column(String(120), nullable=False, default="admin")
    password_enc = Column(Text, nullable=False, default="")
    channel_offset = Column(Integer, nullable=False, default=32)
    track_multiplier = Column(Integer, nullable=False, default=100)
    time_offset_hours = Column(Float, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    auto_discover = Column(Boolean, nullable=False, default=True)

    status = Column(String(30), nullable=False, default="unknown")
    model = Column(String(120), nullable=True)
    firmware = Column(String(120), nullable=True)
    serial = Column(String(120), nullable=True)
    hdd_json = Column(Text, nullable=True)
    hdd_used_percent = Column(Float, nullable=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    last_checked = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    cameras = relationship(
        "Camera", back_populates="nvr", cascade="all, delete-orphan", order_by="Camera.channel"
    )
    alerts = relationship("Alert", back_populates="nvr", cascade="all, delete-orphan")

    @property
    def base_url(self) -> str:
        return f"rtsp://{self.username}:{self.password}@{self.host}"


class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = (UniqueConstraint("nvr_id", "channel", name="uq_camera_nvr_channel"),)

    id = Column(Integer, primary_key=True)
    nvr_id = Column(Integer, ForeignKey("nvrs.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False, default="")
    ip_address = Column(String(64), nullable=True)
    mac_address = Column(String(64), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    online = Column(Boolean, nullable=True)
    rtsp_path = Column(String(400), nullable=True)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    nvr = relationship("NVR", back_populates="cameras")
    job_files = relationship("JobFile", back_populates="camera")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    source = Column(String(30), nullable=False, default="web")  # web|miniapp|bot|system
    kind = Column(String(30), nullable=False, default="footage")  # footage|health|discovery
    status = Column(String(30), nullable=False, default="queued", index=True)
    progress = Column(Integer, nullable=False, default=0)
    message = Column(String(600), nullable=True)
    error = Column(Text, nullable=True)
    folder_name = Column(String(250), nullable=True)
    drive_folder_key = Column(String(80), nullable=True)
    drive_link = Column(String(500), nullable=True)
    drive_folder_id = Column(String(200), nullable=True)
    request_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)
    retry_json = Column(Text, nullable=True)
    telegram_chat_id = Column(String(60), nullable=True)
    telegram_message_id = Column(String(60), nullable=True)
    telegram_user_id = Column(String(60), nullable=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="jobs")
    agent = relationship("Agent")
    files = relationship("JobFile", back_populates="job", cascade="all, delete-orphan")


class JobFile(Base):
    __tablename__ = "job_files"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True)
    channel = Column(Integer, nullable=True)
    filename = Column(String(300), nullable=False)
    local_path = Column(String(700), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    drive_file_id = Column(String(200), nullable=True)
    status = Column(String(30), nullable=False, default="ready")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    job = relationship("Job", back_populates="files")
    camera = relationship("Camera", back_populates="job_files")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    nvr_id = Column(Integer, ForeignKey("nvrs.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"), nullable=True)
    type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False, default="warning")
    fingerprint = Column(String(200), nullable=False, index=True)
    message = Column(Text, nullable=False)
    acknowledged = Column(Boolean, nullable=False, default=False)
    resolved = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolve_note = Column(Text, nullable=True)
    last_notified_at = Column(DateTime(timezone=True), nullable=True)

    nvr = relationship("NVR", back_populates="alerts")


class AlertException(Base):
    """Suppress matching alerts.

    An exception can target an exact fingerprint, or broadly combine any of
    ``alert_type`` / ``nvr_id`` / ``camera_id``. For example:

    * camera + nvr  -> ignore every alert for a ghost camera
    * alert_type=hdd + nvr -> ignore HDD capacity on an always-full NVR
    * fingerprint           -> ignore exactly one recurring alert
    """

    __tablename__ = "alert_exceptions"

    id = Column(Integer, primary_key=True)
    alert_type = Column(String(50), nullable=True)
    nvr_id = Column(Integer, ForeignKey("nvrs.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"), nullable=True)
    fingerprint = Column(String(200), nullable=True, index=True)
    reason = Column(String(300), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class Agent(Base):
    """A lightweight remote campus worker that executes jobs locally."""

    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    campus = Column(String(40), nullable=False, default="tk")
    zones_json = Column(Text, nullable=True)  # JSON list, empty = all zones
    trigger_word = Column(String(40), nullable=False, default="tk")
    token_enc = Column(Text, nullable=False, default="")
    # Public HTTPS base URL that serves /agent-files, e.g.
    # https://agent-cctv.footprints.work.  Blank means files are copied back
    # to the main host like before.
    public_url = Column(String(400), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    status = Column(String(30), nullable=False, default="unknown")
    version = Column(String(40), nullable=True)
    hostname = Column(String(160), nullable=True)
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    last_job_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    @property
    def zones(self) -> list[str]:
        import json

        try:
            value = json.loads(self.zones_json or "[]")
            return [str(item).strip().lower() for item in value if str(item).strip()]
        except (ValueError, TypeError):
            return []


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String(80), nullable=True)
    action = Column(String(120), nullable=False)
    # success / info / warning / error, used by the Settings activity viewer.
    level = Column(String(20), nullable=True, default="info")
    detail = Column(Text, nullable=True)
    ip = Column(String(60), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
