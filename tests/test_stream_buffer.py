from __future__ import annotations

from trade_compass_agent.runtime.stream_buffer import SessionStreamBuffer
from trade_compass_agent.runtime.types import TurnEvent


def test_stream_buffer_replay_after_last_event_id() -> None:
    buffer = SessionStreamBuffer(capacity=10)
    first = buffer.append(TurnEvent(event="status", data={"text": "a"}, id="evt-1"))
    second = buffer.append(TurnEvent(event="delta", data={"text": "b"}, id="evt-2"))
    third = buffer.append(TurnEvent(event="done", data={"ok": True}, id="evt-3"))

    assert buffer.replay_after(None) == [first, second, third]
    assert buffer.replay_after("evt-1") == [second, third]
    assert buffer.replay_after("evt-2") == [third]
    assert buffer.replay_after("evt-3") == []
    assert buffer.replay_after("missing") == [first, second, third]


def test_stream_buffer_assigns_ids_for_events_without_id() -> None:
    buffer = SessionStreamBuffer()
    evt = buffer.append(TurnEvent(event="tool_start", data={"tool": "get_bars"}))
    assert evt.id is not None
    assert evt.id.startswith("buf-")
