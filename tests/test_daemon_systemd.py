from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from trade_compass_agent.daemon import systemd
from trade_compass_agent.daemon.cli import run_service_command
from trade_compass_agent.daemon.constants import SYSTEMD_UNIT_NAME
from trade_compass_agent.daemon.program_args import build_service_environment
from trade_compass_agent.daemon.status import gather_status, print_status
from trade_compass_agent.diagnostics import _service_manager_check


def _stable_unit_inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        systemd,
        "build_serve_program_arguments",
        lambda **values: [
            "/opt/trade compass/bin/trade-compass",
            "serve",
            "--host",
            values["host"],
            "--port",
            str(values["port"]),
        ],
    )
    monkeypatch.setattr(systemd, "service_working_directory", lambda: tmp_path / "runtime")
    monkeypatch.setattr(
        systemd,
        "build_service_environment",
        lambda: {
            "PATH": "/opt/trade%compass/bin:/usr/bin",
            "VIRTUAL_ENV": "/opt/trade compass",
            "TRADE_COMPASS_SERVICE_MARKER": "1",
        },
    )
    monkeypatch.setattr(
        systemd,
        "systemd_linger_status",
        lambda: (True, "systemd linger enabled"),
    )


def test_generate_systemd_unit_is_user_scoped_and_journal_backed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_unit_inputs(monkeypatch, tmp_path)

    unit = systemd.generate_systemd_unit(port=19704)

    assert 'ExecStart="/opt/trade compass/bin/trade-compass" "serve"' in unit
    assert 'Environment="PATH=/opt/trade%%compass/bin:/usr/bin"' in unit
    assert "Restart=on-failure" in unit
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit
    assert "UMask=0077" in unit
    assert "NoNewPrivileges=true" in unit
    assert "WantedBy=default.target" in unit
    assert "User=" not in unit


def test_systemd_unit_path_respects_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert systemd.systemd_unit_path() == tmp_path / "xdg/systemd/user" / SYSTEMD_UNIT_NAME


def test_service_environment_preserves_locations_but_not_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path / "custom-home"))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(tmp_path / "custom.yaml"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-enter-unit")

    environment = build_service_environment()

    assert environment["TRADE_COMPASS_HOME"] == str(tmp_path / "custom-home")
    assert environment["TRADE_COMPASS_CONFIG"] == str(tmp_path / "custom.yaml")
    assert "DEEPSEEK_API_KEY" not in environment


def test_unit_is_current_ignores_only_path_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_unit_inputs(monkeypatch, tmp_path)
    unit_path = tmp_path / SYSTEMD_UNIT_NAME
    monkeypatch.setattr(systemd, "systemd_unit_path", lambda: unit_path)
    expected = systemd.generate_systemd_unit(port=19704)
    unit_path.write_text(
        expected.replace(
            'Environment="PATH=/opt/trade%%compass/bin:/usr/bin"',
            'Environment="PATH=/old/bin"',
        ),
        encoding="utf-8",
    )

    assert systemd.unit_is_current(port=19704) is True
    assert systemd.unit_is_current(port=19999) is False


def test_install_writes_unit_and_enables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_unit_inputs(monkeypatch, tmp_path)
    unit_path = tmp_path / "config/systemd/user" / SYSTEMD_UNIT_NAME
    monkeypatch.setattr(systemd, "systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(systemd, "_require_systemd_user_manager", lambda: None)
    systemctl = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(systemd, "_systemctl", systemctl)

    systemd.install(port=19704)

    assert unit_path.is_file()
    assert stat.S_IMODE(unit_path.stat().st_mode) == 0o644
    assert systemctl.call_args_list == [
        call("daemon-reload"),
        call("enable", "--now", SYSTEMD_UNIT_NAME),
    ]


def test_install_existing_unit_still_enables_and_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stable_unit_inputs(monkeypatch, tmp_path)
    unit_path = tmp_path / SYSTEMD_UNIT_NAME
    monkeypatch.setattr(systemd, "systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(systemd, "_require_systemd_user_manager", lambda: None)
    unit_path.write_text(systemd.generate_systemd_unit(port=19704), encoding="utf-8")
    systemctl = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(systemd, "_systemctl", systemctl)

    systemd.install(port=19704)

    systemctl.assert_called_once_with("enable", "--now", SYSTEMD_UNIT_NAME)


def test_read_systemd_runtime_parses_manager_state(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "\n".join(
        [
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "MainPID=4242",
            f"FragmentPath=/home/user/.config/systemd/user/{SYSTEMD_UNIT_NAME}",
        ]
    )
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        systemd,
        "_systemctl",
        lambda *_, **__: subprocess.CompletedProcess([], 0, output, ""),
    )

    runtime = systemd.read_systemd_runtime()

    assert runtime["available"] is True
    assert runtime["loaded"] is True
    assert runtime["state"] == "running"
    assert runtime["pid"] == 4242


def test_systemd_linger_status_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/usr/bin/loginctl")
    monkeypatch.setattr(
        systemd.subprocess,
        "run",
        lambda *_, **__: subprocess.CompletedProcess([], 0, "no\n", ""),
    )

    enabled, detail = systemd.systemd_linger_status()

    assert enabled is False
    assert "may stop at logout" in detail


def test_linux_service_cli_dispatches_to_systemd(tmp_path: Path) -> None:
    with (
        patch("trade_compass_agent.daemon.cli.sys.platform", "linux"),
        patch("trade_compass_agent.daemon.cli.resolve_web_dist", return_value=tmp_path),
        patch("trade_compass_agent.daemon.cli.systemd.install") as install,
    ):
        run_service_command("install", port=19704, force=True)

    install.assert_called_once_with(port=19704, host="127.0.0.1", force=True)


def test_linux_service_cli_reports_missing_user_manager(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("trade_compass_agent.daemon.cli.sys.platform", "linux"),
        patch(
            "trade_compass_agent.daemon.cli.systemd.start",
            side_effect=systemd.SystemdUnavailableError("no user bus"),
        ),
        pytest.raises(SystemExit) as exc,
    ):
        run_service_command("start", port=19704)

    assert exc.value.code == 1
    assert "no user bus" in capsys.readouterr().err


def test_linux_status_combines_systemd_and_http_health(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = tmp_path / SYSTEMD_UNIT_NAME
    unit.touch()
    runtime = {"available": True, "loaded": True, "state": "running", "pid": 123}
    with (
        patch("trade_compass_agent.daemon.status.sys.platform", "linux"),
        patch("trade_compass_agent.daemon.status.systemd_unit_path", return_value=unit),
        patch("trade_compass_agent.daemon.status.read_systemd_runtime", return_value=runtime),
        patch(
            "trade_compass_agent.daemon.status.systemd_linger_status",
            return_value=(True, "systemd linger enabled"),
        ),
        patch("trade_compass_agent.daemon.status.probe_port", return_value=True),
        patch("trade_compass_agent.daemon.status.probe_health", return_value=(True, "ok")),
    ):
        status = gather_status(host="127.0.0.1", port=19704)
        print_status(status)

    assert status.systemd_loaded is True
    assert status.health_ok is True
    output = capsys.readouterr().out
    assert "Systemd:   running" in output
    assert "journalctl --user" in output
    assert "systemd linger enabled" in output


def test_linux_doctor_warns_when_systemd_user_manager_is_unavailable() -> None:
    with (
        patch("trade_compass_agent.diagnostics.sys.platform", "linux"),
        patch(
            "trade_compass_agent.daemon.systemd.systemd_user_manager_available",
            return_value=(False, "no user bus"),
        ),
    ):
        check = _service_manager_check()

    assert check.status == "WARN"
    assert check.detail == "no user bus"


def test_linux_doctor_warns_when_linger_is_disabled() -> None:
    with (
        patch("trade_compass_agent.diagnostics.sys.platform", "linux"),
        patch(
            "trade_compass_agent.daemon.systemd.systemd_user_manager_available",
            return_value=(True, "systemd user manager available"),
        ),
        patch(
            "trade_compass_agent.daemon.systemd.systemd_linger_status",
            return_value=(False, "systemd linger disabled"),
        ),
    ):
        check = _service_manager_check()

    assert check.status == "WARN"
    assert "linger disabled" in check.detail
