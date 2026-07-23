from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from trade_compass_agent.config import load_project_dotenv


def test_load_project_dotenv_reads_deepseek_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")

    with (
        patch("trade_compass_agent.config.PROJECT_ROOT", tmp_path),
        patch("trade_compass_agent.config.is_source_checkout", return_value=True),
    ):
        load_project_dotenv()

    assert os.environ.get("DEEPSEEK_API_KEY") == "from-dotenv"


def test_load_project_dotenv_does_not_override_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "existing")
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")

    with (
        patch("trade_compass_agent.config.PROJECT_ROOT", tmp_path),
        patch("trade_compass_agent.config.is_source_checkout", return_value=True),
    ):
        load_project_dotenv()

    assert os.environ.get("DEEPSEEK_API_KEY") == "existing"


def test_load_project_dotenv_uses_installed_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("DEEPSEEK_API_KEY=installed-home\n", encoding="utf-8")
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(home))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with patch("trade_compass_agent.config.is_source_checkout", return_value=False):
        load_project_dotenv()

    assert os.environ.get("DEEPSEEK_API_KEY") == "installed-home"
