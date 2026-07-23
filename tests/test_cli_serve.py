from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from trade_compass_agent.cli import DEFAULT_PORT, _resolve_port, run_serve
from trade_compass_agent.web.security import is_loopback_host


def test_default_port() -> None:
    assert DEFAULT_PORT == 19704


def test_resolve_port_explicit() -> None:
    assert _resolve_port(8080) == 8080


def test_resolve_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADE_COMPASS_PORT", "19999")
    assert _resolve_port(None) == 19999


def test_resolve_port_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADE_COMPASS_PORT", raising=False)
    assert _resolve_port(None) == DEFAULT_PORT


def test_serve_preflight_exits_without_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.delenv("TRADE_COMPASS_WEB_DIST_OVERRIDE", raising=False)

    with patch("trade_compass_agent.web.dist.resolve_web_dist", return_value=None):
        with pytest.raises(SystemExit) as exc:
            run_serve("127.0.0.1", DEFAULT_PORT, dev=False, open_browser=False, no_scheduler=True)
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "no static web bundle" in captured.err.lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_are_supported(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
def test_remote_hosts_are_rejected(host: str) -> None:
    assert is_loopback_host(host) is False


def test_serve_rejects_remote_bind_before_start(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc, patch("uvicorn.run") as mock_run:
        run_serve("0.0.0.0", DEFAULT_PORT, dev=True, open_browser=False, no_scheduler=True)

    assert exc.value.code == 1
    assert "remote listening is not supported" in capsys.readouterr().err
    mock_run.assert_not_called()


def test_serve_dev_skips_preflight(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("TRADE_COMPASS_DEV_CORS", raising=False)
    monkeypatch.delenv("TRADE_COMPASS_NO_SCHEDULER", raising=False)

    with patch("trade_compass_agent.web.dist.resolve_web_dist", return_value=None):
        with patch("uvicorn.run") as mock_run:
            run_serve("127.0.0.1", DEFAULT_PORT, dev=True, open_browser=False, no_scheduler=False)

    assert os.environ.get("TRADE_COMPASS_DEV_CORS") == "true"
    assert os.environ.get("TRADE_COMPASS_NO_SCHEDULER") is None
    captured = capsys.readouterr()
    assert "pnpm --dir apps/web dev" in captured.out
    assert "/agent" in captured.out
    assert "Scheduler: will start via lifespan" in captured.out
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["port"] == DEFAULT_PORT


def test_serve_dev_no_scheduler_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("TRADE_COMPASS_NO_SCHEDULER", raising=False)

    with patch("trade_compass_agent.web.dist.resolve_web_dist", return_value=None):
        with patch("uvicorn.run"):
            run_serve("127.0.0.1", DEFAULT_PORT, dev=True, open_browser=False, no_scheduler=True)

    assert os.environ.get("TRADE_COMPASS_NO_SCHEDULER") == "true"
    captured = capsys.readouterr()
    assert "Scheduler: disabled (--no-scheduler)" in captured.out


def test_serve_initializes_platform_log_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADE_COMPASS_NO_SCHEDULER", raising=False)

    with (
        patch("trade_compass_agent.daemon.log_rotation.start_launchd_log_rotation") as start,
        patch("uvicorn.run"),
    ):
        run_serve(
            "127.0.0.1",
            DEFAULT_PORT,
            dev=True,
            open_browser=False,
            no_scheduler=True,
        )

    start.assert_called_once_with()
