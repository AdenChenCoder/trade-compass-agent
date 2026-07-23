from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import trade_compass_agent.runtime.intake  # noqa: F401 — prime import graph
from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import (
    DEEPSEEK_BASE_URL,
    OpenAIChatClient,
    create_chat_client,
    create_llm_provider,
)
from trade_compass_agent.runtime.exceptions import AgentTurnError
from trade_compass_agent.runtime.intake import enrich_user_message


@pytest.fixture()
def deepseek_config() -> AppConfig:
    return AppConfig(
        agent=AgentConfig(require_llm=True, multimodal=True),
        llm=LLMConfig(
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )


@pytest.fixture()
def mock_openai_client(monkeypatch):
    mock_openai_cls = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAI = mock_openai_cls
    monkeypatch.setitem(sys.modules, "openai", mock_module)
    return mock_openai_cls


def test_create_chat_client_deepseek(
    deepseek_config: AppConfig, monkeypatch, mock_openai_client: MagicMock
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    mock_openai_client.return_value = MagicMock()

    client = create_chat_client(deepseek_config)

    mock_openai_client.assert_called_once_with(
        api_key="test-key", timeout=90.0, max_retries=2, base_url=DEEPSEEK_BASE_URL
    )
    assert client.name == "deepseek"
    assert client.model == "deepseek-chat"


def test_create_llm_provider_deepseek(monkeypatch, mock_openai_client: MagicMock) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    mock_openai_client.return_value = MagicMock()

    provider = create_llm_provider(
        provider="deepseek",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
        enabled=True,
    )

    mock_openai_client.assert_called_once_with(api_key="test-key", base_url=DEEPSEEK_BASE_URL)
    assert provider.name == "deepseek"
    assert provider.model == "deepseek-chat"


def test_stream_complete_retries_read_timeout_before_first_delta(
    mock_openai_client: MagicMock,
) -> None:
    request = httpx.Request("POST", DEEPSEEK_BASE_URL)

    class TimeoutStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise httpx.ReadTimeout("slow response", request=request)

    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=None, content="最终计划"))]
    )
    api = MagicMock()
    api.chat.completions.create.side_effect = [TimeoutStream(), iter([chunk])]
    mock_openai_client.return_value = api
    client = OpenAIChatClient(
        model="deepseek-chat",
        api_key="test-key",
        base_url=DEEPSEEK_BASE_URL,
        timeout=180.0,
        max_retries=1,
    )

    result = client.stream_complete([])

    assert result.content == "最终计划"
    assert api.chat.completions.create.call_count == 2


def test_stream_complete_does_not_retry_after_partial_delta(
    mock_openai_client: MagicMock,
) -> None:
    request = httpx.Request("POST", DEEPSEEK_BASE_URL)
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=None, content="部分内容"))]
    )

    def partial_stream():
        yield chunk
        raise httpx.ReadTimeout("stream interrupted", request=request)

    api = MagicMock()
    api.chat.completions.create.return_value = partial_stream()
    mock_openai_client.return_value = api
    client = OpenAIChatClient(
        model="deepseek-chat",
        api_key="test-key",
        base_url=DEEPSEEK_BASE_URL,
        timeout=180.0,
        max_retries=1,
    )

    with pytest.raises(AgentTurnError, match="stream interrupted"):
        client.stream_complete([])

    api.chat.completions.create.assert_called_once()


def test_deepseek_image_attachment_skips_vision(tmp_path) -> None:
    config = AppConfig(
        memory_dir=tmp_path / "memory",
        data_dir=tmp_path / "data",
        agent=AgentConfig(multimodal=True),
        llm=LLMConfig(provider="deepseek"),
    )
    import base64
    valid_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50).decode()
    enriched = enrich_user_message(
        "看图",
        [{"type": "image", "content": valid_b64, "mime": "image/png"}],
        config=config,
        memory_dir=config.memory_dir,
    )
    assert "附件·图片" in enriched
    assert "image_ocr" in enriched or "image_analyze" in enriched
