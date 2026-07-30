from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from trade_compass_agent.config import invalidate_config_cache, load_app_config
from trade_compass_agent.setup_wizard import (
    SetupCancelled,
    TerminalPrompter,
    run_setup_wizard,
)


class ScriptedPrompter(TerminalPrompter):
    def __init__(
        self,
        *,
        texts: dict[str, str] | None = None,
        confirms: dict[str, bool] | None = None,
        selections: dict[str, str] | None = None,
        multi: list[str] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.texts = texts or {}
        self.confirms = confirms or {}
        self.selections = selections or {}
        self.multi = multi
        self.secrets = secrets or {}
        self.output: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    def section(self, current: int, total: int, title: str) -> None:
        self.write(f"{current}/{total} {title}")

    def text(self, label: str, *, default: str = "", **kwargs) -> str:
        return self.texts.get(label, default)

    def secret(self, label: str, *, configured: bool) -> tuple[bool, str]:
        if label not in self.secrets:
            return False, ""
        return True, self.secrets[label]

    def confirm(self, label: str, *, default: bool) -> bool:
        return self.confirms.get(label, default)

    def select(self, label: str, options, *, current: str) -> str:
        return self.selections.get(label, current)

    def multi_select(self, label: str, options, *, current) -> list[str]:
        return list(current) if self.multi is None else self.multi


def test_terminal_prompter_supports_arrow_key_selection() -> None:
    ui = TerminalPrompter()

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            pipe_input.send_text("\x1b[B\r")
            selected = ui.select(
                "选择",
                (("first", "第一项"), ("second", "第二项")),
                current="first",
            )

    assert selected == "second"


def test_terminal_prompter_supports_space_toggled_multi_select() -> None:
    ui = TerminalPrompter()

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            pipe_input.send_text(" \x1b[B \r")
            selected = ui.multi_select(
                "多选",
                (("first", "第一项"), ("second", "第二项")),
                current=(),
            )

    assert selected == ["first", "second"]


def test_terminal_prompter_uses_selection_for_confirmation_and_masks_secrets() -> None:
    ui = TerminalPrompter()

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            pipe_input.send_text("\x1b[B\r")
            assert ui.confirm("确认", default=False) is True

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            pipe_input.send_text("new-secret\r")
            assert ui.secret("密钥", configured=False) == (True, "new-secret")


def test_terminal_prompter_cancel_raises_setup_cancelled() -> None:
    ui = TerminalPrompter()

    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            pipe_input.send_bytes(b"\x03")
            with pytest.raises(SetupCancelled):
                ui.text("名称")


@pytest.fixture
def setup_files(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """profile: local
data_dir: ./data
memory_dir: ./memory_vault
data_provider: auto
data:
  tushare_enabled: false
agent:
  require_llm: true
  learning_enabled: false
llm:
  provider: deepseek
  model: deepseek-chat
  api_key_env: DEEPSEEK_API_KEY
scheduler:
  enabled: true
  timezone: Asia/Shanghai
channels:
  gateway_enabled: false
  feishu_enabled: false
  wecom_enabled: false
  weixin_enabled: false
privacy:
  allow_external_llm_memory: false
watchlists:
  stocks: ["600519"]
  etfs: ["510300"]
  mid_term: ["600519"]
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=old-secret\nOPENAI_API_KEY=\nTRADE_COMPASS_PORT=\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    env_path.chmod(0o600)
    return config_path, env_path


def test_wizard_writes_config_and_secrets_to_authoritative_files(
    setup_files: tuple[Path, Path],
) -> None:
    config_path, env_path = setup_files
    ui = ScriptedPrompter(
        texts={
            "模型名称": "gpt-test",
            "Web 服务端口": "20880",
            "飞书 App ID": "cli-app-id",
            "企业微信 Bot ID": "wecom-bot-id",
        },
        confirms={
            "启用 Tushare（需要安装 tushare extra）": True,
            "配置增强搜索 API Key": True,
            "允许外部 LLM 额外总结对话/复盘并写入记忆": True,
            "启用对话后学习（需同时允许外部 LLM 记忆）": True,
        },
        selections={"选择主要 LLM 提供商：": "openai"},
        multi=["feishu", "wecom"],
        secrets={
            "OPENAI_API_KEY": "sk-new-secret",
            "TUSHARE_TOKEN": "tushare-secret",
            "飞书 App Secret": "feishu-secret",
            "企业微信 Bot Secret": "wecom-secret",
            "Tavily API Key": "tavily-secret",
        },
    )

    result = run_setup_wizard(config_path, env_path, prompter=ui)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["llm"]["provider"] == "openai"
    assert raw["llm"]["model"] == "gpt-test"
    assert raw["llm"]["api_key_env"] == "OPENAI_API_KEY"
    assert raw["llm"]["vision_model"] == ""
    assert raw["agent"]["require_llm"] is True
    assert raw["agent"]["learning_enabled"] is True
    assert raw["data"]["tushare_enabled"] is True
    assert raw["channels"] == {
        "gateway_enabled": True,
        "feishu_enabled": True,
        "wecom_enabled": True,
        "weixin_enabled": False,
    }
    assert raw["privacy"]["allow_external_llm_memory"] is True

    env = dotenv_values(env_path)
    assert env["OPENAI_API_KEY"] == "sk-new-secret"
    assert env["TUSHARE_TOKEN"] == "tushare-secret"
    assert env["FEISHU_APP_SECRET"] == "feishu-secret"
    assert env["WECOM_SECRET"] == "wecom-secret"
    assert env["TAVILY_API_KEY"] == "tavily-secret"
    assert env["TRADE_COMPASS_PORT"] == "20880"
    assert "sk-new-secret" not in "\n".join(ui.output)
    assert result.configured_key is True
    assert {path.name for path in result.backup_paths} == {
        "config.yaml.setup.bak",
        ".env.setup.bak",
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    invalidate_config_cache()
    config = load_app_config(config_path)
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-test"
    assert config.channels.gateway_enabled is True
    invalidate_config_cache()


def test_wizard_rerun_keeps_existing_secret_when_input_is_blank(
    setup_files: tuple[Path, Path],
) -> None:
    config_path, env_path = setup_files
    ui = ScriptedPrompter()

    result = run_setup_wizard(config_path, env_path, prompter=ui)

    assert dotenv_values(env_path)["DEEPSEEK_API_KEY"] == "old-secret"
    assert result.configured_key is True
    assert "old-secret" not in "\n".join(ui.output)


def test_wizard_cancel_does_not_change_existing_files(
    setup_files: tuple[Path, Path],
) -> None:
    config_path, env_path = setup_files
    original_config = config_path.read_bytes()
    original_env = env_path.read_bytes()
    ui = ScriptedPrompter(
        selections={"选择主要 LLM 提供商：": "openai"},
        secrets={"OPENAI_API_KEY": "not-saved"},
        confirms={"保存以上配置": False},
    )

    with pytest.raises(SetupCancelled):
        run_setup_wizard(config_path, env_path, prompter=ui)

    assert config_path.read_bytes() == original_config
    assert env_path.read_bytes() == original_env
    assert not config_path.with_name("config.yaml.setup.bak").exists()
    assert not env_path.with_name(".env.setup.bak").exists()
