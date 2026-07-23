from __future__ import annotations

import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.session_title import suggest_session_title


def test_suggest_session_title_falls_back_on_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.session_title.create_chat_client",
        lambda config: (_ for _ in ()).throw(RuntimeError("no llm")),
    )
    config = load_app_config()
    title = suggest_session_title("600519 短线怎么看", config)
    assert "600519" in title


def test_suggest_session_title_uses_llm_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TitleClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            return ChatCompletion(content="茅台短线", model="mock", provider="mock")

    monkeypatch.setattr(
        "trade_compass_agent.runtime.session_title.create_chat_client",
        lambda config: TitleClient(),
    )
    config = load_app_config()
    title = suggest_session_title("600519 短线怎么看", config)
    assert title == "茅台短线"
