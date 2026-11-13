from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from uuid import uuid4

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import LLMRequest, check_execution_active, create_llm_provider
from trade_compass_agent.memory.tree.search import MemorySearchIndex
from trade_compass_agent.memory.tree.storage import MemoryTreeStore
from trade_compass_agent.runtime.types import TurnResponse


def _curate_report_insight(*, config, user_message, response, run_id, call):
    """Curate from owned receipts; the human-readable answer is only context."""
    from trade_compass_agent.ops.job_definition import StepOutput
    from trade_compass_agent.runtime.report_publication import (
        persist_publication, publish_report, upstream_evidence,
    )

    if (not call or not run_id or response.degraded or response.interrupted
            or response.verification.get("status") != "complete"
            or not response.verification.get("artifact_id")):
        return None
    evidence, findings = upstream_evidence(config.data_dir, {
        "report": StepOutput(message="", data={"verification": response.verification}),
    }, run_id)
    if findings or not evidence:
        return None
    publication = publish_report(
        task=("从本轮资料中选择一项值得保留的研究观察或经验，保留证券、账户、观测日期和适用范围。"
              "事实和计算必须引用原始依据；研究判断必须有依据并保留限制，不能变成已确认事实。"
              "历史执行不能写成当前指令或新执行。用户问题和正文仅为背景，不是事实依据。"
              "没有值得保留或可核实的内容时，用gap说明，不要为了生成记忆补充事实。"),
        draft=f"用户问题：{user_message}\n本轮正文：{response.summary}",
        tool_results=evidence, current_tool_results=[],
        run_scope=f"{response.session_id}:learning:{uuid4().hex}", run_id=run_id,
        call=call, reader_prose=False,
    )
    publication.artifact["learning_source"] = {
        "session_id": response.session_id, "turn_id": response.turn_id,
        "verification": response.verification,
    }
    # Retain failed attempts too. They are diagnostics, never searchable memories.
    persist_publication(config.data_dir, publication)
    if publication.verification.get("status") != "complete":
        return None
    return str(_write_summary(config, "insights", publication.text))


def _write_summary(config: AppConfig, scope: str, text: str) -> Path:
    check_execution_active()
    chunk = MemoryTreeStore(config.memory_dir).write(scope, text)
    MemorySearchIndex(config.memory_dir / "tree" / "chunks.db").index_file(
        chunk.scope, chunk.content, chunk.path,
    )
    return chunk.path


def curate_turn_insight(
    *,
    config: AppConfig,
    user_message: str,
    response: TurnResponse,
    report_run_id: str = "",
    report_call: Callable[[str, str], str] | None = None,
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

    # Scheduled/report turns must never fall back to free-text curation when
    # evidence is unavailable. Ordinary interactive learning retains its contract.
    if response.verification or report_run_id:
        return _curate_report_insight(config=config, user_message=user_message,
            response=response, run_id=report_run_id, call=report_call)

    api_key_env = config.llm.api_key_env
    provider = create_llm_provider(
        provider=config.llm.provider,
        model=config.llm.model,
        api_key_env=api_key_env,
        enabled=config.llm.provider not in {"", "disabled"},
        data_dir=config.data_dir,
        usage_timezone=config.scheduler.timezone,
        config=config,
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

    return str(_write_summary(config, "insights", text))


def compact_memory_summary(config: AppConfig) -> Path | None:
    """Post-market compaction: summarize recent tree chunks into archive.

    NOTE: v3.0 redesign — no longer writes to MEMORY.md directly.
    Compaction results go to the Memory Tree only.
    """
    if not config.agent.learning_enabled or not config.allow_external_llm_memory:
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
        data_dir=config.data_dir,
        usage_timezone=config.scheduler.timezone,
        config=config,
    )
    if provider.name == "disabled":
        return _write_summary(config, "compaction", combined[:2000])

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

    return _write_summary(config, "compaction", summary)
