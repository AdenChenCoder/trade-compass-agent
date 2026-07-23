from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.llm.providers import ChatCompletion


def _mock_chat_client() -> type:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            return self.stream_complete(messages, tools=tools)

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            text = "测试回复内容。"
            if on_delta:
                for i in range(0, len(text), 2):
                    on_delta(text[i : i + 2])
            return ChatCompletion(
                content=text,
                model="mock",
                provider="mock",
            )

    return MockChatClient


def _collect_sse_events(client: TestClient, session_id: str, stop_after: float = 5.0) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    deadline = time.monotonic() + stop_after

    with client.stream("GET", f"/api/agent/stream?session_id={session_id}") as response:
        assert response.status_code == 200
        event_name = "message"
        data_lines: list[str] = []

        for raw in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if raw.startswith("event:"):
                event_name = raw.split(":", 1)[1].strip()
                continue
            if raw.startswith("data:"):
                data_lines.append(raw.split(":", 1)[1].strip())
                continue
            if raw == "" and data_lines:
                payload = json.loads("".join(data_lines))
                events.append((event_name, payload))
                data_lines = []
                if event_name == "done":
                    break

    return events


def test_successful_turn_emits_single_done_with_ok(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _mock_chat_client()(),
    )

    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    collected: list[tuple[str, dict]] = []
    stream_error: list[BaseException] = []

    def consume_stream() -> None:
        try:
            collected.extend(_collect_sse_events(client, session_id))
        except BaseException as exc:  # pragma: no cover - surfaced below
            stream_error.append(exc)

    thread = threading.Thread(target=consume_stream, daemon=True)
    thread.start()
    time.sleep(0.05)

    turn = client.post(
        "/api/agent/turn",
        json={"message": "今天大盘资金情况", "session_id": session_id},
    )
    thread.join(timeout=5)
    assert not stream_error

    assert turn.status_code == 200
    body = turn.json()
    assert body["summary"]

    done_events = [payload for name, payload in collected if name == "done"]
    assert len(done_events) == 1
    assert done_events[0]["ok"] is True
    assert done_events[0]["summary"] == body["summary"]
    assert done_events[0]["session_id"] == session_id


def test_successful_turn_emits_delta_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _mock_chat_client()(),
    )

    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]

    collected: list[tuple[str, dict]] = []

    def consume_stream() -> None:
        collected.extend(_collect_sse_events(client, session_id))

    thread = threading.Thread(target=consume_stream, daemon=True)
    thread.start()
    time.sleep(0.05)

    turn = client.post(
        "/api/agent/turn",
        json={"message": "今天大盘资金情况", "session_id": session_id},
    )
    thread.join(timeout=5)

    assert turn.status_code == 200
    delta_events = [payload for name, payload in collected if name == "delta"]
    assert delta_events
    streamed = "".join(str(item.get("text", "")) for item in delta_events)
    assert streamed == turn.json()["summary"]


def test_failed_turn_emits_sse_error_without_done(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.web import agent_api

    published: list[tuple[str, str, dict]] = []

    class FailingChatClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            from trade_compass_agent.runtime.exceptions import AgentTurnError

            raise AgentTurnError("LLM request failed: upstream timeout")

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            return self.complete(messages, tools=tools)

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: FailingChatClient(),
    )
    original_publish = agent_api._publish_stream

    def tracking_publish(session_id: str, evt) -> None:
        published.append((session_id, evt.event, evt.data))
        original_publish(session_id, evt)

    monkeypatch.setattr(agent_api, "_publish_stream", tracking_publish)

    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]

    turn = client.post(
        "/api/agent/turn",
        json={"message": "今天大盘资金情况", "session_id": session_id},
    )

    assert turn.status_code == 502
    error_events = [data for _, event, data in published if event == "error"]
    done_events = [data for _, event, data in published if event == "done"]
    assert len(error_events) == 1
    assert error_events[0]["ok"] is False
    assert "LLM request failed" in error_events[0]["message"]
    assert done_events == []
