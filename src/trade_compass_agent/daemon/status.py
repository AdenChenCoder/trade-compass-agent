from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

from trade_compass_agent.daemon.constants import LAUNCHD_LABEL, SYSTEMD_UNIT_NAME
from trade_compass_agent.daemon.launchd import (
    launchd_plist_path,
    log_dir,
    plist_is_current,
    read_launchd_runtime,
)
from trade_compass_agent.daemon.systemd import (
    read_systemd_runtime,
    systemd_linger_status,
    systemd_unit_path,
    unit_is_current,
)


@dataclass
class ServiceStatus:
    platform: str
    label: str
    plist_installed: bool
    plist_path: str
    launchd_loaded: bool
    launchd_state: str
    pid: int | None
    port: int
    host: str
    port_open: bool
    health_ok: bool
    health_detail: str
    systemd_unit_installed: bool = False
    systemd_unit_path: str = ""
    systemd_loaded: bool = False
    systemd_state: str = "not_applicable"
    manager_available: bool = True
    manager_detail: str = ""
    linger_enabled: bool | None = None
    linger_detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ServiceVerification:
    status: ServiceStatus
    checks: tuple[VerificationCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
            "status": self.status.to_dict(),
        }


def probe_health(host: str, port: int, *, timeout: float = 3.0) -> tuple[bool, str]:
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return True, "ok"
            return False, f"HTTP {resp.status}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except OSError as exc:
        return False, str(exc)


def probe_port(host: str, port: int, *, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def gather_status(*, host: str, port: int) -> ServiceStatus:
    if sys.platform.startswith("linux"):
        return _gather_systemd_status(host=host, port=port)
    return _gather_launchd_status(host=host, port=port)


def _gather_launchd_status(*, host: str, port: int) -> ServiceStatus:
    plist = launchd_plist_path()
    runtime = read_launchd_runtime()
    port_open = probe_port(host, port)
    health_ok, health_detail = probe_health(host, port) if port_open else (False, "port closed")

    return ServiceStatus(
        platform="darwin",
        label=LAUNCHD_LABEL,
        plist_installed=plist.is_file(),
        plist_path=str(plist),
        launchd_loaded=runtime.get("loaded", False),
        launchd_state=str(runtime.get("state", "not_loaded")),
        pid=runtime.get("pid"),
        port=port,
        host=host,
        port_open=port_open,
        health_ok=health_ok,
        health_detail=health_detail,
    )


def _gather_systemd_status(*, host: str, port: int) -> ServiceStatus:
    unit = systemd_unit_path()
    runtime = read_systemd_runtime()
    linger_enabled, linger_detail = systemd_linger_status()
    port_open = probe_port(host, port)
    health_ok, health_detail = probe_health(host, port) if port_open else (False, "port closed")
    return ServiceStatus(
        platform="linux",
        label=SYSTEMD_UNIT_NAME,
        plist_installed=False,
        plist_path="",
        launchd_loaded=False,
        launchd_state="not_applicable",
        pid=runtime.get("pid"),
        port=port,
        host=host,
        port_open=port_open,
        health_ok=health_ok,
        health_detail=health_detail,
        systemd_unit_installed=unit.is_file(),
        systemd_unit_path=str(unit),
        systemd_loaded=runtime.get("loaded", False),
        systemd_state=str(runtime.get("state", "not_loaded")),
        manager_available=runtime.get("available", False),
        manager_detail=str(runtime.get("detail", "")),
        linger_enabled=linger_enabled,
        linger_detail=linger_detail,
    )


def print_status(status: ServiceStatus, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        return

    print(f"Label:     {status.label}")
    if status.platform == "linux":
        installed = "installed" if status.systemd_unit_installed else "missing"
        print(f"Unit:      {status.systemd_unit_path} ({installed})")
        print(f"Systemd:   {status.systemd_state} (pid={status.pid})")
        if not status.manager_available and status.manager_detail:
            print(f"Manager:   unavailable ({status.manager_detail})")
        print(f"Linger:    {status.linger_detail}")
    else:
        installed = "installed" if status.plist_installed else "missing"
        print(f"Plist:     {status.plist_path} ({installed})")
        print(f"Launchd:   {status.launchd_state} (pid={status.pid})")
    print(f"Endpoint:  http://{status.host}:{status.port} ({'open' if status.port_open else 'closed'})")
    print(f"Health:    {'ok' if status.health_ok else status.health_detail}")
    if status.platform == "linux" and status.systemd_unit_installed:
        print(f"Logs:      journalctl --user -u {SYSTEMD_UNIT_NAME} -f")
    elif status.plist_installed:
        print(f"Logs:      {log_dir() / 'serve.stdout.log'}")


def verify_status(status: ServiceStatus) -> ServiceVerification:
    installed = (
        status.systemd_unit_installed if status.platform == "linux" else status.plist_installed
    )
    definition_path = (
        status.systemd_unit_path if status.platform == "linux" else status.plist_path
    )
    definition_current, definition_detail = _definition_current(status, installed=installed)
    checks = [
        VerificationCheck(
            name="definition_installed",
            ok=installed,
            detail=definition_path if installed else f"missing: {definition_path}",
        ),
        VerificationCheck(
            name="definition_current",
            ok=definition_current,
            detail=definition_detail,
        ),
    ]

    if status.platform == "linux":
        checks.append(
            VerificationCheck(
                name="manager_available",
                ok=status.manager_available,
                detail=status.manager_detail or "systemd user manager available",
            )
        )
        manager_loaded = status.systemd_loaded and status.systemd_state == "running"
        manager_detail = f"systemd state: {status.systemd_state}"
    else:
        manager_loaded = status.launchd_loaded and status.launchd_state == "running"
        manager_detail = f"launchd state: {status.launchd_state}"

    checks.extend(
        [
            VerificationCheck(
                name="manager_running",
                ok=manager_loaded,
                detail=manager_detail,
            ),
            VerificationCheck(
                name="process",
                ok=status.pid is not None and status.pid > 0,
                detail=f"pid={status.pid}" if status.pid else "no service pid",
            ),
            VerificationCheck(
                name="endpoint",
                ok=status.port_open,
                detail=f"{status.host}:{status.port} " + ("open" if status.port_open else "closed"),
            ),
            VerificationCheck(
                name="health",
                ok=status.health_ok,
                detail=status.health_detail,
            ),
        ]
    )
    if status.platform == "linux":
        checks.append(
            VerificationCheck(
                name="linger",
                ok=status.linger_enabled is True,
                detail=status.linger_detail or "systemd linger state unavailable",
            )
        )
    return ServiceVerification(status=status, checks=tuple(checks))


def print_verification(verification: ServiceVerification, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(verification.to_dict(), ensure_ascii=False, indent=2))
        return
    for check in verification.checks:
        print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    print("Service verification: ready" if verification.ok else "Service verification: failed")


def _definition_current(status: ServiceStatus, *, installed: bool) -> tuple[bool, str]:
    if not installed:
        return False, "service definition is not installed"
    try:
        if status.platform == "linux":
            current = unit_is_current(port=status.port, host=status.host)
        else:
            current = plist_is_current(port=status.port, host=status.host)
    except Exception as exc:
        return False, f"could not compare service definition: {exc}"
    if current:
        return True, "matches the current executable, host, port, and runtime paths"
    return False, "definition drift detected; run service install --force"
