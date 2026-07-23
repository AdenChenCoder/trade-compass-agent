from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from trade_compass_agent.daemon.constants import LAUNCHD_LABEL
from trade_compass_agent.daemon.launchd import generate_launchd_plist, plist_is_current


def test_generate_launchd_plist_contains_serve_and_label():
    plist = generate_launchd_plist(port=19704)
    assert LAUNCHD_LABEL in plist
    assert "<string>serve</string>" in plist
    assert "<string>19704</string>" in plist
    assert "KeepAlive" in plist
    assert "RunAtLoad" in plist
    assert "serve.stdout.log" in plist
    assert "NumberOfFiles" in plist
    assert "<integer>65536</integer>" in plist
    assert "<key>Umask</key>" in plist
    assert "<integer>63</integer>" in plist


def test_plist_is_current_ignores_path_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trade_compass_agent.daemon.launchd.launchd_plist_path",
        lambda: tmp_path / f"{LAUNCHD_LABEL}.plist",
    )
    path = tmp_path / f"{LAUNCHD_LABEL}.plist"
    expected = generate_launchd_plist(port=19704)
    mutated = re.sub(
        r"(<key>PATH</key>\s*<string>)(.*?)(</string>)",
        r"\1/old/path\3",
        expected,
        flags=re.S,
    )
    path.write_text(mutated, encoding="utf-8")
    assert plist_is_current(port=19704) is True


def test_plist_is_current_detects_port_change(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trade_compass_agent.daemon.launchd.launchd_plist_path",
        lambda: tmp_path / f"{LAUNCHD_LABEL}.plist",
    )
    path = tmp_path / f"{LAUNCHD_LABEL}.plist"
    path.write_text(generate_launchd_plist(port=19704), encoding="utf-8")
    assert plist_is_current(port=19999) is False


def test_launchd_plist_escapes_and_preserves_runtime_location():
    environment = {
        "PATH": "/usr/bin",
        "VIRTUAL_ENV": "/tmp/venv",
        "TRADE_COMPASS_SERVICE_MARKER": "1",
        "TRADE_COMPASS_HOME": "/tmp/home&state",
    }
    with patch(
        "trade_compass_agent.daemon.launchd.build_service_environment",
        return_value=environment,
    ):
        plist = generate_launchd_plist(port=19704)

    assert "TRADE_COMPASS_HOME" in plist
    assert "/tmp/home&amp;state" in plist


def test_generate_launchd_plist_does_not_load_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=must-not-be-loaded\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with patch("trade_compass_agent.config.active_env_path", return_value=env_path):
        generate_launchd_plist(port=19704)

    assert "DEEPSEEK_API_KEY" not in os.environ
