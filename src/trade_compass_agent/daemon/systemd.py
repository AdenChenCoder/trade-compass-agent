from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from trade_compass_agent.concurrency import atomic_write
from trade_compass_agent.daemon.constants import SERVICE_DESCRIPTION, SYSTEMD_UNIT_NAME
from trade_compass_agent.daemon.program_args import (
    build_service_environment,
    build_serve_program_arguments,
    service_working_directory,
)


class SystemdUnavailableError(RuntimeError):
    """Raised when the systemd user manager cannot be used."""


def systemd_unit_path() -> Path:
    configured = os.getenv("XDG_CONFIG_HOME", "").strip()
    config_home = Path(configured).expanduser() if configured else Path.home() / ".config"
    return config_home / "systemd" / "user" / SYSTEMD_UNIT_NAME


def generate_systemd_unit(*, port: int, host: str = "127.0.0.1") -> str:
    program_args = build_serve_program_arguments(port=port, host=host)
    executable = " ".join(_systemd_quote(argument) for argument in program_args)
    working_dir = _systemd_quote(str(service_working_directory()))
    environment = "\n".join(
        f"Environment={_systemd_quote(f'{key}={value}')}"
        for key, value in build_service_environment().items()
    )
    return f"""[Unit]
Description={SERVICE_DESCRIPTION}
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={executable}
WorkingDirectory={working_dir}
{environment}
Restart=on-failure
RestartSec=10
TimeoutStopSec=90
KillSignal=SIGINT
LimitNOFILE=65536
UMask=0077
NoNewPrivileges=true
RestrictSUIDSGID=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def unit_is_current(*, port: int, host: str = "127.0.0.1") -> bool:
    path = systemd_unit_path()
    if not path.is_file():
        return False
    installed = path.read_text(encoding="utf-8")
    expected = generate_systemd_unit(port=port, host=host)
    return _normalize_unit_for_comparison(installed) == _normalize_unit_for_comparison(expected)


def refresh_unit_if_needed(*, port: int, host: str = "127.0.0.1") -> bool:
    path = systemd_unit_path()
    if not path.is_file() or unit_is_current(port=port, host=host):
        return False
    _require_systemd_user_manager()
    _write_unit(path, generate_systemd_unit(port=port, host=host))
    _systemctl("daemon-reload")
    _systemctl("try-restart", SYSTEMD_UNIT_NAME, check=False)
    print("Updated systemd user service definition")
    return True


def install(*, port: int, host: str = "127.0.0.1", force: bool = False) -> None:
    _require_systemd_user_manager()
    path = systemd_unit_path()
    if path.is_file() and not force:
        changed = refresh_unit_if_needed(port=port, host=host)
        _systemctl("enable", "--now", SYSTEMD_UNIT_NAME)
        if changed:
            print("Service definition updated")
            return
        print(f"Service already installed: {path}")
        print("Service enabled and started")
        return

    _write_unit(path, generate_systemd_unit(port=port, host=host))
    print(f"Installing systemd user service: {path}")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", SYSTEMD_UNIT_NAME)
    print("Service installed, enabled, and started")
    print("  status: trade-compass service status")
    print(f"  logs:   journalctl --user -u {SYSTEMD_UNIT_NAME} -f")
    linger, detail = systemd_linger_status()
    if linger is not True:
        print(f"  warning: {detail}")


def uninstall() -> None:
    _require_systemd_user_manager()
    _systemctl("disable", "--now", SYSTEMD_UNIT_NAME, check=False)
    path = systemd_unit_path()
    if path.is_file():
        path.unlink()
        print(f"Removed {path}")
    _systemctl("daemon-reload")
    _systemctl("reset-failed", SYSTEMD_UNIT_NAME, check=False)
    print("Service uninstalled")


def start(*, port: int, host: str = "127.0.0.1") -> None:
    _require_systemd_user_manager()
    if not systemd_unit_path().is_file():
        install(port=port, host=host, force=True)
        return
    refresh_unit_if_needed(port=port, host=host)
    _systemctl("start", SYSTEMD_UNIT_NAME)
    print("Service started")


def stop() -> None:
    _require_systemd_user_manager()
    _systemctl("stop", SYSTEMD_UNIT_NAME, check=False)
    print("Service stopped")


def restart(*, port: int, host: str = "127.0.0.1") -> None:
    _require_systemd_user_manager()
    if not systemd_unit_path().is_file():
        install(port=port, host=host, force=True)
        return
    refresh_unit_if_needed(port=port, host=host)
    _systemctl("restart", SYSTEMD_UNIT_NAME)
    print("Service restarted")


def read_systemd_runtime() -> dict:
    if shutil.which("systemctl") is None:
        return {
            "available": False,
            "loaded": False,
            "state": "systemctl_missing",
            "pid": None,
        }
    proc = _systemctl(
        "show",
        SYSTEMD_UNIT_NAME,
        "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "user manager unavailable").strip()
        return {
            "available": False,
            "loaded": False,
            "state": "unavailable",
            "pid": None,
            "detail": detail,
        }
    properties = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    pid_text = properties.get("MainPID", "0")
    return {
        "available": True,
        "loaded": properties.get("LoadState") == "loaded",
        "state": properties.get("SubState") or properties.get("ActiveState") or "unknown",
        "active_state": properties.get("ActiveState", "unknown"),
        "pid": int(pid_text) if pid_text.isdigit() and pid_text != "0" else None,
        "fragment_path": properties.get("FragmentPath", ""),
    }


def systemd_user_manager_available() -> tuple[bool, str]:
    if shutil.which("systemctl") is None:
        return False, "systemctl is not installed"
    proc = _systemctl("show-environment", capture_output=True, check=False, timeout=5)
    if proc.returncode == 0:
        return True, "systemd user manager available"
    detail = (proc.stderr or proc.stdout or "systemd user manager unavailable").strip()
    return False, detail


def systemd_linger_status() -> tuple[bool | None, str]:
    """Return whether the user manager survives logout and boot without login."""
    if shutil.which("loginctl") is None:
        return None, "loginctl unavailable; cannot verify linger"
    proc = subprocess.run(
        ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "cannot query linger").strip()
        return None, detail
    enabled = proc.stdout.strip().lower() == "yes"
    if enabled:
        return True, "systemd linger enabled"
    return False, "systemd linger disabled; service may stop at logout"


def _require_systemd_user_manager() -> None:
    available, detail = systemd_user_manager_available()
    if not available:
        raise SystemdUnavailableError(
            "systemd user manager is unavailable: "
            f"{detail}. Use foreground `trade-compass serve` or enable a user session."
        )


def _systemctl(
    *arguments: str,
    capture_output: bool = False,
    check: bool = True,
    timeout: float = 90,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        capture_output=capture_output,
        text=True,
        check=check,
        timeout=timeout,
    )


def _write_unit(path: Path, content: str) -> None:
    atomic_write(path, content)
    path.chmod(0o644)


def _systemd_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("systemd unit values cannot contain newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _normalize_unit_for_comparison(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return re.sub(
        r'^Environment="PATH=.*"$',
        'Environment="PATH=__TRADE_COMPASS_PATH__"',
        normalized,
        flags=re.MULTILINE,
    )
