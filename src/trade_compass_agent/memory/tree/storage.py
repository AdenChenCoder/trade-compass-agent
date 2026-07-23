from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class MemoryChunk:
    scope: str
    content: str
    path: Path
    created_at: datetime


class MemoryTreeStore:
    """Bucket storage under ``memory_vault/tree/<scope>/``."""

    def __init__(self, root: Path) -> None:
        self.root = root / "tree"
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, scope: str, content: str) -> MemoryChunk:
        normalized_scope = _normalize_scope(scope)
        bucket = self.root / normalized_scope
        bucket.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y%m%dT%H%M%S%f')}.md"
        path = bucket / filename
        body = f"---\nscope: {normalized_scope}\ncreated_at: {now.isoformat()}\n---\n\n{content.strip()}\n"
        path.write_text(body, encoding="utf-8")
        return MemoryChunk(scope=normalized_scope, content=content.strip(), path=path, created_at=now)

    def recent_chunks(self, *, limit: int = 8, max_chars: int = 2000) -> list[MemoryChunk]:
        files = sorted(self.root.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        chunks: list[MemoryChunk] = []
        total = 0
        for path in files[: limit * 3]:
            text = path.read_text(encoding="utf-8")
            scope = path.parent.name
            content = _strip_frontmatter(text)
            if not content:
                continue
            if total + len(content) > max_chars:
                break
            chunks.append(
                MemoryChunk(
                    scope=scope,
                    content=content,
                    path=path,
                    created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                )
            )
            total += len(content)
            if len(chunks) >= limit:
                break
        return chunks


def write_memory_chunk(memory_dir: Path, scope: str, content: str) -> MemoryChunk:
    return MemoryTreeStore(memory_dir).write(scope, content)


def _normalize_scope(scope: str) -> str:
    cleaned = scope.strip().lower().replace(" ", "_")
    if not _SCOPE_RE.match(cleaned):
        raise ValueError(f"invalid memory scope: {scope}")
    return cleaned


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3 :].strip()
    return text.strip()
