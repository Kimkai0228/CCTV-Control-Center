from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlencode

import pytest
from fastapi import HTTPException

from app.telegram_webapp import validate_init_data

BOT_TOKEN = "123456:TEST-TOKEN"


def make_init_data(user_id: int = 42, auth_date: int | None = None) -> str:
    auth_date = auth_date or int(time.time())
    user = json.dumps({"id": user_id, "first_name": "Test", "username": "tester"}, separators=(",", ":"))
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAExample",
        "user": user,
    }
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**values, "hash": digest}, quote_via=quote)


def test_valid_init_data():
    payload = validate_init_data(make_init_data(), BOT_TOKEN)
    assert payload["user"]["id"] == 42
    assert payload["user"]["username"] == "tester"


def test_invalid_hash_rejected():
    data = make_init_data().replace("hash=", "hash=bad")
    with pytest.raises(HTTPException) as exc:
        validate_init_data(data, BOT_TOKEN)
    assert exc.value.status_code == 401


def test_wrong_bot_token_rejected():
    with pytest.raises(HTTPException):
        validate_init_data(make_init_data(), "999:OTHER")


def test_expired_init_data_rejected():
    old = int(time.time()) - 60 * 60 * 48
    with pytest.raises(HTTPException) as exc:
        validate_init_data(make_init_data(auth_date=old), BOT_TOKEN)
    assert "expired" in exc.value.detail.lower()


def test_missing_bot_token():
    with pytest.raises(HTTPException) as exc:
        validate_init_data(make_init_data(), "")
    assert exc.value.status_code == 503
