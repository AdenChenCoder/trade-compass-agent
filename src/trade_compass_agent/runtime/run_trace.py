from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_compass_agent.runtime.types import TurnEvent, TurnResponse


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunCard:
    turn_id: str
    session_id: str
    started_at: str
    finished_at: str
    interrupted: bool
    summary: str
    section_count: int
    event_count: int
    events: list[dict[str, Any]] = field(default_factory=list)


class TurnTraceWriter:
    """Append-only JSONL trace for a single agent turn."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = run_dir / "trace.jsonl"
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, evt: TurnEvent) -> None:
        payload = {
            "id": evt.id,
            "event": evt.event,
            "data": evt.data,
            "recorded_at": _utc_now(),
        }
        with self._lock:
            self._events.append(payload)
            with self.trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @property
    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)


def write_run_card(
    runs_root: Path,
    *,
    turn_id: str,
    session_id: str,
    started_at: str,
    result: TurnResponse,
    trace_writer: TurnTraceWriter,
) -> Path:
    run_dir = runs_root / turn_id
    run_dir.mkdir(parents=True, exist_ok=True)
    card = RunCard(
        turn_id=turn_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=_utc_now(),
        interrupted=result.interrupted,
        summary=result.summary,
        section_count=len(result.sections),
        event_count=len(trace_writer.events),
        events=trace_writer.events,
    )
    path = run_dir / "run_card.json"
    path.write_text(
        json.dumps(asdict(card), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
