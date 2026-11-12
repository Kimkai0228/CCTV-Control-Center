from __future__ import annotations

import pytest

from app.security import decrypt_secret, encrypt_secret, hash_password, verify_password


def test_password_hash_roundtrip():
    stored = hash_password("S3cret!pass")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("S3cret!pass", stored)
    assert not verify_password("wrong-password", stored)


def test_password_hash_is_salted():
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)


def test_password_minimum_length():
    with pytest.raises(ValueError):
        hash_password("abc")


def test_secret_encryption_roundtrip():
    encrypted = encrypt_secret("telegram-token-value")
    assert encrypted != "telegram-token-value"
    assert decrypt_secret(encrypted) == "telegram-token-value"


def test_secret_decrypt_tolerates_plaintext():
    assert decrypt_secret("legacy-plain-value") == "legacy-plain-value"
    assert decrypt_secret("") == ""
    assert decrypt_secret(None) == ""
