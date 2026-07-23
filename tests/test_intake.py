from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.runtime.intake import enrich_user_message, parse_attachments


@pytest.fixture()
def app_config(tmp_path) -> AppConfig:
    return AppConfig(
        memory_dir=tmp_path / "memory",
        agent=AgentConfig(multimodal=True),
        llm=LLMConfig(provider="openai"),
    )


def test_parse_attachments_filters_invalid() -> None:
    result = parse_attachments(
        [
            {"type": "text", "content": "hello"},
            {"type": "bad"},
            {"type": "url", "url": "https://example.com"},
        ]
    )
    assert len(result) == 2
    assert result[0].type == "text"
    assert result[1].url == "https://example.com"


def test_enrich_url_attachment(app_config: AppConfig) -> None:
    enriched = enrich_user_message(
        "分析这只股票",
        [{"type": "url", "url": "https://example.com/report"}],
        config=app_config,
        memory_dir=app_config.memory_dir,
    )
    assert "https://example.com/report" in enriched
    assert "fetch_url" in enriched


@patch("trade_compass_agent.runtime.intake.create_vision_client")
@patch("trade_compass_agent.runtime.intake.is_vision_capable", return_value=True)
def test_enrich_image_vision(mock_vision_check, mock_vision_factory, app_config: AppConfig) -> None:
    mock_client = MagicMock()
    mock_client.model = "gpt-4o-mini"
    mock_client._client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content="K线图，震荡上行"))
    ]
    mock_vision_factory.return_value = mock_client

    enriched = enrich_user_message(
        "看图",
        [{"type": "image", "content": "abc123", "mime": "image/png"}],
        config=app_config,
        memory_dir=app_config.memory_dir,
    )
    assert "K线图" in enriched or "附件·图片" in enriched


def test_multimodal_disabled_skips_attachments(app_config: AppConfig) -> None:
    config = AppConfig(
        memory_dir=app_config.memory_dir,
        agent=AgentConfig(multimodal=False),
    )
    enriched = enrich_user_message(
        "hello",
        [{"type": "text", "content": "secret"}],
        config=config,
        memory_dir=config.memory_dir,
    )
    assert enriched == "hello"
