"""Video compression with Intel QuickSync and an instant software fallback.

QuickSync is probed once (and re-probed when the device or ffmpeg changes).
If the host container has no /dev/dri render node, or the probe fails, we
immediately use libx264 so a job never stalls waiting on broken hardware.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any

_probe_lock = threading.Lock()
_HW_RECHECK_SECONDS = 600


@dataclass
class CompressionResult:
    path: str
    compressed: bool
    backend: str
    message: str
    original_size: int = 0
    final_size: int = 0

    @property
    def saved_percent(self) -> float:
        if not self.original_size:
            return 0.0
        return round(max(0.0, (1 - self.final_size / self.original_size) * 100), 1)


@dataclass
class _HardwareState:
    backend: str | None = None
    detail: str = "not checked"
    qsv_ok: bool | None = None
    qsv_detail: str = ""
    qsv_init: list[str] | None = None
    vaapi_ok: bool | None = None
    vaapi_detail: str = ""
    checked_at: float = 0.0


_hw_state = _HardwareState()


def _render_nodes() -> list[str]:
    return sorted(glob.glob("/dev/dri/renderD*"))


def _has_render_node() -> bool:
    return bool(_render_nodes())


def _render_node() -> str:
    nodes = _render_nodes()
    return nodes[0] if nodes else "/dev/dri/renderD128"


def _node_access_problem() -> str | None:
    """Explain why the render node cannot be used by this process, if so."""
    nodes = _render_nodes()
    if not nodes:
        return None
    node = nodes[0]
    if os.access(node, os.R_OK | os.W_OK):
        return None
    try:
        info = os.stat(node)
        gid = info.st_gid
        mode = oct(info.st_mode & 0o777)
    except OSError:
        gid, mode = -1, "?"
    return (
        f"render node {node} is present but not accessible to uid={os.getuid()} "
        f"gid={os.getgid()} (device gid={gid}, mode={mode}); add the host render "
        "group GID or run the container with the matching PUID/PGID"
    )


def _probe_encode(ffmpeg: str, encoder: str, init_args: list[str], vf: str) -> tuple[bool, str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        *init_args,
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=30:d=0.4",
        "-vf",
        vf,
        "-c:v",
        encoder,
        "-frames:v",
        "2",
        "-f",
        "null",
        "-",
    ]
    try:
        process = subprocess.run(cmd, capture_output=True, timeout=25)
        if process.returncode == 0:
            return True, ""
        return False, process.stderr.decode("utf-8", "ignore")[-400:] or "probe failed"
    except Exception as exc:  # subprocess timeout / missing driver
        return False, f"probe error: {exc}"


def _probe_qsv(ffmpeg: str) -> tuple[bool, str, list[str]]:
    """Try direct MFX first, then QSV derived from a VAAPI device."""
    direct = ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
    ok, detail = _probe_encode(ffmpeg, "h264_qsv", direct, "format=nv12,hwupload=extra_hw_frames=16")
    if ok:
        return True, "", direct
    node = _render_node()
    derived = [
        "-init_hw_device",
        f"vaapi=va:{node}",
        "-init_hw_device",
        "qsv=hw@va",
        "-filter_hw_device",
        "hw",
    ]
    ok2, detail2 = _probe_encode(
        ffmpeg, "h264_qsv", derived, "format=nv12,hwupload=extra_hw_frames=16"
    )
    if ok2:
        return True, "", derived
    # Keep the more informative of the two errors.
    return False, (detail or detail2), direct


def _probe_vaapi(ffmpeg: str) -> tuple[bool, str]:
    node = _render_node()
    init = ["-init_hw_device", f"vaapi=va:{node}", "-filter_hw_device", "va"]
    return _probe_encode(ffmpeg, "h264_vaapi", init, "format=nv12,hwupload")


def probe_hardware(force: bool = False) -> tuple[str | None, str]:
    """Return the best hardware backend ("qsv"/"vaapi"/None) and a detail string."""
    global _hw_state
    now = time.time()
    with _probe_lock:
        if not force and _hw_state.backend is not None:
            if now - _hw_state.checked_at < _HW_RECHECK_SECONDS:
                return _hw_state.backend, _hw_state.detail
        if not force and _hw_state.backend is None and _hw_state.checked_at:
            if now - _hw_state.checked_at < _HW_RECHECK_SECONDS:
                return None, _hw_state.detail

        state = _HardwareState(checked_at=now)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            state.detail = "ffmpeg not installed"
            _hw_state = state
            return None, state.detail
        if not _has_render_node():
            state.detail = "no /dev/dri render node mapped into the container"
            _hw_state = state
            return None, state.detail
        access_problem = _node_access_problem()
        if access_problem:
            state.detail = access_problem
            _hw_state = state
            return None, state.detail

        state.qsv_ok, state.qsv_detail, state.qsv_init = _probe_qsv(ffmpeg)
        if state.qsv_ok:
            state.backend = "qsv"
            state.detail = "Intel QuickSync ready (MFX/QSV)"
            _hw_state = state
            return state.backend, state.detail

        # MFX is missing or unsupported on newer GPUs; VAAPI still uses the
        # same QuickSync hardware encoder and is the reliable fallback.
        state.vaapi_ok, state.vaapi_detail = _probe_vaapi(ffmpeg)
        if state.vaapi_ok:
            state.backend = "vaapi"
            state.detail = "Intel QuickSync ready (VAAPI)"
            _hw_state = state
            return state.backend, state.detail

        state.backend = None
        state.detail = (
            f"qsv: {(state.qsv_detail or 'unavailable').strip()} | "
            f"vaapi: {(state.vaapi_detail or 'unavailable').strip()}"
        )[-600:]
        _hw_state = state
        return None, state.detail


def probe_qsv(force: bool = False) -> tuple[bool, str]:
    """Backward-compatible probe: True when any hardware encoder is usable."""
    backend, detail = probe_hardware(force)
    return bool(backend), detail


def qsv_info() -> dict[str, Any]:
    backend, detail = probe_hardware()
    state = _hw_state
    return {
        "available": bool(backend),
        "backend": backend or "software",
        "detail": detail,
        "qsv": {"available": bool(state.qsv_ok), "detail": state.qsv_detail},
        "vaapi": {"available": bool(state.vaapi_ok), "detail": state.vaapi_detail},
        "render_node": _has_render_node(),
        "render_nodes": _render_nodes(),
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
        "checked_at": state.checked_at,
        "ffmpeg": shutil.which("ffmpeg") or "",
    }


def _audio_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", "96k", "-ac", "1"]


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    try:
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        if process.returncode == 0:
            return True, ""
        return False, process.stderr.decode("utf-8", "ignore")[-800:]
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except Exception as exc:
        return False, str(exc)


def compress_file(
    input_path: str,
    settings: dict[str, Any] | None = None,
    *,
    force_backend: str | None = None,
) -> CompressionResult:
    """Compress an MP4 in place and return the resulting path.

    ``settings`` uses the same dotted keys as the settings table. If anything
    fails, the original path is returned untouched.
    """
    settings = settings or {}

    def flag(key: str, default: str) -> str:
        return str(settings.get(key, default))

    if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
        return CompressionResult(input_path, False, "none", "input missing", 0, 0)
    if flag("compression.enabled", "true").lower() not in {"1", "true", "yes", "on"}:
        return CompressionResult(input_path, False, "disabled", "compression disabled", os.path.getsize(input_path), os.path.getsize(input_path))

    original_size = os.path.getsize(input_path)
    base, ext = os.path.splitext(input_path)
    output_path = f"{base}_compressed{ext or '.mp4'}"
    backend_pref = (force_backend or flag("compression.backend", "auto")).lower()
    errors: list[str] = []

    hw_backend, hw_detail = probe_hardware()
    use_hw = bool(hw_backend) and backend_pref in {"auto", "qsv", "vaapi"}
    if backend_pref == "qsv" and hw_backend != "qsv":
        use_hw = False
    if backend_pref == "vaapi" and hw_backend != "vaapi":
        use_hw = False

    if use_hw:
        if hw_backend == "qsv":
            init_args = list(
                _hw_state.qsv_init
                or ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
            )
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-y",
                *init_args,
                "-i",
                input_path,
                "-vf",
                "format=nv12,hwupload=extra_hw_frames=64",
                "-c:v",
                "h264_qsv",
                "-preset",
                flag("compression.qsv_preset", "faster"),
                "-global_quality",
                flag("compression.qsv_quality", "25"),
                "-look_ahead",
                "0",
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                *_audio_args(),
                "-max_muxing_queue_size",
                "4096",
                output_path,
            ]
            label = "Intel QuickSync (QSV)"
        else:
            node = _render_node()
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-init_hw_device",
                f"vaapi=va:{node}",
                "-filter_hw_device",
                "va",
                "-i",
                input_path,
                "-vf",
                "format=nv12,hwupload",
                "-c:v",
                "h264_vaapi",
                "-qp",
                flag("compression.qsv_quality", "25"),
                "-movflags",
                "+faststart",
                *_audio_args(),
                "-max_muxing_queue_size",
                "4096",
                output_path,
            ]
            label = "Intel QuickSync (VAAPI)"
        ok, err = _run(cmd, timeout=1800)
        if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            return CompressionResult(
                output_path,
                True,
                hw_backend,
                label,
                original_size,
                os.path.getsize(output_path),
            )
        errors.append(f"{hw_backend}: {err or hw_detail}")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        # Force the next probe to notice a broken/removed device.
        probe_hardware(force=True)
        if backend_pref in {"qsv", "vaapi"}:
            backend_pref = "software"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        input_path,
        "-c:v",
        "libx264",
        "-preset",
        flag("compression.cpu_preset", "ultrafast"),
        "-crf",
        flag("compression.cpu_crf", "30"),
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        *_audio_args(),
        "-threads",
        "0",
        "-max_muxing_queue_size",
        "4096",
        output_path,
    ]
    ok, err = _run(cmd, timeout=3600)
    if ok and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        return CompressionResult(
            output_path,
            True,
            "software",
            "libx264",
            original_size,
            os.path.getsize(output_path),
        )
    errors.append(f"cpu: {err}")
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass
    return CompressionResult(input_path, False, "none", "; ".join(errors)[-800:], original_size, original_size)


def replace_original(result: CompressionResult, original_path: str | None = None) -> str:
    """Swap a compressed file back onto its original filename.

    Returns the path that should be used by the caller. When no original
    filename is supplied the compressed path is returned unchanged.
    """
    if not result.compressed or not os.path.exists(result.path):
        return original_path or result.path
    if not original_path or os.path.abspath(original_path) == os.path.abspath(result.path):
        return result.path
    try:
        if os.path.exists(original_path):
            os.remove(original_path)
        os.replace(result.path, original_path)
        return original_path
    except OSError:
        return result.path


def compress_video_qsv(input_path: str, settings: dict[str, Any] | None = None) -> str:
    """Backward-compatible v2 helper: returns the path to use."""
    result = compress_file(input_path, settings)
    return result.path


def make_safe_temp(suffix: str = ".mp4") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path
