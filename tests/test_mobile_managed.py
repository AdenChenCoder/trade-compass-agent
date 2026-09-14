import gzip
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest
import yaml

from trade_compass_agent.config import load_app_config
from trade_compass_agent.mobile.client import request_json
from trade_compass_agent.mobile.helper import helper_binary
from trade_compass_agent.mobile.identity import load_or_create_identity
from trade_compass_agent.runtime.session import SessionMessageRecord, SessionStore

ORIGIN = "https://computer.test.ts.net"


def until(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("consumer state did not become ready")


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    async def check_public_entry(*_args):
        return {"state": "inconclusive", "checked": 0, "reachable": 0}
    monkeypatch.setattr("trade_compass_agent.mobile.reachability.check_public_entry", check_public_entry)

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "scheduler:\n  enabled: false\nmobile:\n  provider: tailscale\n  enabled: false\n"
    )
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config_file))
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_NO_SCHEDULER", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-not-for-child")
    monkeypatch.setenv("TS_AUTHKEY", "test-secret-not-for-child")
    config = load_app_config()
    state = config.data_dir / "mobile" / "funnel"
    certs = state / "tsnet" / "certs"
    certs.mkdir(parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "computer.test.ts.net")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("computer.test.ts.net")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    (certs / "computer.test.ts.net.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (certs / "computer.test.ts.net.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    helper = tmp_path / "fake-helper"
    helper.write_text(
        f"#!{sys.executable}\n"
        + """import argparse, json, os, select, sys, time
from pathlib import Path
from datetime import datetime, timezone
p=argparse.ArgumentParser()
for name in ('state-dir','mobile-bridge-config','instance'): p.add_argument('--'+name)
for name in ('connect','wait-for-mobile','parent-stdin'): p.add_argument('--'+name, action='store_true')
a=p.parse_args(); state=Path(a.state_dir)
(state/'env-leaks.json').write_text(json.dumps([k for k in ('DEEPSEEK_API_KEY','TS_AUTHKEY','TRADE_COMPASS_CONFIG') if k in os.environ]))
while True:
 if select.select([sys.stdin],[],[],0)[0] and not sys.stdin.buffer.read(1):
  (state/'parent-closed').touch(); break
 phase='needs_login_or_network'
 if (state/'authorized').exists(): phase='listening_mobile_access_unverified' if Path(a.mobile_bridge_config).exists() else 'waiting_for_mobile'
 value={'phase':phase,'instance':a.instance,'origin':'https://computer.test.ts.net','action_url':'https://login.tailscale.com/test-only' if phase=='needs_login_or_network' else '', 'updated_at':datetime.now(timezone.utc).isoformat()}
 temp=state/'status.tmp'; temp.write_text(json.dumps(value)); temp.chmod(0o600); temp.replace(state/'status.json')
 time.sleep(.02)
"""
    )
    helper.chmod(0o700)
    monkeypatch.setattr(
        "trade_compass_agent.mobile.managed.helper_binary", lambda directory: helper
    )
    return config, config_file, state, helper


def test_local_onboarding_process_lifecycle_and_original_sessions(managed):
    from trade_compass_agent.web.app import create_app

    config, config_file, state, _ = managed
    identity = load_or_create_identity(config.data_dir / "mobile")
    identity_bytes = identity.pem_path.read_bytes()
    sessions = SessionStore(config.data_dir / "agent_sessions")
    session = sessions.create()
    sessions.append(session, SessionMessageRecord(role="user", content="Original desktop history"))
    with TestClient(create_app(), client=("127.0.0.1", 1)) as client:
        assert client.get("/api/mobile/status").json()["requested"] is False
        assert client.post("/api/mobile/access", json={"enabled": True}).status_code == 200
        until(
            lambda: (
                client.get("/api/mobile/status").json()["connection"]["phase"]
                == "needs_login_or_network"
            )
        )
        controller = client.app.state.mobile_controller
        process = controller.managed.process
        assert (
            client.get("/api/mobile/status").json()["connection"]["action_url"]
            == "https://login.tailscale.com/test-only"
        )
        assert (
            client.get("/api/agent/sessions").status_code == 200
        )  # Login never blocks the workbench.
        assert client.post("/api/mobile/access", json={"enabled": True}).status_code == 200
        assert controller.managed.process is process
        assert json.loads((state / "env-leaks.json").read_text()) == []
        (state / "authorized").touch()
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        invite = client.post("/api/mobile/pairing/invitations").json()
        assert invite["pwa_url"] == ORIGIN + "/mobile/"
        assert invite["endpoints"] == [ORIGIN]
        assert invite["host"] == "127.0.0.1"
        secret = secrets.token_urlsafe(32)
        endpoint = f"https://127.0.0.1:{invite['port']}"
        code, device = request_json(
            endpoint,
            invite["certificate_sha256"],
            "POST",
            "/mobile/v1/pairing/claim",
            body={"invitation": invite["invitation"], "name": "Phone", "device_secret": secret},
        )
        assert code == 202
        assert "verification_code" not in device
        device = next(d for d in client.get("/api/mobile/devices").json()["devices"] if d["device_id"] == device["device_id"])
        client.post(
            f"/api/mobile/devices/{device['device_id']}/approve",
            json={"verification_code": device["verification_code"]},
        ).raise_for_status()
        before = request_json(
            endpoint, invite["certificate_sha256"], "GET", "/mobile/v1/sessions", secret=secret
        )
        assert before[0] == 200
        assert before[1] == client.get("/api/agent/sessions").json()
        assert client.post("/api/mobile/access", json={"enabled": False}).status_code == 200
        assert process.returncode is not None and (state / "parent-closed").exists()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", invite["port"])) != 0
        assert not yaml.safe_load(config_file.read_text())["mobile"]["enabled"]
        assert config_file.with_suffix(".mobile-backup.yaml").exists()
        assert client.post("/api/mobile/access", json={"enabled": True}).status_code == 200
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        restored = client.get("/api/mobile/status").json()
        assert restored["computer_id"] == identity.computer_id
        assert identity.pem_path.read_bytes() == identity_bytes
        assert (
            request_json(
                f"https://127.0.0.1:{restored['port']}",
                restored["certificate_sha256"],
                "GET",
                "/mobile/v1/sessions",
                secret=secret,
            )
            == before
        )
        restarted_process = controller.managed.process
    assert restarted_process.returncode is not None


def test_helper_exit_closes_mobile_surface_and_allows_retry(managed):
    from trade_compass_agent.web.app import create_app

    _, _, state, _ = managed
    (state / "authorized").touch()
    with TestClient(create_app(), client=("127.0.0.1", 1)) as client:
        client.post("/api/mobile/access", json={"enabled": True})
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        controller = client.app.state.mobile_controller
        previous = controller.managed.process
        previous.kill()
        until(lambda: client.get("/api/mobile/status").json()["connection"]["phase"] == "error")
        assert not client.get("/api/mobile/status").json()["enabled"]
        assert client.get("/api/agent/sessions").status_code == 200
        client.post("/api/mobile/access", json={"enabled": True}).raise_for_status()
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        assert controller.managed.process.pid != previous.pid


def test_public_check_failure_and_recovery_preserve_phone_access(managed, monkeypatch):
    from trade_compass_agent.web.app import create_app

    current = {"state": "reachable", "checked": 2, "reachable": 2}

    async def check(*_args):
        if current["state"] == "inconclusive":
            raise RuntimeError("diagnostic failure")
        return dict(current)

    monkeypatch.setattr("trade_compass_agent.mobile.reachability.check_public_entry", check)
    monkeypatch.setattr("trade_compass_agent.mobile.reachability.CHECK_INTERVAL", .03)
    config, config_file, state, _ = managed
    (state / "authorized").touch()
    sessions = SessionStore(config.data_dir / "agent_sessions")
    session = sessions.create()
    sessions.append(session, SessionMessageRecord(role="user", content="Keep this original history"))
    with TestClient(create_app(), client=("127.0.0.1", 1)) as client:
        client.post("/api/mobile/access", json={"enabled": True}).raise_for_status()
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        owner = client.app.state.mobile_controller.managed
        service, process = client.app.state.mobile_service, owner.process
        secret = secrets.token_urlsafe(32)
        claim = service.devices.claim(service.devices.create_invitation()["invitation"], "Original phone", secret)
        service.devices.approve(claim["device_id"], claim["verification_code"])
        before = service.devices.lookup(secret)
        config_before = config_file.read_bytes()
        for phase, successes in [("partial", 1), ("unreachable", 0), ("inconclusive", 0), ("reachable", 2)]:
            current.update(state=phase, reachable=successes)
            observed = until(lambda: value if (value := client.get("/api/mobile/status").json())
                             ["connection"]["public_check"]["state"] == phase else None)
            assert observed["enabled"] and observed["connection"]["phase"] == "ready"
            assert observed["connection"]["public_check"]["scope"] == "computer"
            assert client.app.state.mobile_service is service and owner.process is process
            assert process.returncode is None and service.devices.lookup(secret) == before
            assert config_file.read_bytes() == config_before
            assert any(d["device_id"] == claim["device_id"] for d in client.get("/api/mobile/devices").json()["devices"])
            result = request_json(f"https://127.0.0.1:{service.port}", observed["certificate_sha256"],
                                  "GET", "/mobile/v1/sessions", secret=secret)
            assert result == (200, client.get("/api/agent/sessions").json())
            path = f"/sessions/{session.session_id}/messages"
            messages = request_json(f"https://127.0.0.1:{service.port}", observed["certificate_sha256"],
                                    "GET", "/mobile/v1" + path, secret=secret)
            assert messages == (200, client.get("/api/agent" + path).json())
        monitor = owner.reachability
        client.post("/api/mobile/access", json={"enabled": False}).raise_for_status()
        assert monitor.task.done()


@pytest.mark.skipif(not hasattr(signal, 'SIGSTOP'), reason='process suspension needs POSIX signals')
def test_suspended_component_stops_claiming_ready_and_recovers_without_pairing(managed, monkeypatch):
    from trade_compass_agent.web.app import create_app
    from trade_compass_agent.mobile import managed as implementation

    monkeypatch.setattr(implementation, 'STATUS_MAX_AGE', .5, raising=False)
    _, _, state, _ = managed
    (state / 'authorized').touch()
    with TestClient(create_app(), client=('127.0.0.1', 1)) as client:
        client.post('/api/mobile/access', json={'enabled': True}).raise_for_status()
        until(lambda: client.get('/api/mobile/status').json()['enabled'])
        service = client.app.state.mobile_service
        process = client.app.state.mobile_controller.managed.process
        secret = secrets.token_urlsafe(32)
        claim = service.devices.claim(service.devices.create_invitation()['invitation'], 'phone', secret)
        service.devices.approve(claim['device_id'], claim['verification_code'])
        before = service.devices.lookup(secret)
        os.kill(process.pid, signal.SIGSTOP)
        try:
            until(lambda: client.get('/api/mobile/status').json()['connection']['phase'] == 'control_status_unavailable')
            assert not client.get('/api/mobile/status').json()['enabled']
            assert client.get('/api/agent/sessions').status_code == 200
        finally:
            os.kill(process.pid, signal.SIGCONT)
        until(lambda: client.get('/api/mobile/status').json()['enabled'])
        assert client.app.state.mobile_service is service
        assert client.app.state.mobile_controller.managed.process is process
        assert service.devices.lookup(secret) == before


@pytest.mark.parametrize("mobile_section", ["", "mobile: null\n", "mobile: {}\n"])
def test_first_enable_without_mobile_settings_survives_desktop_restart(managed, mobile_section):
    from trade_compass_agent.web.app import create_app

    _, config_file, state, _ = managed
    original = "scheduler:\n  enabled: false\n" + mobile_section
    config_file.write_text(original)
    with TestClient(create_app(), client=("127.0.0.1", 1)) as client:
        assert client.get("/api/mobile/status").json()["provider"] == "tailscale"
        client.post("/api/mobile/access", json={"enabled": True}).raise_for_status()
        until(lambda: client.get("/api/mobile/status").json()["connection"]["action_url"])
        assert config_file.with_suffix(".mobile-backup.yaml").read_text() == original

    assert load_app_config().mobile.provider == "tailscale"
    with TestClient(create_app(), client=("127.0.0.1", 1)) as client:
        status = until(lambda: (value if (value := client.get("/api/mobile/status").json())
                                .get("connection", {}).get("action_url") else None))
        assert status["requested"] and not status["enabled"]
        (state / "authorized").touch()
        until(lambda: client.get("/api/mobile/status").json()["enabled"])
        client.post("/api/mobile/access", json={"enabled": False}).raise_for_status()
        assert load_app_config().mobile.provider == "tailscale"


def test_unsafe_action_and_previous_instance_status_are_ignored(managed):
    from types import SimpleNamespace
    from trade_compass_agent.mobile.managed import ManagedConnection, provider_action

    config, _, state, _ = managed
    connection = ManagedConnection(SimpleNamespace(state=SimpleNamespace()), config)
    status = state / "status.json"
    status.write_text(json.dumps({"instance": "previous", "phase": "ready"}))
    status.chmod(0o600)
    assert connection._read() is None
    for value in [
        "https://evil.example",
        "https://login.tailscale.com.evil.example",
        "javascript:alert(1)",
        "https://user@login.tailscale.com/",
        "https://login.tailscale.com:444/",
        "http://login.tailscale.com",
    ]:
        assert provider_action(value) == ""


def test_parent_crash_closes_the_inherited_child_pipe(managed, tmp_path):
    config, _, state, helper = managed
    program = tmp_path / "owner.py"
    program.write_text('''import asyncio
from pathlib import Path
from types import SimpleNamespace
from trade_compass_agent.config import AppConfig, MobileConfig
from trade_compass_agent.mobile import managed
import sys
managed.helper_binary = lambda _: Path(sys.argv[2])
async def main():
 connection = managed.ManagedConnection(SimpleNamespace(state=SimpleNamespace()),
     AppConfig(data_dir=Path(sys.argv[1]), mobile=MobileConfig(provider='tailscale')))
 await connection.start()
 print('started', flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
''')
    owner = subprocess.Popen([sys.executable, str(program), str(config.data_dir), str(helper)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        until(lambda: (state / "status.json").exists())
        owner.kill()
        owner.wait(timeout=5)
        until(lambda: (state / "parent-closed").exists())
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


def test_packaged_binary_integrity_cache_repair_and_readonly_assets(tmp_path, monkeypatch):
    from trade_compass_agent.mobile import helper

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    content = b"isolated test component"
    packed = gzip.compress(content)
    entry = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "archive_sha256": hashlib.sha256(packed).hexdigest(),
    }
    (bundle / "manifest.json").write_text(
        json.dumps({"protocol": 1, "platforms": {"darwin-arm64": entry}})
    )
    archive = bundle / "darwin-arm64.gz"
    archive.write_bytes(packed)
    for path in bundle.iterdir():
        path.chmod(0o400)
    bundle.chmod(0o500)
    monkeypatch.setattr(helper, "BUNDLE", bundle)
    monkeypatch.setattr(helper.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(helper.platform, "machine", lambda: "arm64")
    output = helper_binary(tmp_path / "state")
    assert output.read_bytes() == content and output.stat().st_mode & 0o777 == 0o700
    output.write_bytes(b"tampered cache")
    assert helper_binary(tmp_path / "state").read_bytes() == content
    assert archive.read_bytes() == packed
    output.unlink()
    output.symlink_to(archive)
    with pytest.raises(RuntimeError):
        helper_binary(tmp_path / "state")
    output.unlink()
    archive.chmod(0o600)
    archive.write_bytes(b"damaged package")
    with pytest.raises(RuntimeError):
        helper_binary(tmp_path / "state")
    assert not output.exists()
    bundle.chmod(0o700)
