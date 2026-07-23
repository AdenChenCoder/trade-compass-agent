from __future__ import annotations

import sys

from trade_compass_agent.daemon import launchd, systemd
from trade_compass_agent.daemon.status import (
    gather_status,
    print_status,
    print_verification,
    verify_status,
)
from trade_compass_agent.web.dist import resolve_web_dist


def _service_backend():
    if sys.platform == "darwin":
        return launchd
    if sys.platform.startswith("linux"):
        return systemd
    print("trade-compass service supports macOS launchd and Linux systemd", file=sys.stderr)
    raise SystemExit(1)


def _require_production_bundle() -> None:
    if resolve_web_dist() is None:
        print(
            "Error: no static web bundle found. Build before installing the service:\n"
            "  pnpm --dir apps/web build\n",
            file=sys.stderr,
        )
        raise SystemExit(1)


def run_service_command(
    command: str,
    *,
    port: int,
    host: str = "127.0.0.1",
    force: bool = False,
    as_json: bool = False,
) -> None:
    from trade_compass_agent.web.security import is_loopback_host

    if not is_loopback_host(host):
        print("trade-compass service only supports a loopback host", file=sys.stderr)
        raise SystemExit(1)
    backend = _service_backend()

    try:
        if command == "install":
            _require_production_bundle()
            backend.install(port=port, host=host, force=force)
        elif command == "uninstall":
            backend.uninstall()
        elif command == "start":
            backend.start(port=port, host=host)
        elif command == "stop":
            backend.stop()
        elif command == "restart":
            backend.restart(port=port, host=host)
        elif command == "status":
            print_status(gather_status(host=host, port=port), as_json=as_json)
        elif command == "verify":
            verification = verify_status(gather_status(host=host, port=port))
            print_verification(verification, as_json=as_json)
            if not verification.ok:
                raise SystemExit(1)
        else:
            print(f"Unknown service command: {command}", file=sys.stderr)
            raise SystemExit(1)
    except systemd.SystemdUnavailableError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
