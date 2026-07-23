"""Working-tier observation store — SQLite-backed auto-capture buffer.

Observations are raw tool results and significant events captured automatically
during agent execution. They are NOT injected into the system prompt. Instead,
they feed the consolidation pipeline which compresses them into Episodic and
Semantic tiers.

Stores captured observations for consolidation and retrieval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    raw_preview TEXT,
    importance INTEGER DEFAULT 5,
    concepts TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    dedup_hash TEXT NOT NULL,
    consolidated INTEGER DEFAULT 0
);
"""

_SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at);
CREATE INDEX IF NOT EXISTS idx_obs_dedup ON observations(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_obs_consolidated ON observations(consolidated);
CREATE INDEX IF NOT EXISTS idx_obs_importance ON observations(importance);
"""

_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
    summary, concepts,
    content=observations,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS obs_fts_insert AFTER INSERT ON observations BEGIN
    INSERT INTO obs_fts(rowid, summary, concepts)
    VALUES (new.rowid, new.summary, new.concepts);
END;
"""

TTL_DAYS = 90
DEDUP_WINDOW_SECONDS = 300  # 5 minutes
MAX_SUMMARY_CHARS = 500
MAX_RAW_PREVIEW_CHARS = 2000
MAX_OBSERVATIONS = 10000  # cap per project


@dataclass
class Observation:
    id: str
    session_id: str
    tool_name: str
    summary: str
    raw_preview: str
    importance: int
    concepts: list[str]
    created_at: str
    dedup_hash: str
    consolidated: bool = False
    recall_count: int = 0
    recall_days: list[str] | None = None
    last_recalled_at: str | None = None
    unique_sessions_recalled: list[str] | None = None
    promoted_at: str | None = None
    daily_count: int = 0
    grounded_count: int = 0

    @property
    def total_signal(self) -> int:
        """Combined signal strength across independent evidence sources."""
        return self.recall_count + self.daily_count + self.grounded_count


class ObservationStore:
    """Thread-safe SQLite observation buffer (Working tier)."""

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
        conn.executescript(_SCHEMA_TABLE)
        conn.commit()
        # Migrate old DBs that lack importance/concepts columns
        self._migrate_schema()
        # Now safe to create indexes and FTS on the full schema
        conn.executescript(_SCHEMA_INDEXES)
        try:
            conn.executescript(_SCHEMA_FTS)
        except sqlite3.OperationalError:
            pass  # FTS5 may already exist or be unsupported
        conn.commit()

    def append(
        self,
        session_id: str,
        tool_name: str,
        summary: str,
        raw_preview: str = "",
        importance: int = 5,
        concepts: list[str] | None = None,
    ) -> bool:
        """Append an observation. Returns False if dedup blocked it."""
        summary = summary[:MAX_SUMMARY_CHARS]
        raw_preview = raw_preview[:MAX_RAW_PREVIEW_CHARS]
        importance = max(1, min(10, importance))
        h = _obs_hash(tool_name, summary)
        now = datetime.now(timezone.utc)

        conn = self._get_conn()
        # Dedup check within window
        cutoff = (now - timedelta(seconds=DEDUP_WINDOW_SECONDS)).isoformat()
        row = conn.execute(
            "SELECT 1 FROM observations WHERE dedup_hash = ? AND created_at > ? LIMIT 1",
            (h, cutoff),
        ).fetchone()
        if row:
            return False

        obs_id = f"{now.strftime('%Y%m%d%H%M%S')}_{h[:8]}"
        concepts_json = json.dumps(concepts or [], ensure_ascii=False)
        conn.execute(
            "INSERT OR IGNORE INTO observations (id, session_id, tool_name, summary, raw_preview, importance, concepts, created_at, dedup_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (obs_id, session_id, tool_name, summary, raw_preview, importance, concepts_json, now.isoformat(), h),
        )
        conn.commit()

        # Capacity eviction: drop lowest importance when over cap
        if self.count() > MAX_OBSERVATIONS:
            self._evict_lowest(batch=100)

        return True

    def recent(self, limit: int = 20, session_id: str | None = None) -> list[Observation]:
        """Get recent observations, optionally filtered by session."""
        conn = self._get_conn()
        if session_id:
            rows = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM observations WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {self._SELECT_COLS} FROM observations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_obs(r) for r in rows]

    def unconsolidated(self, limit: int = 50) -> list[Observation]:
        """Get observations not yet consumed by consolidation."""
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT {self._SELECT_COLS} FROM observations WHERE consolidated = 0 ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_obs(r) for r in rows]

    def mark_consolidated(self, ids: list[str]) -> None:
        """Mark observations as consumed by consolidation."""
        if not ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE observations SET consolidated = 1 WHERE id = ?",
            [(i,) for i in ids],
        )
        conn.commit()

    def high_importance(self, min_importance: int = 7, limit: int = 20) -> list[Observation]:
        """Get high-importance observations for potential promotion."""
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT {self._SELECT_COLS} FROM observations WHERE importance >= ? ORDER BY importance DESC, created_at DESC LIMIT ?",
            (min_importance, limit),
        ).fetchall()
        return [self._row_to_obs(r) for r in rows]

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        session_id: str | None = None,
        track_recall: bool = True,
    ) -> list[Observation]:
        """Full-text search on summary + concepts via FTS5.

        When *track_recall* is True (default), automatically records recall
        for all returned observations so retrieval reinforces useful memories.
        """
        from trade_compass_agent.memory.tree.search import _sanitize_fts5_query

        conn = self._get_conn()
        fts_q = _sanitize_fts5_query(query)
        rows: list[tuple] = []
        if fts_q:
            try:
                rows = conn.execute(
                    f"SELECT o.{', o.'.join(self._SELECT_COLS.split(', '))} "
                    "FROM observations o JOIN obs_fts f ON o.rowid = f.rowid "
                    "WHERE obs_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            tokens = [t for t in query.split() if len(t) >= 2]
            if tokens:
                where = " OR ".join("summary LIKE ?" for _ in tokens)
                params = [f"%{t}%" for t in tokens] + [limit]
                rows = conn.execute(
                    f"SELECT {self._SELECT_COLS} FROM observations "
                    f"WHERE {where} ORDER BY created_at DESC LIMIT ?",
                    params,
                ).fetchall()
        results = [self._row_to_obs(r) for r in rows]
        if track_recall and results:
            try:
                self.record_recall([r.id for r in results], session_id=session_id)
            except Exception:
                logger.debug("Inline recall tracking failed", exc_info=True)
        return results

    # -------------------------------------------------------------------
    # Recall tracking (Dreaming Phase 0)
    # -------------------------------------------------------------------

    def record_recall(self, obs_ids: list[str], session_id: str | None = None) -> None:
        """Record that these observations were retrieved by a search query."""
        if not obs_ids:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        for obs_id in obs_ids:
            row = conn.execute(
                "SELECT recall_days, unique_sessions_recalled FROM observations WHERE id = ?",
                (obs_id,),
            ).fetchone()
            if not row:
                continue
            days = _safe_json_list(row[0])
            sessions = _safe_json_list(row[1])
            if today not in days:
                days.append(today)
            if session_id and session_id not in sessions:
                sessions.append(session_id)
            conn.execute(
                "UPDATE observations SET recall_count = recall_count + 1, "
                "recall_days = ?, last_recalled_at = ?, unique_sessions_recalled = ? WHERE id = ?",
                (json.dumps(days), now_iso, json.dumps(sessions), obs_id),
            )
        conn.commit()

    def mark_promoted(self, obs_ids: list[str]) -> None:
        """Mark observations as promoted to Semantic tier."""
        if not obs_ids:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.executemany(
            "UPDATE observations SET promoted_at = ? WHERE id = ?",
            [(now_iso, oid) for oid in obs_ids],
        )
        conn.commit()

    def promotion_candidates(
        self, min_signal: int = 3, limit: int = 50, *, require_consolidated: bool = True,
    ) -> list[Observation]:
        """Get observations eligible for promotion scoring.

        Uses total_signal (recall + daily + grounded) instead of recall_count
        alone; multiple independent signals are required.
        Set *require_consolidated* to False for cold-start bootstrap.
        """
        conn = self._get_conn()
        where = "WHERE promoted_at IS NULL "
        if require_consolidated:
            where += "AND consolidated = 1 "
        where += "AND (recall_count + daily_count + grounded_count) >= ? "
        rows = conn.execute(
            f"SELECT {self._SELECT_COLS} FROM observations "
            f"{where}"
            "ORDER BY (recall_count + daily_count + grounded_count) DESC, importance DESC LIMIT ?",
            (min_signal, limit),
        ).fetchall()
        return [self._row_to_obs(r) for r in rows]

    def concept_frequency(self, lookback_days: int = 7) -> dict[str, int]:
        """Count concept occurrences within lookback window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT concepts FROM observations WHERE created_at >= ?", (cutoff,)
        ).fetchall()
        freq: dict[str, int] = {}
        for (concepts_json,) in rows:
            for c in _safe_json_list(concepts_json):
                freq[c] = freq.get(c, 0) + 1
        return freq

    def recent_by_days(self, lookback_days: int = 7) -> list[Observation]:
        """Get all observations within lookback window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT {self._SELECT_COLS} FROM observations WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        return [self._row_to_obs(r) for r in rows]

    def cleanup_old(self) -> int:
        """Remove observations older than TTL. Returns count deleted."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)).isoformat()
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM observations WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount

    def _evict_lowest(self, batch: int = 100) -> int:
        """Evict lowest-importance observations to stay under cap."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM observations WHERE id IN ("
            "  SELECT id FROM observations ORDER BY importance ASC, created_at ASC LIMIT ?"
            ")",
            (batch,),
        )
        conn.commit()
        evicted = cursor.rowcount
        if evicted:
            logger.info("Evicted %d low-importance observations (cap=%d)", evicted, MAX_OBSERVATIONS)
        return evicted

    def count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM observations").fetchone()
        return row[0] if row else 0

    _SELECT_COLS = (
        "id, session_id, tool_name, summary, raw_preview, importance, concepts, "
        "created_at, dedup_hash, consolidated, "
        "recall_count, recall_days, last_recalled_at, unique_sessions_recalled, promoted_at, "
        "daily_count, grounded_count"
    )

    def _row_to_obs(self, row: tuple) -> Observation:
        """Convert a DB row to Observation, handling JSON fields."""
        (id_, session_id, tool_name, summary, raw_preview, importance,
         concepts_json, created_at, dedup_hash, consolidated,
         *extra) = row
        concepts = _safe_json_list(concepts_json)
        recall_count = extra[0] if len(extra) > 0 else 0
        recall_days = _safe_json_list(extra[1]) if len(extra) > 1 else []
        last_recalled_at = extra[2] if len(extra) > 2 else None
        unique_sessions = _safe_json_list(extra[3]) if len(extra) > 3 else []
        promoted_at = extra[4] if len(extra) > 4 else None
        daily_count = extra[5] if len(extra) > 5 else 0
        grounded_count = extra[6] if len(extra) > 6 else 0
        return Observation(
            id=id_, session_id=session_id, tool_name=tool_name,
            summary=summary, raw_preview=raw_preview,
            importance=importance or 5, concepts=concepts,
            created_at=created_at, dedup_hash=dedup_hash,
            consolidated=bool(consolidated),
            recall_count=recall_count or 0,
            recall_days=recall_days,
            last_recalled_at=last_recalled_at,
            unique_sessions_recalled=unique_sessions,
            promoted_at=promoted_at,
            daily_count=daily_count or 0,
            grounded_count=grounded_count or 0,
        )

    def bump_daily(self, obs_ids: list[str]) -> None:
        """Bump daily_count for observations referenced during dreaming ingestion."""
        if not obs_ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE observations SET daily_count = daily_count + 1 WHERE id = ?",
            [(oid,) for oid in obs_ids],
        )
        conn.commit()

    def bump_grounded(self, obs_ids: list[str]) -> None:
        """Bump grounded_count for observations confirmed by insights/patterns."""
        if not obs_ids:
            return
        conn = self._get_conn()
        conn.executemany(
            "UPDATE observations SET grounded_count = grounded_count + 1 WHERE id = ?",
            [(oid,) for oid in obs_ids],
        )
        conn.commit()

    def _migrate_schema(self) -> None:
        """Add missing columns (backward compat across versions)."""
        conn = self._get_conn()
        try:
            conn.execute("SELECT importance FROM observations LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE observations ADD COLUMN importance INTEGER DEFAULT 5")
            conn.execute("ALTER TABLE observations ADD COLUMN concepts TEXT DEFAULT '[]'")
            conn.commit()
        # v2: recall tracking + promotion columns (Dreaming Phase 0)
        try:
            conn.execute("SELECT recall_count FROM observations LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE observations ADD COLUMN recall_count INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE observations ADD COLUMN recall_days TEXT DEFAULT '[]'")
            conn.execute("ALTER TABLE observations ADD COLUMN last_recalled_at TEXT")
            conn.execute("ALTER TABLE observations ADD COLUMN unique_sessions_recalled TEXT DEFAULT '[]'")
            conn.execute("ALTER TABLE observations ADD COLUMN promoted_at TEXT")
            conn.commit()
        # v3: multi-source signal columns
        try:
            conn.execute("SELECT daily_count FROM observations LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE observations ADD COLUMN daily_count INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE observations ADD COLUMN grounded_count INTEGER DEFAULT 0")
            conn.commit()


def _safe_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _obs_hash(tool_name: str, summary: str) -> str:
    normalized = f"{tool_name}:{' '.join(summary.lower().split())}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Importance heuristic (rule-based, no LLM call)
# ---------------------------------------------------------------------------

_HIGH_IMPORTANCE_TOOLS = {"place_paper_trade", "batch_paper_trades", "search_decisions", "kline_forecast"}
_MEDIUM_IMPORTANCE_TOOLS = {"get_bars", "get_fundamentals", "get_market_pulse", "chart_pattern"}

_HIGH_KEYWORDS = ["涨停", "跌停", "突破", "止损", "止盈", "暴跌", "暴涨", "异动", "龙头"]
_MEDIUM_KEYWORDS = ["放量", "缩量", "金叉", "死叉", "支撑", "压力", "主力"]


def estimate_importance(tool_name: str, summary: str) -> int:
    """Heuristic importance score (1-10) without LLM call."""
    score = 5

    if tool_name in _HIGH_IMPORTANCE_TOOLS:
        score += 3
    elif tool_name in _MEDIUM_IMPORTANCE_TOOLS:
        score += 1

    summary_lower = summary.lower()
    for kw in _HIGH_KEYWORDS:
        if kw in summary_lower:
            score += 2
            break
    for kw in _MEDIUM_KEYWORDS:
        if kw in summary_lower:
            score += 1
            break

    return max(1, min(10, score))


def extract_concepts(summary: str) -> list[str]:
    """Extract stock codes and key concepts from summary text."""
    import re
    concepts = []
    codes = re.findall(r"\b[036]\d{5}\b", summary)
    concepts.extend(codes[:5])
    for kw in _HIGH_KEYWORDS + _MEDIUM_KEYWORDS:
        if kw in summary:
            concepts.append(kw)
    return concepts[:10]
