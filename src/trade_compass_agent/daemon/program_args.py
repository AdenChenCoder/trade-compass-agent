from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from trade_compass_agent.config import runtime_root

SERVICE_LOCATION_ENV_VARS = (
    "TRADE_COMPASS_HOME",
    "TRADE_COMPASS_CONFIG",
    "TRADE_COMPASS_ENV_FILE",
    "TRADE_COMPASS_DATA_DIR",
    "TRADE_COMPASS_MEMORY_DIR",
)


def resolve_trade_compass_binary() -> Path:
    """Resolve the trade-compass CLI executable (venv or PATH)."""
    for python_path in (Path(sys.executable), Path(sys.executable).resolve()):
        candidate = python_path.parent / "trade-compass"
        if candidate.is_file():
            return candidate
    found = shutil.which("trade-compass")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "trade-compass executable not found. Install with: uv pip install -e ."
    )


def build_serve_program_arguments(*, port: int, host: str = "127.0.0.1") -> list[str]:
    binary = resolve_trade_compass_binary()
    return [str(binary), "serve", "--host", host, "--port", str(port)]


def build_service_path() -> str:
    """Snapshot PATH for launchd (minimal default misses Homebrew/nvm/uv)."""
    priority: list[str] = []
    for python_path in (Path(sys.executable), Path(sys.executable).resolve()):
        bin_dir = str(python_path.parent)
        if bin_dir not in priority:
            priority.append(bin_dir)
    priority.extend(
        [
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
    )
    for part in os.environ.get("PATH", "").split(":"):
        if part and part not in priority:
            priority.append(part)
    return ":".join(priority)


def build_service_environment() -> dict[str, str]:
    """Snapshot non-secret runtime locations needed after login/reboot."""
    environment = {
        "PATH": build_service_path(),
        "VIRTUAL_ENV": str(Path(sys.executable).resolve().parent.parent),
        "TRADE_COMPASS_SERVICE_MARKER": "1",
    }
    for key in SERVICE_LOCATION_ENV_VARS:
        value = os.getenv(key, "").strip()
        if value:
            environment[key] = value
    return environment


def service_working_directory() -> Path:
    return runtime_root()
