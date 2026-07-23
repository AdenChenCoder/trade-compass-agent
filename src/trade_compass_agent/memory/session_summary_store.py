"""Episodic tier — session summaries with FTS5 full-text search.

Stores compressed session summaries (Tier 1). Each session gets a 1-2 paragraph
LLM-generated summary capturing what was discussed, what tools were used, and
which symbols were analyzed.

Provides the `session_search` tool for cross-session recall:
"What did we discuss about 半导体 last week?"
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id  TEXT PRIMARY KEY,
    title       TEXT,
    summary     TEXT NOT NULL,
    turn_count  INTEGER DEFAULT 0,
    tools_used  TEXT,
    symbols     TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
    summary, symbols, title,
    content=session_summaries,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS session_fts_insert AFTER INSERT ON session_summaries BEGIN
    INSERT INTO session_fts(rowid, summary, symbols, title)
    VALUES (new.rowid, new.summary, new.symbols, new.title);
END;

CREATE TRIGGER IF NOT EXISTS session_fts_update AFTER UPDATE ON session_summaries BEGIN
    INSERT INTO session_fts(session_fts, rowid, summary, symbols, title)
    VALUES ('delete', old.rowid, old.summary, old.symbols, old.title);
    INSERT INTO session_fts(rowid, summary, symbols, title)
    VALUES (new.rowid, new.summary, new.symbols, new.title);
END;

CREATE TRIGGER IF NOT EXISTS session_fts_delete AFTER DELETE ON session_summaries BEGIN
    INSERT INTO session_fts(session_fts, rowid, summary, symbols, title)
    VALUES ('delete', old.rowid, old.summary, old.symbols, old.title);
END;
"""

TTL_DAYS = 90


@dataclass
class SessionSummaryRecord:
    session_id: str
    title: str | None
    summary: str
    turn_count: int
    tools_used: list[str]
    symbols: list[str]
    started_at: str
    ended_at: str | None
    is_trivial: bool = False


class SessionSummaryStore:
    """SQLite + FTS5 session summary store (Episodic tier)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def upsert(
        self,
        session_id: str,
        summary: str,
        *,
        title: str | None = None,
        turn_count: int = 0,
        tools_used: list[str] | None = None,
        symbols: list[str] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        """Insert or update a session summary."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO session_summaries
               (session_id, title, summary, turn_count, tools_used, symbols, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 title = excluded.title,
                 summary = excluded.summary,
                 turn_count = excluded.turn_count,
                 tools_used = excluded.tools_used,
                 symbols = excluded.symbols,
                 ended_at = excluded.ended_at
            """,
            (
                session_id,
                title,
                summary,
                turn_count,
                json.dumps(tools_used or [], ensure_ascii=False),
                json.dumps(symbols or [], ensure_ascii=False),
                started_at or now,
                ended_at or now,
            ),
        )
        conn.commit()

    def search(self, query: str, limit: int = 5) -> list[SessionSummaryRecord]:
        """Full-text search across session summaries.

        Uses FTS5 first, falls back to LIKE for CJK or special characters.
        """
        conn = self._get_conn()
        rows = []
        # Try FTS5 first
        try:
            rows = conn.execute(
                """SELECT s.session_id, s.title, s.summary, s.turn_count,
                          s.tools_used, s.symbols, s.started_at, s.ended_at
                   FROM session_summaries s
                   JOIN session_fts f ON s.rowid = f.rowid
                   WHERE session_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass

        # Fallback to LIKE if FTS5 returns nothing (common for CJK text)
        if not rows:
            rows = conn.execute(
                """SELECT session_id, title, summary, turn_count,
                          tools_used, symbols, started_at, ended_at
                   FROM session_summaries
                   WHERE summary LIKE ? OR symbols LIKE ? OR title LIKE ?
                   ORDER BY ended_at DESC
                   LIMIT ?""",
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()

        return [self._row_to_record(r) for r in rows]

    def recent(self, limit: int = 10) -> list[SessionSummaryRecord]:
        """Get most recent session summaries."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT session_id, title, summary, turn_count,
                      tools_used, symbols, started_at, ended_at
               FROM session_summaries
               ORDER BY ended_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get(self, session_id: str) -> SessionSummaryRecord | None:
        """Get summary for a specific session."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT session_id, title, summary, turn_count,
                      tools_used, symbols, started_at, ended_at
               FROM session_summaries WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()
        return row[0] if row else 0

    def delete(self, session_id: str) -> bool:
        """Remove a single session summary row."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM session_summaries WHERE session_id = ?", (session_id,)
        )
        conn.commit()
        return cursor.rowcount > 0

    def cleanup_old(self) -> int:
        """Remove summaries older than TTL."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM session_summaries WHERE ended_at < ?", (cutoff,)
        )
        conn.commit()
        return cursor.rowcount

    def _row_to_record(self, row: tuple) -> SessionSummaryRecord:
        return SessionSummaryRecord(
            session_id=row[0],
            title=row[1],
            summary=row[2],
            turn_count=row[3],
            tools_used=json.loads(row[4]) if row[4] else [],
            symbols=json.loads(row[5]) if row[5] else [],
            started_at=row[6],
            ended_at=row[7],
        )
