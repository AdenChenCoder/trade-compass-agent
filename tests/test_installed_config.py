from __future__ import annotations

from pathlib import Path
import stat
from unittest.mock import patch

import pytest
import yaml

from trade_compass_agent.config import (
    Settings,
    ensure_runtime_dirs,
    initialize_user_files,
    invalidate_config_cache,
    load_app_config,
    resolve_config_path,
    update_scheduler_config,
)


def _write_packaged_config(path: Path) -> None:
    path.write_text(
        """profile: local
data_dir: ./data
memory_dir: ./memory_vault
data_provider: auto
agent:
  require_llm: true
llm:
  provider: deepseek
  model: deepseek-chat
  api_key_env: DEEPSEEK_API_KEY
scheduler:
  enabled: true
""",
        encoding="utf-8",
    )


@pytest.fixture
def installed_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    packaged = tmp_path / "default.yaml"
    env_example = tmp_path / "env.example"
    _write_packaged_config(packaged)
    env_example.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(home))
    monkeypatch.delenv("TRADE_COMPASS_CONFIG", raising=False)
    invalidate_config_cache()
    with (
        patch("trade_compass_agent.config.is_source_checkout", return_value=False),
        patch("trade_compass_agent.config.PACKAGED_CONFIG_PATH", packaged),
        patch("trade_compass_agent.config.PACKAGED_ENV_EXAMPLE_PATH", env_example),
    ):
        yield home, packaged
    invalidate_config_cache()


def test_installed_defaults_resolve_state_under_user_home(installed_layout) -> None:
    home, packaged = installed_layout

    assert resolve_config_path() == packaged
    config = load_app_config()

    assert config.llm.provider == "deepseek"
    assert config.data_dir == home / "data"
    assert config.memory_dir == home / "memory_vault"


def test_setup_copies_templates_without_overwriting(installed_layout) -> None:
    home, _ = installed_layout

    config_path, env_path = initialize_user_files()
    config_path.write_text("profile: customized\n", encoding="utf-8")
    initialize_user_files()

    assert config_path == home / "config.yaml"
    assert env_path == home / ".env"
    assert config_path.read_text(encoding="utf-8") == "profile: customized\n"
    assert resolve_config_path() == config_path
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_runtime_state_directories_are_owner_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"

    ensure_runtime_dirs(Settings(data_dir=data_dir, memory_dir=memory_dir))

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(memory_dir.stat().st_mode) == 0o700


def test_setup_force_never_overwrites_env_secrets(installed_layout) -> None:
    _, _ = installed_layout
    config_path, env_path = initialize_user_files()
    config_path.write_text("profile: customized\n", encoding="utf-8")
    env_path.write_text("DEEPSEEK_API_KEY=keep-me\n", encoding="utf-8")

    initialize_user_files(force=True)

    assert "profile: local" in config_path.read_text(encoding="utf-8")
    assert env_path.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=keep-me\n"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_scheduler_update_never_writes_packaged_default(installed_layout) -> None:
    home, packaged = installed_layout
    original = packaged.read_text(encoding="utf-8")

    updated = update_scheduler_config({"enabled": False})

    user_raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert updated.scheduler.enabled is False
    assert user_raw["scheduler"]["enabled"] is False
    assert packaged.read_text(encoding="utf-8") == original
