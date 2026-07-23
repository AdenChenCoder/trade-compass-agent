"""Tests for scheduler session cleanup."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
from trade_compass_agent.ops.session_cleanup import (
    parse_scheduler_session_date,
    sweep_scheduler_sessions,
)


def test_parse_scheduler_session_date() -> None:
    assert parse_scheduler_session_date("scheduler-premarket-2026-06-15") == date(2026, 6, 15)
    assert parse_scheduler_session_date("scheduler-prompt-abc-2026-01-02") == date(2026, 1, 2)
    assert parse_scheduler_session_date("user-session-abc") is None


def test_sweep_scheduler_sessions_removes_old_jsonl(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    sessions_dir.mkdir()
    old_id = "scheduler-eod_review-2026-06-01"
    keep_id = "scheduler-eod_review-2026-06-14"
    (sessions_dir / f"{old_id}.jsonl").write_text('{"type":"meta","created_at":"2026-06-01"}\n')
    (sessions_dir / f"{keep_id}.jsonl").write_text('{"type":"meta","created_at":"2026-06-14"}\n')

    removed = sweep_scheduler_sessions(
        tmp_path,
        retention_days=7,
        now=date(2026, 6, 15),
        force=True,
    )
    assert removed == 1
    assert not (sessions_dir / f"{old_id}.jsonl").exists()
    assert (sessions_dir / f"{keep_id}.jsonl").exists()


def test_sweep_scheduler_sessions_removes_summary_row(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    sessions_dir.mkdir()
    old_id = "scheduler-postmarket-2026-06-01"
    path = sessions_dir / f"{old_id}.jsonl"
    path.write_text('{"type":"meta","created_at":"2026-06-01"}\n')

    summary_store = SessionSummaryStore(tmp_path / "sessions.db")
    summary_store.upsert(old_id, "old scheduler run", ended_at="2026-06-01T10:00:00+00:00")

    removed = sweep_scheduler_sessions(
        tmp_path,
        retention_days=7,
        now=date(2026, 6, 15),
        force=True,
    )
    assert removed == 1
    assert summary_store.get(old_id) is None


def test_sweep_scheduler_sessions_throttles_without_force(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    sessions_dir.mkdir()
    old_id = "scheduler-close-2026-06-01"
    (sessions_dir / f"{old_id}.jsonl").write_text('{"type":"meta"}\n')

    first = sweep_scheduler_sessions(tmp_path, now=date(2026, 6, 15), force=True)
    assert first == 1

    (sessions_dir / f"{old_id}.jsonl").write_text('{"type":"meta"}\n')
    second = sweep_scheduler_sessions(tmp_path, now=date(2026, 6, 15), force=False)
    assert second == 0
    assert (sessions_dir / f"{old_id}.jsonl").exists()
