from __future__ import annotations

import datetime as dt
import json
import uuid

from app import models


def _make_nvr(admin, campus="ttp"):
    suffix = uuid.uuid4().hex[:6]
    response = admin.post(
        "/api/nvrs",
        json={
            "name": f"Test NVR {suffix}",
            "code": f"NVR{suffix}",
            "campus": campus,
            "zone": "ecp",
            "protocol": "hikvision",
            "host": "192.0.2.10",
            "port": 80,
            "username": "admin",
            "password": "secret",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["nvr"]


def test_disabled_camera_is_ignored_everywhere(admin, db):
    nvr = _make_nvr(admin, campus="ttp")
    created = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 9, "name": "Unused port"},
    )
    assert created.status_code == 200, created.text
    camera_id = created.json()["camera"]["id"]
    summary_before = admin.get("/api/v1/cameras/summary").json()

    disabled = admin.patch(f"/api/cameras/{camera_id}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["camera"]["enabled"] is False
    assert disabled.json()["camera"]["online"] is None

    tree = admin.get("/api/tree").json()
    tree_nvr = next(item for item in tree["nvrs"] if item["id"] == nvr["id"])
    assert camera_id not in {camera["id"] for camera in tree_nvr["cameras"]}

    rejected = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 9}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "folder_name": "Disabled camera test",
        },
    )
    assert rejected.status_code == 400
    assert "disabled" in rejected.json()["detail"].lower()

    summary = admin.get("/api/v1/cameras/summary").json()
    assert summary["total_cameras"] == summary_before["total_cameras"] - 1

    enabled = admin.patch(f"/api/cameras/{camera_id}", json={"enabled": True})
    assert enabled.status_code == 200
    tree = admin.get("/api/tree").json()
    tree_nvr = next(item for item in tree["nvrs"] if item["id"] == nvr["id"])
    assert camera_id in {camera["id"] for camera in tree_nvr["cameras"]}
    admin.delete(f"/api/cameras/{camera_id}")


def test_alert_resolve_note_is_saved(admin, db):
    alert = models.Alert(
        type="camera_offline",
        severity="warning",
        fingerprint=f"test:resolve-note:{uuid.uuid4().hex}",
        message="Test camera offline",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    moved = admin.post(
        f"/api/alerts/{alert.id}/resolve",
        json={"note": "Checked camera; PoE cable reseated"},
    )
    assert moved.status_code == 200, moved.text

    rows = admin.get("/api/alerts?resolved=true&limit=500").json()["alerts"]
    saved = next(item for item in rows if item["id"] == alert.id)
    assert saved["resolve_note"] == "Checked camera; PoE cable reseated"
    db.query(models.Alert).filter(models.Alert.id == alert.id).delete()
    db.commit()


def test_camera_rest_api_key(admin):
    rotated = admin.post("/api/system/api-key/rotate")
    assert rotated.status_code == 200, rotated.text
    key = rotated.json()["key"]
    nvr = _make_nvr(admin)
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 1, "name": "Gate", "ip_address": "192.0.2.11", "mac_address": "AA:BB:CC:DD:EE:FF"},
    )
    assert camera.status_code == 200, camera.text

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    assert anon.get("/api/v1/cameras").status_code == 401
    response = anon.get("/api/v1/cameras", headers={"X-API-Key": key})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    listed = next(item for item in body["cameras"] if item["nvr"]["id"] == nvr["id"])
    assert listed["ip_address"] == "192.0.2.11"
    assert listed["mac_address"] == "AA:BB:CC:DD:EE:FF"

    summary = anon.get("/api/v1/cameras/summary", headers={"X-API-Key": key})
    assert summary.status_code == 200
    assert summary.json()["total_cameras"] >= 1

    assert anon.get("/api/v1/nvrs", headers={"X-API-Key": key}).status_code == 200
    assert anon.get("/api/v1/cameras", headers={"X-API-Key": "wrong"}).status_code == 401


def test_alert_exception_suppresses_only_matching_alerts(admin, db):
    nvr = _make_nvr(admin)
    created = admin.post(
        "/api/alert-exceptions",
        json={"alert_type": "hdd", "nvr_id": nvr["id"], "reason": "Always full by design"},
    )
    assert created.status_code == 200, created.text

    from app.services.health import HealthChecker

    checker = HealthChecker(db)
    row = db.get(models.NVR, nvr["id"])
    assert checker._is_excepted("hdd", "nvr:x:hdd:1", nvr=row) is True
    assert checker._is_excepted("camera_offline", "nvr:x:cam:1:offline", nvr=row) is False

    listed = admin.get("/api/alert-exceptions").json()["exceptions"]
    assert any(item["nvr_id"] == nvr["id"] for item in listed)

    exception_id = created.json()["exception"]["id"]
    assert admin.delete(f"/api/alert-exceptions/{exception_id}").status_code == 200


def test_agent_lifecycle_and_token_auth(admin):
    word = f"zt{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP test agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    agent = body["agent"]
    token = body["token"]
    assert "token" not in agent
    assert agent["trigger_word"] == word

    duplicate = admin.post(
        "/api/agents",
        json={"name": "duplicate", "campus": "ttp", "trigger_word": word},
    )
    assert duplicate.status_code == 400

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    assert anon.post("/api/agents/heartbeat", json={}).status_code == 401
    heartbeat = anon.post(
        "/api/agents/heartbeat",
        headers={"X-Agent-Token": token},
        json={"version": "test", "hostname": "pytest"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["agent"]["campus"] == "ttp"

    next_job = anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token})
    assert next_job.status_code == 200
    assert next_job.json()["job"] is None

    listed = admin.get("/api/agents").json()
    assert any(item["id"] == agent["id"] for item in listed["agents"])
    assert all("token" not in item for item in listed["agents"])

    rotated = admin.post(f"/api/agents/{agent['id']}/rotate-token")
    assert rotated.status_code == 200
    new_token = rotated.json()["token"]
    assert new_token != token
    assert anon.post("/api/agents/heartbeat", headers={"X-Agent-Token": token}, json={}).status_code == 401
    assert anon.post("/api/agents/heartbeat", headers={"X-Agent-Token": new_token}, json={}).status_code == 200

    assert admin.delete(f"/api/agents/{agent['id']}").status_code == 200


def test_job_dispatched_to_agent_and_reports_back(admin):
    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 2, "name": "Office"},
    )
    assert camera.status_code == 200

    word = f"zj{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP dispatch agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    token = created.json()["token"]

    job_response = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 2}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "11:00",
            "folder_name": "Agent dispatch test",
            "campus": "ttp",
            "upload": False,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job = job_response.json()["job"]
    assert job["status"] == "dispatched", job
    assert job["agent_id"] == agent["id"]

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    polled = anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token})
    assert polled.status_code == 200, polled.text
    payload = polled.json()["job"]
    assert payload["job_id"] == job["id"]
    assert payload["cameras"][0]["nvr"]["password"] == "secret"
    assert payload["cameras"][0]["channel"] == 2

    progress = anon.post(
        f"/api/agents/jobs/{job['id']}/progress",
        headers={"X-Agent-Token": token},
        json={
            "status": "completed",
            "progress": 100,
            "message": "done",
            "result": {"files": [{"filename": "test.mp4", "size_bytes": 10}]},
        },
    )
    assert progress.status_code == 200, progress.text
    fetched = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    assert fetched["status"] == "completed"
    assert fetched["result"]["files"][0]["filename"] == "test.mp4"

    admin.delete(f"/api/jobs/{job['id']}")
    admin.delete(f"/api/agents/{agent['id']}")


def test_cancelling_running_agent_job_is_final(admin):
    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 3, "name": "Gate"},
    )
    assert camera.status_code == 200

    word = f"zc{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP cancel agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    token = created.json()["token"]

    job_response = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 3}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "folder_name": "Agent cancel test",
            "campus": "ttp",
            "upload": False,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job = job_response.json()["job"]
    assert job["status"] == "dispatched"

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    polled = anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token})
    assert polled.status_code == 200, polled.text
    assert polled.json()["job"]["job_id"] == job["id"]

    cancelled = admin.post(f"/api/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["status"] == "cancelled"

    # Cancelling twice is idempotent (double click or a stale modal).
    again = admin.post(f"/api/jobs/{job['id']}/cancel")
    assert again.status_code == 200, again.text
    assert again.json()["job"]["status"] == "cancelled"

    # A worker that was restarted or still mid-flight must not resurrect it.
    late = anon.post(
        f"/api/agents/jobs/{job['id']}/progress",
        headers={"X-Agent-Token": token},
        json={"status": "completed", "progress": 100, "message": "late completion"},
    )
    assert late.status_code == 200, late.text
    fetched = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    assert fetched["status"] == "cancelled"

    admin.delete(f"/api/jobs/{job['id']}")
    admin.delete(f"/api/agents/{agent['id']}")


def test_trigger_word_conflicts_are_rejected(admin):
    global_word = admin.get("/api/settings").json()["values"]["telegram.trigger_word"]
    same_as_global = admin.post(
        "/api/agents",
        json={"name": "conflict agent", "campus": "tk", "trigger_word": global_word},
    )
    assert same_as_global.status_code == 400

    word = f"zc{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={"name": "conflict agent 2", "campus": "tk", "trigger_word": word},
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    try:
        switched = admin.put(
            "/api/integrations/telegram",
            json={"trigger_word": word, "alerts_enabled": True, "miniapp_enabled": True},
        )
        assert switched.status_code == 400
    finally:
        admin.delete(f"/api/agents/{agent['id']}")


def test_bot_reload_keeps_a_single_poller(monkeypatch):
    """Reloading must not leave two getUpdates loops racing (Telegram 409)."""
    import threading
    import time

    from app import bot as bot_module

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "instances": []}

    class FakeBot:
        def __init__(self, token, triggers):
            self.token = token
            self.triggers = triggers
            self._stop = threading.Event()

        def infinity_polling(self, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["instances"].append(self)
            try:
                while not self._stop.is_set():
                    time.sleep(0.01)
            finally:
                with lock:
                    state["active"] -= 1

        def stop_polling(self):
            self._stop.set()

    monkeypatch.setattr(bot_module, "RUN_BOT", True)
    monkeypatch.setattr(bot_module, "_bot_generation", 0)
    monkeypatch.setattr(bot_module, "_bot", None)
    monkeypatch.setattr(bot_module, "_bot_thread", None)
    monkeypatch.setattr(bot_module, "_build_bot", lambda token, triggers: FakeBot(token, triggers))
    monkeypatch.setattr(bot_module, "get_setting", lambda db, key: "fake-token")
    monkeypatch.setattr(bot_module, "_trigger_words", lambda db: ("tk", {}))

    def wait_for(predicate, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    bot_module.start_bot()
    assert wait_for(lambda: state["active"] == 1)
    first = state["instances"][0]

    bot_module.reload_bot()
    assert wait_for(lambda: len(state["instances"]) >= 2)
    assert wait_for(lambda: state["active"] == 1)
    assert first._stop.is_set()
    assert state["peak"] == 1

    bot_module.reload_bot()
    assert wait_for(lambda: len(state["instances"]) >= 3)
    assert wait_for(lambda: state["active"] == 1)
    assert state["peak"] == 1

    bot_module.stop_bot()
    assert wait_for(lambda: state["active"] == 0)
    bot_module._stop_event.clear()


def test_trigger_word_live_setting(admin):
    response = admin.put(
        "/api/integrations/telegram",
        json={"trigger_word": "/TTP-!bad", "alerts_enabled": True, "miniapp_enabled": True},
    )
    assert response.status_code == 200, response.text
    values = admin.get("/api/settings").json()["values"]
    assert values["telegram.trigger_word"] == "ttpbad"


def test_agents_page_renders(admin):
    assert admin.get("/agents").status_code == 200


def test_quiet_hours_pauses_telegram_but_keeps_alerts(admin, db, monkeypatch):
    from app.services import telegram
    from app.services.health import HealthChecker
    from app.settings_service import set_setting

    try:
        set_setting(db, "telegram.alert_mute_enabled", "true")
        set_setting(db, "telegram.alert_mute_start", "17:00")
        set_setting(db, "telegram.alert_mute_end", "07:00")
        set_setting(db, "app.timezone", "Asia/Phnom_Penh")
        db.commit()

        tz = dt.timezone(dt.timedelta(hours=7))
        assert telegram.alert_muted(db, dt.datetime(2026, 11, 5, 18, 0, tzinfo=tz)) is True
        assert telegram.alert_muted(db, dt.datetime(2026, 11, 6, 6, 59, tzinfo=tz)) is True
        assert telegram.alert_muted(db, dt.datetime(2026, 11, 5, 12, 0, tzinfo=tz)) is False
        assert telegram.alert_muted(db, dt.datetime(2026, 11, 6, 7, 0, tzinfo=tz)) is False

        # Alerts are still written for the WebUI while the outbound message is paused.
        checker = HealthChecker(db)
        assert checker._alert(
            fingerprint="test:quiet-hours",
            alert_type="nvr_offline",
            severity="critical",
            message="quiet-hours alert",
        )
        db.commit()
        assert (
            db.query(models.Alert)
            .filter(models.Alert.fingerprint == "test:quiet-hours")
            .count()
            == 1
        )

        sent = []
        monkeypatch.setattr(telegram, "alert_muted", lambda _db, now=None: True)
        monkeypatch.setattr(
            telegram,
            "send_message",
            lambda *args, **kwargs: sent.append(args) or (True, "ok"),
        )
        ok, message = telegram.send_alert(db, "should not be sent")
        assert ok is False
        assert "quiet hours" in message
        assert sent == []
    finally:
        set_setting(db, "telegram.alert_mute_enabled", "false")
        set_setting(db, "telegram.alert_mute_start", "17:00")
        set_setting(db, "telegram.alert_mute_end", "07:00")
        db.commit()
        db.query(models.Alert).filter(models.Alert.fingerprint == "test:quiet-hours").delete()
        db.commit()


def test_agent_uploaded_file_is_directly_downloadable(admin):
    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 4, "name": "Loading bay"},
    )
    assert camera.status_code == 200, camera.text

    word = f"zu{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP upload agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    token = created.json()["token"]

    job_response = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 4}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "folder_name": "Agent download test",
            "campus": "ttp",
            "upload": False,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job = job_response.json()["job"]

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    polled = anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token})
    assert polled.status_code == 200, polled.text
    assert polled.json()["job"]["job_id"] == job["id"]

    uploaded = anon.post(
        f"/api/agents/jobs/{job['id']}/files",
        headers={"X-Agent-Token": token},
        data={"channel": "4", "camera": "Loading bay"},
        files={"file": ("clip.mp4", b"fake-mp4-bytes", "video/mp4")},
    )
    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert body["stored"] is True
    assert body["file"]["filename"] == "clip.mp4"
    assert body["file"]["size_bytes"] == len(b"fake-mp4-bytes")

    fetched = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    assert len(fetched["files"]) == 1
    download_url = fetched["files"][0]["download_url"]
    assert download_url
    downloaded = admin.get(download_url)
    assert downloaded.status_code == 200
    assert downloaded.content == b"fake-mp4-bytes"

    # A late terminal update must keep the uploaded file and the download link.
    terminal = anon.post(
        f"/api/agents/jobs/{job['id']}/progress",
        headers={"X-Agent-Token": token},
        json={"status": "completed", "progress": 100, "message": "done"},
    )
    assert terminal.status_code == 200, terminal.text
    finished = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    assert finished["status"] == "completed"
    assert finished["files"][0]["download_url"] == download_url

    admin.delete(f"/api/jobs/{job['id']}")
    admin.delete(f"/api/agents/{agent['id']}")


def test_agent_chunked_upload_is_resumable(admin):
    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 5, "name": "Chunked gate"},
    )
    assert camera.status_code == 200, camera.text

    word = f"zv{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP chunk agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    token = created.json()["token"]

    job_response = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 5}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "folder_name": "Chunked upload test",
            "campus": "ttp",
            "upload": False,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job = job_response.json()["job"]

    from fastapi.testclient import TestClient

    anon = TestClient(admin.app)
    assert anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token}).status_code == 200
    headers = {"X-Agent-Token": token}
    url = f"/api/agents/jobs/{job['id']}/files/chunk"
    meta = {"channel": "5", "camera": "Chunked gate", "nvr_id": str(nvr["id"])}

    first = anon.post(
        url,
        headers=headers,
        data={**meta, "offset": "0", "total": "10", "final": "false"},
        files={"file": ("clip.mp4", b"AAAA", "video/mp4")},
    )
    assert first.status_code == 200, first.text
    assert first.json()["stored"] is False
    assert first.json()["size_bytes"] == 4

    # A retry of the same chunk must overwrite, not append twice.
    retried = anon.post(
        url,
        headers=headers,
        data={**meta, "offset": "0", "total": "10", "final": "false"},
        files={"file": ("clip.mp4", b"AAAA", "video/mp4")},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["size_bytes"] == 4

    last = anon.post(
        url,
        headers=headers,
        data={**meta, "offset": "4", "total": "10", "final": "true"},
        files={"file": ("clip.mp4", b"BBBBBB", "video/mp4")},
    )
    assert last.status_code == 200, last.text
    body = last.json()
    assert body["stored"] is True
    assert body["file"]["size_bytes"] == 10

    fetched = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    assert len(fetched["files"]) == 1
    downloaded = admin.get(fetched["files"][0]["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"AAAABBBBBB"

    anon.post(
        f"/api/agents/jobs/{job['id']}/progress",
        headers=headers,
        json={"status": "completed", "progress": 100, "message": "done"},
    )
    admin.delete(f"/api/jobs/{job['id']}")
    admin.delete(f"/api/agents/{agent['id']}")


def test_camera_live_view_streams_jpeg_frames(admin, monkeypatch):
    from app.services.nvr import NVRClient

    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 1, "name": "Live gate"},
    )
    assert camera.status_code == 200, camera.text
    camera_id = camera.json()["camera"]["id"]

    listed = admin.get("/api/nvrs").json()["nvrs"]
    row = next(item for item in listed if item["id"] == nvr["id"])
    live_camera = next(item for item in row["cameras"] if item["id"] == camera_id)
    assert live_camera["live_url"] == f"/api/cameras/{camera_id}/live.mjpg"

    jpeg = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
    monkeypatch.setattr(NVRClient, "snapshot", lambda self, channel, timeout=None: jpeg)

    response = admin.get(
        f"/api/cameras/{camera_id}/live.mjpg",
        params={"frames": 1},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert b"Content-Type: image/jpeg" in response.content
    assert jpeg in response.content

    admin.delete(f"/api/cameras/{camera_id}")
    admin.delete(f"/api/nvrs/{nvr['id']}")


def test_agent_download_link_signing_roundtrip():
    from app.services.download_links import (
        build_agent_download_url,
        sign_download,
        verify_download,
    )

    token = "agent-token-for-tests"
    expires = 4_000_000_000
    signature = sign_download(token, 12, "clip.mp4", expires)
    assert verify_download(token, 12, "clip.mp4", expires, signature)
    assert not verify_download(token, 12, "other.mp4", expires, signature)
    assert not verify_download(token, 13, "clip.mp4", expires, signature)
    assert not verify_download("wrong", 12, "clip.mp4", expires, signature)
    assert not verify_download(token, 12, "clip.mp4", expires, "")

    url = build_agent_download_url(
        "https://agent.example/", token, 12, "clip 1.mp4", ttl_seconds=600
    )
    assert url and url.startswith("https://agent.example/agent-files/12/clip%201.mp4?")
    assert "expires=" in url and "sig=" in url
    assert build_agent_download_url("", token, 12, "clip.mp4") is None
    assert build_agent_download_url("ftp://agent", token, 12, "clip.mp4") is None
    assert build_agent_download_url("https://agent.example", "", 12, "clip.mp4") is None


def test_agent_files_endpoint_serves_full_and_range(admin, monkeypatch, tmp_path):
    import time

    from fastapi.testclient import TestClient

    from app.routers import agent_files
    from app.services.download_links import build_agent_download_url, sign_download

    anon = TestClient(admin.app)

    # The main host has no AGENT_TOKEN, so it must not serve agent files.
    monkeypatch.setattr(agent_files, "AGENT_TOKEN", "")
    assert anon.get("/agent-files/1/clip.mp4?expires=1&sig=x").status_code == 404

    token = "pytest-agent-download-token"
    monkeypatch.setattr(agent_files, "AGENT_TOKEN", token)
    monkeypatch.setattr(agent_files, "AGENT_FILES_DIR", tmp_path)

    job_dir = tmp_path / "job_77"
    job_dir.mkdir()
    payload = b"0123456789abcdef"
    (job_dir / "clip.mp4").write_bytes(payload)

    url = build_agent_download_url("https://agent.test", token, 77, "clip.mp4")
    path = url.split("https://agent.test", 1)[1]

    full = anon.get(path)
    assert full.status_code == 200, full.text
    assert full.content == payload
    assert full.headers.get("accept-ranges") == "bytes"
    assert full.headers.get("x-accel-buffering") == "no"

    ranged = anon.get(path, headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206, ranged.text
    assert ranged.content == payload[2:6]
    assert ranged.headers.get("content-range") == "bytes 2-5/16"

    expired = sign_download(token, 77, "clip.mp4", int(time.time()) - 5)
    stale = anon.get(f"/agent-files/77/clip.mp4?expires={int(time.time()) - 5}&sig={expired}")
    assert stale.status_code == 410

    tampered = anon.get(
        f"/agent-files/77/clip.mp4?expires={url.split('expires=')[1].split('&')[0]}&sig=deadbeef"
    )
    assert tampered.status_code == 403


def test_agent_public_url_enables_direct_download(admin):
    from fastapi.testclient import TestClient

    from app.services.download_links import verify_download

    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 6, "name": "Direct dock"},
    )
    assert camera.status_code == 200, camera.text

    word = f"zw{uuid.uuid4().hex[:5]}"
    created = admin.post(
        "/api/agents",
        json={
            "name": "TTP direct agent",
            "campus": "ttp",
            "zones": ["ecp"],
            "trigger_word": word,
            "public_url": "https://agent-cctv.example/",
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    agent = created.json()["agent"]
    token = created.json()["token"]
    assert agent["public_url"] == "https://agent-cctv.example"

    invalid = admin.patch(
        f"/api/agents/{agent['id']}", json={"public_url": "agent-cctv.example"}
    )
    assert invalid.status_code == 400

    updated = admin.patch(
        f"/api/agents/{agent['id']}",
        json={"public_url": "https://agent-cctv.example"},
    )
    assert updated.status_code == 200, updated.text
    listed = admin.get("/api/agents").json()["agents"]
    stored = next(item for item in listed if item["id"] == agent["id"])
    assert stored["public_url"] == "https://agent-cctv.example"

    anon = TestClient(admin.app)
    heartbeat = anon.post(
        "/api/agents/heartbeat",
        headers={"X-Agent-Token": token},
        json={"version": "test"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["keep_files_days"] >= 1
    assert heartbeat.json()["direct_download"] is True

    job_response = admin.post(
        "/api/jobs",
        json={
            "cameras": [{"nvr_id": nvr["id"], "channel": 6}],
            "date": "30-10-2026",
            "start_time": "12:00",
            "end_time": "12:10",
            "folder_name": "Direct download test",
            "campus": "ttp",
            "upload": False,
        },
    )
    assert job_response.status_code == 200, job_response.text
    job = job_response.json()["job"]

    polled = anon.get("/api/agents/jobs/next", headers={"X-Agent-Token": token})
    assert polled.status_code == 200, polled.text
    payload = polled.json()["job"]
    assert payload["download"]["direct"] is True
    assert payload["download"]["keep_days"] >= 1

    progress = anon.post(
        f"/api/agents/jobs/{job['id']}/progress",
        headers={"X-Agent-Token": token},
        json={
            "status": "completed",
            "progress": 100,
            "message": "done",
            "result": {
                "files": [
                    {
                        "filename": "direct.mp4",
                        "size_bytes": 4,
                        "channel": "6",
                        "camera": "Direct dock",
                        "agent_file": True,
                    }
                ]
            },
        },
    )
    assert progress.status_code == 200, progress.text

    fetched = admin.get(f"/api/jobs/{job['id']}").json()["job"]
    direct = next(f for f in fetched["files"] if f["filename"] == "direct.mp4")
    assert direct["agent_download"] is True
    assert direct["on_agent"] is False
    assert direct["download_url"].startswith(
        "https://agent-cctv.example/agent-files/"
    )
    query = direct["download_url"].split("?", 1)[1]
    parts = dict(pair.split("=", 1) for pair in query.split("&"))
    assert verify_download(
        token, job["id"], "direct.mp4", int(parts["expires"]), parts["sig"]
    )

    admin.delete(f"/api/jobs/{job['id']}")
    admin.delete(f"/api/agents/{agent['id']}")


def test_agent_retention_cleanup_removes_old_job_folders(tmp_path, monkeypatch):
    import os
    import time

    from app import agent_runner

    monkeypatch.setattr(agent_runner, "AGENT_FILES_DIR", tmp_path)
    monkeypatch.setattr(agent_runner, "_keep_days", 7)
    monkeypatch.setattr(agent_runner, "_last_cleanup", 0.0)

    old_dir = tmp_path / "job_1"
    old_dir.mkdir()
    old_file = old_dir / "old.mp4"
    old_file.write_bytes(b"old")
    old_time = time.time() - 10 * 86400
    os.utime(old_file, (old_time, old_time))
    os.utime(old_dir, (old_time, old_time))

    fresh_dir = tmp_path / "job_2"
    fresh_dir.mkdir()
    (fresh_dir / "fresh.mp4").write_bytes(b"fresh")

    agent_runner._cleanup_results(force=True)
    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert (fresh_dir / "fresh.mp4").exists()

    agent_runner._apply_server_settings({"keep_files_days": "30"})
    assert agent_runner._keep_days == 30
    agent_runner._apply_server_settings({"keep_files_days": "bad"})
    assert agent_runner._keep_days == 30


def _make_operator(admin, role="operator"):
    username = f"logtest_{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users",
        json={
            "username": username,
            "password": "LogTestPass1!",
            "email": f"{username}@example.com",
            "role": role,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["user"]["id"], username, "LogTestPass1!"


def test_activity_log_levels_and_permissions(admin, db):
    from fastapi.testclient import TestClient

    from app.security import audit

    marker = uuid.uuid4().hex[:8]
    audit(db, None, "job_failed", f"failure-marker-{marker}", level=None)
    audit(db, None, "settings_updated", f"success-marker-{marker}", level=None)

    entries = admin.get("/api/logs/activity", params={"q": marker}).json()["entries"]
    by_detail = {entry["detail"]: entry for entry in entries}
    assert by_detail[f"failure-marker-{marker}"]["level"] == "error"
    assert by_detail[f"success-marker-{marker}"]["level"] == "success"

    errors = admin.get(
        "/api/logs/activity", params={"q": marker, "level": "error"}
    ).json()["entries"]
    assert [entry["detail"] for entry in errors] == [f"failure-marker-{marker}"]

    assert admin.get("/api/logs/activity", params={"level": "bogus"}).status_code == 400

    user_id, username, password = _make_operator(admin)
    with TestClient(admin.app) as operator:
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code == 200
        assert operator.get("/api/logs/activity").status_code == 403
        assert operator.get("/api/logs/system").status_code == 403
    assert admin.delete(f"/api/users/{user_id}").status_code == 200


def test_system_log_endpoint_and_download(admin):
    from app.routers.logs import LOG_FILE

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    marker = f"system-marker-{uuid.uuid4().hex[:8]}"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"2026-11-10 10:00:00 [ERROR] cctv.test: {marker}\n")
        handle.write("2026-11-10 10:00:01 [INFO] cctv.test: all good\n")

    all_lines = admin.get("/api/logs/system", params={"lines": 500, "q": marker}).json()
    assert all_lines["missing"] is False
    assert any(marker in line for line in all_lines["lines"])

    errors = admin.get(
        "/api/logs/system", params={"lines": 500, "level": "error", "q": marker}
    ).json()["lines"]
    assert errors == [f"2026-11-10 10:00:00 [ERROR] cctv.test: {marker}"]

    assert admin.get("/api/logs/system", params={"level": "bogus"}).status_code == 400
    download = admin.get("/api/logs/system/download")
    assert download.status_code == 200
    assert marker in download.text


def test_dashboard_reports_save_validation_and_permissions(admin):
    from fastapi.testclient import TestClient

    payload = admin.get("/api/dashboard").json()
    original = payload["reports"]
    assert payload["can_customize"] is True
    assert {chart["type"] for chart in original} >= {"pie", "area"}

    layout = {
        "reports": [
            {
                "type": "pie",
                "metric": "campus",
                "title": "Campus split",
                "days": 7,
                "span": 2,
            },
            {
                "type": "area",
                "metric": "total",
                "title": "Requests trend",
                "days": 14,
                "span": 1,
            },
        ]
    }
    saved = admin.put("/api/dashboard/reports", json=layout)
    assert saved.status_code == 200, saved.text
    reports = saved.json()["reports"]
    assert [chart["metric"] for chart in reports] == ["campus", "total"]
    assert reports[0]["span"] == 2
    assert admin.get("/api/dashboard").json()["reports"] == reports

    invalid = admin.put(
        "/api/dashboard/reports",
        json={"reports": [{"type": "pie", "metric": "total", "days": 7}]},
    )
    assert invalid.status_code == 400
    invalid_days = admin.put(
        "/api/dashboard/reports",
        json={"reports": [{"type": "pie", "metric": "source", "days": 3}]},
    )
    assert invalid_days.status_code == 400

    user_id, username, password = _make_operator(admin)
    with TestClient(admin.app) as operator:
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code == 200
        forbidden = operator.put("/api/dashboard/reports", json=layout)
        assert forbidden.status_code == 403
        visible = operator.get("/api/dashboard").json()
        assert visible["can_customize"] is False
        assert visible["reports"] == reports
    assert admin.delete(f"/api/users/{user_id}").status_code == 200

    restored = admin.put("/api/dashboard/reports", json={"reports": original})
    assert restored.status_code == 200, restored.text


def test_dashboard_analytics_shape_and_operator_scope(admin):
    from fastapi.testclient import TestClient

    data = admin.get("/api/dashboard/analytics", params={"days": 7}).json()
    assert data["days"] == 7
    assert len(data["timeline"]["labels"]) == 7
    assert len(data["timeline"]["total"]) == 7
    for metric in ("source", "campus", "status", "agent"):
        assert metric in data["dimensions"]
        assert metric in data["timeline"]
        for series in data["timeline"][metric].values():
            assert len(series) == 7
    for key in ("requests", "completed", "partial", "failed", "cancelled", "files", "bytes"):
        assert key in data["totals"]

    user_id, username, password = _make_operator(admin)
    with TestClient(admin.app) as operator:
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code == 200
        own = operator.get("/api/dashboard/analytics", params={"days": 7}).json()
        assert own["totals"]["requests"] == 0
        assert sum(own["timeline"]["total"]) == 0
    assert admin.delete(f"/api/users/{user_id}").status_code == 200


def test_dashboard_reports_custom_range_save_and_validation(admin):
    original = admin.get("/api/dashboard").json()["reports"]

    layout = {
        "reports": [
            {
                "type": "pie",
                "metric": "campus",
                "title": "Campus split",
                "span": 1,
                "range": "custom",
                "start_date": "2026-01-01",
                "end_date": "2026-01-10",
            },
            {
                "type": "area",
                "metric": "total",
                "title": "Requests trend",
                "span": 2,
                "range": "days",
                "days": 14,
            },
        ]
    }
    saved = admin.put("/api/dashboard/reports", json=layout)
    assert saved.status_code == 200, saved.text
    reports = saved.json()["reports"]
    assert reports[0]["range"] == "custom"
    assert reports[0]["start_date"] == "2026-01-01"
    assert reports[0]["end_date"] == "2026-01-10"
    assert reports[0]["days"] == 10
    assert reports[1]["range"] == "days"
    assert reports[1]["start_date"] is None
    assert admin.get("/api/dashboard").json()["reports"] == reports

    reversed_range = admin.put(
        "/api/dashboard/reports",
        json={
            "reports": [
                {
                    "type": "pie",
                    "metric": "source",
                    "range": "custom",
                    "start_date": "2026-02-01",
                    "end_date": "2026-01-01",
                }
            ]
        },
    )
    assert reversed_range.status_code == 400

    bad_date = admin.put(
        "/api/dashboard/reports",
        json={
            "reports": [
                {
                    "type": "pie",
                    "metric": "source",
                    "range": "custom",
                    "start_date": "not-a-date",
                    "end_date": "2026-01-01",
                }
            ]
        },
    )
    assert bad_date.status_code == 400

    too_long = admin.put(
        "/api/dashboard/reports",
        json={
            "reports": [
                {
                    "type": "area",
                    "metric": "total",
                    "range": "custom",
                    "start_date": "2020-01-01",
                    "end_date": "2022-01-01",
                }
            ]
        },
    )
    assert too_long.status_code == 400

    restored = admin.put("/api/dashboard/reports", json={"reports": original})
    assert restored.status_code == 200, restored.text


def test_dashboard_analytics_custom_range(admin):
    from fastapi.testclient import TestClient

    data = admin.get(
        "/api/dashboard/analytics",
        params={"start": "2026-01-01", "end": "2026-01-10"},
    ).json()
    assert data["days"] == 10
    assert data["start"] == "2026-01-01"
    assert data["end"] == "2026-01-10"
    assert len(data["timeline"]["labels"]) == 10
    assert data["timeline"]["labels"][0] == "2026-01-01"
    assert data["timeline"]["labels"][-1] == "2026-01-10"
    assert len(data["timeline"]["total"]) == 10

    assert admin.get(
        "/api/dashboard/analytics", params={"start": "2026-01-01"}
    ).status_code == 400
    assert admin.get(
        "/api/dashboard/analytics",
        params={"start": "2026-02-01", "end": "2026-01-01"},
    ).status_code == 400
    assert admin.get(
        "/api/dashboard/analytics",
        params={"start": "2020-01-01", "end": "2022-01-01"},
    ).status_code == 400
    assert admin.get(
        "/api/dashboard/analytics",
        params={"start": "bad", "end": "2026-01-01"},
    ).status_code == 400

    user_id, username, password = _make_operator(admin)
    with TestClient(admin.app) as operator:
        assert operator.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code == 200
        own = operator.get(
            "/api/dashboard/analytics",
            params={"start": "2026-01-01", "end": "2026-01-10"},
        ).json()
        assert sum(own["timeline"]["total"]) == 0
    assert admin.delete(f"/api/users/{user_id}").status_code == 200


def test_merge_retry_result_preserves_successful_cameras():
    from app.services.jobs import merge_retry_result

    previous = {
        "files": [
            {"filename": "front.mp4", "size_bytes": 10, "channel": 1, "camera": "Front"}
        ],
        "cameras": [
            {"nvr_id": 1, "channel": 1, "nvr": "NVR", "camera": "Front", "status": "success", "filename": "front.mp4"},
            {"nvr_id": 1, "channel": 2, "nvr": "NVR", "camera": "Back", "status": "failed", "message": "timeout"},
        ],
        "errors": ["NVR CH2: timeout"],
        "drive": {"folder_id": "drive-folder-1"},
    }
    fresh = {
        "files": [
            {"filename": "back.mp4", "size_bytes": 20, "channel": 2, "camera": "Back"}
        ],
        "cameras": [
            {"nvr_id": 1, "channel": 2, "nvr": "NVR", "camera": "Back", "status": "success", "filename": "back.mp4"}
        ],
        "errors": [],
    }

    merged = merge_retry_result(previous, fresh, [(1, 2)])

    assert [row["status"] for row in merged["cameras"]] == ["success", "success"]
    assert {entry["filename"] for entry in merged["files"]} == {"front.mp4", "back.mp4"}
    # The error belonging to the retried camera is gone.
    assert merged["errors"] == []
    # The original Drive folder receipt is kept when the retry did not upload.
    assert merged["drive"] == {"folder_id": "drive-folder-1"}


def test_job_retry_only_failed_cameras(admin, db, monkeypatch):
    user = db.query(models.User).filter_by(username="testadmin").first()
    assert user is not None
    job = models.Job(
        ref=f"RETRY-{uuid.uuid4().hex[:6].upper()}",
        user_id=user.id,
        source="web",
        kind="footage",
        status="partial",
        progress=100,
        folder_name="Retry test",
        request_json=json.dumps(
            {
                "cameras": [
                    {"nvr_id": 1, "channel": 1},
                    {"nvr_id": 1, "channel": 2},
                ],
                "date": "01-01-2026",
                "start_time": "10:00",
                "end_time": "10:05",
            }
        ),
        result_json=json.dumps(
            {
                "files": [
                    {
                        "filename": "front.mp4",
                        "size_bytes": 10,
                        "channel": 1,
                        "camera": "Front",
                        "nvr": "NVR",
                    }
                ],
                "cameras": [
                    {
                        "nvr_id": 1,
                        "channel": 1,
                        "nvr": "NVR",
                        "camera": "Front",
                        "status": "success",
                        "filename": "front.mp4",
                    },
                    {
                        "nvr_id": 1,
                        "channel": 2,
                        "nvr": "NVR",
                        "camera": "Back",
                        "status": "failed",
                        "message": "timeout",
                    },
                ],
                "errors": [],
            }
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # A camera that already succeeded cannot be retried.
    rejected = admin.post(
        f"/api/jobs/{job.id}/retry",
        json={"cameras": [{"nvr_id": 1, "channel": 1}]},
    )
    assert rejected.status_code == 400

    calls: list[int] = []
    monkeypatch.setattr("app.routers.jobs.jobs.start_job", lambda job_id: calls.append(job_id))
    response = admin.post(
        f"/api/jobs/{job.id}/retry",
        json={"cameras": [{"nvr_id": 1, "channel": 2}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()["job"]
    assert body["status"] == "queued"
    assert body["retry_pending"] is True
    assert calls == [job.id]

    db.expire_all()
    stored = db.get(models.Job, job.id)
    assert json.loads(stored.retry_json) == [{"nvr_id": 1, "channel": 2}]

    db.delete(stored)
    db.commit()


def test_job_retry_requires_failed_cameras(admin, db, monkeypatch):
    user = db.query(models.User).filter_by(username="testadmin").first()
    job = models.Job(
        ref=f"RETRY-OK-{uuid.uuid4().hex[:6].upper()}",
        user_id=user.id,
        source="web",
        kind="footage",
        status="completed",
        progress=100,
        folder_name="Retry ok test",
        request_json=json.dumps(
            {
                "cameras": [{"nvr_id": 1, "channel": 1}],
                "date": "01-01-2026",
                "start_time": "10:00",
                "end_time": "10:05",
            }
        ),
        result_json=json.dumps(
            {
                "files": [{"filename": "ok.mp4", "channel": 1}],
                "cameras": [
                    {
                        "nvr_id": 1,
                        "channel": 1,
                        "nvr": "NVR",
                        "camera": "Front",
                        "status": "success",
                        "filename": "ok.mp4",
                    }
                ],
                "errors": [],
            }
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    calls: list[int] = []
    monkeypatch.setattr("app.routers.jobs.jobs.start_job", lambda job_id: calls.append(job_id))
    response = admin.post(f"/api/jobs/{job.id}/retry", json={"cameras": []})
    assert response.status_code == 400
    assert calls == []

    db.delete(job)
    db.commit()


def test_explicit_folder_name_wins_over_incident_and_date(admin, db):
    from app.services import jobs as job_service

    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 5, "name": "Gate"},
    )
    assert camera.status_code == 200, camera.text

    job = job_service.create_job(
        db,
        {
            "cameras": [{"nvr_id": nvr["id"], "channel": 5}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "folder_name": "Renamed Incident Folder",
            "incident": "Theft",
            "campus": "ttp",
            "upload": False,
        },
        source="bot",
        auto_start=False,
    )
    try:
        assert job.folder_name == "Renamed Incident Folder"
    finally:
        db.delete(job)
        db.commit()


def test_incident_without_folder_name_uses_incident_and_date(admin, db):
    from app.services import jobs as job_service

    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 6, "name": "Lobby"},
    )
    assert camera.status_code == 200, camera.text

    job = job_service.create_job(
        db,
        {
            "cameras": [{"nvr_id": nvr["id"], "channel": 6}],
            "date": "30-10-2026",
            "start_time": "10:00",
            "end_time": "10:05",
            "incident": "Theft",
            "campus": "ttp",
            "upload": False,
        },
        source="web",
        auto_start=False,
    )
    try:
        assert job.folder_name == "Theft 30-10-2026"
    finally:
        db.delete(job)
        db.commit()


def test_miniapp_uses_24_hour_pickers_and_top_level_destination(admin):
    response = admin.get("/miniapp")
    assert response.status_code == 200, response.text
    html = response.text

    assert 'id="m-start-hour"' in html
    assert 'id="m-start-minute"' in html
    assert 'id="m-end-hour"' in html
    assert 'id="m-end-minute"' in html
    assert 'type="time" id="m-start"' not in html
    assert 'type="time" id="m-end"' not in html

    # Requester email and destination belong in the main form, not "More options".
    zone_at = html.index("Zone")
    email_at = html.index("Requester email")
    destination_at = html.index("Destination")
    folder_at = html.index("Folder / incident name")
    more_at = html.index("More options")
    assert zone_at < email_at < folder_at
    assert zone_at < destination_at < folder_at
    assert email_at < more_at


def test_live_view_profile_and_rtsp_url():
    from app import models
    from app.security import encrypt_secret
    from app.services import live

    balanced = live.profile_for("balanced", "main")
    assert (balanced.width, balanced.fps, balanced.stream) == (960, 10, "main")

    high_sub = live.profile_for("high", "sub")
    assert high_sub.stream == "sub"
    assert high_sub.width <= 960
    assert high_sub.fps <= 10

    nvr = models.NVR(
        name="Test",
        code="TESTLIVE",
        protocol="hikvision",
        host="192.0.2.50",
        port=80,
        username="admin",
        password_enc=encrypt_secret("p@ss:word"),
    )
    camera = models.Camera(channel=3, name="Gate")

    url = live.rtsp_url(nvr, camera, "main")
    assert url == "rtsp://admin:p%40ss%3Aword@192.0.2.50:554/Streaming/Channels/301"
    assert live.rtsp_url(nvr, camera, "sub").endswith("/302")

    camera.rtsp_path = "/custom/stream"
    assert live.rtsp_url(nvr, camera).endswith("@192.0.2.50:554/custom/stream")

    camera.rtsp_path = "rtsp://viewer:secret@192.0.2.50/live"
    assert live.rtsp_url(nvr, camera) == "rtsp://viewer:secret@192.0.2.50/live"

    command = " ".join(live.build_command("/proc/self/fd/9", balanced))
    assert "scale=w='min(960,iw)':h=-2" in command
    assert "-f mpjpeg" in command
    assert "rtsp://" not in command


def test_live_view_uses_ffmpeg_when_available(admin, monkeypatch):
    from app.services import live

    nvr = _make_nvr(admin, campus="ttp")
    camera = admin.post(
        f"/api/nvrs/{nvr['id']}/cameras",
        json={"channel": 1, "name": "Live ffmpeg gate"},
    )
    assert camera.status_code == 200, camera.text
    camera_id = camera.json()["camera"]["id"]

    frame = (
        b"--ffmpeg\r\nContent-type: image/jpeg\r\nContent-length: 8\r\n\r\n"
        b"\xff\xd8fake\xff\xd9\r\n"
    )

    class FakeProcess:
        def __init__(self, data):
            self.stdout = __import__("io").BytesIO(data)

    class FakeStream:
        def __init__(self, data):
            self.process = FakeProcess(data)

        def stop(self):
            pass

    monkeypatch.setattr(live, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(live, "start_process", lambda url, profile: FakeStream(frame))

    response = admin.get(f"/api/cameras/{camera_id}/live.mjpg")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=ffmpeg"
    assert frame in response.content

    admin.delete(f"/api/cameras/{camera_id}")
    admin.delete(f"/api/nvrs/{nvr['id']}")
