from __future__ import annotations

import importlib.util
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from trade_compass_agent.config import active_env_path, load_app_config, resolve_config_path
from trade_compass_agent.web.dist import resolve_web_dist


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str


def collect_doctor_checks() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    config_path = resolve_config_path()
    if config_path.is_file():
        checks.append(DoctorCheck("config", "PASS", str(config_path)))
    else:
        checks.append(DoctorCheck("config", "FAIL", f"missing: {config_path}"))
        return checks

    try:
        config = load_app_config(config_path)
    except Exception as exc:
        checks.append(DoctorCheck("config-parse", "FAIL", str(exc)))
        return checks
    checks.append(DoctorCheck("config-parse", "PASS", f"profile={config.profile}"))

    checks.append(_sensitive_file_check("env", active_env_path()))

    checks.append(_writable_directory_check("data", config.data_dir))
    checks.append(_writable_directory_check("memory", config.memory_dir))

    web_dist = resolve_web_dist()
    checks.append(
        DoctorCheck(
            "web-ui",
            "PASS" if web_dist is not None else "FAIL",
            str(web_dist) if web_dist is not None else "static bundle missing",
        )
    )

    provider = config.llm.provider.strip().lower()
    api_key = os.getenv(config.llm.api_key_env, "").strip()
    if provider == "disabled":
        status = "FAIL" if config.agent.require_llm else "WARN"
        checks.append(DoctorCheck("llm", status, "provider is disabled"))
    elif not api_key and provider not in {"ollama", "lmstudio"}:
        status = "FAIL" if config.agent.require_llm else "WARN"
        checks.append(DoctorCheck("llm", status, f"missing {config.llm.api_key_env}"))
    else:
        dependency = "anthropic" if provider == "anthropic" else "openai"
        installed = importlib.util.find_spec(dependency) is not None
        checks.append(
            DoctorCheck(
                "llm",
                "PASS" if installed else "FAIL",
                f"provider={provider}; dependency={dependency}",
            )
        )

    checks.append(_service_manager_check())
    return checks


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any(check.status == "FAIL" for check in checks) else 0


def _writable_directory_check(name: str, path: Path) -> DoctorCheck:
    marker = path / ".trade-compass-write-test"
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n", encoding="utf-8")
        marker.unlink()
    except OSError as exc:
        return DoctorCheck(name, "FAIL", f"{path}: {exc}")
    return DoctorCheck(name, "PASS", str(path))


def _sensitive_file_check(name: str, path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(name, "WARN", f"missing: {path}")
    if os.name == "nt":
        return DoctorCheck(name, "PASS", str(path))
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return DoctorCheck(name, "FAIL", f"{path}: expected permissions 0600, found {mode:04o}")
    return DoctorCheck(name, "PASS", f"{path} ({mode:04o})")


def _service_manager_check() -> DoctorCheck:
    if sys.platform == "darwin":
        return DoctorCheck("service", "PASS", "launchd available")
    if sys.platform.startswith("linux"):
        from trade_compass_agent.daemon.systemd import (
            systemd_linger_status,
            systemd_user_manager_available,
        )

        available, detail = systemd_user_manager_available()
        if not available:
            return DoctorCheck("service", "WARN", detail)
        linger, linger_detail = systemd_linger_status()
        if linger is not True:
            return DoctorCheck("service", "WARN", f"{detail}; {linger_detail}")
        return DoctorCheck("service", "PASS", f"{detail}; {linger_detail}")
    return DoctorCheck("service", "WARN", "foreground mode only on this platform")
