from __future__ import annotations

import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

from trade_compass_agent import cli
from trade_compass_agent.config import PROJECT_ROOT
from trade_compass_agent.development import _shutdown_dev_processes, run_development


def _process(pid: int, poll_result) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.poll.side_effect = poll_result if isinstance(poll_result, BaseException) else None
    if not isinstance(poll_result, BaseException):
        process.poll.return_value = poll_result
    return process


def test_dev_starts_backend_and_frontend_then_stops_both() -> None:
    backend = _process(101, None)
    frontend = _process(102, 7)

    with (
        patch("trade_compass_agent.development.is_source_checkout", return_value=True),
        patch("trade_compass_agent.development.shutil.which", return_value="/mock/pnpm"),
        patch("trade_compass_agent.development.probe_port", return_value=False),
        patch("trade_compass_agent.development.subprocess.Popen", side_effect=[backend, frontend]) as popen,
        patch("trade_compass_agent.development._shutdown_dev_processes") as shutdown,
        pytest.raises(SystemExit) as exc,
    ):
        run_development(api_port=20880, open_browser=False, no_scheduler=True)

    assert exc.value.code == 7
    backend_call, frontend_call = popen.call_args_list
    assert backend_call.args[0] == [
        sys.executable,
        "-m",
        "trade_compass_agent.cli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "20880",
        "--dev",
        "--no-scheduler",
    ]
    assert frontend_call.args[0] == [
        "/mock/pnpm",
        "--dir",
        str(PROJECT_ROOT / "apps" / "web"),
        "run",
        "dev",
        "--strictPort",
    ]
    assert backend_call.kwargs["env"]["TRADE_COMPASS_DEV_SUPERVISED"] == "true"
    assert frontend_call.kwargs["env"]["TRADE_COMPASS_API_ORIGIN"] == (
        "http://127.0.0.1:20880"
    )
    shutdown.assert_called_once_with([backend, frontend])


def test_dev_ctrl_c_stops_both_processes() -> None:
    backend = _process(101, KeyboardInterrupt())
    frontend = _process(102, None)

    with (
        patch("trade_compass_agent.development.is_source_checkout", return_value=True),
        patch("trade_compass_agent.development.shutil.which", return_value="/mock/pnpm"),
        patch("trade_compass_agent.development.probe_port", return_value=False),
        patch("trade_compass_agent.development.subprocess.Popen", side_effect=[backend, frontend]),
        patch("trade_compass_agent.development._shutdown_dev_processes") as shutdown,
    ):
        run_development(api_port=19704, open_browser=False, no_scheduler=False)

    shutdown.assert_called_once_with([backend, frontend])


def test_dev_rejects_running_service_port(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("trade_compass_agent.development.is_source_checkout", return_value=True),
        patch("trade_compass_agent.development.shutil.which", return_value="/mock/pnpm"),
        patch("trade_compass_agent.development.probe_port", return_value=True),
        patch("trade_compass_agent.development.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc,
    ):
        run_development(api_port=19704, open_browser=False, no_scheduler=False)

    assert exc.value.code == 1
    assert "trade-compass service stop" in capsys.readouterr().err
    popen.assert_not_called()


def test_dev_requires_pnpm(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("trade_compass_agent.development.is_source_checkout", return_value=True),
        patch("trade_compass_agent.development.shutil.which", return_value=None),
        patch("trade_compass_agent.development.subprocess.Popen") as popen,
        pytest.raises(SystemExit) as exc,
    ):
        run_development(api_port=19704, open_browser=False, no_scheduler=False)

    assert exc.value.code == 1
    assert "pnpm" in capsys.readouterr().err
    popen.assert_not_called()


def test_shutdown_dev_processes_terminates_process_groups() -> None:
    process = _process(101, None)

    with patch("trade_compass_agent.development.os.killpg") as killpg:
        _shutdown_dev_processes([process])

    killpg.assert_called_once_with(101, signal.SIGTERM)
    process.wait.assert_called_once_with(timeout=5.0)


def test_dev_command_dispatches_shared_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    run_development = MagicMock()
    monkeypatch.setattr("trade_compass_agent.development.run_development", run_development)
    monkeypatch.setattr(
        sys,
        "argv",
        ["trade-compass", "dev", "--port", "20880", "--open", "--no-scheduler"],
    )

    cli.main()

    run_development.assert_called_once_with(
        api_port=20880,
        open_browser=True,
        no_scheduler=True,
    )
