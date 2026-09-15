from __future__ import annotations

import asyncio
import http.client
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

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


@pytest.fixture
def browser_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.delenv("TRADE_COMPASS_DEV_CORS", raising=False)
    monkeypatch.delenv("TRADE_COMPASS_NO_SCHEDULER", raising=False)


@pytest.mark.parametrize(("dev", "host"), [(False, "127.0.0.1"), (True, "127.0.0.1"), (False, "::1")])
def test_open_waits_for_this_server_to_respond(dev, host, browser_runtime):
    import uvicorn

    opened = threading.Event()
    responses = []
    server = None

    async def slow_app(scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            await asyncio.sleep(1.4)  # A realistic startup longer than the old one-second delay.
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
        else:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"status":"degraded"}'})

    def browser_open(url):
        address = urlsplit(url)
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
        try:
            connection.request("GET", "/agent")
            responses.append(connection.getresponse().status)
        except OSError:
            responses.append("connection refused")
        finally:
            connection.close()
            opened.set()

    def serve(_app, **kwargs):
        nonlocal server
        assert kwargs["reload"] is dev
        server = uvicorn.Server(uvicorn.Config(slow_app, host=kwargs["host"], port=kwargs["port"],
                                headers=kwargs.get("headers"), log_level="error"))

        def stop_after_check():
            opened.wait(10)
            server.should_exit = True

        stopper = threading.Thread(target=stop_after_check)
        stopper.start()
        try:
            server.run()
        finally:
            opened.set()
            stopper.join(2)

    # Reserve a socket only to select a free local port; the real server must bind it itself.
    with socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET) as reservation:
        try:
            reservation.bind((host, 0))
        except OSError:
            if host == "::1":
                pytest.skip("IPv6 loopback unavailable")
            raise
        port = reservation.getsockname()[1]
    with (
        patch("trade_compass_agent.cli.webbrowser.open", side_effect=browser_open),
        patch("trade_compass_agent.web.dist.resolve_web_dist", return_value=Path("unused")),
        patch("trade_compass_agent.daemon.log_rotation.start_launchd_log_rotation"),
        patch("uvicorn.run", side_effect=serve),
    ):
        run_serve(host, port, dev=dev, open_browser=True, no_scheduler=True)
    assert responses == [200]


@pytest.mark.parametrize("occupied", [False, True])
def test_open_does_not_launch_after_failed_startup(occupied, browser_runtime):
    class OtherApplication(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), OtherApplication) as other:
        serving = threading.Thread(target=other.serve_forever)
        serving.start()

        def fail_start(*args, **kwargs):
            if occupied:
                threading.Event().wait(1.3)  # Import completes before discovering the occupied port.
            raise SystemExit(1)

        try:
            with (
                patch("trade_compass_agent.cli.webbrowser.open") as browser,
                patch("trade_compass_agent.daemon.log_rotation.start_launchd_log_rotation"),
                patch("uvicorn.run", side_effect=fail_start),
            ):
                with pytest.raises(SystemExit):
                    run_serve("127.0.0.1", other.server_port, dev=True, open_browser=True, no_scheduler=True)
                # A pending opener must also be cancelled if startup exits immediately.
                threading.Event().wait(1.2 if not occupied else 0.3)
                browser.assert_not_called()
        finally:
            other.shutdown()
            serving.join(2)
