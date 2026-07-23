from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.run_trace import TurnTraceWriter, write_run_card
from trade_compass_agent.runtime.types import TurnEvent, TurnResponse


def test_turn_trace_writer_appends_jsonl(tmp_path) -> None:
    writer = TurnTraceWriter(tmp_path / "run-1")
    writer.record(TurnEvent(event="status", data={"text": "思考中"}, id="evt-1"))
    writer.record(TurnEvent(event="done", data={"ok": True}, id="evt-2"))

    lines = (tmp_path / "run-1" / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "status"


def test_write_run_card_persists_summary(tmp_path) -> None:
    writer = TurnTraceWriter(tmp_path / "runs" / "turn-a")
    writer.record(TurnEvent(event="done", data={"ok": True}, id="evt-1"))
    result = TurnResponse(session_id="sess-1", summary="完成", sections=[], turn_id="turn-a")
    path = write_run_card(
        tmp_path / "runs",
        turn_id="turn-a",
        session_id="sess-1",
        started_at="2026-01-01T00:00:00+00:00",
        result=result,
        trace_writer=writer,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-1"
    assert payload["summary"] == "完成"
    assert payload["event_count"] == 1


def test_agent_turn_writes_run_card(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            if on_delta:
                on_delta("ok")
            return ChatCompletion(content="ok", model="mock", provider="mock")

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: MockChatClient(),
    )

    created = client.post("/api/agent/sessions")
    session_id = created.json()["session_id"]
    response = client.post(
        "/api/agent/turn",
        json={"message": "hello", "session_id": session_id},
    )
    assert response.status_code == 200
    turn_id = response.json()["turn_id"]

    from trade_compass_agent.config import load_app_config

    data_dir = load_app_config().data_dir
    assert (data_dir / "runs" / turn_id / "run_card.json").is_file()
    assert (data_dir / "runs" / turn_id / "trace.jsonl").is_file()
