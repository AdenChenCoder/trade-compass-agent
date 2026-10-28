"""Connection metadata and bounded credential checks; never performs inference."""

from __future__ import annotations

import yaml

from trade_compass_agent.config import resolve_config_path

from trade_compass_agent.llm.providers import _PROVIDER_KEY_ENVS, resolve_api_key


CONNECTIONS = [
    {"provider": "lmstudio", "label": "LM Studio", "model": "qwen3.8-27b"},
    {"provider": "ollama", "label": "Ollama", "model": "gemma4:26b"},
    {"provider": "deepseek", "label": "DeepSeek", "model": "deepseek-flash"},
    {"provider": "openai", "label": "OpenAI", "model": "gpt-4o-mini"},
    {"provider": "anthropic", "label": "Anthropic", "model": "claude-3-5-sonnet-latest"},
    {"provider": "openrouter", "label": "OpenRouter", "model": "deepseek/deepseek-chat"},
    {"provider": "dashscope", "label": "DashScope", "model": "qwen-plus"},
    {"provider": "disabled", "label": "停用", "model": ""},
]


def connection_settings(config):
    path = resolve_config_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    saved = (raw or {}).get("llm_connections") or {}
    options = [{**option, "model": (saved.get(option["provider"]) or {}).get("model", option["model"])}
               for option in CONNECTIONS]
    for option in options:
        if option["provider"] == config.llm.provider:
            option["model"] = config.llm.model
    return {"provider": config.llm.provider, "model": config.llm.model, "options": options}


def check_connection(config):
    provider = config.llm.provider
    if provider == "disabled":
        return {"status": "disabled", "message": "尚未启用 Agent。"}
    if provider not in {option["provider"] for option in CONNECTIONS}:
        return {"status": "unsupported", "message": "当前版本不支持此接入，请在设置中选择可用的模型接入。"}
    if provider in {"ollama", "lmstudio"}:
        return {"status": "configured", "message": "已配置本地模型，请确认服务已启动且模型已加载。"}
    key = resolve_api_key(config.llm.api_key_env) or resolve_api_key(_PROVIDER_KEY_ENVS.get(provider, ""))
    return {"status": "configured" if key else "unauthenticated",
            "message": "已配置 API Key；执行任务时将检查连接。" if key else "尚未配置 API Key，请运行 trade-compass setup 配置。"}
