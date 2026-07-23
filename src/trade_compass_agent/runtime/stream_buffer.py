from __future__ import annotations

import threading
from collections import deque

from trade_compass_agent.runtime.types import TurnEvent

DEFAULT_CAPACITY = 200


class SessionStreamBuffer:
    """Ring buffer of recent SSE events for Last-Event-ID replay."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, capacity)
        self._events: deque[TurnEvent] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, evt: TurnEvent) -> TurnEvent:
        with self._lock:
            if not evt.id:
                self._seq += 1
                evt = TurnEvent(event=evt.event, data=dict(evt.data), id=f"buf-{self._seq}")
            self._events.append(evt)
            return evt

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def replay_after(self, last_event_id: str | None) -> list[TurnEvent]:
        with self._lock:
            events = list(self._events)
        if not last_event_id:
            return list(events)
        for index, evt in enumerate(events):
            if evt.id == last_event_id:
                return events[index + 1 :]
        return events
