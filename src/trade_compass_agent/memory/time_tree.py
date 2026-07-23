"""Hierarchical time tree — Dreaming Phase 6.

Organizes memories by time: day -> week -> month -> year.
Each level is an LLM-compressed summary of its children.

Uses bounded time buckets and hierarchical summaries backed by SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

LLM_CALL = Callable[[str, str], str]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS time_nodes (
    id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_count INTEGER DEFAULT 0,
    key_concepts TEXT DEFAULT '[]',
    key_symbols TEXT DEFAULT '[]',
    sealed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS time_node_sources (
    node_id TEXT NOT NULL REFERENCES time_nodes(id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (node_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_tn_level ON time_nodes(level);
CREATE INDEX IF NOT EXISTS idx_tn_sealed ON time_nodes(sealed_at);
"""


@dataclass
class TimeNode:
    id: str          # "2025-06-15" | "2025-W24" | "2025-06" | "2025"
    level: str       # "day" | "week" | "month" | "year"
    summary: str
    source_count: int = 0
    key_concepts: list[str] = field(default_factory=list)
    key_symbols: list[str] = field(default_factory=list)
    sealed_at: str | None = None
    created_at: str = ""


class TimeTree:
    """SQLite-backed hierarchical time tree."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(str(self._db_path), timeout=10)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> TimeNode | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, level, summary, source_count, key_concepts, key_symbols, sealed_at, created_at "
            "FROM time_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def upsert_node(self, node: TimeNode) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO time_nodes (id, level, summary, source_count, key_concepts, key_symbols, sealed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (node.id, node.level, node.summary, node.source_count,
             json.dumps(node.key_concepts, ensure_ascii=False),
             json.dumps(node.key_symbols, ensure_ascii=False),
             node.sealed_at, node.created_at),
        )
        conn.commit()

    def add_sources(self, node_id: str, source_type: str, source_ids: list[str]) -> None:
        conn = self._get_conn()
        conn.executemany(
            "INSERT OR IGNORE INTO time_node_sources (node_id, source_type, source_id) VALUES (?, ?, ?)",
            [(node_id, source_type, sid) for sid in source_ids],
        )
        conn.commit()

    def children(self, parent_level: str, parent_id: str) -> list[TimeNode]:
        """Get child nodes linked via time_node_sources (source_type='child_node')."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT t.id, t.level, t.summary, t.source_count, t.key_concepts, t.key_symbols, t.sealed_at, t.created_at "
            "FROM time_nodes t JOIN time_node_sources s ON t.id = s.source_id "
            "WHERE s.node_id = ? AND s.source_type = 'child_node' ORDER BY t.id",
            (parent_id,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def nodes_by_level(self, level: str, *, sealed_only: bool = False) -> list[TimeNode]:
        conn = self._get_conn()
        q = "SELECT id, level, summary, source_count, key_concepts, key_symbols, sealed_at, created_at FROM time_nodes WHERE level = ?"
        params: list[Any] = [level]
        if sealed_only:
            q += " AND sealed_at IS NOT NULL"
        q += " ORDER BY id DESC"
        rows = conn.execute(q, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ------------------------------------------------------------------
    # Seal operations
    # ------------------------------------------------------------------

    def seal_day(
        self,
        date: str,
        obs_summaries: list[str],
        session_summaries: list[str],
        concepts: list[str],
        symbols: list[str],
        obs_ids: list[str],
        session_ids: list[str],
        llm_call: LLM_CALL | None = None,
    ) -> TimeNode:
        """Create/seal a day node from observations and session summaries."""
        all_texts = obs_summaries + session_summaries
        if not all_texts:
            summary = f"{date}: 无交易活动记录"
        elif llm_call:
            combined = "\n".join(f"- {t[:150]}" for t in all_texts[:30])
            prompt = f"将以下 {date} 的交易观察和会话摘要压缩为 200-400 字的日度总结：\n{combined}"
            try:
                summary = llm_call("你是交易记忆压缩引擎。用简洁中文总结。", prompt)
            except Exception as exc:
                logger.warning("LLM seal_day failed: %s; using concatenation", exc)
                summary = f"{date}: " + "; ".join(t[:80] for t in all_texts[:10])
        else:
            summary = f"{date}: " + "; ".join(t[:80] for t in all_texts[:10])

        from collections import Counter
        top_concepts = [c for c, _ in Counter(concepts).most_common(10)]
        top_symbols = [s for s, _ in Counter(symbols).most_common(10)]

        node = TimeNode(
            id=date,
            level="day",
            summary=summary[:2000],
            source_count=len(all_texts),
            key_concepts=top_concepts,
            key_symbols=top_symbols,
            sealed_at=datetime.now(timezone.utc).isoformat(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.upsert_node(node)
        self.add_sources(date, "observation", obs_ids)
        self.add_sources(date, "session", session_ids)
        logger.info("Sealed day node %s with %d sources", date, node.source_count)
        return node

    def seal_week(
        self,
        week_id: str,
        day_nodes: list[TimeNode],
        llm_call: LLM_CALL | None = None,
    ) -> TimeNode:
        """Create/seal a week node from day nodes."""
        if not day_nodes:
            summary = f"{week_id}: 无数据"
        elif llm_call:
            combined = "\n".join(f"### {n.id}\n{n.summary[:300]}" for n in day_nodes)
            prompt = f"将以下一周 ({week_id}) 的每日总结压缩为 300-500 字的周度总结：\n{combined}"
            try:
                summary = llm_call("你是交易记忆压缩引擎。用简洁中文总结。", prompt)
            except Exception:
                summary = f"{week_id}: " + "; ".join(n.summary[:100] for n in day_nodes)
        else:
            summary = f"{week_id}: " + "; ".join(n.summary[:100] for n in day_nodes)

        all_concepts = [c for n in day_nodes for c in n.key_concepts]
        all_symbols = [s for n in day_nodes for s in n.key_symbols]
        from collections import Counter

        node = TimeNode(
            id=week_id,
            level="week",
            summary=summary[:3000],
            source_count=sum(n.source_count for n in day_nodes),
            key_concepts=[c for c, _ in Counter(all_concepts).most_common(15)],
            key_symbols=[s for s, _ in Counter(all_symbols).most_common(15)],
            sealed_at=datetime.now(timezone.utc).isoformat(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.upsert_node(node)
        self.add_sources(week_id, "child_node", [n.id for n in day_nodes])
        logger.info("Sealed week node %s from %d day nodes", week_id, len(day_nodes))
        return node

    def seal_month(
        self,
        month_id: str,
        week_nodes: list[TimeNode],
        llm_call: LLM_CALL | None = None,
    ) -> TimeNode:
        """Create/seal a month node from week nodes."""
        if not week_nodes:
            summary = f"{month_id}: 无数据"
        elif llm_call:
            combined = "\n".join(f"### {n.id}\n{n.summary[:400]}" for n in week_nodes)
            prompt = f"将以下月份 ({month_id}) 的每周总结压缩为 400-600 字的月度总结：\n{combined}"
            try:
                summary = llm_call("你是交易记忆压缩引擎。用简洁中文总结。", prompt)
            except Exception:
                summary = f"{month_id}: " + "; ".join(n.summary[:120] for n in week_nodes)
        else:
            summary = f"{month_id}: " + "; ".join(n.summary[:120] for n in week_nodes)

        from collections import Counter
        all_concepts = [c for n in week_nodes for c in n.key_concepts]
        all_symbols = [s for n in week_nodes for s in n.key_symbols]

        node = TimeNode(
            id=month_id,
            level="month",
            summary=summary[:4000],
            source_count=sum(n.source_count for n in week_nodes),
            key_concepts=[c for c, _ in Counter(all_concepts).most_common(20)],
            key_symbols=[s for s, _ in Counter(all_symbols).most_common(20)],
            sealed_at=datetime.now(timezone.utc).isoformat(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.upsert_node(node)
        self.add_sources(month_id, "child_node", [n.id for n in week_nodes])
        logger.info("Sealed month node %s from %d week nodes", month_id, len(week_nodes))
        return node

    # ------------------------------------------------------------------
    # Cascade check
    # ------------------------------------------------------------------

    def maybe_cascade(self, date: str, llm_call: LLM_CALL | None = None) -> dict[str, TimeNode | None]:
        """After sealing a day node, check if week/month should be sealed too."""
        result: dict[str, TimeNode | None] = {"week": None, "month": None}

        dt = datetime.strptime(date, "%Y-%m-%d")
        week_id = dt.strftime("%G-W%V")

        # Check week: >= 5 sealed day nodes this week
        week_start = dt - timedelta(days=dt.weekday())
        week_days = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        day_nodes = [n for d in week_days if (n := self.get_node(d)) and n.sealed_at]
        if len(day_nodes) >= 5:
            existing_week = self.get_node(week_id)
            if not existing_week or not existing_week.sealed_at:
                result["week"] = self.seal_week(week_id, day_nodes, llm_call)

        # Check month: >= 3 sealed week nodes this month
        month_id = dt.strftime("%Y-%m")
        month_weeks = self.nodes_by_level("week", sealed_only=True)
        this_month_weeks = [w for w in month_weeks if w.id.startswith(str(dt.year)) and _week_in_month(w.id, month_id)]
        if len(this_month_weeks) >= 3:
            existing_month = self.get_node(month_id)
            if not existing_month or not existing_month.sealed_at:
                result["month"] = self.seal_month(month_id, this_month_weeks, llm_call)

        return result

    # ------------------------------------------------------------------
    # Retrieval API
    # ------------------------------------------------------------------

    def recall(self, scope: str = "today") -> str:
        """Retrieve time-scoped summary.

        scope: "today" | "this_week" | "this_month" | "this_year" | "YYYY-MM-DD" | "YYYY-WNN"
        """
        now = datetime.now()
        if scope == "today":
            node_id = now.strftime("%Y-%m-%d")
        elif scope == "this_week":
            node_id = now.strftime("%G-W%V")
        elif scope == "this_month":
            node_id = now.strftime("%Y-%m")
        elif scope == "this_year":
            node_id = str(now.year)
        else:
            node_id = scope

        node = self.get_node(node_id)
        if not node:
            return f"No time node found for '{scope}'"

        parts = [f"## {node.level.title()}: {node.id}", node.summary]
        if node.key_concepts:
            parts.append(f"Key concepts: {', '.join(node.key_concepts[:10])}")
        if node.key_symbols:
            parts.append(f"Key symbols: {', '.join(node.key_symbols[:10])}")
        parts.append(f"Sources: {node.source_count}")

        child_nodes = self.children(node.level, node.id)
        if child_nodes:
            parts.append(f"\n### Sub-periods ({len(child_nodes)}):")
            for cn in child_nodes[:10]:
                parts.append(f"- **{cn.id}**: {cn.summary[:100]}...")
        return "\n".join(parts)

    def concept_timeline(self, concept: str, lookback_days: int = 30) -> list[TimeNode]:
        """Find day nodes mentioning a concept within the lookback window."""
        cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, level, summary, source_count, key_concepts, key_symbols, sealed_at, created_at "
            "FROM time_nodes WHERE level = 'day' AND id >= ? AND key_concepts LIKE ? ORDER BY id",
            (cutoff, f"%{concept}%"),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_node(self, row: tuple) -> TimeNode:
        id_, level, summary, source_count, concepts_json, symbols_json, sealed_at, created_at = row
        return TimeNode(
            id=id_, level=level, summary=summary,
            source_count=source_count or 0,
            key_concepts=_safe_json_list(concepts_json),
            key_symbols=_safe_json_list(symbols_json),
            sealed_at=sealed_at, created_at=created_at,
        )


def _safe_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _week_in_month(week_id: str, month_id: str) -> bool:
    """Check if an ISO week falls within a given month (approximate)."""
    try:
        year = int(week_id.split("-W")[0])
        week = int(week_id.split("-W")[1])
        # Thursday of the week determines the month
        jan4 = datetime(year, 1, 4)
        start_of_w1 = jan4 - timedelta(days=jan4.weekday())
        thursday = start_of_w1 + timedelta(weeks=week - 1, days=3)
        return thursday.strftime("%Y-%m") == month_id
    except (ValueError, IndexError):
        return False


def current_week_id() -> str:
    return datetime.now().strftime("%G-W%V")
