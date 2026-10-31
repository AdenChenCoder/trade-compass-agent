from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.exceptions import TurnInterruptedError


def _slow_cancellable_client() -> type:
    class SlowChatClient:
        name = "mock"
        model = "mock"
        _cancel_after_deltas = 3

        def complete(self, messages, *, tools=None):
            return self.stream_complete(messages, tools=tools)

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            text = "这是一段会被中断的流式回复内容。"
            emitted = ""
            if on_delta:
                for i in range(0, len(text), 2):
                    if is_cancelled and is_cancelled():
                        raise TurnInterruptedError(emitted)
                    piece = text[i : i + 2]
                    emitted += piece
                    on_delta(piece)
                    time.sleep(0.05)
            return ChatCompletion(content=text, model="mock", provider="mock")

    return SlowChatClient


def test_control_interrupt_stops_active_turn(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _slow_cancellable_client()(),
    )

    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    turn_response: dict = {}
    turn_error: list[BaseException] = []

    def run_turn() -> None:
        try:
            response = client.post(
                "/api/agent/turn",
                json={"message": "测试中断", "session_id": session_id},
            )
            turn_response["status"] = response.status_code
            turn_response["body"] = response.json()
        except BaseException as exc:
            turn_error.append(exc)

    worker = threading.Thread(target=run_turn, daemon=True)
    worker.start()
    time.sleep(0.15)

    control = client.post(
        "/api/agent/control",
        json={"session_id": session_id, "action": "interrupt"},
    )
    assert control.status_code == 200
    assert control.json()["ok"] is True

    worker.join(timeout=10)
    assert not turn_error
    assert turn_response.get("status") == 200
    body = turn_response["body"]
    assert body.get("interrupted") is True
    assert body.get("turn_id")
    assert "已停止" in body.get("summary", "")


def test_control_without_active_turn_returns_404(client: TestClient) -> None:
    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]
    response = client.post(
        "/api/agent/control",
        json={"session_id": session_id, "action": "interrupt"},
    )
    assert response.status_code == 404


def test_model_deadline_reaches_http_and_stream_terminal_state(client, monkeypatch):
    from types import SimpleNamespace
    from trade_compass_agent.llm.providers import LLMExecutionCancelled
    from trade_compass_agent.web import agent_api

    def expired(*a, **kw):
        raise LLMExecutionCancelled("Workflow model deadline reached")

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client",
                        lambda _: SimpleNamespace(stream_complete=expired))
    events = []
    publish = agent_api._publish_stream

    def observe(session_id, event):
        events.append(event)
        publish(session_id, event)

    monkeypatch.setattr(agent_api, "_publish_stream", observe)
    response = client.post("/api/agent/turn", json={"session_id": "expired-http", "message": "核对。"})
    assert response.status_code == 504
    assert "已有记录已保留" in response.json()["detail"]
    assert events[-1].event == "error" and events[-1].data["ok"] is False
    assert not any(event.event == "done" for event in events)
    assert client.post("/api/agent/control", json={"session_id": "expired-http", "action": "interrupt"}).status_code == 404
