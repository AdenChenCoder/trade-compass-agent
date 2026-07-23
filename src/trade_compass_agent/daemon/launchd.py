from __future__ import annotations

import os
import re
import subprocess
import time
from html import escape
from pathlib import Path

from trade_compass_agent.config import load_app_config
from trade_compass_agent.daemon.constants import LAUNCHD_LABEL, SERVICE_DESCRIPTION
from trade_compass_agent.daemon.program_args import (
    build_service_environment,
    build_serve_program_arguments,
    service_working_directory,
)

LAUNCHD_DOMAIN = f"gui/{os.getuid()}"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def log_dir() -> Path:
    path = load_app_config().data_dir / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_launchd_plist(*, port: int, host: str = "127.0.0.1") -> str:
    program_args = build_serve_program_arguments(port=port, host=host)
    working_dir = str(service_working_directory())
    logs = log_dir()
    environment = build_service_environment()
    args_xml = "\n        ".join(f"<string>{escape(arg)}</string>" for arg in program_args)
    environment_xml = "\n        ".join(
        f"<key>{escape(key)}</key>\n        <string>{escape(value)}</string>"
        for key, value in environment.items()
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>Comment</key>
    <string>{SERVICE_DESCRIPTION}</string>
    <key>ProgramArguments</key>
    <array>
        {args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{escape(working_dir)}</string>
    <key>EnvironmentVariables</key>
    <dict>
        {environment_xml}
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>Umask</key>
    <integer>63</integer>
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
    <key>HardResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{escape(str(logs / 'serve.stdout.log'))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(logs / 'serve.stderr.log'))}</string>
</dict>
</plist>
"""


def _normalize_plist(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _normalize_plist_for_comparison(text: str) -> str:
    normalized = _normalize_plist(text)
    return re.sub(
        r"(<key>PATH</key>\s*<string>)(.*?)(</string>)",
        r"\1__TRADE_COMPASS_PATH__\3",
        normalized,
        flags=re.S,
    )


def plist_is_current(*, port: int, host: str = "127.0.0.1") -> bool:
    path = launchd_plist_path()
    if not path.is_file():
        return False
    installed = path.read_text(encoding="utf-8")
    expected = generate_launchd_plist(port=port, host=host)
    return _normalize_plist_for_comparison(installed) == _normalize_plist_for_comparison(expected)


def refresh_plist_if_needed(*, port: int, host: str = "127.0.0.1") -> bool:
    path = launchd_plist_path()
    if not path.is_file() or plist_is_current(port=port, host=host):
        return False
    path.write_text(generate_launchd_plist(port=port, host=host), encoding="utf-8")
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    subprocess.run(["launchctl", "bootout", target], check=False, timeout=90)
    subprocess.run(["launchctl", "bootstrap", LAUNCHD_DOMAIN, str(path)], check=False, timeout=30)
    print("Updated launchd service definition")
    return True


def install(*, port: int, host: str = "127.0.0.1", force: bool = False) -> None:
    path = launchd_plist_path()
    if path.is_file() and not force:
        if not plist_is_current(port=port, host=host):
            refresh_plist_if_needed(port=port, host=host)
            print("Service definition updated")
            return
        print(f"Service already installed: {path}")
        print("Use: trade-compass service install --force")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_launchd_plist(port=port, host=host), encoding="utf-8")
    print(f"Installing launchd service: {path}")
    subprocess.run(["launchctl", "bootstrap", LAUNCHD_DOMAIN, str(path)], check=True, timeout=30)
    print("Service installed and loaded")
    print("  status: trade-compass service status")
    print(f"  logs:   tail -f {log_dir()}/serve.stdout.log")


def uninstall() -> None:
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    subprocess.run(["launchctl", "bootout", target], check=False, timeout=90)
    path = launchd_plist_path()
    if path.is_file():
        path.unlink()
        print(f"Removed {path}")
    print("Service uninstalled")


def start(*, port: int, host: str = "127.0.0.1") -> None:
    path = launchd_plist_path()
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    if not path.is_file():
        install(port=port, host=host, force=True)
        return
    refresh_plist_if_needed(port=port, host=host)
    try:
        subprocess.run(["launchctl", "kickstart", target], check=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        if exc.returncode not in {3, 113}:
            raise
        subprocess.run(["launchctl", "bootstrap", LAUNCHD_DOMAIN, str(path)], check=True, timeout=30)
        subprocess.run(["launchctl", "kickstart", target], check=True, timeout=30)
    print("Service started")


def stop() -> None:
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    try:
        subprocess.run(["launchctl", "bootout", target], check=True, timeout=90)
    except subprocess.CalledProcessError as exc:
        if exc.returncode not in {3, 113}:
            raise
    print("Service stopped")


def restart(*, port: int, host: str = "127.0.0.1") -> None:
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    path = launchd_plist_path()
    refresh_plist_if_needed(port=port, host=host)
    try:
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
    except subprocess.CalledProcessError as exc:
        if exc.returncode not in {3, 113}:
            raise
        if not path.is_file():
            install(port=port, host=host, force=True)
            return
        subprocess.run(["launchctl", "bootstrap", LAUNCHD_DOMAIN, str(path)], check=True, timeout=30)
        subprocess.run(["launchctl", "kickstart", target], check=True, timeout=30)
    print("Service restarted")


def read_launchd_runtime() -> dict:
    target = f"{LAUNCHD_DOMAIN}/{LAUNCHD_LABEL}"
    try:
        proc = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        output = proc.stdout
    except subprocess.CalledProcessError:
        return {"loaded": False, "state": "not_loaded", "pid": None}

    pid_match = re.search(r"\bpid = (\d+)", output)
    state_match = re.search(r"\bstate = (\S+)", output)
    return {
        "loaded": True,
        "state": state_match.group(1) if state_match else "unknown",
        "pid": int(pid_match.group(1)) if pid_match else None,
        "raw": output,
    }


def wait_for_port(host: str, port: int, *, timeout: float = 30.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.3)
    return False
