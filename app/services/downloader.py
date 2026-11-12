"""Footage extraction from Hikvision/DVR SDK and TruVision ISAPI."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests
from requests.auth import HTTPDigestAuth

from .. import models
from ..config import BASE_DIR
from ..security import decrypt_secret
from .nvr import NVRClient

ProgressCallback = Callable[[int, str], None] | None

_sdk_lock = threading.Lock()
_sdk: Any = None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_boundary(date_str: str, time_str: str) -> datetime:
    time_str = (time_str or "").strip()
    date_str = (date_str or "").strip()
    for time_fmt in ("%H:%M:%S", "%H:%M"):
        for date_fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(f"{date_str} {time_str}", f"{date_fmt} {time_fmt}")
            except ValueError:
                continue
    raise ValueError(f"Invalid date/time: {date_str} {time_str}")


def safe_filename(name: str, fallback: str = "CCTV") -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', "", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] or fallback


# ---------------------------------------------------------------------------
# Hikvision / DVR SDK
# ---------------------------------------------------------------------------


class NET_DVR_TIME(ctypes.Structure):
    _fields_ = [
        ("dwYear", ctypes.c_uint32),
        ("dwMonth", ctypes.c_uint32),
        ("dwDay", ctypes.c_uint32),
        ("dwHour", ctypes.c_uint32),
        ("dwMinute", ctypes.c_uint32),
        ("dwSecond", ctypes.c_uint32),
    ]


class NET_DVR_DEVICEINFO_V30(ctypes.Structure):
    _fields_ = [
        ("sSerialNumber", ctypes.c_byte * 48),
        ("byAlarmInPortNum", ctypes.c_byte),
        ("byAlarmOutPortNum", ctypes.c_byte),
        ("byDiskNum", ctypes.c_byte),
        ("byDVRType", ctypes.c_byte),
        ("byChanNum", ctypes.c_byte),
        ("byStartChan", ctypes.c_byte),
        ("byAudioChanNum", ctypes.c_byte),
        ("byIPChanNum", ctypes.c_byte),
        ("byZeroChanNum", ctypes.c_byte),
        ("byMainProto", ctypes.c_byte),
        ("bySubProto", ctypes.c_byte),
        ("bySupport", ctypes.c_byte),
        ("bySupport1", ctypes.c_byte),
        ("bySupport2", ctypes.c_byte),
        ("wDevType", ctypes.c_uint16),
        ("bySupport3", ctypes.c_byte),
        ("byMultiStream", ctypes.c_byte),
        ("wStartDChan", ctypes.c_uint16),
        ("wStartDTalkChan", ctypes.c_uint16),
        ("wHighAlarmInPortNum", ctypes.c_uint16),
        ("wHighAlarmOutPortNum", ctypes.c_uint16),
        ("bySupport4", ctypes.c_byte),
        ("byLanguageType", ctypes.c_byte),
        ("byVoiceInChanNum", ctypes.c_byte),
        ("byStartVoiceInChanNo", ctypes.c_byte),
        ("byRes3", ctypes.c_byte * 2),
        ("byMirrorChanNum", ctypes.c_byte),
        ("wStartMirrorChanNo", ctypes.c_uint16),
        ("byRes2", ctypes.c_byte * 2),
    ]


def _load_sdk():
    global _sdk
    if _sdk is not None:
        return _sdk
    candidates = [
        BASE_DIR / "lib" / "libhcnetsdk.so",
        Path("./lib/libhcnetsdk.so"),
        Path("/app/lib/libhcnetsdk.so"),
    ]
    for path in candidates:
        if path.exists():
            _sdk = ctypes.cdll.LoadLibrary(str(path))
            return _sdk
    raise RuntimeError("Hikvision SDK library not found in lib/libhcnetsdk.so")


def download_hikvision(
    nvr: models.NVR,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str,
    progress: ProgressCallback = None,
) -> tuple[bool, str]:
    """Download a recording range using the official Hikvision SDK."""
    password = decrypt_secret(nvr.password_enc)
    try:
        sdk = _load_sdk()
    except Exception as exc:
        return False, str(exc)

    with _sdk_lock:
        try:
            sdk.NET_DVR_Init()
            device_info = NET_DVR_DEVICEINFO_V30()
            user_id = sdk.NET_DVR_Login_V30(
                nvr.host.encode("utf-8"),
                int(nvr.sdk_port or 8000),
                (nvr.username or "admin").encode("utf-8"),
                password.encode("utf-8"),
                ctypes.byref(device_info),
            )
            if user_id < 0:
                return False, f"NVR login failed (SDK error {sdk.NET_DVR_GetLastError()})"

            target_channel = int(channel) + (0 if nvr.protocol == "dvr" else int(nvr.channel_offset or 0))
            start_struct = NET_DVR_TIME(start_dt.year, start_dt.month, start_dt.day, start_dt.hour, start_dt.minute, start_dt.second)
            end_struct = NET_DVR_TIME(end_dt.year, end_dt.month, end_dt.day, end_dt.hour, end_dt.minute, end_dt.second)

            abs_path = os.path.abspath(output_path)
            handle = sdk.NET_DVR_GetFileByTime(
                user_id,
                target_channel,
                ctypes.byref(start_struct),
                ctypes.byref(end_struct),
                abs_path.encode("utf-8"),
            )
            if handle < 0:
                error = sdk.NET_DVR_GetLastError()
                sdk.NET_DVR_Logout(user_id)
                sdk.NET_DVR_Cleanup()
                return False, f"Could not start download (SDK error {error})"

            sdk.NET_DVR_PlayBackControl_V40.argtypes = [
                ctypes.c_int,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            sdk.NET_DVR_PlayBackControl_V40.restype = ctypes.c_bool
            if not sdk.NET_DVR_PlayBackControl_V40(handle, 1, None, 0, None, None):
                error = sdk.NET_DVR_GetLastError()
                sdk.NET_DVR_StopGetFile(handle)
                sdk.NET_DVR_Logout(user_id)
                sdk.NET_DVR_Cleanup()
                return False, f"Could not start data stream (SDK error {error})"

            last_progress = -1
            while True:
                value = sdk.NET_DVR_GetDownloadPos(handle)
                if value == 100:
                    break
                if value > 100 or value < 0:
                    sdk.NET_DVR_StopGetFile(handle)
                    sdk.NET_DVR_Logout(user_id)
                    sdk.NET_DVR_Cleanup()
                    return False, f"Download failed / recording not found (code {value})"
                if progress and value != last_progress:
                    last_progress = value
                    progress(int(value), f"Downloading from NVR {value}%")
                time.sleep(1)

            sdk.NET_DVR_StopGetFile(handle)
            sdk.NET_DVR_Logout(user_id)
            sdk.NET_DVR_Cleanup()
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return False, "NVR returned an empty file (no recording in that range)"
            return True, "Downloaded"
        except Exception as exc:
            try:
                sdk.NET_DVR_Cleanup()
            except Exception:
                pass
            return False, f"SDK error: {exc}"


# ---------------------------------------------------------------------------
# TruVision / Hikvision ISAPI chunk downloader
# ---------------------------------------------------------------------------


def _isapi_auth(nvr: models.NVR) -> HTTPDigestAuth:
    return HTTPDigestAuth(nvr.username, decrypt_secret(nvr.password_enc))


def download_truvision(
    nvr: models.NVR,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str,
    progress: ProgressCallback = None,
) -> tuple[bool, str]:
    """Search recording chunks via ISAPI, merge, then trim to the exact window."""
    password = decrypt_secret(nvr.password_enc)
    auth = HTTPDigestAuth(nvr.username, password)
    base = f"http://{nvr.host}" if int(nvr.port or 80) == 80 else f"http://{nvr.host}:{int(nvr.port)}"
    track_id = int(channel) * int(nvr.track_multiplier or 100) + 1

    offset_hours = float(nvr.time_offset_hours or 0)
    # v2 auto-detection: some NVRs report UTC while requests are local (+7).
    search_start = start_dt - timedelta(hours=2)
    search_end = end_dt + timedelta(minutes=5)
    if offset_hours:
        search_start -= timedelta(hours=offset_hours)
        search_end -= timedelta(hours=offset_hours)

    def iso(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    search_id = str(uuid.uuid4()).upper()
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<CMSearchDescription>"
        f"<searchID>{search_id}</searchID>"
        f"<trackList><trackID>{track_id}</trackID></trackList>"
        f"<timeSpanList><timeSpan><startTime>{iso(search_start)}</startTime>"
        f"<endTime>{iso(search_end)}</endTime></timeSpan></timeSpanList>"
        "<maxResults>100</maxResults>"
        "<searchResultPostion>0</searchResultPostion>"
        "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
        "</CMSearchDescription>"
    )
    try:
        response = requests.post(
            f"{base}/ISAPI/ContentMgmt/search",
            auth=auth,
            data=xml.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:
        return False, f"ISAPI search failed: {exc}"

    items = re.findall(r"<searchMatchItem>(.*?)</searchMatchItem>", response.text, re.DOTALL)
    target_chunks: list[str] = []
    first_actual_start: datetime | None = None
    for item in items:
        try:
            meta_start = datetime.strptime(
                re.search(r"<startTime>([^<]+)</startTime>", item).group(1).replace("T", " ").replace("Z", ""),
                "%Y-%m-%d %H:%M:%S",
            )
            meta_end = datetime.strptime(
                re.search(r"<endTime>([^<]+)</endTime>", item).group(1).replace("T", " ").replace("Z", ""),
                "%Y-%m-%d %H:%M:%S",
            )
            uri = re.search(r"<playbackURI>(.*?)</playbackURI>", item, re.DOTALL).group(1).replace("&amp;", "&")
        except (AttributeError, ValueError):
            continue
        if offset_hours:
            meta_start += timedelta(hours=offset_hours)
            meta_end += timedelta(hours=offset_hours)
        elif (start_dt - meta_start).total_seconds() > 18000:
            # Legacy auto-fix for UTC-reporting NVRs (Cambodia +7).
            meta_start += timedelta(hours=7)
            meta_end += timedelta(hours=7)

        if meta_start <= start_dt < meta_end or start_dt < meta_start < end_dt:
            target_chunks.append(uri)
            if first_actual_start is None:
                first_actual_start = meta_start

    if not target_chunks:
        return False, "No recordings found for that time window"

    work_dir = os.path.dirname(os.path.abspath(output_path))
    base_name = Path(output_path).stem
    downloaded: list[str] = []
    concat_file = os.path.join(work_dir, f"concat_{uuid.uuid4().hex[:8]}.txt")
    merged_path = os.path.join(work_dir, f"merged_{base_name}_{uuid.uuid4().hex[:6]}.mp4")

    try:
        for index, uri in enumerate(target_chunks):
            chunk_path = os.path.join(work_dir, f"raw_{base_name}_{index}.mp4")
            if progress:
                pct = 5 + int((index / max(1, len(target_chunks))) * 45)
                progress(pct, f"Downloading chunk {index + 1}/{len(target_chunks)}")
            download_xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f"<downloadRequest><playbackURI>{uri}</playbackURI></downloadRequest>"
            )
            try:
                dl = requests.post(
                    f"{base}/ISAPI/ContentMgmt/download",
                    auth=auth,
                    data=download_xml.encode("utf-8"),
                    stream=True,
                    timeout=(15, 90),
                )
                if dl.status_code != 200:
                    continue
                with open(chunk_path, "wb") as handle:
                    for chunk in dl.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                if os.path.getsize(chunk_path) > 0:
                    downloaded.append(chunk_path)
            except requests.RequestException:
                if os.path.exists(chunk_path):
                    try:
                        os.remove(chunk_path)
                    except OSError:
                        pass

        if not downloaded:
            return False, "All recording chunks failed to download"

        if progress:
            progress(55, "Merging recording chunks")
        if len(downloaded) > 1:
            with open(concat_file, "w", encoding="utf-8") as handle:
                for path in downloaded:
                    handle.write(f"file '{os.path.abspath(path)}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", merged_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1200,
            )
            source = merged_path if os.path.exists(merged_path) else downloaded[0]
        else:
            source = downloaded[0]

        offset_sec = 0.0
        if first_actual_start:
            offset_sec = max(0.0, (start_dt - first_actual_start).total_seconds())
        duration_sec = max(1.0, (end_dt - start_dt).total_seconds())

        if progress:
            progress(65, "Trimming to requested time")
        process = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(offset_sec),
                "-i",
                source,
                "-t",
                str(duration_sec),
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                output_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=1800,
        )
        if process.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, "Failed to trim the recording"
        return True, "Downloaded"
    except Exception as exc:
        return False, f"TruVision download error: {exc}"
    finally:
        for path in downloaded:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        for path in (concat_file, merged_path):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


def download_camera(
    nvr: models.NVR,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    output_path: str,
    progress: ProgressCallback = None,
) -> tuple[bool, str]:
    if end_dt <= start_dt:
        return False, "End time must be after start time"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    protocol = (nvr.protocol or "hikvision").lower()
    if protocol in {"truvision", "isapi"}:
        return download_truvision(nvr, channel, start_dt, end_dt, output_path, progress)
    if protocol in {"hikvision", "dvr", "hik", "nvr"}:
        return download_hikvision(nvr, channel, start_dt, end_dt, output_path, progress)
    if protocol == "rtsp":
        return False, "RTSP-only devices cannot export past recordings (use Hikvision or TruVision protocol)"
    return False, f"Unsupported protocol: {nvr.protocol}"


# ---------------------------------------------------------------------------
# Backward-compatible v2 helpers
# ---------------------------------------------------------------------------


def download_video(nvr_code, ip, port, username, password, channel, date_str, start_time, end_time, output_filename):
    fake = models.NVR(
        name=nvr_code,
        code=nvr_code,
        campus="legacy",
        zone="legacy",
        protocol="dvr" if "dvr" in nvr_code.lower() else "hikvision",
        host=ip,
        port=int(port),
        sdk_port=int(port),
        username=username,
        password_enc=password,
        channel_offset=0 if "dvr" in nvr_code.lower() else 32,
    )
    start_dt = parse_boundary(date_str, start_time)
    end_dt = parse_boundary(date_str, end_time)
    ok, _ = download_hikvision(fake, int(channel), start_dt, end_dt, output_filename)
    return ok


def download_truvision_footage(nvr_code, camera_id, start_time, end_time, nvr: models.NVR | None = None):
    if nvr is None:
        raise ValueError("nvr model is required in v3")
    path = os.path.join(os.getcwd(), f"TruVision_{nvr.code}_{camera_id}.mp4")
    ok, _ = download_truvision(nvr, int(camera_id), start_time, end_time, path)
    return path if ok else None
