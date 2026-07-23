from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_FTS5_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_CJK_RANGE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
    r"\U00020000-\U0002a6df\U0002a700-\U0002ebef]"
)
_PUNCT_ONLY = re.compile(r"^[\s\W]+$")


def _sanitize_fts5_query(raw: str) -> str:
    """Convert a raw user query into a safe FTS5 query.

    Handles:
    - Embedded double-quotes (escaped as "")
    - FTS5 operators (AND/OR/NOT/NEAR) stripped
    - CJK text without spaces: split into overlapping bigrams
    - Pure punctuation / empty input → empty string
    """
    raw = raw.strip()
    if not raw or _PUNCT_ONLY.match(raw):
        return ""

    tokens = raw.split()
    quoted: list[str] = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if t.upper() in _FTS5_OPERATORS:
            continue
        escaped = t.replace('"', '""')
        if not _CJK_RANGE.search(escaped) or len(escaped) <= 2:
            quoted.append(f'"{escaped}"')
        else:
            bigrams = [escaped[i : i + 2] for i in range(len(escaped) - 1)]
            quoted.extend(f'"{bg}"' for bg in bigrams)

    if not quoted:
        return ""
    return " OR ".join(quoted)


@dataclass(frozen=True)
class MemorySearchHit:
    scope: str
    content: str
    path: str
    rank: float


class MemorySearchIndex:
    """Optional FTS index over tree buckets (``chunks.db``)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    scope,
                    content,
                    path UNINDEXED
                )
                """
            )
            conn.commit()

    def index_file(self, scope: str, content: str, path: Path) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks_fts WHERE path = ?", (str(path),))
            conn.execute(
                "INSERT INTO chunks_fts(scope, content, path) VALUES (?, ?, ?)",
                (scope, content, str(path)),
            )
            conn.commit()

    def search(self, query: str, *, limit: int = 8) -> list[MemorySearchHit]:
        q = query.strip()
        if not q:
            return []
        fts_q = _sanitize_fts5_query(q)
        if not fts_q:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT scope, content, path, rank
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_q, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            MemorySearchHit(
                scope=str(row["scope"]),
                content=str(row["content"]),
                path=str(row["path"]),
                rank=float(row["rank"]),
            )
            for row in rows
        ]


def search_memory_chunks(memory_dir: Path, query: str, *, limit: int = 8) -> list[MemorySearchHit]:
    index = MemorySearchIndex(memory_dir / "tree" / "chunks.db")
    hits = [h for h in index.search(query, limit=limit * 2) if h.scope != "knowledge"]
    if hits:
        return hits[:limit]
    return _fallback_scan(memory_dir, query, limit=limit)


def reindex_memory_vault(memory_dir: Path, *, index_knowledge: bool = False) -> int:
    """Rebuild FTS index from tree buckets, reflections, and optionally KNOWLEDGE."""
    index = MemorySearchIndex(memory_dir / "tree" / "chunks.db")
    with index._connect() as conn:
        conn.execute("DELETE FROM chunks_fts")
        conn.commit()

    count = 0
    tree_root = memory_dir / "tree"
    if tree_root.is_dir():
        for path in sorted(tree_root.rglob("*.md")):
            scope = path.parent.name
            index.index_file(scope, path.read_text(encoding="utf-8"), path)
            count += 1

    reflections_root = memory_dir / "reflections"
    if reflections_root.is_dir():
        for path in sorted(reflections_root.glob("*.md")):
            stem = path.stem
            scope = f"reflection-{stem.split('-')[0]}" if "-" in stem else "reflection"
            index.index_file(scope, path.read_text(encoding="utf-8"), path)
            count += 1

    for filename, scope in (("DREAM_DIARY.md", "dream-diary"),):
        path = memory_dir / filename
        if path.is_file():
            index.index_file(scope, path.read_text(encoding="utf-8"), path)
            count += 1

    if index_knowledge:
        knowledge_path = memory_dir / "KNOWLEDGE.md"
        if knowledge_path.is_file():
            index.index_file("knowledge", knowledge_path.read_text(encoding="utf-8"), knowledge_path)
            count += 1

    return count


def _fallback_scan(memory_dir: Path, query: str, *, limit: int) -> list[MemorySearchHit]:
    tree_root = memory_dir / "tree"
    if not tree_root.is_dir():
        return []
    needle = query.lower()
    results: list[MemorySearchHit] = []
    for path in sorted(tree_root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        text = path.read_text(encoding="utf-8")
        if needle not in text.lower():
            continue
        scope = path.parent.name
        snippet = text[:500]
        results.append(MemorySearchHit(scope=scope, content=snippet, path=str(path), rank=0.0))
        if len(results) >= limit:
            break
    return results
