from __future__ import annotations

import asyncio
import json
import secrets
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.config import AppConfig, MobileConfig
from trade_compass_agent.domain import Notification
from trade_compass_agent.mobile.api import create_mobile_app
from trade_compass_agent.mobile.client import PinnedHTTPSConnection, request_json
from trade_compass_agent.mobile.identity import load_or_create_identity
from trade_compass_agent.mobile.pairing import DeviceStore, PairingError
from trade_compass_agent.mobile.server import mobile_listener
from trade_compass_agent.ops.notifications import JsonNotificationStore
from trade_compass_agent.runtime.session import SessionMessageRecord, SessionStore


@pytest.fixture
def access(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       mobile=MobileConfig(enabled=True, host="127.0.0.1", port=0))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(config.memory_dir))
    directory = config.data_dir / "mobile"
    devices = DeviceStore(directory)
    identity = load_or_create_identity(directory)
    mobile = TestClient(create_mobile_app(config, devices, identity), base_url="https://computer")
    from trade_compass_agent.web.app import create_app
    local_app = create_app()
    local_app.state.mobile_service = SimpleNamespace(
        running=True, config=config, identity=identity, devices=devices, port=19705,
    )
    local = TestClient(local_app, client=("127.0.0.1", 12345))
    return config, devices, identity, mobile, local


def pair(mobile, local):
    invitation = local.post("/api/mobile/pairing/invitations").json()
    secret = secrets.token_urlsafe(32)
    response = mobile.post("/mobile/v1/pairing/claim", json={
        "invitation": invitation["invitation"], "device_secret": secret, "name": "我的手机",
    })
    assert response.status_code == 202
    assert "verification_code" not in response.json()
    device = next(d for d in local.get("/api/mobile/devices").json()["devices"]
                  if d["device_id"] == response.json()["device_id"])
    return secret, device


def approve(local, claim):
    claim = next(d for d in local.get("/api/mobile/devices").json()["devices"] if d["device_id"] == claim["device_id"])
    response = local.post(f"/api/mobile/devices/{claim['device_id']}/approve",
                          json={"verification_code": claim["verification_code"]})
    assert response.status_code == 200


def test_pairing_approval_restart_and_revocation(access):
    config, devices, identity, mobile, local = access
    secret, claim = pair(mobile, local)
    headers = {"Authorization": f"Bearer {secret}"}
    assert mobile.get("/mobile/v1/sessions").status_code == 401
    assert mobile.get("/mobile/v1/sessions", headers=headers).status_code == 403
    assert mobile.get("/mobile/v1/pairing/status", headers=headers).json()["status"] == "pending"
    wrong = f"{(int(claim['verification_code']) + 1) % 1_000_000:06d}"
    assert local.post(f"/api/mobile/devices/{claim['device_id']}/approve",
                      json={"verification_code": wrong}).status_code == 409
    approve(local, claim)
    assert mobile.get("/mobile/v1/info", headers=headers).json()["capabilities"] == [
        "sessions.read", "sessions.create", "sessions.send", "notifications.read",
    ]
    restored = DeviceStore(config.data_dir / "mobile")
    restarted_identity = load_or_create_identity(config.data_dir / "mobile")
    assert identity == restarted_identity
    restarted = TestClient(create_mobile_app(config, restored, restarted_identity),
                           base_url="https://computer")
    assert restarted.get("/mobile/v1/sessions", headers=headers).status_code == 200
    assert local.delete(f"/api/mobile/devices/{claim['device_id']}").status_code == 200
    assert restarted.get("/mobile/v1/sessions", headers=headers).status_code == 401
    assert mobile.get("/mobile/v1/pairing/status", headers=headers).status_code == 401
    assert secret.encode() not in devices.path.read_bytes()
    assert "secret_hash" not in json.dumps(local.get("/api/mobile/devices").json())
    assert devices.path.stat().st_mode & 0o777 == 0o600
    assert identity.pem_path.stat().st_mode & 0o777 == 0o600


def test_same_history_pagination_and_task_records(access):
    config, _, _, mobile, local = access
    store = SessionStore(config.data_dir / "agent_sessions")
    session = store.create()
    for text in ["重复的合法消息", "重复的合法消息"]:
        store.append(session, SessionMessageRecord(role="user", content=text))
    store.append(session, SessionMessageRecord(role="assistant", content="结论", sections=[
        {"title": "分析", "content": "**正文**", "symbols": ["600519"]},
    ]))
    store.get_or_create("scheduler-hidden")
    # Include an existing channel-style Unicode session ID.
    channel_session = store.get_or_create("channel-微信-user")
    store.append(channel_session, SessionMessageRecord(role="user", content="渠道历史"))
    notices = JsonNotificationStore(config.data_dir / "notifications.jsonl")
    notices.append(Notification(channel="scheduler:test", title="定时任务", message="完整结果"))
    before = {p: p.read_bytes() for p in config.data_dir.rglob("*.jsonl")}
    secret, claim = pair(mobile, local)
    approve(local, claim)
    mobile.headers["Authorization"] = f"Bearer {secret}"
    assert mobile.get("/mobile/v1/sessions").json() == local.get("/api/agent/sessions").json()
    pages = []
    for cursor in [None, 2]:
        params = {"limit": 1} if cursor is None else {"limit": 2, "before": cursor}
        remote = mobile.get(f"/mobile/v1/sessions/{session.session_id}/messages", params=params)
        desktop = local.get(f"/api/agent/sessions/{session.session_id}/messages", params=params)
        assert remote.status_code == 200
        assert remote.json() == desktop.json()
        assert remote.headers["cache-control"] == "no-store"
        pages.append(remote.json())
    assert len(pages[1]["messages"]) == 2
    assert pages[1]["messages"][0]["content"] == pages[1]["messages"][1]["content"]
    assert mobile.get(f"/mobile/v1/sessions/{channel_session.session_id}/messages").status_code == 200
    assert mobile.get("/mobile/v1/notifications").json() == local.get("/api/notifications").json()
    assert mobile.get("/mobile/v1/sessions/missing/messages").status_code == 404
    assert before == {p: p.read_bytes() for p in config.data_dir.rglob("*.jsonl")}


@pytest.mark.parametrize("status", ["completed", "failed", "timed_out", "degraded"])
def test_task_outcome_survives_delivery_restart_and_both_notification_apis(access, status):
    from trade_compass_agent.ops.delivery import DeliveryRouter
    from trade_compass_agent.ops.job_definition import DeliveryConfig
    from trade_compass_agent.ops.run_store import SqliteRunStore

    config, _, _, mobile, local = access
    config = replace(config, notifications=replace(config.notifications, macos_enabled=False))
    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("outcome-fixture")
    runs.start_run(run)
    if status == "completed":
        runs.complete_run(run, message="完整结果")
    elif status == "failed":
        runs.fail_run(run, error="任务失败")
    elif status == "timed_out":
        runs.timeout_run(run)
    else:
        runs.degrade_run(run, error="部分步骤失败", message="已有的部分结果")
    DeliveryRouter(config).deliver(run, DeliveryConfig(channels=("web_log",)))
    reopened = JsonNotificationStore(config.data_dir / "notifications.jsonl")
    assert reopened.recent()[0].task_status == status
    # Rotating/appending the log must preserve the earlier outcome and source identity.
    reopened.append(Notification(channel="manual", title="普通警告", message="提醒", severity="warning"))
    assert reopened.events()[0]["event_id"] == f"job-run:{run.id}"
    secret, claim = pair(mobile, local)
    approve(local, claim)
    mobile.headers["Authorization"] = f"Bearer {secret}"
    remote = mobile.get("/mobile/v1/notifications").json()
    assert remote == local.get("/api/notifications").json()
    assert remote[0]["task_status"] == status
    assert remote[0]["message"] == run.message or remote[0]["message"] == run.error
    assert remote[0]["severity"] == ("info" if status == "completed" else "warning")
    assert remote[1]["task_status"] is None
    assert remote[1]["severity"] == "warning"


@pytest.mark.parametrize("path", ["/api/mobile/devices", "/api/config", "/api/agent/skills",
                                  "/health", "/docs", "/openapi.json", "/agent"])
def test_mobile_does_not_expose_desktop_routes(access, path):
    _, _, _, mobile, local = access
    secret, claim = pair(mobile, local)
    approve(local, claim)
    assert mobile.get(path, headers={"Authorization": f"Bearer {secret}"}).status_code == 404


def test_mobile_transport_validation_and_path_boundary(access, tmp_path):
    config, _, _, mobile, local = access
    secret, claim = pair(mobile, local)
    approve(local, claim)
    headers = {"Authorization": f"Bearer {secret}"}
    assert mobile.get("http://computer/mobile/v1/sessions", headers=headers).status_code == 403
    assert mobile.get("/mobile/v1/sessions", headers={**headers, "Origin": "https://evil"}).status_code == 403
    assert mobile.get(f"/mobile/v1/sessions?token={secret}").status_code == 401
    assert mobile.post("/mobile/v1/turn", headers=headers, json={"message": "run"}).status_code == 404
    assert mobile.post("/mobile/v1/pairing/claim", content=b"x" * 70000).status_code == 413
    malformed = mobile.post("/mobile/v1/pairing/claim", json={"device_secret": secret})
    assert malformed.status_code == 422
    assert secret not in malformed.text
    external = tmp_path / "private.jsonl"
    external.write_text('{"role":"user","content":"private"}\n')
    (config.data_dir / "agent_sessions" / "linked.jsonl").symlink_to(external)
    for path in ["linked", "..%5Cprivate", "%00", "..%2Fprivate", "x" * 201]:
        response = mobile.get(f"/mobile/v1/sessions/{path}/messages", headers=headers)
        assert response.status_code == 404
        assert "private" not in response.text
    remote_admin = TestClient(local.app, client=("192.0.2.10", 123))
    assert remote_admin.get("/api/mobile/status").status_code == 403
    assert local.post("/api/mobile/pairing/invitations", headers={"Origin": "https://evil"}).status_code == 403


def test_invitation_expiration_single_use_and_pending_expiry(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("trade_compass_agent.mobile.pairing.time.time", lambda: now[0])
    devices = DeviceStore(tmp_path / "mobile")
    invitation = devices.create_invitation()
    now[0] += 301
    with pytest.raises(PairingError):
        devices.claim(invitation["invitation"], "late", secrets.token_urlsafe(32))
    invitation = devices.create_invitation()

    def claim(_):
        try:
            return devices.claim(invitation["invitation"], "client", secrets.token_urlsafe(32))
        except PairingError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    accepted = [result for result in results if result]
    assert len(accepted) == 1
    now[0] += 301
    with pytest.raises(PairingError):
        devices.approve(accepted[0]["device_id"], accepted[0]["verification_code"])
    assert devices.list_devices() == []


def test_real_tls_pinning_revoke_keepalive_and_listener_shutdown(tmp_path):
    config = AppConfig(data_dir=tmp_path,
                       mobile=MobileConfig(enabled=True, host="127.0.0.1", port=0))

    async def scenario():
        async with mobile_listener(config) as service:
            endpoint = f"https://127.0.0.1:{service.port}"
            fingerprint = service.identity.certificate_sha256
            invitation = service.devices.create_invitation()
            secret = secrets.token_urlsafe(32)
            body = {"invitation": invitation["invitation"], "name": "TLS client", "device_secret": secret}
            with pytest.raises(ssl.SSLCertVerificationError):
                await asyncio.to_thread(request_json, endpoint, "0" * 64, "POST",
                                        "/mobile/v1/pairing/claim", body=body)
            # The wrong-pin attempt sent no HTTP body and did not consume the invitation.
            status, claim = await asyncio.to_thread(request_json, endpoint, fingerprint, "POST",
                                                    "/mobile/v1/pairing/claim", body=body)
            assert status == 202
            local_device = next(d for d in service.devices.list_devices() if d["device_id"] == claim["device_id"])
            service.devices.approve(claim["device_id"], local_device["verification_code"])
            conn = PinnedHTTPSConnection("127.0.0.1", service.port, fingerprint)

            def read():
                conn.request("GET", "/mobile/v1/sessions", headers={"Authorization": f"Bearer {secret}"})
                response = conn.getresponse()
                response.read()
                return response.status

            try:
                assert await asyncio.to_thread(read) == 200
                service.devices.revoke(claim["device_id"])
                assert await asyncio.to_thread(read) == 401
            finally:
                conn.close()
            port = service.port
        assert service.task.done()
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        # Persisted credentials and identity survive restarting the actual listener.
        async with mobile_listener(config) as restarted:
            assert restarted.identity == service.identity
            assert restarted.devices.lookup(secret)["status"] == "revoked"

    asyncio.run(scenario())


def test_disabled_listener_writes_nothing_and_port_conflicts_fail_closed(tmp_path):
    async def scenario():
        config = AppConfig(data_dir=tmp_path)
        async with mobile_listener(config) as service:
            assert service is None
        assert not (tmp_path / "mobile").exists()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            config = replace(config, mobile=MobileConfig(True, "127.0.0.1", occupied.getsockname()[1]))
            with pytest.raises(OSError):
                async with mobile_listener(config):
                    pytest.fail("A conflicting port must not start a second listener")
        assert not (tmp_path / "mobile").exists()

    asyncio.run(scenario())


def test_parent_lifecycle_starts_one_scheduler_and_stops_mobile(tmp_path, monkeypatch):
    from trade_compass_agent.web import app as app_module
    from trade_compass_agent.ops import tick_scheduler

    counts = {"start": 0, "stop": 0}

    class Scheduler:
        def __init__(self, _config):
            pass

        def start_background(self):
            counts["start"] += 1

        def shutdown(self, wait):
            counts["stop"] += 1

    config = AppConfig(data_dir=tmp_path / "data",
                       mobile=MobileConfig(True, "127.0.0.1", 0))
    monkeypatch.setattr(app_module, "load_app_config", lambda: config)
    monkeypatch.setattr(tick_scheduler, "TickScheduler", Scheduler)
    monkeypatch.setattr(tick_scheduler, "get_active_scheduler", lambda: None)
    monkeypatch.setattr(tick_scheduler, "set_active_scheduler", lambda _: None)
    monkeypatch.delenv("TRADE_COMPASS_NO_SCHEDULER", raising=False)
    app = app_module.create_app()
    with TestClient(app, client=("127.0.0.1", 12345)) as local:
        assert counts == {"start": 1, "stop": 0}
        invitation = local.post("/api/mobile/pairing/invitations").json()
        endpoint = f"https://127.0.0.1:{invitation['port']}"
        secret = secrets.token_urlsafe(32)
        status, claim = request_json(endpoint, invitation["certificate_sha256"], "POST",
                                    "/mobile/v1/pairing/claim", body={
                                        "invitation": invitation["invitation"],
                                        "device_secret": secret, "name": "生命周期测试",
                                    })
        assert status == 202
        approve(local, claim)
        assert request_json(endpoint, invitation["certificate_sha256"], "GET",
                            "/mobile/v1/sessions", secret=secret)[0] == 200
        service = app.state.mobile_service
    assert counts == {"start": 1, "stop": 1}
    assert app.state.mobile_service is None
    assert service.task.done()
    with pytest.raises(OSError):
        request_json(endpoint, invitation["certificate_sha256"], "GET",
                     "/mobile/v1/sessions", secret=secret)
    # Full parent process lifecycle restores an approved device without a new login.
    with TestClient(app, client=("127.0.0.1", 12345)) as local:
        current = local.get("/api/mobile/status").json()
        assert current["computer_id"] == invitation["computer_id"]
        assert request_json(f"https://127.0.0.1:{current['port']}", current["certificate_sha256"],
                            "GET", "/mobile/v1/sessions", secret=secret)[0] == 200
    assert counts == {"start": 2, "stop": 2}


def test_default_parent_keeps_mobile_closed(tmp_path, monkeypatch):
    from trade_compass_agent.web import app as app_module

    monkeypatch.setattr(app_module, "load_app_config", lambda: AppConfig(data_dir=tmp_path))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_NO_SCHEDULER", "true")
    with TestClient(app_module.create_app(), client=("127.0.0.1", 12345)) as local:
        assert local.post("/api/mobile/pairing/invitations").status_code == 503
        assert local.get("/api/agent/sessions").status_code == 200
    assert not (tmp_path / "mobile").exists()


def test_mobile_submission_uses_same_session_and_survives_client_disconnect(access, monkeypatch):
    import threading
    import time
    from trade_compass_agent.web import agent_api
    from trade_compass_agent.runtime.types import TurnResponse
    from trade_compass_agent.runtime.turn_control import get_turn_registry

    config, _, _, mobile, local = access
    store = SessionStore(config.data_dir / "agent_sessions")
    session = store.create()
    started, release = threading.Event(), threading.Event()
    calls = []

    class Loop:
        def run_turn(self, message, **kwargs):
            calls.append(kwargs["session_id"])
            current = store.load(kwargs["session_id"])
            store.append(current, SessionMessageRecord(role="user", content=message))
            started.set()
            assert release.wait(5)
            store.append(current, SessionMessageRecord(role="assistant", content="同一个会话的回复"))
            return TurnResponse(session_id=current.session_id, summary="同一个会话的回复", sections=[])

    monkeypatch.setattr(agent_api, "_loop_for_session", lambda *args: Loop())
    secret, claim = pair(mobile, local)
    approve(local, claim)
    headers = {"Authorization": f"Bearer {secret}"}
    body = {"request_id": "one-request-12345678", "session_id": session.session_id, "message": "手机的问题"}
    try:
        response = mobile.post("/mobile/v1/turns", json=body, headers=headers)
        assert response.status_code == 202
        assert started.wait(2)
        assert mobile.post("/mobile/v1/turns", json=body, headers=headers).json() == response.json()
        assert mobile.post("/mobile/v1/turns", json={**body, "message": "另一条"}, headers=headers).status_code == 409
        assert local.post("/api/agent/turn", json={"session_id": session.session_id, "message": "同时发送"}).status_code == 409
        desktop = local.get(f"/api/agent/sessions/{session.session_id}/messages").json()
        assert desktop["messages"][0]["content"] == "手机的问题"
        assert desktop["has_active_turn"] is True
        # Dropping the client does not cancel the computer-owned worker.
        mobile.close()
        release.set()
        deadline = time.monotonic() + 5
        while get_turn_registry().has_active_turn(session.session_id) and time.monotonic() < deadline:
            time.sleep(0.01)
        desktop = local.get(f"/api/agent/sessions/{session.session_id}/messages").json()
        assert [m["content"] for m in desktop["messages"]] == ["手机的问题", "同一个会话的回复"]
        assert calls == [session.session_id]
    finally:
        release.set()


def test_uncertain_receipt_is_not_replayed_after_restart(tmp_path):
    import sqlite3
    import hashlib
    from trade_compass_agent.mobile.turns import MobileTurns

    store = MobileTurns(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                     ("device", "request", "session", hashlib.sha256(b"question").hexdigest(), "old-turn", "running"))
    restored = MobileTurns(tmp_path)
    receipt = restored.submit("device", "request", "session", "question")
    assert receipt["status"] == "unknown"
    assert receipt["turn_id"] == "old-turn"


def test_changed_data_root_cannot_send_into_a_different_session_store(access, monkeypatch, tmp_path):
    config, _, _, mobile, local = access
    session = SessionStore(config.data_dir / "agent_sessions").create()
    secret, claim = pair(mobile, local)
    approve(local, claim)
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "different-data"))
    response = mobile.post("/mobile/v1/turns", headers={"Authorization": f"Bearer {secret}"}, json={
        "session_id": session.session_id, "request_id": "changed-root-1234567", "message": "继续对话",
    })
    assert response.status_code == 503
    assert not (tmp_path / "different-data/agent_sessions").exists()


def test_ui_can_enable_disable_without_restart_and_keeps_config_backup(tmp_path, monkeypatch):
    import yaml
    from trade_compass_agent.web import app as app_module

    path = tmp_path / "config.yaml"
    original = "mobile:\n  enabled: false\n  host: 127.0.0.1\n  port: 0\nscheduler:\n  enabled: false\n"
    path.write_text(original)
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_NO_SCHEDULER", "true")
    with TestClient(app_module.create_app(), client=("127.0.0.1", 12345)) as local:
        assert local.get("/api/mobile/status").json()["enabled"] is False
        assert local.post("/api/mobile/access", json={"enabled": True}).status_code == 200
        assert local.get("/api/mobile/status").json()["enabled"] is True
        assert yaml.safe_load(path.read_text())["mobile"]["enabled"] is True
        assert path.with_suffix(".mobile-backup.yaml").read_text() == original
        assert local.post("/api/mobile/access", json={"enabled": False}).status_code == 200
        assert local.get("/api/mobile/status").json()["enabled"] is False
        assert yaml.safe_load(path.read_text())["mobile"]["enabled"] is False


def test_project_mobile_entry_is_bundled_and_does_not_enable_access(access):
    config, _, _, _, local = access
    local.app.state.mobile_service = None
    before = {str(path): path.read_bytes() for path in config.data_dir.rglob('*') if path.is_file()}
    page = local.get('/mobile/')
    assert page.status_code == 200
    assert 'manifest.webmanifest' in page.text
    assert 'chatgpt.site' not in page.text
    manifest = local.get('/mobile/manifest.webmanifest')
    assert manifest.status_code == 200
    assert manifest.json()['display'] == 'standalone'
    assert local.get('/mobile/sw.js').status_code == 200
    assert local.get('/mobile/missing.js').status_code == 404
    assert local.get('/api/mobile/status').json() == {'enabled': False}
    assert before == {str(path): path.read_bytes() for path in config.data_dir.rglob('*') if path.is_file()}


def test_phone_creates_multiple_canonical_sessions_and_keeps_them_after_restart(access):
    config, devices, identity, mobile, local = access
    secret, claim = pair(mobile, local)
    headers = {"Authorization": f"Bearer {secret}"}
    assert mobile.post("/mobile/v1/sessions").status_code == 401
    assert mobile.post("/mobile/v1/sessions", headers=headers).status_code == 403
    approve(local, claim)
    # Creating is independent of the list's page size and of existing sessions.
    ids = []
    for _ in range(101):
        result = mobile.post("/mobile/v1/sessions", headers=headers)
        assert result.status_code == 200
        assert set(result.json()) == {"session_id", "updated_at"}
        ids.append(result.json()["session_id"])
    assert len(set(ids)) == 101
    desktop = local.get("/api/agent/sessions?limit=100").json()
    assert desktop == mobile.get("/mobile/v1/sessions?limit=100", headers=headers).json()
    assert len(desktop["sessions"]) == 100
    assert all(item["message_count"] == 0 for item in desktop["sessions"])
    restarted = TestClient(create_mobile_app(config, DeviceStore(config.data_dir / "mobile"), identity),
                           base_url="https://computer")
    for sid in (ids[0], ids[-1]):
        remote = restarted.get(f"/mobile/v1/sessions/{sid}/messages", headers=headers)
        assert remote.status_code == 200
        assert remote.json()["messages"] == []
        assert remote.json() == local.get(f"/api/agent/sessions/{sid}/messages").json()
    devices.revoke(claim["device_id"])
    assert restarted.post("/mobile/v1/sessions", headers=headers).status_code == 401
    assert len(list((config.data_dir / "agent_sessions").glob("*.jsonl"))) == 101


def test_phone_creation_rejects_changed_data_root(access, monkeypatch, tmp_path):
    config, _, _, mobile, local = access
    secret, claim = pair(mobile, local)
    approve(local, claim)
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "different-data"))
    response = mobile.post("/mobile/v1/sessions", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 503
    assert not list((config.data_dir / "agent_sessions").glob("*.jsonl"))
    assert not (tmp_path / "different-data/agent_sessions").exists()


def test_session_cursor_keeps_all_history_accessible_after_head_changes(access):
    import os

    config, devices, identity, mobile, local = access
    secret, claim = pair(mobile, local)
    approve(local, claim)
    headers = {"Authorization": f"Bearer {secret}"}
    store = SessionStore(config.data_dir / "agent_sessions")
    # Equal timestamps need a deterministic secondary ordering; scheduler sessions
    # must remain excluded from both user-facing entry points and every page.
    for index in range(205):
        sid = f"history-{index:03d}"
        store.get_or_create(sid)
        os.utime(store.path / f"{sid}.jsonl", (1700000000, 1700000000))
    store.get_or_create("scheduler-hidden")
    first = mobile.get("/mobile/v1/sessions?limit=100", headers=headers).json()
    assert first == local.get("/api/agent/sessions?limit=100").json()
    ids = [item["session_id"] for item in first["sessions"]]
    assert len(ids) == 100
    # Deleting the boundary must not invalidate its cursor or skip a later record.
    assert store.delete(ids[-1])
    store.get_or_create("newest")
    cursor = first["next_cursor"]
    while cursor:
        params = {"limit": 100, "cursor": cursor}
        result = mobile.get("/mobile/v1/sessions", params=params, headers=headers)
        assert result.status_code == 200
        payload = result.json()
        assert payload == local.get("/api/agent/sessions", params=params).json()
        ids.extend(item["session_id"] for item in payload["sessions"])
        cursor = payload["next_cursor"]
    assert len(ids) == len(set(ids)) == 205
    assert set(ids) == {f"history-{index:03d}" for index in range(205)}
    assert mobile.get("/mobile/v1/sessions", params={"cursor": "not-a-cursor"}, headers=headers).status_code == 422
    assert mobile.get("/mobile/v1/sessions", params={"cursor": first["next_cursor"]}).status_code == 401
