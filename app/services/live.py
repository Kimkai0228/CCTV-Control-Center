"""Low-latency live view: RTSP to MJPEG through ffmpeg.

The ISAPI snapshot endpoint returns a single low-resolution JPEG, so polling it
tops out around three frames per second.  Pulling the recorder's RTSP stream
and transcoding it to MJPEG keeps the browser side unchanged (a plain ``<img>``
tag) while providing smooth playback at the camera's native picture size.

The RTSP URL contains the NVR password, so it is never placed on the ffmpeg
command line.  A tiny concat script is written to an in-memory file descriptor
and passed to ffmpeg by path; only ``/proc/self/fd/<n>`` appears in the process
list.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from urllib.parse import quote

from .. import models
from ..security import decrypt_secret

# quality key -> (max output width, output fps, MJPEG qscale)
# qscale is the ffmpeg MJPEG quality: lower is better and uses more bandwidth.
PRESETS: dict[str, tuple[int, int, int]] = {
    "low": (640, 8, 22),
    "balanced": (960, 10, 16),
    "high": (1280, 12, 8),
}

BOUNDARY = "ffmpeg"

# The concat demuxer opens the RTSP URL internally, so its nested protocols
# must be allow-listed.  RTP is included for the default UDP transport.
PROTOCOL_WHITELIST = "file,crypto,data,rtsp,tcp,udp,rtp"


@dataclass(frozen=True)
class LiveProfile:
    width: int
    fps: int
    quality: int
    stream: str


@dataclass
class LiveStream:
    process: subprocess.Popen
    cleanup_path: str | None = None

    def stop(self) -> None:
        try:
            self.process.terminate()
            self.process.wait(timeout=3)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
        if self.cleanup_path:
            try:
                os.remove(self.cleanup_path)
            except OSError:
                pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def profile_for(quality: str | None, stream: str | None) -> LiveProfile:
    key = (quality or "balanced").strip().lower()
    width, fps, qscale = PRESETS.get(key, PRESETS["balanced"])
    selected = (stream or "main").strip().lower()
    if selected not in {"main", "sub"}:
        selected = "main"
    if selected == "sub":
        # Substreams are smaller already; avoid upscaling them.
        width = min(width, 960)
        fps = min(fps, 10)
    return LiveProfile(width=width, fps=fps, quality=qscale, stream=selected)


def _credentials(nvr: models.NVR) -> str:
    username = quote(nvr.username or "", safe="")
    password = quote(decrypt_secret(nvr.password_enc) or "", safe="")
    return f"{username}:{password}@"


def _host(nvr: models.NVR) -> str:
    host = (nvr.host or "").strip()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def rtsp_url(nvr: models.NVR, camera: models.Camera, stream: str = "main") -> str:
    """Build the RTSP URL for a camera.

    A per-camera ``rtsp_path`` wins, which lets installs with non-standard
    channel numbering override the default.  Otherwise Hikvision, TruVision
    and DVR recorders all use ``/Streaming/Channels/<channel><stream>``.
    """
    path = (camera.rtsp_path or "").strip()
    creds = _credentials(nvr)
    if path.lower().startswith("rtsp://"):
        scheme, rest = path.split("://", 1)
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            return path
        return f"{scheme}://{creds}{rest}"

    index = 2 if stream == "sub" else 1
    if not path:
        path = f"/Streaming/Channels/{int(camera.channel) * 100 + index}"
    elif not path.startswith("/"):
        path = "/" + path
    return f"rtsp://{creds}{_host(nvr)}:554{path}"


def _url_source(url: str) -> tuple[int | None, str, str | None]:
    """Keep the credentialed RTSP URL off the ffmpeg command line.

    Returns ``(pass_fd, input_path, cleanup_path)``.  On Linux the script lives
    in an anonymous memfd and is inherited by ffmpeg.  A mode-600 temporary
    file is used only as a fallback for non-Linux development hosts.
    """
    content = f"ffconcat version 1.0\nfile '{url.replace(chr(10), '').replace(chr(13), '')}'\n".encode()
    try:
        fd = os.memfd_create("cctv-live")  # type: ignore[attr-defined]
        os.write(fd, content)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd, f"/proc/self/fd/{fd}", None
    except (AttributeError, OSError):
        fd, path = tempfile.mkstemp(prefix="cctv-live-", suffix=".txt")
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content)
        finally:
            os.close(fd)
        return None, path, path


def build_command(input_path: str, profile: LiveProfile) -> list[str]:
    return [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        PROTOCOL_WHITELIST,
        "-f",
        "concat",
        "-safe",
        "0",
        "-fflags",
        "nobuffer",
        "-probesize",
        "1000000",
        "-analyzeduration",
        "1000000",
        "-i",
        input_path,
        "-an",
        "-vf",
        f"scale=w='min({profile.width},iw)':h=-2",
        "-r",
        str(profile.fps),
        "-c:v",
        "mjpeg",
        "-q:v",
        str(profile.quality),
        "-f",
        "mpjpeg",
        "-",
    ]


def start_process(url: str, profile: LiveProfile) -> LiveStream:
    """Start ffmpeg and return the process with a pipe on stdout."""
    pass_fd, input_path, cleanup_path = _url_source(url)
    try:
        process = subprocess.Popen(
            build_command(input_path, profile),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            pass_fds=(pass_fd,) if pass_fd is not None else (),
        )
    except Exception:
        if pass_fd is not None:
            os.close(pass_fd)
        if cleanup_path:
            try:
                os.remove(cleanup_path)
            except OSError:
                pass
        raise
    if pass_fd is not None:
        # ffmpeg inherited the descriptor; the parent no longer needs it.
        os.close(pass_fd)
    return LiveStream(process=process, cleanup_path=cleanup_path)
