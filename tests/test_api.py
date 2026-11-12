from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_login_and_dashboard(admin):
    assert admin.get("/api/auth/me").json()["user"]["role"] == "superadmin"
    dashboard = admin.get("/api/dashboard")
    assert dashboard.status_code == 200
    body = dashboard.json()
    assert "summary" in body
    assert "compression" in body
    assert admin.get("/").status_code == 200
    assert admin.get("/miniapp").status_code == 200


def test_bad_login(client):
    response = client.post("/api/auth/login", json={"username": "nobody", "password": "nope"})
    assert response.status_code == 401


def test_protected_pages_redirect(client):
    anon = TestClient(client.app)
    response = anon.get("/jobs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_user_crud_and_permissions(admin):
    username = f"pytest_{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={
            "username": username,
            "password": "FirstPass1!",
            "email": f"{username}@example.com",
            "role": "operator",
        },
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["user"]["id"]

    reset = admin.post(f"/api/users/{user_id}/reset-password", json={"password": "SecondPass2!"})
    assert reset.status_code == 200

    with TestClient(admin.app) as operator:
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": "FirstPass1!"}
        ).status_code == 401
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": "SecondPass2!"}
        ).status_code == 200
        assert operator.get("/api/users").status_code == 403
        assert operator.get("/api/settings").status_code == 403
        assert operator.get("/api/dashboard").status_code == 200
        assert operator.get("/api/tree").status_code == 200

    assert admin.delete(f"/api/users/{user_id}").status_code == 200
    assert admin.delete(f"/api/users/{user_id}").status_code == 404


def test_superadmin_cannot_be_deleted(admin):
    users = admin.get("/api/users").json()["users"]
    superadmin = next(user for user in users if user["role"] == "superadmin")
    response = admin.delete(f"/api/users/{superadmin['id']}")
    assert response.status_code == 400


def test_settings_masking_for_regular_admin(admin):
    token = f"123456:TEST{uuid.uuid4().hex[:10]}"
    updated = admin.put(
        "/api/integrations/telegram",
        json={"bot_token": token, "group_id": "-1001234567890", "alerts_enabled": True, "miniapp_enabled": True},
    )
    assert updated.status_code == 200, updated.text

    values = admin.get("/api/settings").json()["values"]
    assert values["telegram.bot_token"] == "********"
    assert "google.service_account_json" in values

    username = f"admin_{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={"username": username, "password": "AdminPass1!", "role": "admin"},
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["user"]["id"]
    try:
        with TestClient(admin.app) as regular_admin:
            assert regular_admin.post(
                "/api/auth/login", json={"username": username, "password": "AdminPass1!"}
            ).status_code == 200
            settings = regular_admin.get("/api/settings")
            assert settings.status_code == 200
            assert "telegram.bot_token" not in settings.json()["values"]
            assert "google.service_account_json" not in settings.json()["values"]
    finally:
        admin.delete(f"/api/users/{user_id}")


def test_settings_validation(admin):
    before = admin.get("/api/settings").json()["values"]["app.name"]
    response = admin.put(
        "/api/settings",
        json={"values": {"app.primary_color": "not-a-color", "health.hdd_threshold_percent": 10}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert len(body["errors"]) >= 2
    assert admin.get("/api/settings").json()["values"]["app.name"] == before

    unknown = admin.put("/api/settings", json={"values": {"not.a.setting": "x"}})
    assert unknown.json()["ok"] is False
    assert "Unknown setting" in unknown.json()["errors"][0]


def test_job_validation(admin):
    no_cameras = admin.post(
        "/api/jobs",
        json={"cameras": [], "date": "30-10-2026", "start_time": "10:00", "end_time": "11:00"},
    )
    assert no_cameras.status_code == 400

    reversed_range = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": 1, "channel": 1}],
            "date": "30-10-2026",
            "start_time": "11:00",
            "end_time": "10:00",
        },
    )
    assert reversed_range.status_code == 400

    too_long = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": 1, "channel": 1}],
            "date": "30-10-2026",
            "start_time": "00:00",
            "end_time": "23:00",
        },
    )
    assert too_long.status_code == 400
    assert "12 hours" in too_long.json()["detail"]


def test_miniapp_requires_identity(client):
    anon = TestClient(client.app)
    assert anon.get("/api/miniapp/bootstrap").status_code == 401


def test_miniapp_browser_preview(admin):
    response = admin.get("/api/miniapp/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert "tree" in body
    assert "identity" in body
    assert body["identity"]["role"] == "superadmin"
