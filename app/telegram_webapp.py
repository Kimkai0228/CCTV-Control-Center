"""Telegram MiniApp initData validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException

MAX_AGE_SECONDS = 60 * 60 * 24


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE_SECONDS) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData is missing")
    if not bot_token:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram initData hash is missing")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData signature")

    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if max_age and auth_date and (time.time() - auth_date) > max_age:
        raise HTTPException(status_code=401, detail="Telegram session expired, reopen the MiniApp")

    user: dict = {}
    if values.get("user"):
        try:
            user = json.loads(values["user"])
        except ValueError:
            user = {}
    return {"user": user, "auth_date": auth_date, "raw": values}
