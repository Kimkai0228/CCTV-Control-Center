"""Request/response schemas for the JSON API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    email: str | None = None
    role: str = "operator"
    telegram_user_id: str | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None
    telegram_user_id: str | None = None


class PasswordReset(BaseModel):
    password: str


class CameraInput(BaseModel):
    nvr_id: int
    channel: int


class JobCreate(BaseModel):
    cameras: list[CameraInput]
    date: str
    start_time: str
    end_time: str
    folder_name: str | None = None
    incident: str | None = None
    campus: str | None = None
    requester: str | None = None
    requester_email: str | None = None
    drive_folder_key: str | None = None
    upload: bool = True
    notify_emails: list[str] = Field(default_factory=list)
    notify_approver: bool = False
    notify_telegram: bool = False
    agent_id: int | None = None


class JobRetryInput(BaseModel):
    """Cameras to retry. Empty means every failed camera in the job."""

    cameras: list[CameraInput] = Field(default_factory=list)


class NVRInput(BaseModel):
    name: str
    code: str | None = None
    campus: str = "tk"
    zone: str = "general"
    protocol: str = "hikvision"
    host: str
    port: int = 80
    sdk_port: int = 8000
    username: str = "admin"
    password: str | None = None
    channel_offset: int = 32
    track_multiplier: int = 100
    time_offset_hours: float = 0
    enabled: bool = True
    auto_discover: bool = True


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    ip_address: str | None = None
    mac_address: str | None = None


class CameraCreate(BaseModel):
    channel: int
    name: str
    ip_address: str | None = None
    mac_address: str | None = None


class AlertExceptionInput(BaseModel):
    alert_type: str | None = None
    nvr_id: int | None = None
    camera_id: int | None = None
    fingerprint: str | None = None
    reason: str | None = None
    enabled: bool = True


class AlertIgnoreInput(BaseModel):
    mode: str = "fingerprint"  # fingerprint|camera|nvr|type
    reason: str | None = None


class AlertResolveInput(BaseModel):
    note: str | None = None


class AgentInput(BaseModel):
    name: str
    campus: str = "tk"
    zones: list[str] = Field(default_factory=list)
    trigger_word: str = "tk"
    public_url: str | None = None
    enabled: bool = True
    notes: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    campus: str | None = None
    zones: list[str] | None = None
    trigger_word: str | None = None
    public_url: str | None = None
    enabled: bool | None = None
    notes: str | None = None


class DashboardReportInput(BaseModel):
    id: str | None = None
    type: str = "pie"
    metric: str = "source"
    title: str = ""
    days: int = 30
    span: int = 1
    range: str = "days"
    start_date: str | None = None
    end_date: str | None = None


class DashboardReportsInput(BaseModel):
    reports: list[DashboardReportInput] = Field(default_factory=list)


class AgentJobProgress(BaseModel):
    status: str
    progress: int = 0
    message: str | None = None
    error: str | None = None
    drive_link: str | None = None
    drive_folder_id: str | None = None
    result: dict[str, Any] | None = None


class FolderInput(BaseModel):
    key: str
    name: str
    folder_id: str
    campus: str | None = None
    enabled: bool = True


class GoogleSettings(BaseModel):
    service_account_json: str | None = None
    impersonate_email: str | None = None
    fallback_folder_id: str | None = None
    share_expiry_days: int = 30
    upload_enabled: bool = True


class TelegramSettings(BaseModel):
    bot_token: str | None = None
    group_id: str | None = None
    alert_chat_id: str | None = None
    alerts_enabled: bool = True
    alert_mute_enabled: bool | None = None
    alert_mute_start: str | None = None
    alert_mute_end: str | None = None
    miniapp_enabled: bool = True
    allowed_user_ids: str | None = None
    trigger_word: str | None = None


class SMTPSettings(BaseModel):
    enabled: bool = True
    host: str = "smtp.gmail.com"
    port: int = 587
    username: str = ""
    password: str | None = None
    from_email: str = ""
    from_name: str = "Footprints CCTV"
    cc_emails: str = ""
    approver_email: str = ""


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class TestConnection(BaseModel):
    target: str | None = None
    recipient: str | None = None
