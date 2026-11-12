"""Signed direct-download links for campus agent files.

The agent host is reachable from a browser but the main host does not keep a
copy of large agent outputs.  Instead of exposing the agent token, the main
host signs a short-lived URL scoped to one job file.  The agent verifies the
signature with the same token it already uses for its API calls, so no extra
secret has to be configured on the campus host.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

# Links are regenerated whenever a Jobs page is loaded.  Six hours is long
# enough for a very large download while keeping a leaked URL short-lived.
DEFAULT_TTL_SECONDS = 6 * 60 * 60


def _payload(job_id: int, filename: str, expires: int) -> bytes:
    return f"{int(job_id)}\n{filename}\n{int(expires)}".encode("utf-8")


def sign_download(token: str, job_id: int, filename: str, expires: int) -> str:
    return hmac.new(
        (token or "").encode("utf-8"),
        _payload(job_id, filename, expires),
        hashlib.sha256,
    ).hexdigest()


def verify_download(
    token: str,
    job_id: int,
    filename: str,
    expires: int,
    signature: str,
) -> bool:
    if not token or not signature:
        return False
    expected = sign_download(token, job_id, filename, expires)
    return hmac.compare_digest(expected, str(signature))


def build_agent_download_url(
    public_url: str,
    token: str,
    job_id: int,
    filename: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str | None:
    base = (public_url or "").strip().rstrip("/")
    if not base or not token or not filename:
        return None
    if not base.startswith(("http://", "https://")):
        return None
    expires = int(time.time()) + max(60, int(ttl_seconds))
    signature = sign_download(token, job_id, filename, expires)
    return (
        f"{base}/agent-files/{int(job_id)}/{quote(filename, safe='')}"
        f"?expires={expires}&sig={signature}"
    )
