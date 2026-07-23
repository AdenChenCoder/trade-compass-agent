from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _security_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    for name in list(sys.modules):
        if name.startswith("trade_compass_agent.web."):
            del sys.modules[name]
    app_module = importlib.import_module("trade_compass_agent.web.app")
    return TestClient(app_module.app, raise_server_exceptions=raise_server_exceptions)


def test_rejects_untrusted_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = _security_client(monkeypatch, tmp_path)

    response = client.get("/health", headers={"Host": "attacker.example"})

    assert response.status_code == 400
    assert "host" in response.text.lower()


@pytest.mark.parametrize("host", ["127.0.0.2:19704", "localhost:19704", "[::1]:19704"])
def test_accepts_loopback_host_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
) -> None:
    client = _security_client(monkeypatch, tmp_path)

    response = client.get("/health", headers={"Host": host})

    assert response.status_code == 200


def test_rejects_oversized_declared_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_MAX_REQUEST_BYTES", "4")
    client = _security_client(monkeypatch, tmp_path)

    response = client.post("/api/channels/inbound/feishu", content=b"12345")

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Origin": "https://attacker.example"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "http://127.0.0.1:19704"}, 501),
        ({"Origin": "http://localhost:3000"}, 501),
        ({}, 501),
    ],
)
def test_browser_writes_require_local_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    client = _security_client(monkeypatch, tmp_path)

    response = client.post("/api/channels/inbound/feishu", json={}, headers=headers)

    assert response.status_code == expected_status


def test_inbound_http_callbacks_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _security_client(monkeypatch, tmp_path)

    response = client.post("/api/channels/inbound/feishu", json={"challenge": "unsafe"})

    assert response.status_code == 501
    assert "disabled" in response.json()["detail"].lower()


def test_internal_errors_do_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _security_client(monkeypatch, tmp_path, raise_server_exceptions=False)
    monitoring = importlib.import_module("trade_compass_agent.web.monitoring")

    def fail_health() -> dict:
        raise RuntimeError("secret-internal-path")

    monkeypatch.setattr(monitoring, "build_health_report", fail_health)
    response = client.get("/health")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret-internal-path" not in response.text
