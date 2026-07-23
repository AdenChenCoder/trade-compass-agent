from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.web import agent_api


def _mock_chat_client() -> type:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            if on_delta:
                on_delta("测试回复")
            return ChatCompletion(content="测试回复", model="mock", provider="mock")

    return MockChatClient


def _parse_sse_body(body: str) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    event_id: str | None = None
    event_name = "message"

    for line in body.splitlines():
        if line.startswith("id:"):
            event_id = line.split(":", 1)[1].strip()
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:"):
            continue
        if line == "" and event_name:
            events.append((event_id, event_name))
            event_id = None
            event_name = "message"
    return events


def test_agent_stream_replays_after_last_event_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _mock_chat_client()(),
    )
    agent_api._stream_buffers.clear()

    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]

    turn = client.post(
        "/api/agent/turn",
        json={"message": "测试 SSE 重放", "session_id": session_id},
    )
    assert turn.status_code == 200

    buffered = agent_api._buffer_for_session(session_id).replay_after("missing-id")
    assert any(evt.event == "done" for evt in buffered)
    first_id = buffered[0].id
    assert first_id

    replay_response = client.get(
        f"/api/agent/stream?session_id={session_id}",
        headers={"Last-Event-ID": first_id},
    )
    assert replay_response.status_code == 200
    replay_events = _parse_sse_body(replay_response.text)
    replay_names = [name for _, name in replay_events]
    assert "done" in replay_names
    assert replay_names.index("done") >= 0
