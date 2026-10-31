from __future__ import annotations

import logging
import os
import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.llm.lmstudio_context import ContextMeasurement, measure_context as _measure_lmstudio_context
from trade_compass_agent.llm.usage import (
    LLMCallAudit,
    current_llm_usage_context,
    llm_usage_context,
    mark_llm_transport_attempt,
    provider_token_usage,
)


logger = logging.getLogger(__name__)

_request_deadline: ContextVar[float | None] = ContextVar("llm_request_deadline", default=None)
_request_cancelled: ContextVar[Callable[[], bool] | None] = ContextVar("llm_request_cancelled", default=None)
_maintenance_mode: ContextVar[bool] = ContextVar("llm_maintenance_mode", default=False)


class LLMExecutionCancelled(BaseException):
    """Stop a workflow without entering memory handlers' exception fallbacks."""


def _lmstudio_loaded_context_window(base_url: str, model: str, api_key: str) -> int | None:
    """Read the loaded instance's window, never the architecture's maximum."""
    import httpx

    remaining = _remaining_request_time()
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        response = httpx.get(root + "/api/v1/models", trust_env=False,
                            timeout=min(2.0, remaining) if remaining is not None else 2.0,
                            headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        response.raise_for_status()
        payload = response.json()
    except Exception:
        payload = {}
    _remaining_request_time()
    exact, matching = [], []
    for entry in payload.get("models", []) if isinstance(payload, dict) else []:
        for instance in entry.get("loaded_instances", []):
            window = instance.get("config", {}).get("context_length")
            if type(window) is not int or window <= 0:
                continue
            if instance.get("id") == model:
                exact.append(window)
            elif entry.get("key") == model:
                matching.append(window)
    windows = exact or matching
    return min(windows) if windows else None


@contextmanager
def llm_request_deadline(deadline: float, *, is_cancelled: Callable[[], bool] | None = None):
    parent = _request_deadline.get()
    parent_cancelled = _request_cancelled.get()
    token = _request_deadline.set(min(parent, deadline) if parent is not None else deadline)
    cancel_token = _request_cancelled.set(
        (lambda: bool((parent_cancelled and parent_cancelled()) or (is_cancelled and is_cancelled())))
        if parent_cancelled or is_cancelled else None
    )
    try:
        yield
    finally:
        _request_cancelled.reset(cancel_token)
        _request_deadline.reset(token)


@contextmanager
def llm_maintenance_mode():
    """Use non-thinking generation only for factual compression or formatting."""
    token = _maintenance_mode.set(True)
    try:
        yield
    finally:
        _maintenance_mode.reset(token)


def _remaining_request_time() -> float | None:
    cancelled = _request_cancelled.get()
    if cancelled and cancelled():
        raise LLMExecutionCancelled("Workflow model execution cancelled")
    deadline = _request_deadline.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if cancelled is not None:
            raise LLMExecutionCancelled("Workflow model deadline reached")
        raise TimeoutError("Scheduled agent deadline reached before model request")
    return remaining


def check_execution_active() -> None:
    """Reject late commits from work sharing a model/workflow execution budget."""
    _remaining_request_time()


def _watch_stream_cancellation(stream, is_cancelled: Callable[[], bool] | None) -> Callable[[], None]:
    """Close a stalled stream when its shared budget expires, even without deltas."""
    deadline = _request_deadline.get()
    cancelled = _request_cancelled.get()
    if deadline is None and cancelled is None and is_cancelled is None:
        return lambda: None
    stopped = threading.Event()

    def watch() -> None:
        while not stopped.wait(.05):
            if ((deadline is not None and time.monotonic() >= deadline)
                    or (cancelled and cancelled()) or (is_cancelled and is_cancelled())):
                try:
                    # Closing a socket in another thread does not wake a blocked
                    # recv on macOS. Shutdown first so cancellation wakes the reader.
                    response = getattr(stream, "response", None)
                    network = getattr(response, "extensions", {}).get("network_stream")
                    sock = network.get_extra_info("socket") if network is not None else None
                    if sock is not None:
                        import socket
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    stream.close()
                except Exception:
                    logger.debug("Failed to close cancelled model stream", exc_info=True)
                return

    worker = threading.Thread(target=watch, name="llm-stream-cancellation", daemon=True)
    worker.start()

    def stop() -> None:
        stopped.set()
        worker.join(timeout=.2)

    return stop


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
    reasoning_content: str = field(default="", repr=False)
    image_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChatCompletion:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    reasoning_content: str = field(default="", repr=False)
    incomplete_reason: str = ""


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

def is_lmstudio_qwen(provider: str, model: str) -> bool:
    return provider.startswith("lmstudio") and any(
        version in model.lower() for version in ("qwen3.5", "qwen3.6", "qwen3.8")
    )


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


def _usage_http_client():
    """Count every transport attempt made by the OpenAI SDK, including retries."""

    import httpx

    def on_request(_request) -> None:
        mark_llm_transport_attempt()

    return httpx.Client(event_hooks={"request": [on_request]})


class OpenAIProvider:
    """OpenAI-compatible chat completion backend."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
        usage_db_path: Path | None = None,
        usage_timezone: str = "Asia/Shanghai",
    ) -> None:
        if name.startswith("lmstudio"):
            self.model = model
            self.name = name
            self._local_chat = OpenAIChatClient(
                model=model, api_key=api_key, base_url=base_url, name=name,
                max_retries=0, usage_db_path=usage_db_path, usage_timezone=usage_timezone,
                discover_context=True,
            )
            return
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - installation integrity guard
            raise RuntimeError(
                "openai package is required by the default LLM providers; "
                "reinstall trade-compass-agent"
            ) from exc
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        if usage_db_path is not None:
            client_kwargs["http_client"] = _usage_http_client()
        self._client = OpenAI(**client_kwargs)
        self.model = model
        self.name = name
        self._usage_db_path = usage_db_path
        self._usage_timezone = usage_timezone

    def complete(self, request: LLMRequest) -> LLMResponse:
        current = current_llm_usage_context()
        if getattr(self, "_local_chat", None) is not None:
            with llm_usage_context(job_id=current.job_id or request.purpose):
                response = self._local_chat.complete([ChatMessage(role="user", content=request.prompt)])
            return LLMResponse(text=(response.content or "").strip(), model=self.model, provider=self.name)
        timeout = _remaining_request_time()
        with llm_usage_context(job_id=current.job_id or request.purpose):
            audit = LLMCallAudit(
                db_path=self._usage_db_path,
                provider=self.name,
                model=self.model,
                operation="completion",
                timezone_name=self._usage_timezone,
            )
            try:
                with audit:
                    audit.mark_invocation()
                    response = self._client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": request.prompt}],
                        max_tokens=120,
                        temperature=0.3,
                        **({"timeout": timeout} if timeout is not None else {}),
                    )
            except Exception as exc:
                audit.finish(status="failed", error=exc)
                raise
            audit.finish(
                status="completed",
                usage=provider_token_usage(response),
                model=str(getattr(response, "model", "") or self.model),
                provider_request_id=str(getattr(response, "id", "") or ""),
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
    data_dir: Path | None = None,
    usage_timezone: str = "Asia/Shanghai",
    config: AppConfig | None = None,
) -> LLMProvider:
    """Build an LLM provider from config flags.

    Returns :class:`DisabledLLMProvider` when enhancement is off, the provider
    slot is ``disabled``, or the API key env var is unset — so the default
    path never requires network or credentials.
    """

    if not enabled or provider == "disabled":
        return DisabledLLMProvider()

    if provider in {"codex"}:
        from dataclasses import replace
        from trade_compass_agent.llm.native import NativeAgentClient, NativeTextProvider

        config = config or load_app_config()
        config = replace(config, llm=replace(config.llm, provider=provider, model=model),
                         data_dir=data_dir or config.data_dir)
        return NativeTextProvider(NativeAgentClient(config))

    if provider not in {"ollama", "lmstudio", *_PROVIDER_KEY_ENVS}:
        from trade_compass_agent.runtime.exceptions import AgentUnavailableError

        raise AgentUnavailableError(f"Unsupported model connection: {provider}")

    if provider in {"ollama", "lmstudio"}:
        try:
            return OpenAIProvider(
                model=model,
                api_key=provider,
                base_url=_PROVIDER_BASE_URLS[provider],
                name=provider,
                usage_db_path=data_dir / "llm_usage.db" if data_dir is not None else None,
                usage_timezone=usage_timezone,
            )
        except RuntimeError:
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
                usage_db_path=data_dir / "llm_usage.db" if data_dir is not None else None,
                usage_timezone=usage_timezone,
            )
        except RuntimeError:
            return DisabledLLMProvider()

    return DisabledLLMProvider()


def _message_to_api(msg: ChatMessage, *, preserve_reasoning: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
    if msg.tool_call_id:
        payload["tool_call_id"] = msg.tool_call_id
    if msg.name:
        payload["name"] = msg.name
    if msg.tool_calls:
        payload["tool_calls"] = msg.tool_calls
        payload["content"] = msg.content or None
    if preserve_reasoning and msg.role == "assistant" and msg.reasoning_content:
        payload["reasoning_content"] = msg.reasoning_content
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
        usage_db_path: Path | None = None,
        usage_timezone: str = "Asia/Shanghai",
        discover_context: bool = False,
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
            "max_retries": 0 if name.startswith("lmstudio") else max_retries,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        if usage_db_path is not None:
            client_kwargs["http_client"] = _usage_http_client()
        self._client = OpenAI(**client_kwargs)
        self.model = model
        self.name = name
        self._preserve_reasoning_context = name.startswith("lmstudio") and any(
            version in model.lower() for version in ("qwen3.6", "qwen3.8")
        )
        self.max_retries = 0 if name.startswith("lmstudio") else max(0, max_retries)
        self._timeout = timeout
        self._usage_db_path = usage_db_path
        self._usage_timezone = usage_timezone
        self._discover_context = discover_context and name.startswith("lmstudio")
        self._context_base_url = base_url or str(getattr(self._client, "base_url", ""))
        self._context_api_key = api_key

    @property
    def context_window(self) -> int | None:
        if not self._discover_context:
            return None
        return _lmstudio_loaded_context_window(self._context_base_url, self.model, self._context_api_key) or 32768

    @property
    def response_token_limit(self) -> int:
        if not self.name.startswith("lmstudio"):
            return 0
        if self._preserve_reasoning_context:
            window = self.context_window
            return 32768 if window is not None and window >= 65536 else 16384
        return 8192 if _request_deadline.get() is not None else 0

    def _measure_input_context(self, kwargs: dict[str, Any]) -> ContextMeasurement:
        import json
        from trade_compass_agent.runtime.compression.budget import estimate_tokens
        messages = kwargs.get("messages", [])
        remaining = _remaining_request_time()
        measured = _measure_lmstudio_context(self._context_base_url, self.model, kwargs,
            timeout=min(2.0, remaining) if remaining is not None else 2.0) if self._discover_context else None
        _remaining_request_time()
        if measured is not None:
            return ContextMeasurement(measured.input_tokens + 512, measured.context_window, measured.public_message_tokens)
        text = kwargs.get("prompt") or json.dumps({"messages": messages, "tools": kwargs.get("tools", [])}, ensure_ascii=False)
        return ContextMeasurement(int(estimate_tokens(text, chars_per_cjk=1.0, chars_per_ascii=3.0) * 1.1) + 512,
                                  self.context_window or 0, native=False)

    def measure_input_tokens(self, messages: list[ChatMessage], *, tools: list[dict[str, Any]] | None = None) -> ContextMeasurement | None:
        request = {
            "messages": [_message_to_api(m, preserve_reasoning=self._preserve_reasoning_context) for m in messages],
            "tools": tools or [],
        }
        if any(not isinstance(m.get("content") or "", str) for m in request["messages"]):
            return None
        return self._measure_input_context(request)

    def _validate_context_budget(self, kwargs: dict[str, Any]) -> None:
        from trade_compass_agent.runtime.exceptions import LLMContextBudgetError

        messages = kwargs.get("messages", [])
        # Image encoding has a different token cost; never count base64 as text.
        if any(not isinstance(m.get("content") or "", str) for m in messages):
            return
        if not self.context_window:
            return
        measured = self._measure_input_context(kwargs)
        window = measured.context_window
        if not window:
            return
        required = measured.input_tokens
        reserve = int(kwargs.get("max_tokens") or self.response_token_limit)
        if required + reserve > window:
            raise LLMContextBudgetError(
                f"上下文预算不足：估算输入 {required}、输出预留 {reserve}、已加载窗口 {window}。"
                "请增大本地模型窗口或分段提供资料；本轮已停止，已执行操作以回执为准。")

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        if self.name.startswith("lmstudio"):
            return self.stream_complete(messages, tools=tools)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_api(m, preserve_reasoning=self._preserve_reasoning_context) for m in messages],
            "temperature": 0.3,
        }
        if tools:
            kwargs["tools"] = tools
        return self._complete_api(kwargs, operation="chat")

    def complete_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ChatCompletion:
        """Audit a multimodal OpenAI-compatible request without flattening content."""

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self.name.startswith("lmstudio"):
            if self._preserve_reasoning_context or _request_deadline.get() is not None:
                kwargs.setdefault("max_tokens", self.response_token_limit)
            kwargs.update(stream=True, stream_options={"include_usage": True})
            return self._stream_api(kwargs, operation="vision")
        return self._complete_api(kwargs, operation="vision")

    def _complete_api(self, kwargs: dict[str, Any], *, operation: str) -> ChatCompletion:
        timeout = _remaining_request_time()
        if timeout is not None:
            kwargs = {**kwargs, "timeout": timeout}
        audit = LLMCallAudit(
            db_path=getattr(self, "_usage_db_path", None),
            provider=self.name,
            model=self.model,
            operation=operation,
            timezone_name=getattr(self, "_usage_timezone", "Asia/Shanghai"),
        )
        try:
            with audit:
                audit.mark_invocation()
                response = self._client.chat.completions.create(**kwargs)
                _remaining_request_time()
        except LLMExecutionCancelled as exc:
            audit.finish(status="failed", error=exc)
            raise
        except Exception as exc:
            audit.finish(status="failed", error=exc)
            from trade_compass_agent.runtime.exceptions import AgentTurnError

            raise AgentTurnError(f"LLM request failed: {_request_error_detail(exc)}") from exc
        audit.finish(
            status="completed",
            usage=provider_token_usage(response),
            model=str(getattr(response, "model", "") or self.model),
            provider_request_id=str(getattr(response, "id", "") or ""),
        )
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
        if is_lmstudio_qwen(self.name, self.model) and _maintenance_mode.get() and not tools:
            # LM Studio 0.4.x can silently ignore chat_template_kwargs for a
            # downloaded MLX model. Its raw completion endpoint applies no
            # template: render Qwen's non-thinking text format here.
            prompt_parts = []
            for message in messages:
                if message.role not in {"system", "user", "assistant"} or message.tool_calls:
                    raise ValueError("Non-thinking maintenance requires plain text messages")
                prompt_parts.append(f"<|im_start|>{message.role}\n{message.content}<|im_end|>\n")
            prompt_parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")
            summary_kwargs = {
                "model": self.model, "prompt": "".join(prompt_parts),
                "temperature": 0.3, "max_tokens": 4096, "stream": True,
                "stream_options": {"include_usage": True},
                "stop": ["<|im_end|>", "<|im_start|>"],
            }
            if self._preserve_reasoning_context:
                summary_kwargs.update(temperature=0.7, top_p=0.8, presence_penalty=1.5)
                summary_kwargs["extra_body"] = {"top_k": 20, "repeat_penalty": 1.0}
            return self._stream_api(summary_kwargs, operation="maintenance", raw=True,
                                    on_delta=on_delta, is_cancelled=is_cancelled)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_api(m, preserve_reasoning=self._preserve_reasoning_context) for m in messages],
            "temperature": 0.3,
            "stream": True,
        }
        if self.name != "ollama":
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = tools
        if self.name.startswith("lmstudio"):
            if self._preserve_reasoning_context:
                kwargs["max_tokens"] = self.response_token_limit
            elif _request_deadline.get() is not None:
                kwargs["max_tokens"] = 8192
        return self._stream_api(kwargs, operation="chat_stream", on_delta=on_delta, is_cancelled=is_cancelled)

    def _stream_api(
        self, kwargs: dict[str, Any], *, operation: str,
        raw: bool = False, on_delta: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> ChatCompletion:
        if is_lmstudio_qwen(self.name, self.model) and not raw:
            # Qwen's general thinking profile; generic low-temperature decoding
            # can exhaust the output budget without reaching a final answer.
            kwargs.update(temperature=1.0, top_p=0.95,
                          presence_penalty=0.0 if "qwen3.8" in self.model.lower() else 1.5)
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "top_k": 20, "repeat_penalty": 1.0}
        audit = LLMCallAudit(
            db_path=getattr(self, "_usage_db_path", None),
            provider=self.name,
            model=self.model,
            operation=operation,
            timezone_name=getattr(self, "_usage_timezone", "Asia/Shanghai"),
        )
        attempt = 0
        stream_usage = None
        response_model = self.model
        provider_request_id = ""
        try:
            with audit:
                while True:
                    if is_cancelled and is_cancelled():
                        from trade_compass_agent.runtime.exceptions import TurnInterruptedError

                        raise TurnInterruptedError("")
                    content_parts: list[str] = []
                    reasoning_parts: list[str] = []
                    tool_calls_by_index: dict[int, dict[str, str]] = {}
                    reasoning_started = False
                    stream_created = False
                    stream = None

                    def stop_watching() -> None:
                        pass
                    try:
                        timeout = _remaining_request_time()
                        request_kwargs = {**kwargs, "timeout": min(self._timeout, timeout)} if timeout is not None else kwargs
                        self._validate_context_budget(request_kwargs)
                        audit.mark_invocation()
                        endpoint = self._client.completions if raw else self._client.chat.completions
                        stream = endpoint.create(**request_kwargs)
                        stream_created = True
                        stop_watching = _watch_stream_cancellation(stream, is_cancelled)
                        for chunk in stream:
                            _remaining_request_time()
                            chunk_usage = getattr(chunk, "usage", None)
                            if chunk_usage is not None:
                                stream_usage = chunk_usage
                            response_model = str(getattr(chunk, "model", "") or response_model)
                            provider_request_id = str(getattr(chunk, "id", "") or provider_request_id)
                            if is_cancelled and is_cancelled():
                                from trade_compass_agent.runtime.exceptions import TurnInterruptedError

                                raise TurnInterruptedError("".join(content_parts))
                            if not chunk.choices:
                                continue
                            if self.name.startswith("lmstudio") and chunk.choices[0].finish_reason == "length":
                                from trade_compass_agent.runtime.exceptions import AgentTurnError, LLMIncompleteResponseError
                                final_delta = getattr(chunk.choices[0], "delta", None)
                                private_only = reasoning_started or bool(
                                    getattr(final_delta, "reasoning_content", None) or getattr(final_delta, "reasoning", None))
                                if (private_only and not "".join(content_parts).strip() and not tool_calls_by_index
                                        and not (getattr(final_delta, "content", None) or "").strip()
                                        and not getattr(final_delta, "tool_calls", None)):
                                    raise LLMIncompleteResponseError("模型达到输出上限，仅产生思考，未返回最终答案")
                                raise AgentTurnError("模型已达到输出上限，未完成本轮结果")
                            if raw:
                                part = chunk.choices[0].text or ""
                                content_parts.append(part)
                                if on_delta and part:
                                    on_delta(part)
                                continue
                            delta = chunk.choices[0].delta
                            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                            reasoning_started = reasoning_started or bool(reasoning)
                            if self._preserve_reasoning_context and isinstance(reasoning, str):
                                reasoning_parts.append(reasoning)
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
                        _remaining_request_time()
                        if is_cancelled and is_cancelled():
                            from trade_compass_agent.runtime.exceptions import TurnInterruptedError
                            raise TurnInterruptedError("".join(content_parts))
                        if self.name.startswith("lmstudio") and not any(content_parts) and not tool_calls_by_index:
                            from trade_compass_agent.runtime.exceptions import LLMIncompleteResponseError
                            raise LLMIncompleteResponseError("模型未返回最终答案或工具调用")
                        break
                    except Exception as exc:
                        from trade_compass_agent.runtime.exceptions import AgentTurnError, LLMContextBudgetError, LLMIncompleteResponseError, TurnInterruptedError

                        _remaining_request_time()
                        if is_cancelled and is_cancelled():
                            raise TurnInterruptedError("".join(content_parts)) from exc
                        if isinstance(exc, (TurnInterruptedError, LLMIncompleteResponseError, LLMContextBudgetError)):
                            raise
                        has_partial_response = bool(content_parts or tool_calls_by_index or reasoning_started)
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
                            # Avoid hammering a recovering connection; honour cancellation
                            # before replaying a request that produced no output.
                            for _ in range(min(20, 5 * 2 ** (attempt - 1))):
                                if is_cancelled and is_cancelled():
                                    raise TurnInterruptedError("")
                                _remaining_request_time()
                                time.sleep(0.05)
                            continue
                        raise AgentTurnError(f"LLM request failed: {_request_error_detail(exc)}") from exc
                    finally:
                        stop_watching()
                        close = getattr(stream, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:
                                logger.debug("Failed to close LLM response stream", exc_info=True)
        except (Exception, LLMExecutionCancelled) as exc:
            audit.finish(
                status="failed", error=exc, usage=provider_token_usage(None, stream_usage),
                model=response_model, provider_request_id=provider_request_id,
            )
            raise
        audit.finish(
            status="completed",
            usage=provider_token_usage(None, stream_usage),
            model=response_model,
            provider_request_id=provider_request_id,
        )

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
            reasoning_content="".join(reasoning_parts),
        )


def _request_error_detail(exc: Exception) -> str:
    import socket

    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, socket.gaierror) or any(token in str(cause).lower() for token in (
            "name or service not known", "nodename nor servname", "name resolution",
        )):
            return "DNS 解析失败，无法连接模型服务；请检查本机网络或 DNS。"
        cause = cause.__cause__ or cause.__context__
    return str(exc)


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
    "ollama, lmstudio, openai, deepseek, anthropic, openrouter, dashscope, codex. "
    "Ollama and LM Studio do not require API keys; remote providers require "
    "their matching API key env (see config/default.yaml)."
)


def create_chat_client(config: AppConfig | None = None) -> ChatClient:
    """Return a chat client; raises :class:`AgentUnavailableError` when required but missing key."""

    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    app_config = config or load_app_config()
    llm = app_config.llm
    if llm.provider in {"codex"}:
        from trade_compass_agent.llm.native import NativeAgentClient

        return NativeAgentClient(app_config)
    if llm.provider not in {"", "disabled", "ollama", "lmstudio", *_PROVIDER_KEY_ENVS}:
        raise AgentUnavailableError(f"Unsupported model connection: {llm.provider}")
    max_retries = 0 if _request_deadline.get() is not None else llm.max_retries
    api_key = resolve_api_key(llm.api_key_env)

    if not api_key and llm.provider in _PROVIDER_KEY_ENVS:
        api_key = resolve_api_key(_PROVIDER_KEY_ENVS[llm.provider])

    if llm.provider in {"ollama", "lmstudio"}:
        base_url = _PROVIDER_BASE_URLS.get(llm.provider)
        try:
            return OpenAIChatClient(
                model=llm.model,
                api_key=llm.provider,
                base_url=base_url,
                name=llm.provider,
                timeout=llm.timeout,
                max_retries=max_retries,
                usage_db_path=app_config.data_dir / "llm_usage.db",
                usage_timezone=app_config.scheduler.timezone,
                discover_context=llm.provider == "lmstudio",
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
                max_retries=max_retries,
                usage_db_path=app_config.data_dir / "llm_usage.db",
                usage_timezone=app_config.scheduler.timezone,
            )
        except RuntimeError as exc:
            raise AgentUnavailableError(str(exc)) from exc

    raise AgentUnavailableError(_UNCONFIGURED_MSG)


_VISION_CAPABLE_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku",
    "claude-3.5-sonnet", "claude-4-sonnet", "claude-4-opus",
    "qwen-vl-max", "qwen-vl-plus",
    "deepseek-flash", "deepseek-vl",
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
        if vision_provider == "codex":
            from dataclasses import replace
            from trade_compass_agent.llm.native import NativeAgentClient
            return NativeAgentClient(replace(app_config, llm=replace(llm, provider="codex", model=vision_model)))
        key_env = llm.vision_api_key_env or _PROVIDER_KEY_ENVS.get(vision_provider, llm.api_key_env)
        local_provider = vision_provider in {"ollama", "lmstudio"}
        api_key = resolve_api_key(key_env) if key_env and not local_provider else ""
        # Ollama and LM Studio don't require API keys
        if not api_key and vision_provider not in {"ollama", "lmstudio"}:
            return None
        base_url = _PROVIDER_BASE_URLS.get(vision_provider)
        try:
            return OpenAIChatClient(
                model=vision_model,
                api_key=vision_provider if local_provider else api_key,
                base_url=base_url,
                name=f"{vision_provider}-vision",
                timeout=llm.timeout,
                max_retries=llm.max_retries,
                usage_db_path=app_config.data_dir / "llm_usage.db",
                usage_timezone=app_config.scheduler.timezone,
                discover_context=vision_provider == "lmstudio",
            )
        except RuntimeError:
            return None

    if llm.provider == "codex":
        from trade_compass_agent.llm.native import NativeAgentClient
        return NativeAgentClient(app_config)

    if is_vision_capable(llm.model):
        try:
            return create_chat_client(config)
        except AgentUnavailableError:
            return None

    return None
