from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Protocol

from trade_compass_agent.config import AppConfig, load_app_config


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    purpose: str
    allow_memory: bool = False


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    provider: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str = ""
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChatCompletion:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""


class ChatClient(Protocol):
    name: str

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion: ...

    def stream_complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ChatCompletion: ...


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class DisabledLLMProvider:
    name = "disabled"

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="LLM provider disabled. Deterministic rule-based output used.",
            model="none",
            provider=self.name,
        )


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"

_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": DEEPSEEK_BASE_URL,
    "openrouter": OPENROUTER_BASE_URL,
    "dashscope": DASHSCOPE_BASE_URL,
    "ollama": OLLAMA_BASE_URL,
    "lmstudio": LMSTUDIO_BASE_URL,
}

_PROVIDER_KEY_ENVS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}


class OpenAIProvider:
    """OpenAI-compatible chat completion backend."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - installation integrity guard
            raise RuntimeError(
                "openai package is required by the default LLM providers; "
                "reinstall trade-compass-agent"
            ) from exc
        client_kwargs: dict[str, str] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)
        self.model = model
        self.name = name

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": request.prompt}],
            max_tokens=120,
            temperature=0.3,
        )
        text = response.choices[0].message.content or ""
        return LLMResponse(text=text.strip(), model=self.model, provider=self.name)


def resolve_api_key(api_key_env: str) -> str | None:
    """Return the configured API key env value when non-empty."""

    value = os.getenv(api_key_env, "").strip()
    return value or None


def create_llm_provider(
    *,
    provider: str = "disabled",
    model: str = "gpt-4o-mini",
    api_key_env: str = "OPENAI_API_KEY",
    enabled: bool = False,
) -> LLMProvider:
    """Build an LLM provider from config flags.

    Returns :class:`DisabledLLMProvider` when enhancement is off, the provider
    slot is ``disabled``, or the API key env var is unset — so the default
    path never requires network or credentials.
    """

    if not enabled or provider == "disabled":
        return DisabledLLMProvider()

    api_key = resolve_api_key(api_key_env)
    if not api_key:
        return DisabledLLMProvider()

    if provider in {"openai", "deepseek"}:
        base_url = DEEPSEEK_BASE_URL if provider == "deepseek" else None
        try:
            return OpenAIProvider(
                model=model,
                api_key=api_key,
                base_url=base_url,
                name=provider,
            )
        except RuntimeError:
            return DisabledLLMProvider()

    return DisabledLLMProvider()


def _message_to_api(msg: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
    if msg.tool_call_id:
        payload["tool_call_id"] = msg.tool_call_id
    if msg.name:
        payload["name"] = msg.name
    if msg.tool_calls:
        payload["tool_calls"] = msg.tool_calls
        payload["content"] = msg.content or None
    return payload


class OpenAIChatClient:
    """OpenAI-compatible chat completion with optional tool calling."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "openai package is required by the default LLM providers; "
                "reinstall trade-compass-agent"
            ) from exc
        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = OpenAI(**client_kwargs)
        self.model = model
        self.name = name
        self.max_retries = max(0, max_retries)

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_api(m) for m in messages],
            "temperature": 0.3,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            from trade_compass_agent.runtime.exceptions import AgentTurnError

            raise AgentTurnError(f"LLM request failed: {exc}") from exc
        choice = response.choices[0].message
        tool_calls: list[ToolCall] = []
        if choice.tool_calls:
            for item in choice.tool_calls:
                fn = item.function
                tool_calls.append(
                    ToolCall(
                        id=item.id,
                        name=fn.name,
                        arguments=fn.arguments or "{}",
                    )
                )
        # Some providers (LM Studio + Qwen thinking models) put output in
        # reasoning_content instead of content; merge them.
        content = choice.content or ""
        reasoning = getattr(choice, "reasoning_content", None) or ""
        if not content and reasoning:
            content = reasoning
        return ChatCompletion(
            content=content or None,
            tool_calls=tool_calls,
            model=self.model,
            provider=self.name,
        )

    def stream_complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ChatCompletion:
        # For thinking models (LM Studio + Qwen), streaming puts everything in
        # reasoning_content with empty content. Fall back to non-streaming.
        if self.name in {"lmstudio"}:
            result = self.complete(messages, tools=tools)
            if on_delta and result.content:
                on_delta(result.content)
            return result

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_api(m) for m in messages],
            "temperature": 0.3,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        attempt = 0
        while True:
            content_parts: list[str] = []
            tool_calls_by_index: dict[int, dict[str, str]] = {}
            stream_created = False
            try:
                stream = self._client.chat.completions.create(**kwargs)
                stream_created = True
                for chunk in stream:
                    if is_cancelled and is_cancelled():
                        from trade_compass_agent.runtime.exceptions import TurnInterruptedError

                        raise TurnInterruptedError("".join(content_parts))
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        for item in delta.tool_calls:
                            idx = item.index
                            entry = tool_calls_by_index.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if item.id:
                                entry["id"] = item.id
                            if item.function:
                                if item.function.name:
                                    entry["name"] = item.function.name
                                if item.function.arguments:
                                    entry["arguments"] += item.function.arguments
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_delta and not tool_calls_by_index:
                            on_delta(delta.content)
                break
            except Exception as exc:
                from trade_compass_agent.runtime.exceptions import AgentTurnError, TurnInterruptedError

                if isinstance(exc, TurnInterruptedError):
                    raise
                has_partial_response = bool(content_parts or tool_calls_by_index)
                can_retry = (
                    stream_created
                    and not has_partial_response
                    and attempt < self.max_retries
                    and _is_transient_stream_error(exc)
                )
                if can_retry:
                    attempt += 1
                    logger.warning(
                        "LLM stream interrupted before first delta; retrying (%d/%d): %s",
                        attempt,
                        self.max_retries,
                        exc,
                    )
                    continue
                raise AgentTurnError(f"LLM request failed: {exc}") from exc

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_calls_by_index):
            entry = tool_calls_by_index[idx]
            if entry["name"]:
                tool_calls.append(
                    ToolCall(
                        id=entry["id"] or f"call_{idx}",
                        name=entry["name"],
                        arguments=entry["arguments"] or "{}",
                    )
                )

        return ChatCompletion(
            content="".join(content_parts) if content_parts else None,
            tool_calls=tool_calls,
            model=self.model,
            provider=self.name,
        )


def _is_transient_stream_error(exc: Exception) -> bool:
    """Return whether a pre-delta stream failure is safe to replay."""
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
    except ImportError:  # pragma: no cover - httpx is an OpenAI dependency
        pass
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(token in name or token in message for token in (
        "timeout",
        "connection error",
        "connection reset",
        "server disconnected",
    ))


_UNCONFIGURED_MSG = (
    "LLM is required but not configured. Set llm.provider to one of: "
    "openai, deepseek, anthropic, openrouter, dashscope — "
    "and export the matching API key env (OPENAI_API_KEY, DEEPSEEK_API_KEY, "
    "ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or DASHSCOPE_API_KEY; "
    "see config/default.yaml)."
)


def create_chat_client(config: AppConfig | None = None) -> ChatClient:
    """Return a chat client; raises :class:`AgentUnavailableError` when required but missing key."""

    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    app_config = config or load_app_config()
    llm = app_config.llm
    api_key = resolve_api_key(llm.api_key_env)

    if not api_key and llm.provider in _PROVIDER_KEY_ENVS:
        api_key = resolve_api_key(_PROVIDER_KEY_ENVS[llm.provider])

    if llm.provider in {"ollama", "lmstudio"}:
        base_url = _PROVIDER_BASE_URLS.get(llm.provider)
        try:
            return OpenAIChatClient(
                model=llm.model,
                api_key=api_key or llm.provider,
                base_url=base_url,
                name=llm.provider,
                timeout=llm.timeout,
                max_retries=llm.max_retries,
            )
        except RuntimeError as exc:
            raise AgentUnavailableError(str(exc)) from exc

    if llm.provider in {"", "disabled"} or not api_key:
        raise AgentUnavailableError(_UNCONFIGURED_MSG)

    if llm.provider in {"openai", "deepseek", "anthropic", "openrouter", "dashscope"}:
        base_url = _PROVIDER_BASE_URLS.get(llm.provider)
        try:
            return OpenAIChatClient(
                model=llm.model,
                api_key=api_key,
                base_url=base_url,
                name=llm.provider,
                timeout=llm.timeout,
                max_retries=llm.max_retries,
            )
        except RuntimeError as exc:
            raise AgentUnavailableError(str(exc)) from exc

    raise AgentUnavailableError(_UNCONFIGURED_MSG)


_VISION_CAPABLE_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku",
    "claude-3.5-sonnet", "claude-4-sonnet", "claude-4-opus",
    "qwen-vl-max", "qwen-vl-plus",
    "deepseek-vl",
    "gemma4", "gemma3",
}


def is_vision_capable(model: str) -> bool:
    """Check if a model likely supports vision/image input."""
    model_lower = model.lower()
    for prefix in _VISION_CAPABLE_MODELS:
        if prefix in model_lower:
            return True
    if "vision" in model_lower or "vl" in model_lower:
        return True
    return False


def create_vision_client(config: AppConfig | None = None) -> ChatClient | None:
    """Return a vision-capable chat client, or None if unavailable.

    Uses llm.vision_model if configured, otherwise checks if the default model supports vision.
    Supports a separate vision_provider/vision_api_key_env for calling a different endpoint.
    """
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    app_config = config or load_app_config()
    llm = app_config.llm

    vision_model = llm.vision_model
    if vision_model:
        vision_provider = llm.vision_provider or llm.provider
        key_env = llm.vision_api_key_env or _PROVIDER_KEY_ENVS.get(vision_provider, llm.api_key_env)
        api_key = resolve_api_key(key_env) if key_env else ""
        # Ollama and LM Studio don't require API keys
        if not api_key and vision_provider not in {"ollama", "lmstudio"}:
            return None
        base_url = _PROVIDER_BASE_URLS.get(vision_provider)
        try:
            return OpenAIChatClient(
                model=vision_model,
                api_key=api_key or "ollama",
                base_url=base_url,
                name=f"{vision_provider}-vision",
                timeout=llm.timeout,
                max_retries=llm.max_retries,
            )
        except RuntimeError:
            return None

    if is_vision_capable(llm.model):
        try:
            return create_chat_client(config)
        except AgentUnavailableError:
            return None

    return None
