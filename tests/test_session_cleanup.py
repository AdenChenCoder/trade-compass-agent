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


def test_old_trade_attempts_and_corrupt_sessions_keep_normal_access(tmp_path):
    import json
    from datetime import datetime
    from trade_compass_agent.runtime.session import SessionStore, SessionMessageRecord
    store = SessionStore(tmp_path/'agent_sessions')
    summaries = SessionSummaryStore(tmp_path/'sessions.db')
    for index, (tool, receipt) in enumerate([
        ('place_paper_trade', {'status': 'executed', 'trade_id': 'fill-1'}),
        ('place_paper_trade', {'trade_rejected': True, 'code': 'stale_quote'}),
        ('batch_paper_trades', {'executed': 1}),
    ]):
        session_id = f'scheduler-prompt-case-{index}-2026-09-01'
        session = store.get_or_create(session_id)
        store.append(session, SessionMessageRecord(role='assistant', content='依据市场行情决定下单',
            tool_calls=[{'id': str(index), 'function': {'name': tool, 'arguments': '{}'}}]))
        store.append(session, SessionMessageRecord(role='tool', name=tool, content=json.dumps(receipt)))
        summaries.upsert(session_id, '交易分析记录', ended_at='2026-09-01T10:00:00+08:00')
    corrupt = tmp_path/'agent_sessions'/'scheduler-broken-2026-09-01.jsonl'
    corrupt.write_text('{"role":"assistant","tool_calls":[')
    assert sweep_scheduler_sessions(tmp_path, now=date(2026, 10, 2), force=True) == 0
    for index in range(3):
        session_id = f'scheduler-prompt-case-{index}-2026-09-01'
        assert store.load(session_id) is not None
        assert store.load_display_page(session_id).total_messages == 1  # Tool rows remain in the full transcript.
        assert summaries.get(session_id) is not None
    assert corrupt.exists()
    # New hold/no-trade turns also survive without a tool call when autonomous mode was on.
    held = store.get_or_create('scheduler-prompt-hold-2026-09-01')
    store.append(held, SessionMessageRecord(role='user', content='分析持仓', timestamp=datetime(2026, 9, 1),
                                           autonomous_trading_enabled=True))
    store.append(held, SessionMessageRecord(role='assistant', content='继续持有，无需买卖'))
    reloaded = store.load(held.session_id)
    assert reloaded.messages[0].autonomous_trading_enabled
    store.replace_context(reloaded, reloaded.messages)
    assert store.load_context(reloaded)[0].autonomous_trading_enabled
    assert sweep_scheduler_sessions(tmp_path, now=date(2026, 10, 2), force=True) == 0
    assert store.load_display_page(held.session_id).messages[0].autonomous_trading_enabled
