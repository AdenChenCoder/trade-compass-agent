"""Signal tracking — connect trading signals to real outcomes.

Tracks each emitted signal through its lifecycle:
- Signal emitted → waiting for entry
- Position opened → tracking P&L
- Position closed → outcome recorded
- Outcome analyzed → lesson extracted

Tracks signal outcomes so later evaluation can use resolved evidence.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TrackedSignal:
    signal_id: str
    symbol: str
    rating: str
    confidence: float
    entry_price: float | None
    stop_loss: float | None
    target_price: float | None
    emitted_at: str
    status: str = "pending"  # pending, active, closed, expired
    actual_entry: float | None = None
    actual_exit: float | None = None
    actual_pnl: float | None = None
    outcome: str = ""  # win, loss, breakeven, expired
    days_held: int = 0
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    lessons: list[str] = field(default_factory=list)
    source_skills: list[str] = field(default_factory=list)
    closed_at: str | None = None


class SignalTracker:
    """Track signals from emission through outcome."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "signal_tracking.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from trade_compass_agent.concurrency import get_path_lock
        self._lock = get_path_lock(self.path)

    def track_signal(self, signal_data: dict[str, Any]) -> TrackedSignal:
        """Start tracking a new signal (deduplicated by signal_id)."""
        signal_id = signal_data.get("signal_id", "")
        tracked = TrackedSignal(
            signal_id=signal_id,
            symbol=signal_data.get("symbol", ""),
            rating=signal_data.get("rating", ""),
            confidence=signal_data.get("confidence", 0.5),
            entry_price=signal_data.get("entry_price"),
            stop_loss=signal_data.get("stop_loss"),
            target_price=signal_data.get("target_price"),
            emitted_at=signal_data.get("timestamp", datetime.now().isoformat()),
            source_skills=list(signal_data.get("source_skills") or []),
        )
        with self._lock:
            existing = self._load_all()
            if any(r.signal_id == signal_id for r in existing):
                return tracked
            self._append(tracked)
        return tracked

    def update_entry(self, signal_id: str, actual_entry: float) -> None:
        """Record that a position was opened."""
        with self._lock:
            records = self._load_all()
            for r in records:
                if r.signal_id == signal_id:
                    r.actual_entry = actual_entry
                    r.status = "active"
                    break
            self._save_all(records)

    def update_exit(
        self, signal_id: str, actual_exit: float, days_held: int = 0
    ) -> TrackedSignal | None:
        """Record position closed and compute outcome."""
        with self._lock:
            records = self._load_all()
            target: TrackedSignal | None = None
            for r in records:
                if r.signal_id == signal_id:
                    r.actual_exit = actual_exit
                    r.days_held = days_held
                    r.status = "closed"
                    r.closed_at = datetime.now().isoformat()
                    if r.actual_entry and r.actual_entry > 0:
                        r.actual_pnl = (actual_exit - r.actual_entry) / r.actual_entry * 100
                        if r.actual_pnl > 1.0:
                            r.outcome = "win"
                        elif r.actual_pnl < -1.0:
                            r.outcome = "loss"
                        else:
                            r.outcome = "breakeven"
                    target = r
                    break
            self._save_all(records)
            return target

    def get_active(self) -> list[TrackedSignal]:
        """Get all signals with open positions."""
        return [r for r in self._load_all() if r.status == "active"]

    def get_closed(self, limit: int = 50) -> list[TrackedSignal]:
        """Get recently closed signals."""
        closed = [r for r in self._load_all() if r.status == "closed"]
        return closed[-limit:]

    def get_stats(self) -> dict[str, Any]:
        """Compute aggregate tracking statistics."""
        closed = [r for r in self._load_all() if r.status == "closed"]
        if not closed:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0}

        wins = [r for r in closed if r.outcome == "win"]
        losses = [r for r in closed if r.outcome == "loss"]
        avg_win = sum(r.actual_pnl or 0 for r in wins) / len(wins) if wins else 0
        avg_loss = sum(abs(r.actual_pnl or 0) for r in losses) / len(losses) if losses else 0

        return {
            "total": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed), 3) if closed else 0.0,
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "payoff_ratio": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0,
        }

    def _append(self, record: TrackedSignal) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self._to_dict(record), ensure_ascii=False) + "\n")

    def _load_all(self) -> list[TrackedSignal]:
        if not self.path.exists():
            return []
        records: list[TrackedSignal] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                records.append(self._from_dict(data))
            except (json.JSONDecodeError, KeyError):
                continue
        return records

    def _save_all(self, records: list[TrackedSignal]) -> None:
        from trade_compass_agent.concurrency import atomic_write
        content = "\n".join(
            json.dumps(self._to_dict(r), ensure_ascii=False) for r in records
        ) + "\n"
        atomic_write(self.path, content)

    @staticmethod
    def _to_dict(r: TrackedSignal) -> dict[str, Any]:
        return {
            "signal_id": r.signal_id,
            "symbol": r.symbol,
            "rating": r.rating,
            "confidence": r.confidence,
            "entry_price": r.entry_price,
            "stop_loss": r.stop_loss,
            "target_price": r.target_price,
            "emitted_at": r.emitted_at,
            "status": r.status,
            "actual_entry": r.actual_entry,
            "actual_exit": r.actual_exit,
            "actual_pnl": r.actual_pnl,
            "outcome": r.outcome,
            "days_held": r.days_held,
            "max_favorable": r.max_favorable,
            "max_adverse": r.max_adverse,
            "lessons": r.lessons,
            "source_skills": r.source_skills,
            "closed_at": r.closed_at,
        }

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> TrackedSignal:
        return TrackedSignal(
            signal_id=data.get("signal_id", ""),
            symbol=data.get("symbol", ""),
            rating=data.get("rating", ""),
            confidence=data.get("confidence", 0.5),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            target_price=data.get("target_price"),
            emitted_at=data.get("emitted_at", ""),
            status=data.get("status", "pending"),
            actual_entry=data.get("actual_entry"),
            actual_exit=data.get("actual_exit"),
            actual_pnl=data.get("actual_pnl"),
            outcome=data.get("outcome", ""),
            days_held=data.get("days_held", 0),
            max_favorable=data.get("max_favorable", 0.0),
            max_adverse=data.get("max_adverse", 0.0),
            lessons=data.get("lessons", []),
            source_skills=data.get("source_skills", []),
            closed_at=data.get("closed_at"),
        )
