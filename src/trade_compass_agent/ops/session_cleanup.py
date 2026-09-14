"""Prune persisted scheduler agent sessions (scheduler-{job_id}-{date}).

Per-day session keys are intentional: same job reuses one transcript within a
calendar day. This module removes jsonl transcripts older than the retention
window so agent_sessions/ does not grow without bound.
"""

from __future__ import annotations

import logging
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
from trade_compass_agent.runtime.session import SessionStore

logger = logging.getLogger(__name__)

SCHEDULER_SESSION_PREFIX = "scheduler-"
# Date suffix on scheduler-{job_id}-{YYYY-MM-DD}
_SCHEDULER_SESSION_DATE_RE = re.compile(
    rf"^{SCHEDULER_SESSION_PREFIX}.+-(\d{{4}}-\d{{2}}-\d{{2}})$"
)

DEFAULT_RETENTION_DAYS = 7
MIN_SWEEP_INTERVAL_SECONDS = 300  # 5-minute throttle between cleanup sweeps

_last_sweep_monotonic: float = 0.0


def is_scheduler_session(session_id: str) -> bool:
    """True when session_id belongs to a scheduled job transcript."""
    return session_id.startswith(SCHEDULER_SESSION_PREFIX)


def parse_scheduler_session_date(session_id: str) -> date | None:
    """Return calendar date embedded in a scheduler session id, if present."""
    m = _SCHEDULER_SESSION_DATE_RE.match(session_id)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _has_trading_activity(path: Path) -> bool:
    """Keep attempts as well as fills, including legacy/custom task sessions."""
    trade_tools = {"place_paper_trade", "batch_paper_trades"}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return True  # Unreadable history cannot establish safe deletion.
                if row.get("autonomous_trading_enabled") is True:
                    return True  # Holding/no-trade decisions are evidence too.
                if row.get("role") == "tool" and row.get("name") in trade_tools:
                    return True
                for call in row.get("tool_calls") or []:
                    if call.get("function", call).get("name") in trade_tools:
                        return True
    except (OSError, ValueError, AttributeError, TypeError):
        logger.warning("Keeping unreadable scheduler session %s for recovery", path.name)
        return True
    return False


def sweep_scheduler_sessions(
    data_dir: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: date | None = None,
    force: bool = False,
) -> int:
    """Delete scheduler session transcripts older than retention_days.

    Keeps sessions whose embedded date is on or after (today - retention_days).
    Also removes matching rows from session_summaries when sessions.db exists.

    Returns the number of jsonl files removed.
    """
    global _last_sweep_monotonic

    if retention_days < 1:
        return 0

    if not force:
        elapsed = time.monotonic() - _last_sweep_monotonic
        if elapsed < MIN_SWEEP_INTERVAL_SECONDS:
            return 0

    today = now or date.today()
    cutoff = today - timedelta(days=retention_days)

    sessions_dir = data_dir / "agent_sessions"
    if not sessions_dir.is_dir():
        _last_sweep_monotonic = time.monotonic()
        return 0

    summary_store: SessionSummaryStore | None = None
    sessions_db = data_dir / "sessions.db"
    if sessions_db.exists():
        summary_store = SessionSummaryStore(sessions_db)

    store = SessionStore(sessions_dir)
    removed = 0

    for path in sessions_dir.glob("scheduler-*.jsonl"):
        session_id = path.stem
        from trade_compass_agent.ops.autonomous_trading import SESSION_PREFIX
        if session_id.startswith(SESSION_PREFIX):
            # Trading decisions and tool receipts remain accessible for month-long reviews.
            continue
        session_date = parse_scheduler_session_date(session_id)
        if session_date is None or session_date >= cutoff:
            continue
        if _has_trading_activity(path):
            continue
        if store.delete(session_id):
            removed += 1
        elif path.exists():
            path.unlink()
            removed += 1
        if summary_store is not None:
            summary_store.delete(session_id)

    if removed:
        logger.info(
            "Scheduler session cleanup: removed %d file(s) older than %s",
            removed,
            cutoff.isoformat(),
        )

    _last_sweep_monotonic = time.monotonic()
    return removed
