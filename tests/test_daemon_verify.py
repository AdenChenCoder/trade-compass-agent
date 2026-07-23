from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from trade_compass_agent.daemon.cli import run_service_command
from trade_compass_agent.daemon.status import (
    ServiceStatus,
    print_verification,
    verify_status,
)


def _status(platform: str = "darwin", **overrides) -> ServiceStatus:
    values = {
        "platform": platform,
        "label": "trade-compass",
        "plist_installed": platform == "darwin",
        "plist_path": "/tmp/trade-compass.plist" if platform == "darwin" else "",
        "launchd_loaded": platform == "darwin",
        "launchd_state": "running" if platform == "darwin" else "not_applicable",
        "pid": 4242,
        "port": 19704,
        "host": "127.0.0.1",
        "port_open": True,
        "health_ok": True,
        "health_detail": "ok",
        "systemd_unit_installed": platform == "linux",
        "systemd_unit_path": "/tmp/trade-compass.service" if platform == "linux" else "",
        "systemd_loaded": platform == "linux",
        "systemd_state": "running" if platform == "linux" else "not_applicable",
        "manager_available": True,
        "manager_detail": "systemd user manager available" if platform == "linux" else "",
        "linger_enabled": True if platform == "linux" else None,
        "linger_detail": "systemd linger enabled" if platform == "linux" else "",
    }
    values.update(overrides)
    return ServiceStatus(**values)


def test_verify_ready_launchd_service() -> None:
    with patch("trade_compass_agent.daemon.status.plist_is_current", return_value=True):
        verification = verify_status(_status())

    assert verification.ok is True
    assert all(check.ok for check in verification.checks)
    assert {check.name for check in verification.checks} == {
        "definition_installed",
        "definition_current",
        "manager_running",
        "process",
        "endpoint",
        "health",
    }


def test_verify_ready_systemd_service_requires_linger() -> None:
    with patch("trade_compass_agent.daemon.status.unit_is_current", return_value=True):
        verification = verify_status(_status("linux"))

    assert verification.ok is True
    assert {check.name for check in verification.checks} >= {"manager_available", "linger"}


def test_verify_reports_definition_drift_and_unhealthy_endpoint() -> None:
    status = _status(port_open=False, health_ok=False, health_detail="port closed")
    with patch("trade_compass_agent.daemon.status.plist_is_current", return_value=False):
        verification = verify_status(status)

    failed = {check.name: check.detail for check in verification.checks if not check.ok}
    assert verification.ok is False
    assert "service install --force" in failed["definition_current"]
    assert failed["endpoint"].endswith("closed")
    assert failed["health"] == "port closed"


@pytest.mark.parametrize("linger", [False, None])
def test_verify_rejects_linux_without_confirmed_linger(linger: bool | None) -> None:
    status = _status("linux", linger_enabled=linger, linger_detail="linger unavailable")
    with patch("trade_compass_agent.daemon.status.unit_is_current", return_value=True):
        verification = verify_status(status)

    assert verification.ok is False
    assert next(check for check in verification.checks if check.name == "linger").ok is False


def test_verify_json_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("trade_compass_agent.daemon.status.plist_is_current", return_value=True):
        verification = verify_status(_status())

    print_verification(verification, as_json=True)
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["status"]["health_ok"] is True
    assert all(check["ok"] for check in output["checks"])


def test_verify_command_returns_nonzero_when_not_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unhealthy = _status(plist_installed=False, launchd_loaded=False, pid=None)
    with (
        patch("trade_compass_agent.daemon.cli.sys.platform", "darwin"),
        patch("trade_compass_agent.daemon.cli.gather_status", return_value=unhealthy),
        pytest.raises(SystemExit) as exc,
    ):
        run_service_command("verify", port=19704)

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "[FAIL] definition_installed" in output
    assert "Service verification: failed" in output


def test_verify_command_succeeds_without_mutating_service() -> None:
    with (
        patch("trade_compass_agent.daemon.cli.sys.platform", "darwin"),
        patch("trade_compass_agent.daemon.cli.gather_status", return_value=_status()),
        patch("trade_compass_agent.daemon.status.plist_is_current", return_value=True),
        patch("trade_compass_agent.daemon.cli.launchd") as launchd,
    ):
        run_service_command("verify", port=19704)

    launchd.install.assert_not_called()
    launchd.start.assert_not_called()
    launchd.restart.assert_not_called()
    launchd.stop.assert_not_called()
    launchd.uninstall.assert_not_called()
