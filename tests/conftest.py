"""Pytest bootstrap: isolated database, no bot, no scheduler."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="cctv-v3-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["WORK_DIR"] = os.path.join(_TMP, "work")
os.environ["SECRET_KEY"] = "pytest-secret-key-please-change-123456"
os.environ["SUPER_ADMIN_USER"] = "testadmin"
os.environ["SUPER_ADMIN_PASSWORD"] = "TestPass123!"
os.environ["SUPER_ADMIN_EMAIL"] = "testadmin@example.com"
os.environ["RUN_BOT"] = "false"
os.environ["RUN_SCHEDULER"] = "false"
os.environ["SEED_LEGACY_NVRS"] = "false"
os.environ["PUBLIC_URL"] = "http://testserver"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def admin(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "testadmin", "password": "TestPass123!"},
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def db():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
