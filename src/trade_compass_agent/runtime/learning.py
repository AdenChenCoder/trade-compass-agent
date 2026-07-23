from __future__ import annotations

from pathlib import Path

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import LLMRequest, create_llm_provider
from trade_compass_agent.memory.tree.storage import MemoryTreeStore
from trade_compass_agent.runtime.types import TurnResponse


def curate_turn_insight(
    *,
    config: AppConfig,
    user_message: str,
    response: TurnResponse,
) -> str | None:
    """Append notable insights to memory tree (opt-in).

    NOTE: v3.0 redesign — no longer writes directly to MEMORY.md.
    Only writes to the Memory Tree (archive tier). Promotion to
    KNOWLEDGE.md now requires passing through SemanticWriteGate.
    """
    if not config.agent.learning_enabled:
        return None
    if not config.allow_external_llm_memory:
        return None

    api_key_env = config.llm.api_key_env
    provider = create_llm_provider(
        provider=config.llm.provider,
        model=config.llm.model,
        api_key_env=api_key_env,
        enabled=config.llm.provider not in {"", "disabled"},
    )
    if provider.name == "disabled":
        return None

    prompt = (
        "Summarize one notable trading insight from this agent turn in <=3 bullet points in Chinese. "
        "If nothing worth remembering, reply with exactly: SKIP\n\n"
        f"User: {user_message[:800]}\n\nAssistant: {response.summary[:1200]}"
    )
    try:
        llm_response = provider.complete(LLMRequest(prompt=prompt, purpose="learning_curator", allow_memory=True))
    except Exception:
        return None
    text = llm_response.text.strip()
    if not text or text.upper() == "SKIP":
        return None

    store = MemoryTreeStore(config.memory_dir)
    chunk = store.write("insights", text)
    return str(chunk.path)


def compact_memory_summary(config: AppConfig) -> Path | None:
    """Post-market compaction: summarize recent tree chunks into archive.

    NOTE: v3.0 redesign — no longer writes to MEMORY.md directly.
    Compaction results go to the Memory Tree only.
    """
    if not config.agent.learning_enabled:
        return None

    store = MemoryTreeStore(config.memory_dir)
    chunks = store.recent_chunks(limit=20, max_chars=6000)
    if not chunks:
        return None

    combined = "\n".join(f"[{c.scope}] {c.content[:400]}" for c in chunks)
    provider = create_llm_provider(
        provider=config.llm.provider,
        model=config.llm.model,
        api_key_env=config.llm.api_key_env,
        enabled=config.llm.provider not in {"", "disabled"},
    )
    if provider.name == "disabled":
        chunk = store.write("compaction", combined[:2000])
        return chunk.path

    prompt = (
        "Compact these recent memory snippets into a <=200 word Chinese summary. "
        "Focus on durable trading lessons.\n\n"
        f"{combined[:5000]}"
    )
    try:
        llm_response = provider.complete(LLMRequest(prompt=prompt, purpose="memory_compaction", allow_memory=True))
        summary = llm_response.text.strip()
    except Exception:
        summary = combined[:2000]

    chunk = store.write("compaction", summary)
    return chunk.path
