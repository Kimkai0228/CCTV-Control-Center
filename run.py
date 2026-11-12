#!/usr/bin/env python3
"""Entrypoint for the CCTV V3 web app + Telegram bot."""

from __future__ import annotations

import uvicorn

from app.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
