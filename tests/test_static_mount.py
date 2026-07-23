from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _reload_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    for name in list(sys.modules):
        if name.startswith("trade_compass_agent.web."):
            del sys.modules[name]
    app_module = importlib.import_module("trade_compass_agent.web.app")
    return TestClient(app_module.app)


@pytest.fixture()
def static_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "web_dist"
    bundle.mkdir()
    (bundle / "index.html").write_text(
        "<!DOCTYPE html><html><body><h1>Trade Compass UI</h1></body></html>",
        encoding="utf-8",
    )
    (bundle / "404.html").write_text("<html><body>not found</body></html>", encoding="utf-8")
    return bundle


def test_health_endpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    client = _reload_app(monkeypatch)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert "checks" in body
    assert "uptime_seconds" in body


def test_health_reports_no_scheduler_runtime_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_NO_SCHEDULER", "true")
    client = _reload_app(monkeypatch)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "disabled"


def test_serves_static_from_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, static_bundle: Path
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_WEB_DIST_OVERRIDE", str(static_bundle))
    client = _reload_app(monkeypatch)

    response = client.get("/")
    assert response.status_code == 200
    assert "Trade Compass UI" in response.text

    spa_response = client.get("/agent")
    assert spa_response.status_code == 200
    assert "Trade Compass UI" in spa_response.text

    api_response = client.get("/api/market-pulse")
    assert api_response.status_code == 200

    missing_api_response = client.get("/api/does-not-exist")
    assert missing_api_response.status_code == 404

    missing_asset_response = client.get("/assets/does-not-exist.js")
    assert missing_asset_response.status_code == 404


def test_placeholder_when_no_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setattr(
        "trade_compass_agent.web.dist.resolve_web_dist",
        lambda: None,
    )
    client = _reload_app(monkeypatch)

    response = client.get("/")
    assert response.status_code == 200
    assert "not bundled" in response.text.lower()


def test_dev_cors_only_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DEV_CORS", "true")
    client = _reload_app(monkeypatch)

    response = client.options(
        "/api/market-pulse",
        headers={
            "Origin": "http://127.0.0.1:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
