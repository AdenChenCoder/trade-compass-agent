from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import questionary
import yaml
from dotenv import dotenv_values
from questionary import Choice, Style

from trade_compass_agent.concurrency import atomic_write


_LLM_OPTIONS = (
    ("deepseek", "DeepSeek（推荐）"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("openrouter", "OpenRouter"),
    ("dashscope", "DashScope / 通义千问"),
    ("ollama", "Ollama（本地，无需 API Key）"),
    ("lmstudio", "LM Studio（本地，无需 API Key）"),
    ("disabled", "暂不启用 LLM"),
)
_LLM_DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "openrouter": "deepseek/deepseek-chat",
    "dashscope": "qwen-plus",
    "ollama": "llama3.2",
    "lmstudio": "local-model",
    "disabled": "",
}
_LLM_KEY_ENVS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}
_DATA_OPTIONS = (
    ("auto", "自动（缓存 → Tushare → 新浪 → Baostock → AkShare）"),
    ("tushare", "Tushare"),
    ("akshare", "AkShare"),
    ("sina", "新浪"),
    ("baostock", "Baostock"),
    ("sample", "示例数据（离线演示）"),
)
_CHANNEL_OPTIONS = (
    ("feishu", "飞书"),
    ("wecom", "企业微信"),
    ("weixin", "微信 iLink"),
)
_TIME_FIELDS = (
    ("premarket_time", "盘前检查"),
    ("morning_plan_time", "晨间计划"),
    ("close_time", "收盘检查"),
    ("eod_review_time", "盘后复盘"),
    ("postmarket_time", "盘后归档"),
    ("weekly_time", "周度回顾"),
)
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_PROMPT_STYLE = Style(
    [
        ("qmark", "fg:#00a6a6 bold"),
        ("question", "bold"),
        ("answer", "fg:#00a6a6 bold"),
        ("pointer", "fg:#00a6a6 bold"),
        ("highlighted", "fg:#00a6a6 bold"),
        ("selected", "fg:#00a6a6"),
        ("instruction", "fg:#6c7883"),
        ("text", ""),
        ("disabled", "fg:#858585 italic"),
    ]
)
_SELECT_INSTRUCTION = "（↑/↓ 移动，Enter 确认）"
_CHECKBOX_INSTRUCTION = "（↑/↓ 移动，Space 选择，Enter 确认）"


class SetupCancelled(Exception):
    """Raised when the user cancels before committing setup changes."""


@dataclass(frozen=True)
class SetupWizardResult:
    config_path: Path
    env_path: Path
    backup_paths: tuple[Path, ...]
    provider: str
    model: str
    configured_key: bool


class TerminalPrompter:
    def __init__(
        self,
        *,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self._output = output_fn

    def write(self, text: str = "") -> None:
        self._output(text)

    def section(self, current: int, total: int, title: str) -> None:
        progress = "●" * current + "○" * (total - current)
        self.write(f"\n◆ {title}  {current}/{total}  {progress}")

    def text(
        self,
        label: str,
        *,
        default: str = "",
        validator: Callable[[str], bool] | None = None,
        error: str = "输入无效，请重试。",
    ) -> str:
        def validate(value: str) -> bool | str:
            return True if validator is None or validator(value.strip()) else error

        value = questionary.text(
            label,
            default=default,
            validate=validate,
            qmark="›",
            style=_PROMPT_STYLE,
        ).ask(kbi_msg="")
        return self._required(value).strip()

    def secret(self, label: str, *, configured: bool) -> tuple[bool, str]:
        instruction = (
            "（已配置；Enter 保留，输入 - 清除）" if configured else "（输入内容会被隐藏）"
        )
        value = questionary.password(
            label,
            instruction=instruction,
            qmark="›",
            style=_PROMPT_STYLE,
        ).ask(kbi_msg="")
        value = self._required(value).strip()
        if not value:
            return False, ""
        return True, "" if value == "-" else value

    def confirm(self, label: str, *, default: bool) -> bool:
        options = (
            [Choice("是", value=True), Choice("否", value=False)]
            if default
            else [Choice("否", value=False), Choice("是", value=True)]
        )
        value = questionary.select(
            label,
            choices=options,
            default=default,
            instruction=_SELECT_INSTRUCTION,
            pointer="›",
            qmark="?",
            style=_PROMPT_STYLE,
        ).ask(kbi_msg="")
        return bool(self._required(value))

    def select(
        self,
        label: str,
        options: Sequence[tuple[str, str]],
        *,
        current: str,
    ) -> str:
        value = questionary.select(
            label,
            choices=[Choice(title, value=value) for value, title in options],
            default=current,
            instruction=_SELECT_INSTRUCTION,
            pointer="›",
            qmark="?",
            style=_PROMPT_STYLE,
        ).ask(kbi_msg="")
        return str(self._required(value))

    def multi_select(
        self,
        label: str,
        options: Sequence[tuple[str, str]],
        *,
        current: Sequence[str],
    ) -> list[str]:
        current_set = set(current)
        value = questionary.checkbox(
            label,
            choices=[
                Choice(title, value=value, checked=value in current_set) for value, title in options
            ],
            instruction=_CHECKBOX_INSTRUCTION,
            pointer="›",
            qmark="?",
            style=_PROMPT_STYLE,
        ).ask(kbi_msg="")
        return [str(item) for item in self._required(value)]

    @staticmethod
    def _required(value):
        if value is None:
            raise SetupCancelled
        return value


def run_setup_wizard(
    config_path: Path,
    env_path: Path,
    *,
    prompter: TerminalPrompter | None = None,
) -> SetupWizardResult:
    ui = prompter or TerminalPrompter()
    raw = _read_yaml(config_path)
    env = {key: value or "" for key, value in dotenv_values(env_path).items()}
    env_updates: dict[str, str] = {}

    ui.write("\nTrade Compass 配置向导")
    ui.write("直接回车会保留当前值；密钥只写入受保护的 .env，不会显示在摘要中。")

    llm_raw = dict(raw.get("llm", {}) or {})
    agent_raw = dict(raw.get("agent", {}) or {})
    ui.section(1, 6, "模型与认证")
    old_provider = str(llm_raw.get("provider", "deepseek")).strip().lower()
    provider = ui.select(
        "选择主要 LLM 提供商：",
        _LLM_OPTIONS,
        current=old_provider if old_provider in dict(_LLM_OPTIONS) else "deepseek",
    )
    model_default = str(llm_raw.get("model", "")).strip()
    if provider != old_provider or not model_default:
        model_default = _LLM_DEFAULT_MODELS[provider]
    model = ""
    if provider != "disabled":
        model = ui.text("模型名称", default=model_default)
    llm_raw["provider"] = provider
    llm_raw["model"] = model
    agent_raw["require_llm"] = provider != "disabled"
    key_env = _LLM_KEY_ENVS.get(provider, "")
    configured_key = provider in {"ollama", "lmstudio", "disabled"}
    if key_env:
        llm_raw["api_key_env"] = key_env
        changed, value = ui.secret(key_env, configured=bool(env.get(key_env)))
        if changed:
            env_updates[key_env] = value
        configured_key = bool(value if changed else env.get(key_env))
    if provider != "disabled":
        configure_vision = ui.confirm(
            "配置独立视觉模型（图片分析）",
            default=bool(llm_raw.get("vision_model", "")),
        )
        if configure_vision:
            vision_provider = str(llm_raw.get("vision_provider", "") or provider)
            vision_provider = ui.select(
                "选择视觉模型提供商：",
                _LLM_OPTIONS[:-1],
                current=vision_provider
                if vision_provider in dict(_LLM_OPTIONS[:-1])
                else provider,
            )
            llm_raw["vision_provider"] = vision_provider
            llm_raw["vision_model"] = ui.text(
                "视觉模型名称",
                default=str(llm_raw.get("vision_model", "") or _LLM_DEFAULT_MODELS[vision_provider]),
            )
            vision_key_env = _LLM_KEY_ENVS.get(vision_provider, "")
            llm_raw["vision_api_key_env"] = vision_key_env
            if vision_key_env and vision_key_env != key_env:
                changed, value = ui.secret(
                    vision_key_env,
                    configured=bool(env.get(vision_key_env)),
                )
                if changed:
                    env_updates[vision_key_env] = value
        else:
            llm_raw["vision_model"] = ""
            llm_raw["vision_provider"] = ""
            llm_raw["vision_api_key_env"] = ""
    raw["llm"] = llm_raw
    raw["agent"] = agent_raw

    ui.section(2, 6, "存储与服务")
    raw["data_dir"] = ui.text("数据目录", default=str(raw.get("data_dir", "./data")))
    raw["memory_dir"] = ui.text("记忆目录", default=str(raw.get("memory_dir", "./memory_vault")))
    port_default = env.get("TRADE_COMPASS_PORT", "19704") or "19704"
    port = ui.text(
        "Web 服务端口",
        default=port_default,
        validator=lambda value: value.isdigit() and 1 <= int(value) <= 65535,
        error="端口必须是 1-65535 的整数。",
    )
    if port != env.get("TRADE_COMPASS_PORT", ""):
        env_updates["TRADE_COMPASS_PORT"] = port

    ui.section(3, 6, "行情数据")
    data_raw = dict(raw.get("data", {}) or {})
    current_data_provider = str(raw.get("data_provider", "auto"))
    data_provider = ui.select(
        "选择行情数据提供方式：",
        _DATA_OPTIONS,
        current=current_data_provider if current_data_provider in dict(_DATA_OPTIONS) else "auto",
    )
    raw["data_provider"] = data_provider
    use_tushare = False
    if data_provider in {"auto", "tushare"}:
        use_tushare = ui.confirm(
            "启用 Tushare（需要安装 tushare extra）",
            default=bool(data_raw.get("tushare_enabled", False)),
        )
        data_raw["tushare_enabled"] = use_tushare
        data_raw["tushare_token_env"] = "TUSHARE_TOKEN"
        if use_tushare:
            changed, value = ui.secret(
                "TUSHARE_TOKEN",
                configured=bool(env.get("TUSHARE_TOKEN")),
            )
            if changed:
                env_updates["TUSHARE_TOKEN"] = value
    else:
        data_raw["tushare_enabled"] = False
    raw["data"] = data_raw

    ui.section(4, 6, "自动化与关注列表")
    scheduler_raw = dict(raw.get("scheduler", {}) or {})
    scheduler_enabled = ui.confirm(
        "启用内置定时任务",
        default=bool(scheduler_raw.get("enabled", True)),
    )
    scheduler_raw["enabled"] = scheduler_enabled
    if scheduler_enabled and ui.confirm("自定义定时任务时间", default=False):
        scheduler_raw["timezone"] = ui.text(
            "时区",
            default=str(scheduler_raw.get("timezone", "Asia/Shanghai")),
        )
        for key, label in _TIME_FIELDS:
            scheduler_raw[key] = ui.text(
                label,
                default=str(scheduler_raw.get(key, "")),
                validator=lambda value: bool(_TIME_RE.fullmatch(value)),
                error="时间格式必须为 HH:MM。",
            )
        scheduler_raw["weekly_day"] = ui.select(
            "周度回顾日期：",
            (
                ("mon", "周一"),
                ("tue", "周二"),
                ("wed", "周三"),
                ("thu", "周四"),
                ("fri", "周五"),
                ("sat", "周六"),
                ("sun", "周日"),
            ),
            current=str(scheduler_raw.get("weekly_day", "sat")),
        )
    raw["scheduler"] = scheduler_raw
    if ui.confirm("自定义关注列表", default=False):
        watchlists_raw = dict(raw.get("watchlists", {}) or {})
        for key, label in (
            ("stocks", "股票代码"),
            ("etfs", "ETF 代码"),
            ("mid_term", "中线关注代码"),
        ):
            current = ",".join(str(item) for item in watchlists_raw.get(key, []))
            watchlists_raw[key] = _csv(ui.text(label, default=current))
        raw["watchlists"] = watchlists_raw

    ui.section(5, 6, "消息渠道与增强服务")
    channels_raw = dict(raw.get("channels", {}) or {})
    current_channels = [
        name
        for name in ("feishu", "wecom", "weixin")
        if bool(channels_raw.get(f"{name}_enabled", False))
    ]
    selected_channels = ui.multi_select(
        "选择双向消息渠道（可选）：",
        _CHANNEL_OPTIONS,
        current=current_channels,
    )
    channels_raw["gateway_enabled"] = bool(selected_channels)
    for name in ("feishu", "wecom", "weixin"):
        channels_raw[f"{name}_enabled"] = name in selected_channels
    raw["channels"] = channels_raw
    notifications_raw = dict(raw.get("notifications", {}) or {})
    notifications_raw["enabled"] = ui.confirm(
        "启用任务通知",
        default=bool(notifications_raw.get("enabled", True)),
    )
    configured_notification_channels = [
        str(item) for item in notifications_raw.get("channels", ["web_log"])
    ]
    configured_notification_channels = [
        item
        for item in configured_notification_channels
        if item not in {"feishu", "wecom", "weixin"}
    ]
    notifications_raw["channels"] = list(
        dict.fromkeys(["web_log", *configured_notification_channels, *selected_channels])
    )
    if sys.platform == "darwin":
        notifications_raw["macos_enabled"] = ui.confirm(
            "启用 macOS 本地通知",
            default=bool(notifications_raw.get("macos_enabled", False)),
        )
    raw["notifications"] = notifications_raw
    if "feishu" in selected_channels:
        _prompt_env_text(ui, env, env_updates, "FEISHU_APP_ID", "飞书 App ID")
        _prompt_env_secret(ui, env, env_updates, "FEISHU_APP_SECRET", "飞书 App Secret")
    if "wecom" in selected_channels:
        _prompt_env_text(ui, env, env_updates, "WECOM_BOT_ID", "企业微信 Bot ID")
        _prompt_env_secret(ui, env, env_updates, "WECOM_SECRET", "企业微信 Bot Secret")

    if ui.confirm("配置通知 Webhook", default=False):
        _prompt_env_secret(ui, env, env_updates, "FEISHU_WEBHOOK_URL", "飞书 Webhook URL")
        _prompt_env_secret(ui, env, env_updates, "FEISHU_WEBHOOK_SECRET", "飞书签名密钥")
        _prompt_env_secret(ui, env, env_updates, "WECOM_WEBHOOK_URL", "企业微信 Webhook URL")
        _prompt_env_secret(
            ui,
            env,
            env_updates,
            "WEBHOOK_NOTIFICATION_URL",
            "通用 Webhook URL",
        )
    if ui.confirm("配置增强搜索 API Key", default=False):
        _prompt_env_secret(ui, env, env_updates, "TAVILY_API_KEY", "Tavily API Key")
        _prompt_env_secret(ui, env, env_updates, "XAI_API_KEY", "xAI API Key")

    ui.section(6, 6, "隐私与确认")
    privacy_raw = dict(raw.get("privacy", {}) or {})
    privacy_raw["allow_external_llm_memory"] = ui.confirm(
        "允许外部 LLM 额外总结对话/复盘并写入记忆",
        default=bool(privacy_raw.get("allow_external_llm_memory", False)),
    )
    raw["privacy"] = privacy_raw
    if provider != "disabled":
        agent_raw["learning_enabled"] = ui.confirm(
            "启用对话后学习（需同时允许外部 LLM 记忆）",
            default=bool(agent_raw.get("learning_enabled", False)),
        )
    raw["agent"] = agent_raw

    ui.write("\n配置摘要（密钥已隐藏）")
    ui.write(f"  LLM:      {provider} / {model or 'disabled'}")
    ui.write(f"  数据:     {data_provider}" + (" + Tushare" if use_tushare else ""))
    ui.write(f"  自动化:   {'启用' if scheduler_enabled else '停用'}")
    ui.write(f"  消息渠道: {', '.join(selected_channels) or '无'}")
    ui.write(f"  数据目录: {raw['data_dir']}")
    ui.write(f"  记忆目录: {raw['memory_dir']}")
    if not ui.confirm("保存以上配置", default=True):
        raise SetupCancelled

    config_text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    env_text = _update_dotenv(env_path, env_updates)
    backups = _commit(config_path, config_text, env_path, env_text)
    return SetupWizardResult(
        config_path=config_path,
        env_path=env_path,
        backup_paths=backups,
        provider=provider,
        model=model,
        configured_key=configured_key,
    )


def terminal_is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return raw


def _csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _prompt_env_text(
    ui: TerminalPrompter,
    env: dict[str, str],
    updates: dict[str, str],
    key: str,
    label: str,
) -> None:
    current = env.get(key, "")
    value = ui.text(label, default=current)
    if value != current:
        updates[key] = value


def _prompt_env_secret(
    ui: TerminalPrompter,
    env: dict[str, str],
    updates: dict[str, str],
    key: str,
    label: str,
) -> None:
    changed, value = ui.secret(label, configured=bool(env.get(key)))
    if changed:
        updates[key] = value


def _update_dotenv(path: Path, updates: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            result.append(f"{key}={_dotenv_value(remaining.pop(key))}")
        else:
            result.append(line)
    if remaining and result and result[-1]:
        result.append("")
    for key, value in remaining.items():
        result.append(f"{key}={_dotenv_value(value)}")
    return "\n".join(result).rstrip() + "\n"


def _dotenv_value(value: str) -> str:
    if not value:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _commit(
    config_path: Path,
    config_text: str,
    env_path: Path,
    env_text: str,
) -> tuple[Path, ...]:
    backups: list[Path] = []
    for path, content in ((config_path, config_text), (env_path, env_text)):
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        if current is not None:
            backup = path.with_name(f"{path.name}.setup.bak")
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
            backups.append(backup)
        atomic_write(path, content)
        path.chmod(0o600)
    return tuple(backups)
