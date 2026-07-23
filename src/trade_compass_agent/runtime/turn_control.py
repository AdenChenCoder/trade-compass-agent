from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class ActiveTurn:
    turn_id: str
    session_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)


class TurnRegistry:
    """Tracks in-flight agent turns for cooperative interrupt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, ActiveTurn] = {}
        self._session_turn: dict[str, str] = {}

    def register(self, turn_id: str, session_id: str) -> Callable[[], bool]:
        turn = ActiveTurn(turn_id=turn_id, session_id=session_id)
        with self._lock:
            self._turns[turn_id] = turn
            self._session_turn[session_id] = turn_id

        def is_cancelled() -> bool:
            return self.is_cancelled(turn_id)

        return is_cancelled

    def has_active_turn(self, session_id: str) -> bool:
        with self._lock:
            turn_id = self._session_turn.get(session_id)
            return turn_id is not None and turn_id in self._turns

    def is_cancelled(self, turn_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(turn_id)
        return turn.cancel_event.is_set() if turn else False

    def interrupt(self, session_id: str, turn_id: str | None = None) -> bool:
        with self._lock:
            if turn_id:
                turn = self._turns.get(turn_id)
                if turn and turn.session_id == session_id:
                    turn.cancel_event.set()
                    return True
                return False
            active_id = self._session_turn.get(session_id)
            turn = self._turns.get(active_id) if active_id else None
            if turn and turn.session_id == session_id:
                turn.cancel_event.set()
                return True
            return False

    def unregister(self, turn_id: str) -> None:
        with self._lock:
            turn = self._turns.pop(turn_id, None)
            if turn and self._session_turn.get(turn.session_id) == turn_id:
                self._session_turn.pop(turn.session_id, None)


_registry = TurnRegistry()


def get_turn_registry() -> TurnRegistry:
    return _registry
