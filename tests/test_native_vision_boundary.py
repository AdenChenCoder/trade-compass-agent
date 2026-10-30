from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm import providers
from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime import intake
from trade_compass_agent.runtime.tools import chart_pattern, image_tools


@pytest.fixture
def config(tmp_path):
    return AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                     llm=LLMConfig(provider="codex", model="gpt-4o"))


@pytest.mark.parametrize("explicit", [False, True])
def test_native_model_name_cannot_claim_image_input(config, monkeypatch, explicit):
    if explicit:
        config = replace(config, llm=replace(config.llm, vision_model="gpt-4o", vision_provider="codex"))
    monkeypatch.setattr(providers, "create_chat_client", lambda *a, **k: pytest.fail("Text client is not a vision client"))
    monkeypatch.setattr(providers, "OpenAIChatClient", lambda *a, **k: pytest.fail("Codex must not be routed to the OpenAI API"))
    assert providers.create_vision_client(config) is None


def test_codex_preserves_independent_lmstudio_vision(config, monkeypatch):
    config = replace(config, llm=replace(config.llm, vision_model="qwen3.6-35b-a3b", vision_provider="lmstudio"))
    built = []
    client = object()
    monkeypatch.setattr(providers, "OpenAIChatClient", lambda **kw: built.append(kw) or client)
    assert providers.create_vision_client(config) is client
    assert built[0]["name"] == "lmstudio-vision"
    assert built[0]["model"] == "qwen3.6-35b-a3b"
    assert built[0]["base_url"] == providers.LMSTUDIO_BASE_URL


def test_image_description_never_calls_text_only_client():
    client = SimpleNamespace(complete=lambda *a, **k: pytest.fail("Image bytes would be discarded"))
    with pytest.raises(AgentUnavailableError, match="不能读取图片"):
        intake._vision_describe(client, "data:image/png;base64,aW1hZ2U=", "image/png")


@pytest.mark.parametrize("available", [False, True])
def test_image_tool_requires_an_image_transport(config, monkeypatch, available):
    config.data_dir.mkdir()
    path = config.data_dir / "fixture.png"
    path.write_bytes(b"image fixture")
    sent = []
    client = SimpleNamespace(name="lmstudio-vision", complete=lambda *a, **k: pytest.fail("Text-only fallback forbidden"))
    if available:
        client.complete_raw = lambda messages, **kw: sent.append(messages) or ChatCompletion(content="图片中的两项费用")
    monkeypatch.setattr(image_tools, "create_vision_client", lambda cfg: client)
    result = json.loads(image_tools.tool_image_analyze(config, path=str(path)))
    if available:
        assert result["description"] == "图片中的两项费用"
        content = sent[0][0]["content"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].endswith(base64.b64encode(path.read_bytes()).decode())
    else:
        assert "error" in result and "description" not in result
        assert not sent


@pytest.mark.parametrize("client", [None, SimpleNamespace(complete=lambda *a, **k: pytest.fail("No text stand-in for vision"))])
def test_chart_without_image_transport_returns_unavailable(config, monkeypatch, client):
    monkeypatch.setattr(chart_pattern, "create_vision_client", lambda cfg: client)
    stack = SimpleNamespace(config=config, provider=SimpleNamespace(get_bars=lambda *a, **k: pytest.fail("Unusable vision should stop before fetching")))
    result = chart_pattern.analyze_chart_pattern(stack, "600519")
    assert "视觉分析不可用" in result


@pytest.mark.parametrize("vision_fails", [False, True])
def test_chart_sends_image_or_labels_numeric_fallback(config, monkeypatch, vision_fails):
    bars = [SimpleNamespace(open=10 + i / 10, high=11 + i / 10, low=9 + i / 10,
                            close=10.5 + i / 10, volume=1000 + i * 10) for i in range(80)]
    stack = SimpleNamespace(config=config, provider=SimpleNamespace(get_bars=lambda *a, **k: bars))
    sent = []

    def raw(messages, **kw):
        sent.append(messages)
        if vision_fails:
            raise RuntimeError("vision endpoint unavailable")
        return ChatCompletion(content="图像回执")

    client = SimpleNamespace(complete_raw=raw, complete=lambda *a, **k: ChatCompletion(content="日线数据回执"))
    monkeypatch.setattr(chart_pattern, "create_vision_client", lambda cfg: client)
    monkeypatch.setattr(chart_pattern, "render_kline_chart", lambda *a, **k: "aW1hZ2U=")
    result = chart_pattern.analyze_chart_pattern(stack, "600519")
    assert sent[0][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
    if vision_fails:
        assert "图像分析未完成" in result and "仅依据日线数据" in result
        assert "日线数据回执" in result
    else:
        assert "图像回执" in result and "图像分析未完成" not in result
