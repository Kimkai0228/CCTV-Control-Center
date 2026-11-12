"""Hikvision / TruVision ISAPI client used for discovery and health checks.

The implementation is intentionally tolerant: NVR firmwares differ, so every
endpoint has a fallback and parsers look for several tag spellings. A failed
endpoint never crashes a health sweep.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .. import models

requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_xml(text: str) -> ET.Element | None:
    """Parse XML while ignoring namespaces and common malformed prefixes."""
    if not text:
        return None
    cleaned = text.strip().lstrip("\ufeff")
    # Some old firmwares omit the XML declaration but include a namespace prefix.
    cleaned = re.sub(r"^[^<]*", "", cleaned)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        # Last resort: remove namespaced tag names, leaving local names.
        fixed = re.sub(r"<(/?)[A-Za-z0-9_.-]+:", r"<\1", cleaned)
        fixed = re.sub(r"\s+xmlns(:\w+)?=\"[^\"]*\"", "", fixed)
        fixed = re.sub(r"\s+[A-Za-z0-9_.-]+:([A-Za-z0-9_.-]+)=\"", r' \1="', fixed)
        try:
            root = ET.fromstring(fixed)
        except ET.ParseError:
            return None
    for node in root.iter():
        node.tag = _strip_ns(node.tag)
    return root


def _text(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _deep_text(node: ET.Element, *names: str) -> str:
    """Like _text() but also searches nested elements (e.g. sourceInputPortDescriptor)."""
    for name in names:
        for child in node.iter(name):
            if child is not node and child.text and child.text.strip():
                return child.text.strip()
    return ""


def _local_name(node: ET.Element) -> str:
    return _strip_ns(node.tag).lower()


_MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)
# Cameras that refused or never answered a MAC lookup are retried only after
# this delay, so the health sweep does not hammer unreachable devices.
_MAC_RETRY_SECONDS = 6 * 3600
_mac_failures: dict[str, float] = {}
_mac_lock = threading.Lock()


def normalise_mac(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(text) or text == "00:00:00:00:00:00":
        return None
    return text


@dataclass
class HealthResult:
    ok: bool = False
    message: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    cameras: list[dict[str, Any]] = field(default_factory=list)
    storage: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def hdd_used_percent(self) -> float | None:
        worst = None
        for disk in self.storage:
            used = disk.get("used_percent")
            if used is None:
                continue
            worst = used if worst is None else max(worst, used)
        return worst


class NVRClient:
    def __init__(self, nvr: models.NVR, password: str | None = None, timeout: float = 8.0):
        from ..security import decrypt_secret

        self.nvr = nvr
        self.password = password if password is not None else decrypt_secret(nvr.password_enc)
        self.timeout = timeout
        self.base = f"{'https' if int(nvr.port or 80) == 443 else 'http'}://{nvr.host}:{int(nvr.port or 80)}"
        self.auth_candidates = [HTTPDigestAuth(nvr.username, self.password), HTTPBasicAuth(nvr.username, self.password)]
        self._working_auth = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def request(
        self,
        path: str,
        method: str = "GET",
        data: str | None = None,
        *,
        stream: bool = False,
        timeout: float | None = None,
    ) -> requests.Response | None:
        if not self.nvr.host:
            return None
        url = path if path.startswith("http") else f"{self.base}{path}"
        candidates = [self._working_auth] if self._working_auth else self.auth_candidates
        last_exc: Exception | None = None
        for auth in candidates:
            try:
                response = requests.request(
                    method,
                    url,
                    auth=auth,
                    data=data.encode("utf-8") if isinstance(data, str) else data,
                    headers={"Content-Type": "application/xml"} if data else None,
                    timeout=timeout or self.timeout,
                    verify=False,
                    stream=stream,
                )
                if response.status_code in (401, 403) and auth is not self._working_auth:
                    continue
                if response.status_code < 400:
                    self._working_auth = auth
                return response
            except requests.RequestException as exc:
                last_exc = exc
                break
        if last_exc:
            return None
        return None

    def get_text(self, path: str) -> str:
        response = self.request(path)
        if response is None or response.status_code >= 400:
            return ""
        return response.text

    def get_json(self, path: str) -> dict[str, Any] | None:
        text = self.get_text(path)
        if not text:
            return None
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Device info / connection test
    # ------------------------------------------------------------------
    def get_device_info(self) -> dict[str, Any]:
        text = self.get_text("/ISAPI/System/deviceInfo")
        root = parse_xml(text)
        if root is None:
            return {}
        return {
            "name": _text(root, "deviceName"),
            "model": _text(root, "model"),
            "serial": _text(root, "serialNumber"),
            "firmware": _text(root, "firmwareVersion", "firmwareReleasedDate"),
            "device_type": _text(root, "deviceType"),
        }

    def test(self) -> tuple[bool, str]:
        if self.nvr.protocol == "rtsp":
            return self._test_tcp()
        info = self.get_device_info()
        if info.get("model") or info.get("serial") or info.get("firmware"):
            detail = " · ".join(x for x in [info.get("model"), info.get("firmware")] if x)
            return True, detail or "Connected"
        # Some DVRs disable deviceInfo but still answer channel queries.
        cameras = self.discover_cameras(include_status=False)
        if cameras:
            return True, f"Connected · {len(cameras)} channels found"
        return self._test_tcp()

    def _test_tcp(self) -> tuple[bool, str]:
        port = int(self.nvr.port or 80)
        try:
            with socket.create_connection((self.nvr.host, port), timeout=4):
                return True, f"TCP port {port} reachable"
        except OSError as exc:
            return False, f"Cannot reach {self.nvr.host}:{port} ({exc})"

    # ------------------------------------------------------------------
    # Camera discovery
    # ------------------------------------------------------------------
    @staticmethod
    def _flag(value: Any) -> bool | None:
        if value is None or value == "":
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "online", "normal"}:
            return True
        if text in {"0", "false", "no", "off", "offline"}:
            return False
        try:
            return int(float(text)) != 0
        except (TypeError, ValueError):
            return None

    def _working_status(self) -> dict[int, bool]:
        """All-channel status, including DVR analog channels.

        ``/ISAPI/System/workingstatus/chanStatus`` is supported by Hikvision
        DVR/hybrid firmware and reports ``online`` plus ``signal`` (0 = normal,
        1 = signal loss) for every channel. Older firmware can return either the
        documented ``ChanStatus`` list or a wrapped list.
        """
        status: dict[int, bool] = {}
        for path in (
            "/ISAPI/System/workingstatus/chanStatus?format=json",
            "/ISAPI/System/workingstatus/chanStatus",
        ):
            payload = self.get_json(path)
            if not isinstance(payload, dict):
                continue
            items = (
                payload.get("ChanStatus")
                or payload.get("ChanStatusList")
                or payload.get("chanStatus")
                or []
            )
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    channel = int(item.get("chanNo") or item.get("id") or item.get("channel"))
                except (TypeError, ValueError):
                    continue
                online = self._flag(item.get("online"))
                signal = self._flag(item.get("signal"))
                enabled = self._flag(item.get("enable"))
                if online is None:
                    online = True if signal is not None else None
                if enabled is False:
                    online = False
                if signal is True:  # 1 = signal loss
                    online = False
                if online is not None:
                    status[channel] = online
            if status:
                break
        return status

    def _channel_status(self) -> dict[int, bool]:
        status = self._working_status()
        for path in (
            "/ISAPI/ContentMgmt/InputProxy/channels/status",
            "/ISAPI/ContentMgmt/InputProxy/channels/status?format=json",
        ):
            text = self.get_text(path)
            if not text:
                continue
            if text.strip().startswith("{"):
                try:
                    payload = json.loads(text)
                    items = (
                        payload.get("InputProxyChannelStatusList", {}).get("InputProxyChannelStatus", [])
                        if isinstance(payload, dict)
                        else []
                    )
                    if isinstance(items, dict):
                        items = [items]
                    for item in items:
                        try:
                            status[int(item.get("id"))] = str(item.get("online", "")).lower() in {"true", "1"}
                        except (TypeError, ValueError):
                            continue
                except (ValueError, TypeError):
                    pass
                continue
            root = parse_xml(text)
            if root is None:
                continue
            for node in root.iter():
                if _local_name(node) not in {"inputproxychannelstatus", "channelstatus", "videoinputchannelstatus"}:
                    continue
                try:
                    cid = int(_text(node, "id"))
                except (TypeError, ValueError):
                    continue
                online_text = _text(node, "online", "isOnline", "onlineStatus")
                status[cid] = online_text.lower() in {"true", "1", "online", "normal"}
            if status:
                break
        return status

    def discover_cameras(self, include_status: bool = True) -> list[dict[str, Any]]:
        cameras: list[dict[str, Any]] = []
        seen: set[int] = set()
        for path in ("/ISAPI/ContentMgmt/InputProxy/channels", "/ISAPI/System/Video/inputs/channels"):
            text = self.get_text(path)
            root = parse_xml(text)
            if root is None:
                continue
            for node in root.iter():
                if _local_name(node) not in {
                    "inputproxychannel",
                    "videoinputchannel",
                    "channel",
                }:
                    continue
                try:
                    channel = int(_text(node, "id", "channel", "channelID"))
                except (TypeError, ValueError):
                    continue
                if channel in seen:
                    continue
                name = _text(node, "name", "channelName", "deviceName") or f"Channel {channel}"
                # Hikvision/TruVision nest the camera address inside
                # <sourceInputPortDescriptor>; analog DVR channels have none.
                ip_address = _deep_text(
                    node,
                    "ipAddress",
                    "hostName",
                    "ip",
                    "deviceAddress",
                    "srcIpAddress",
                    "address",
                )
                mac_address = normalise_mac(_deep_text(node, "macAddress", "MACAddress", "mac", "deviceMac"))
                # Analog/TVI DVR channels have no InputProxy status. Their
                # signal state is reported directly on the video input:
                # enable=0 or resDesc="NO VIDEO" means the camera is offline.
                enabled = self._flag(_text(node, "videoInputEnabled", "enabled"))
                resolution = _text(node, "resDesc", "resolution", "res")
                analog_online = None
                if enabled is False:
                    analog_online = False
                elif resolution:
                    analog_online = resolution.strip().upper() not in {
                        "NO VIDEO",
                        "NO VIDEO SIGNAL",
                        "NO SIGNAL",
                        "VIDEO LOSS",
                        "SIGNAL LOSS",
                    }
                seen.add(channel)
                cameras.append(
                    {
                        "channel": channel,
                        "name": name,
                        "online": analog_online,
                        "enabled": enabled,
                        "resolution": resolution or None,
                        "ip_address": ip_address or None,
                        "mac_address": mac_address or None,
                    }
                )
            if cameras:
                break
        if include_status and cameras:
            status = self._channel_status()
            for camera in cameras:
                if camera["channel"] in status:
                    camera["online"] = status[camera["channel"]]
        cameras.sort(key=lambda c: c["channel"])
        return cameras

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------
    def get_storage(self) -> list[dict[str, Any]]:
        disks: list[dict[str, Any]] = []
        # JSON variant is available on newer firmware and is much easier to parse.
        for path in ("/ISAPI/ContentMgmt/Storage/HDD?format=json", "/ISAPI/ContentMgmt/Storage?format=json"):
            payload = self.get_json(path)
            if not payload:
                continue
            items = payload.get("hddList", payload.get("HDDList", payload))
            if isinstance(items, dict):
                items = items.get("hdd", items.get("HDD", []))
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                disks.append(self._normalise_disk(item))
            if disks:
                return disks

        text = self.get_text("/ISAPI/ContentMgmt/Storage")
        root = parse_xml(text)
        if root is None:
            return []
        for node in root.iter():
            if _local_name(node) not in {"hdd", "disk", "storage"}:
                continue
            values = {_strip_ns(child.tag).lower(): (child.text or "").strip() for child in node}
            if not values.get("id") and not values.get("name"):
                continue
            disks.append(self._normalise_disk(values))
        return disks

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    def _normalise_disk(self, item: dict[str, Any]) -> dict[str, Any]:
        lower = {str(k).lower(): v for k, v in item.items()}
        capacity = self._to_float(lower.get("capacity") or lower.get("totalspace") or lower.get("total"))
        free = self._to_float(lower.get("freespace") or lower.get("free"))
        used_percent = None
        if capacity and free is not None and capacity > 0:
            used_percent = round(max(0.0, min(100.0, (capacity - free) / capacity * 100)), 1)
        status = str(lower.get("status") or lower.get("healthstatus") or "unknown")
        return {
            "id": str(lower.get("id") or lower.get("name") or ""),
            "name": str(lower.get("name") or lower.get("id") or "HDD"),
            "status": status,
            "capacity": capacity,
            "free_space": free,
            "used_percent": used_percent,
            "property": str(lower.get("property") or ""),
        }

    # ------------------------------------------------------------------
    # Combined health
    # ------------------------------------------------------------------
    def get_health(self, include_cameras: bool = True) -> HealthResult:
        result = HealthResult()
        if self.nvr.protocol == "rtsp":
            ok, message = self._test_tcp()
            result.ok = ok
            result.message = message
            if not ok:
                result.errors.append(message)
            return result

        result.device = self.get_device_info()
        if result.device:
            result.ok = True
        if include_cameras:
            result.cameras = self.discover_cameras()
            if result.cameras:
                result.ok = True
        result.storage = self.get_storage()
        if not result.ok:
            ok, message = self._test_tcp()
            result.ok = ok
            result.message = message
            if not ok:
                result.errors.append(message)
        else:
            result.message = "Online"
        return result

    # ------------------------------------------------------------------
    # Camera MAC addresses
    # ------------------------------------------------------------------
    def camera_mac(self, ip_address: str, timeout: float = 4.0) -> str | None:
        """Ask an IP camera for its MAC using the NVR credentials.

        NVR ISAPI does not report the MAC of added channels, but cameras are
        usually provisioned with the same admin password as their recorder.
        Cameras on an NVR's internal PoE network are not reachable and simply
        return None.
        """
        url = f"http://{ip_address}/ISAPI/System/Network/interfaces"
        for auth in self.auth_candidates:
            try:
                response = requests.get(url, auth=auth, timeout=(3, timeout), verify=False)
            except requests.RequestException:
                return None
            if response.status_code in (401, 403):
                continue
            if response.status_code >= 400:
                return None
            root = parse_xml(response.text)
            if root is None:
                return None
            return normalise_mac(_deep_text(root, "MACAddress", "macAddress"))
        return None

    def fill_missing_macs(self, cameras: list[Any], force: bool = False) -> int:
        """Look up MACs for camera rows that have an IP but no MAC yet."""
        now = time.time()
        targets = []
        for camera in cameras:
            ip = (getattr(camera, "ip_address", None) or "").strip()
            if not ip or getattr(camera, "mac_address", None):
                continue
            with _mac_lock:
                failed_at = _mac_failures.get(ip)
            if not force and failed_at and now - failed_at < _MAC_RETRY_SECONDS:
                continue
            targets.append((camera, ip))
        if not targets:
            return 0
        with ThreadPoolExecutor(max_workers=min(8, len(targets)), thread_name_prefix="cam-mac") as pool:
            results = list(pool.map(lambda item: self.camera_mac(item[1]), targets))
        found = 0
        with _mac_lock:
            for (camera, ip), mac in zip(targets, results):
                if mac:
                    camera.mac_address = mac
                    _mac_failures.pop(ip, None)
                    found += 1
                else:
                    _mac_failures[ip] = now
        return found

    def snapshot(self, channel: int, timeout: float | None = None) -> bytes | None:
        offset = int(self.nvr.channel_offset or 0) if self.nvr.protocol != "dvr" else 0
        stream_id = (int(channel) + offset) * 100 + 1
        for path in (
            f"/ISAPI/Streaming/channels/{stream_id}/picture",
            f"/ISAPI/Streaming/channels/{int(channel)}01/picture",
        ):
            response = self.request(path, timeout=timeout or 6)
            if response is not None and response.status_code == 200 and response.content:
                return response.content
        return None


def apply_camera_address(camera: models.Camera, item: dict[str, Any]) -> None:
    """Copy the NVR-reported IP/MAC onto a camera row.

    The recorder is the source of truth for a channel's IP. When the IP
    changes (camera replaced or re-addressed) the stored MAC is cleared so it
    is looked up again.
    """
    ip = (item.get("ip_address") or "").strip()
    if ip and ip != (camera.ip_address or ""):
        if camera.ip_address:
            camera.mac_address = None
        camera.ip_address = ip
    if item.get("mac_address"):
        camera.mac_address = item["mac_address"]


def nvr_public_dict(nvr: models.NVR, include_password: bool = False) -> dict[str, Any]:
    from ..security import decrypt_secret

    return {
        "id": nvr.id,
        "name": nvr.name,
        "code": nvr.code,
        "campus": nvr.campus,
        "zone": nvr.zone,
        "protocol": nvr.protocol,
        "host": nvr.host,
        "port": nvr.port,
        "sdk_port": nvr.sdk_port,
        "username": nvr.username,
        "password": decrypt_secret(nvr.password_enc) if include_password else "",
        "channel_offset": nvr.channel_offset,
        "track_multiplier": nvr.track_multiplier,
        "time_offset_hours": nvr.time_offset_hours,
        "enabled": nvr.enabled,
        "auto_discover": nvr.auto_discover,
        "status": nvr.status,
        "model": nvr.model,
        "firmware": nvr.firmware,
        "serial": nvr.serial,
        "hdd_used_percent": nvr.hdd_used_percent,
        "hdd": json.loads(nvr.hdd_json) if nvr.hdd_json else [],
        "last_seen": nvr.last_seen.isoformat() if nvr.last_seen else None,
        "last_checked": nvr.last_checked.isoformat() if nvr.last_checked else None,
        "camera_count": len(nvr.cameras),
    }
