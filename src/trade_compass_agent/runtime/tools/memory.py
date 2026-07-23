from __future__ import annotations

import json

from trade_compass_agent.memory.tree.search import MemorySearchIndex, search_memory_chunks
from trade_compass_agent.memory.tree.storage import MemoryTreeStore, write_memory_chunk


def tool_search_memory(memory_dir, query: str, *, limit: int = 8) -> str:
    hits = search_memory_chunks(memory_dir, query, limit=limit)
    payload = [
        {
            "scope": hit.scope,
            "content": hit.content[:800],
            "path": hit.path,
            "rank": hit.rank,
        }
        for hit in hits
    ]
    return json.dumps({"query": query, "results": payload}, ensure_ascii=False)


def tool_write_memory(memory_dir, scope: str, content: str) -> str:
    chunk = write_memory_chunk(memory_dir, scope, content)
    index = MemorySearchIndex(memory_dir / "tree" / "chunks.db")
    index.index_file(chunk.scope, chunk.content, chunk.path)
    return json.dumps(
        {
            "scope": chunk.scope,
            "path": str(chunk.path),
            "created_at": chunk.created_at.isoformat(),
        },
        ensure_ascii=False,
    )


def bootstrap_memory_context(memory_dir, *, max_chars: int = 2000) -> str:
    store = MemoryTreeStore(memory_dir)
    chunks = store.recent_chunks(limit=6, max_chars=max_chars)
    if not chunks:
        return ""
    lines = ["## Recent Memory"]
    for chunk in chunks:
        lines.append(f"- [{chunk.scope}] {chunk.content[:300]}")
    return "\n".join(lines)
