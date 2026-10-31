from __future__ import annotations

import json
import time
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.runtime.exceptions import AttachmentInputError, AgentTurnError, LLMIncompleteResponseError, TurnInterruptedError
from trade_compass_agent.runtime.activity_events import (
    build_tool_end_payload,
    build_tool_start_payload,
)
from trade_compass_agent.llm.providers import ChatMessage, LLMExecutionCancelled, create_chat_client, is_lmstudio_qwen
from trade_compass_agent.llm.usage import llm_usage_context
from trade_compass_agent.runtime.context import ContextBuilder, _sanitize_tool_calls
from trade_compass_agent.runtime.compression.budget import TokenBudget
from trade_compass_agent.runtime.compression.summarizer import (
    latest_persisted_summary,
    summarize_middle_turns,
)
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.data.network import run_with_timeout
from trade_compass_agent.runtime.session import SessionMessageRecord, SessionStore
from trade_compass_agent.runtime.session_title import suggest_session_title
from trade_compass_agent.runtime.skills import discover_skills, load_agent_skills_config
from trade_compass_agent.runtime.intake import enrich_user_message, parse_attachments
from trade_compass_agent.runtime.learning import curate_turn_insight
from trade_compass_agent.runtime.mcp.client import get_mcp_registry
from trade_compass_agent.runtime.tools.registry import PARALLEL_SAFE_TOOLS, ToolRegistry
from trade_compass_agent.runtime.types import TurnEvent, TurnResponse, TurnSection
from trade_compass_agent.runtime.background_review import ReviewNudgeTracker, spawn_background_review, is_committed_mutation, resume_background_reviews
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.capture_hooks import should_capture, extract_summary
from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
from trade_compass_agent.memory.consolidation import ConsolidationWorker, shared_consolidation_worker


import logging
import re

logger = logging.getLogger(__name__)

_MAX_PARALLEL_WORKERS = 6
_PARALLEL_TOOL_TIMEOUT_SECONDS = 15.0
_BATCH_TOOL_TIMEOUT_SECONDS = 60.0
_SEARCH_TOOL_TIMEOUT_SECONDS = 40.0
TOOL_ROUND_LIMIT_MESSAGE = "已达到分析轮次上限，基于已获取数据总结如下：请查看上方工具输出。"

_DSML_BLOCK_RE = re.compile(
    r"<[｜|]{2}DSML[｜|]{2}tool_calls>.*?</[｜|]{2}DSML[｜|]{2}tool_calls>",
    re.DOTALL,
)
_DSML_CALL_RE = re.compile(
    r'<[｜|]{2}DSML[｜|]{2}invoke\s+name="([^"]+)">'
    r'(.*?)'
    r'</[｜|]{2}DSML[｜|]{2}invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<[｜|]{2}DSML[｜|]{2}parameter\s+name="([^"]+)"[^>]*>'
    r'(.*?)'
    r'<[｜|]{2}DSML[｜|]{2}parameter>',
    re.DOTALL,
)


def _parse_dsml_tool_calls(text: str) -> tuple[str, list]:
    """Extract DSML-formatted tool calls from text and return cleaned text + ToolCalls.

    DeepSeek sometimes emits its internal tool call tokens as raw text.
    This function attempts to parse them into structured ToolCall objects
    and strips the DSML blocks from the text.
    """
    from trade_compass_agent.llm.providers import ToolCall

    blocks = list(_DSML_BLOCK_RE.finditer(text))
    if not blocks:
        return text, []

    calls = []
    for block in blocks:
        for m in _DSML_CALL_RE.finditer(block.group()):
            fn_name = m.group(1)
            body = m.group(2)
            args = {}
            for pm in _DSML_PARAM_RE.finditer(body):
                args[pm.group(1)] = pm.group(2).strip()
            call_id = f"dsml_{fn_name}_{len(calls)}"
            calls.append(ToolCall(id=call_id, name=fn_name, arguments=json.dumps(args, ensure_ascii=False)))

    cleaned = _DSML_BLOCK_RE.sub("", text).strip()
    if calls:
        logger.info("Parsed %d DSML tool call(s) from content: %s", len(calls), [c.name for c in calls])
    return cleaned, calls


def _execute_tool_calls(
    tool_calls,
    tools: ToolRegistry,
    is_cancelled: Callable[[], bool] | None,
    prior_tool_results=(),
    reference_date=None,
) -> dict[str, str]:
    """Execute tool calls with selective parallelism.

    Tier A (PARALLEL_SAFE_TOOLS) run concurrently via a thread pool.
    All other tools run sequentially in their original order, after
    the parallel batch completes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context

    results: dict[str, str] = {}

    parallel = [(i, tc) for i, tc in enumerate(tool_calls) if tc.name in PARALLEL_SAFE_TOOLS]
    sequential = [(i, tc) for i, tc in enumerate(tool_calls) if tc.name not in PARALLEL_SAFE_TOOLS]

    def _run_parallel(tc):
        try:
            result = run_with_timeout(
                lambda: tools.execute(tc.name, tc.arguments),
                timeout=_SEARCH_TOOL_TIMEOUT_SECONDS if tc.name in {"web_search", "search_x", "search_x_kol"} else _PARALLEL_TOOL_TIMEOUT_SECONDS,
                description=f"tool:{tc.name}",
            )
        except TimeoutError as exc:
            args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments or {}
            result = json.dumps({"error": str(exc), "timed_out": True,
                **{k: args[k] for k in ("symbol", "query", "timeframe") if k in args}}, ensure_ascii=False)
        return tc.id, result

    if len(parallel) > 1:
        workers = min(_MAX_PARALLEL_WORKERS, len(parallel))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool_par") as pool:
            futures = {pool.submit(copy_context().run, _run_parallel, tc): tc for _, tc in parallel}
            for future in as_completed(futures):
                if is_cancelled and is_cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                tc_id, result = future.result()
                results[tc_id] = result
    else:
        for _, tc in parallel:
            if is_cancelled and is_cancelled():
                break
            tc_id, result = _run_parallel(tc)
            results[tc_id] = result

    for _, tc in sequential:
        if is_cancelled and is_cancelled():
            break
        if tc.name == "write_memory":
            from trade_compass_agent.runtime.report_quality import report_issues
            try:
                arguments = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                content = arguments.get("content", "") if isinstance(arguments, dict) else ""
                evidence = [*prior_tool_results, *((call.name, results[call.id]) for call in tool_calls if call.id in results)]
                issues = report_issues(content, evidence, reference_date=reference_date) if isinstance(content, str) else []
            except (ValueError, TypeError):
                issues = []  # Normal tool argument validation owns malformed arguments.
            if issues:
                results[tc.id] = json.dumps({"error": "记忆内容与本轮事实不一致，未保存；请修正后再提交",
                    "scope": arguments.get("scope", ""), "fact_check_failed": True, "issues": issues}, ensure_ascii=False)
                continue
        if tc.name in {"batch_get_bars", "batch_get_fundamentals", "batch_search_news"}:
            try:
                results[tc.id] = run_with_timeout(
                    lambda tc=tc: tools.execute(tc.name, tc.arguments),
                    _BATCH_TOOL_TIMEOUT_SECONDS, f"tool:{tc.name}",
                )
            except TimeoutError as exc:
                results[tc.id] = json.dumps({"error": str(exc), "timed_out": True}, ensure_ascii=False)
        else:
            results[tc.id] = tools.execute(tc.name, tc.arguments)

    for tc in tool_calls:
        if tc.id not in results:
            results[tc.id] = json.dumps({"error": "cancelled"}, ensure_ascii=False)

    return results


@dataclass
class AgentLoop:
    config: AppConfig
    stack: MarketStack
    session_store: SessionStore
    on_event: Callable[[TurnEvent], None] | None = None
    memory_actor: str = "agent"
    skill_actor: str = "agent"
    report_date: str | None = None
    planning_reference_date: str | None = None
    initial_portfolio: dict | None = None
    verify_report_grounding: bool = False
    _tools: ToolRegistry = field(init=False)
    _event_seq: int = field(init=False, default=0)
    _nudge: ReviewNudgeTracker = field(init=False)
    _memory_store: MemoryStore = field(init=False)
    _skill_store: SkillStore = field(init=False)
    _obs_store: ObservationStore = field(init=False)
    _session_summaries: SessionSummaryStore = field(init=False)
    _consolidation: ConsolidationWorker = field(init=False)

    def __post_init__(self) -> None:
        mcp = get_mcp_registry()
        self._skill_store = SkillStore(self.config.memory_dir / "skills")
        from trade_compass_agent.memory.write_gate import SemanticWriteGate
        self._write_gate = SemanticWriteGate(skill_store=self._skill_store)
        self._memory_store = MemoryStore(
            self.config.memory_dir,
            write_gate=self._write_gate,
            min_inject_confidence=self.config.memory.governance.min_inject_confidence,
        )
        self._obs_store = ObservationStore(self.config.data_dir / "observations.db")
        self._session_summaries = SessionSummaryStore(self.config.data_dir / "sessions.db")

        def _consolidation_llm(system_prompt: str, user_content: str) -> str:
            from trade_compass_agent.llm.providers import llm_maintenance_mode

            with llm_usage_context(job_id="memory-consolidation"), llm_maintenance_mode():
                client = create_chat_client(self.config)
                msgs = [
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=user_content),
                ]
                return client.complete(msgs).content or ""

        self._consolidation = shared_consolidation_worker(
            obs_store=self._obs_store,
            session_store=self._session_summaries,
            memory_store=self._memory_store,
            llm_call=_consolidation_llm,
        )
        self._tools = ToolRegistry(
            self.stack,
            on_event=self.on_event,
            mcp_registry=mcp,
            memory_store=self._memory_store,
            skill_store=self._skill_store,
            session_summary_store=self._session_summaries,
            observation_store=self._obs_store,
            memory_actor=self.memory_actor,
            skill_actor=self.skill_actor,
        )
        self._event_seq = 0
        self._nudge = ReviewNudgeTracker()

    @classmethod
    def from_config(cls, config: AppConfig | None = None, **kwargs) -> AgentLoop:
        app_config = config or load_app_config()
        return cls(
            config=app_config,
            stack=MarketStack.from_config(app_config),
            session_store=SessionStore(app_config.data_dir / "agent_sessions"),
            **kwargs,
        )

    def _emit(self, event: str, data: dict) -> None:
        if self.on_event:
            self._event_seq += 1
            self.on_event(
                TurnEvent(
                    event=event,
                    data=data,
                    id=f"evt-{self._event_seq}",
                )
            )

    def _maybe_background_review(self, messages: list, *, source_session_id="", source_turn_id="") -> None:
        """Check nudge thresholds and spawn background review if needed."""
        if self.memory_actor == "background_review":
            return
        if not self.config.allow_external_llm_memory:
            return
        resume_background_reviews(self.config)
        review_memory, review_skills = self._nudge.should_review()
        maintenance_due = self._memory_store.review_due()
        if not review_memory and not review_skills and not maintenance_due:
            return

        def llm_call(system_prompt: str, msgs: list) -> str:
            client = create_chat_client(self.config)
            chat_msgs = [ChatMessage(role="system", content=system_prompt)]
            for m in msgs[-10:]:
                if isinstance(m, ChatMessage):
                    chat_msgs.append(m)
                elif isinstance(m, dict):
                    chat_msgs.append(ChatMessage(role=m.get("role", "user"), content=m.get("content", "")))
            completion = client.complete(chat_msgs)
            return completion.content or ""

        def memory_write(action, content, target):
            from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
            return tool_memory_write(
                self._memory_store,
                action,
                content,
                target,
                actor="background_review",
                governance=self.config.memory.governance,
            )

        def skill_manage(action, kwargs):
            from trade_compass_agent.runtime.tools.self_improve import tool_skill_manage
            kwargs.pop("actor", None)
            return tool_skill_manage(self._skill_store, action, actor="background_review", **kwargs)

        # Keep this turn's original user request and final decision plus actual tool
        # receipts. Full source transcript remains addressable by session/turn ID.
        start = max((i for i, m in enumerate(messages) if getattr(m, "role", "") == "user"), default=0)
        snapshot = [{"role": getattr(m, "role", "user"), "content": getattr(m, "content", "") or "",
                     "name": getattr(m, "name", None), "tool_call_id": getattr(m, "tool_call_id", None),
                     "tool_calls": getattr(m, "tool_calls", [])}
                    for m in messages[start:]]

        if maintenance_due:
            review_snapshot = self._memory_store.background_review_snapshot()
            spawn_background_review(
                messages_snapshot=snapshot,
                review_memory=True,
                review_skills=False,
                llm_call=llm_call,
                memory_write=memory_write,
                skill_manage=skill_manage,
                memory_store=self._memory_store,
                config=self.config, source_session_id=source_session_id, source_turn_id=source_turn_id,
                maintenance_fingerprint=review_snapshot["fingerprint"],
                maintenance_entry_ids=review_snapshot["entry_ids"],
                maintenance_snapshot=review_snapshot,
            )
        if review_memory or review_skills:
            spawn_background_review(
                messages_snapshot=snapshot,
                review_memory=review_memory,
                review_skills=review_skills,
                llm_call=llm_call,
                memory_write=memory_write,
                skill_manage=skill_manage,
                memory_store=self._memory_store,
                config=self.config, source_session_id=source_session_id, source_turn_id=source_turn_id,
            )
            # The durable request owns retries, even when this AgentLoop exits.
            self._nudge.reset_after_review(review_memory, review_skills)

    def _update_session_summary(
        self,
        session,
        tool_calls_log: list[tuple[str, str]],
        final_text: str,
    ) -> None:
        """Update episodic session summary (lightweight, no LLM call)."""
        import re as _re

        tools_used = list(dict.fromkeys(t[0] for t in tool_calls_log))
        # Extract stock symbols (6-digit codes)
        all_text = final_text + " ".join(r for _, r in tool_calls_log[:10])
        symbols = list(dict.fromkeys(_re.findall(r"\b[036]\d{5}\b", all_text)))[:20]
        turn_count = sum(1 for m in session.messages if m.role == "user")
        preview = final_text[:300] if final_text else ""

        self._session_summaries.upsert(
            session_id=session.session_id,
            summary=preview,
            title=session.title,
            turn_count=turn_count,
            tools_used=tools_used,
            symbols=symbols,
            started_at=session.created_at.isoformat() if session.created_at else None,
        )

    def run_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        attachments: list[dict] | None = None,
        turn_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> TurnResponse:
        from contextlib import nullcontext
        from trade_compass_agent.llm.native_session import native_session_guard
        from trade_compass_agent.llm.providers import llm_request_deadline

        resolved_session = self.session_store.get_or_create(session_id)
        guard = (native_session_guard(self.config.data_dir, resolved_session.session_id)
                 if self.config.llm.provider == "codex" else nullcontext())
        budget_guard = (llm_request_deadline(time.monotonic() + self.config.llm.agent_timeout,
                                             is_cancelled=is_cancelled)
                        if self.config.llm.provider == "codex" else nullcontext())
        with guard, budget_guard, llm_usage_context(session_id=resolved_session.session_id):
            self._nudge.bind_session(self.config.data_dir, resolved_session.session_id)
            return self._run_turn(
                message,
                session_id=resolved_session.session_id,
                attachments=attachments,
                turn_id=turn_id,
                is_cancelled=is_cancelled,
            )

    def _run_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        attachments: list[dict] | None = None,
        turn_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> TurnResponse:
        from trade_compass_agent.llm.native import NativeAgentClient

        def _check_cancelled() -> None:
            if is_cancelled and is_cancelled():
                raise TurnInterruptedError(final_text)

        client = create_chat_client(self.config)
        self._nudge.reset_turn_flags()
        skills_cfg = load_agent_skills_config()
        skills = discover_skills(memory_dir=self.config.memory_dir, skills_config=skills_cfg)
        from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
        from trade_compass_agent.runtime.bootstrap import build_trading_policy

        autonomous_trading_enabled = AutonomousTradingStore(self.config.data_dir).read()
        context = ContextBuilder(
            memory_dir=self.config.memory_dir,
            skills=skills,
            skills_config=skills_cfg,
            memory_store=self._memory_store,
            rules_enabled=self.config.rules.enabled,
            rules_char_limit=self.config.rules.char_limit,
            compression_config=self.config.context_compression,
            trading_policy=build_trading_policy(
                autonomous_trading_enabled,
                interactive=self.memory_actor == "agent",
            ),
        )
        session = self.session_store.get_or_create(session_id)
        from trade_compass_agent.llm.native_session import NativeConversation
        native_conversation = None
        publish_after_verification = session.session_id.startswith("scheduler-")
        if turn_id:
            self._emit(
                "turn_started",
                {"turn_id": turn_id, "session_id": session.session_id},
            )
        parsed_attachments = parse_attachments(attachments)
        native_images = ([att for att in parsed_attachments if att.type == "image"]
                         if isinstance(client, NativeAgentClient) else [])
        enriched = enrich_user_message(
            message,
            [att for att in parsed_attachments if att not in native_images],
            config=self.config,
            memory_dir=self.config.memory_dir,
        )
        if native_images:
            enriched += "\n\n" + "\n".join(f"[附件·图片 {i + 1}]" for i in range(len(native_images)))
        history = [m.to_chat_message() for m in self.session_store.load_context(session)]
        if not isinstance(client, NativeAgentClient):
            # A provider switch must retain access to native-uploaded evidence
            # through the existing vision tools, including text-only backends.
            history = [replace(m, content=m.content + "\n\n" + "\n".join(
                f"[附件·图片保存于 {path}，可使用image_analyze读取]" for path in m.image_paths))
                if m.image_paths else m for m in history]
        self._tools.set_trade_context(
            message, attachments=enriched[len(message.strip()):].strip() if attachments else "",
            previous_reply=next((m.content for m in reversed(history) if m.role == "assistant" and m.content), ""),
        )
        messages = context.build_messages(history, enriched)

        # Phase 0: token budget check — Phase 1 trim (in ContextBuilder) + Phase 2 summarize
        window = getattr(client, "context_window", None)
        reserve = getattr(client, "response_token_limit", 0)
        _budget = TokenBudget(self.config, context_window=window,
                              output_reserve=reserve if type(reserve) is int else 0)
        def compact_context(*, in_turn, request_tools):
            nonlocal messages
            if _budget.enabled:
                measured = None
                if str(getattr(client, "name", "")).startswith("lmstudio"):
                    counter = getattr(client, "measure_input_tokens", None)
                    if callable(counter):
                        measured = counter(messages, tools=request_tools)
                _est_tokens = measured.input_tokens if measured is not None else _budget.estimate(
                    messages, tools_schemas=request_tools,
                )
                source_counts = dict(zip(map(id, messages), measured.public_message_tokens)) if measured is not None else {}
                source_counter = (lambda item: source_counts[id(item)]) if len(source_counts) == len(messages) else None
                _usage = _budget.usage_pct(_est_tokens)
                if _budget.should_summarize(_est_tokens):
                    logger.info(
                        "context: ~%s tokens (%.0f%% of %s budget) — triggering Phase 2 summarization",
                        f"{_est_tokens:,}", _usage * 100, f"{_budget.context_budget:,}",
                    )
                    self._emit("status", {"text": "🗜️ 上下文过长，生成摘要中…"})
                    try:
                        def _summarize_llm(sys_prompt: str, user_prompt: str) -> str:
                            msgs = [
                                ChatMessage(role="system", content=sys_prompt),
                                ChatMessage(role="user", content=user_prompt),
                            ]
                            from trade_compass_agent.llm.providers import llm_maintenance_mode
                            with llm_maintenance_mode():
                                return client.complete(msgs).content or ""

                        compressed, _summary_text, summarized, saved = summarize_middle_turns(
                            messages,
                            llm_call=_summarize_llm,
                            protect_recent_count=_budget.protect_recent_count,
                            protect_recent_tokens=_budget.protect_recent_tokens,
                            previous_summary=latest_persisted_summary(messages),
                            message_token_counter=source_counter,
                        )
                        if summarized > 0:
                            compacted_messages = _sanitize_tool_calls(compressed)
                            if not compacted_messages or compacted_messages[-1] != messages[-1]:
                                raise RuntimeError("compacted context lost the current user message")
                            durable_history = [
                                SessionMessageRecord(
                                    role=item.role,
                                    content=item.content or "",
                                    timestamp=datetime.now(),
                                    tool_call_id=item.tool_call_id,
                                    name=item.name,
                                    tool_calls=list(item.tool_calls or []),
                                    image_paths=list(item.image_paths),
                                )
                                for item in (compacted_messages[1:] if in_turn else compacted_messages[1:-1])
                            ]
                            archive_path = self.session_store.replace_context(
                                session,
                                durable_history,
                            )
                            messages = compacted_messages
                            _pre_chars = sum(len(m.content or "") for m in messages) + saved
                            _savings_ratio = saved / max(_pre_chars, 1)
                            logger.info(
                                "summarization: %d messages → durable summary, ~%d chars saved "
                                "(%.1f%% reduction, archive=%s)",
                                summarized, saved, _savings_ratio * 100, archive_path,
                            )
                            self._emit(
                                "status",
                                {"text": f"🗜️ 已压缩并保存 {summarized} 条历史消息"},
                            )
                    except Exception as _sum_err:
                        logger.error("Phase 2 summarization failed: %s", _sum_err)
                elif _budget.is_emergency(_est_tokens):
                    logger.warning(
                        "context: ~%s tokens (%.0f%% of %s budget) — emergency zone",
                        f"{_est_tokens:,}", _usage * 100, f"{_budget.context_budget:,}",
                    )
                elif _budget.should_trim(_est_tokens):
                    logger.debug(
                        "context: ~%s tokens (%.0f%% of %s budget) — trim applied in build_messages",
                        f"{_est_tokens:,}", _usage * 100, f"{_budget.context_budget:,}",
                    )

        user_record = SessionMessageRecord(role="user", content=enriched, timestamp=datetime.now(),
                                           autonomous_trading_enabled=autonomous_trading_enabled)

        sections: list[TurnSection] = []
        final_text = ""
        published_response = None
        max_rounds = self.config.agent.max_tool_rounds
        incomplete_reason = ""
        tool_calls_log: list[tuple[str, str]] = []

        def record_tool_effects(name: str, arguments: str, result: str) -> None:
            nonlocal incomplete_reason
            try:
                payload = json.loads(result)
                candidates = [payload]
                if name == "dispatch_specialists" and isinstance(payload, dict):
                    for item in payload.get("results", []):
                        try:
                            candidates.append(json.loads(item.get("output", "")))
                        except (ValueError, TypeError, AttributeError):
                            continue
                for candidate in candidates:
                    if isinstance(candidate, dict) and candidate.get("incomplete_reason"):
                        incomplete_reason = str(candidate["incomplete_reason"])
            except (ValueError, TypeError):
                pass
            self._nudge.on_tool_iteration()
            if should_capture(name, result):
                summary = extract_summary(name, result)
                from trade_compass_agent.memory.observation_store import estimate_importance, extract_concepts
                self._obs_store.append(
                    session_id=session.session_id,
                    tool_name=name,
                    summary=summary,
                    raw_preview=result[:2000],
                    importance=estimate_importance(name, summary),
                    concepts=extract_concepts(summary),
                )
            if name == "write_knowledge" and is_committed_mutation(name, arguments, result):
                self._nudge.on_memory_write()
            elif name == "skill_manage" and is_committed_mutation(name, arguments, result):
                self._nudge.on_skill_write()
            section = _section_from_tool(name, result)
            if section:
                sections.append(section)
                self._emit("section", _section_to_dict(section))

        def native_delta(chunk: str) -> None:
            nonlocal final_text
            final_text += chunk
            if not publish_after_verification:
                self._emit("delta", {"text": chunk})

        native_tool_started = {}

        def native_tool_result(tc, result):
            self._emit("tool_end", build_tool_end_payload(tc.name, native_tool_started.pop(tc.id), result))
            tool_calls_log.append((tc.name, result))
            messages.append(ChatMessage(role="tool", content=result, tool_call_id=tc.id, name=tc.name))
            self.session_store.append(session, SessionMessageRecord(
                role="tool", content=result, timestamp=datetime.now(), tool_call_id=tc.id, name=tc.name))
            record_tool_effects(tc.name, tc.arguments, result)

        def native_tool(tc) -> str:
            _check_cancelled()
            assistant = ChatMessage(role="assistant", tool_calls=[{
                "id": tc.id, "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }])
            messages.append(assistant)
            self.session_store.append(session, SessionMessageRecord(
                role="assistant", content="", timestamp=datetime.now(), tool_calls=assistant.tool_calls))
            self._emit("tool_start", build_tool_start_payload(tc.name, tc.arguments))
            native_tool_started[tc.id] = time.monotonic()
            result = _execute_tool_calls([tc], self._tools, is_cancelled, tool_calls_log, self.report_date)[tc.id]
            native_tool_result(tc, result)
            return result

        try:
            native = isinstance(client, NativeAgentClient)
            try:
                if native_images:
                    from trade_compass_agent.llm.native_images import save_image
                    user_record.image_paths = [save_image(att.content, att.url, self.config) for att in native_images]
                    messages[-1] = replace(messages[-1], image_paths=list(user_record.image_paths))
                compact_context(in_turn=False, request_tools=None if self.config.agent.max_tool_rounds == 1 else self._tools.schemas)
                if native:
                    # A rewritten application checkpoint must reach the next
                    # native turn instead of retaining stale vendor history.
                    native_conversation = NativeConversation(
                        self.config.data_dir, session.session_id, session.messages,
                        context=self.session_store.load_context(session),
                    )
            finally:
                # Stopping preflight work must not discard the submitted question.
                self.session_store.append(session, user_record)
            _check_cancelled()
            if not session.title:
                user_count = sum(1 for item in session.messages if item.role == "user")
                if user_count == 1:
                    if self.config.agent.llm_session_titles:
                        self.session_store.set_title(session, suggest_session_title(message, self.config))
                    else:
                        self.session_store.maybe_set_title_from_first_message(session, message)
            if native:
                self._emit("status", {"text": "思考中…"})
                completion = client.run_agent(
                    messages, tools=self._tools.schemas, execute_tool=native_tool,
                    on_delta=native_delta, is_cancelled=is_cancelled,
                    on_tool_interrupted=native_tool_result,
                    conversation=native_conversation,
                )
                final_text = completion.content or ""
                incomplete_reason = completion.incomplete_reason or incomplete_reason
            for _round in range(0 if native else max_rounds):
                _check_cancelled()
                is_last_round = (_round == max_rounds - 1)
                if _round:
                    compact_context(in_turn=True, request_tools=None if is_last_round else self._tools.schemas)
                    _check_cancelled()
                self._emit("status", {"text": "思考中…"})

                def _on_delta(chunk: str) -> None:
                    nonlocal final_text
                    final_text += chunk
                    if not publish_after_verification:
                        self._emit("delta", {"text": chunk})

                try:
                    completion = client.stream_complete(
                        messages,
                        tools=None if is_last_round else self._tools.schemas,
                        on_delta=_on_delta,
                        is_cancelled=is_cancelled,
                    )
                except LLMIncompleteResponseError:
                    final_text = ""
                    break
                _check_cancelled()
                effective_tool_calls = completion.tool_calls
                if not effective_tool_calls and completion.content and not is_last_round:
                    cleaned, dsml_calls = _parse_dsml_tool_calls(completion.content)
                    valid_names = {
                        s.get("function", s).get("name", "")
                        for s in self._tools.schemas
                    }
                    dsml_calls = [c for c in dsml_calls if c.name in valid_names]
                    if dsml_calls:
                        effective_tool_calls = dsml_calls
                        completion = completion.__class__(
                            content=cleaned or None,
                            tool_calls=dsml_calls,
                            model=completion.model,
                            provider=completion.provider,
                            **({"reasoning_content": completion.reasoning_content}
                               if getattr(completion, "reasoning_content", "") else {}),
                        )
                if effective_tool_calls:
                    assistant_tool_calls = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in effective_tool_calls
                    ]
                    assistant_tc_msg = ChatMessage(
                        role="assistant",
                        content=completion.content or "",
                        tool_calls=assistant_tool_calls,
                        reasoning_content=getattr(completion, "reasoning_content", ""),
                    )
                    messages.append(assistant_tc_msg)
                    self.session_store.append(
                        session,
                        SessionMessageRecord(
                            role="assistant",
                            content=completion.content or "",
                            timestamp=datetime.now(),
                            tool_calls=assistant_tool_calls,
                        ),
                    )
                    final_text = ""
                    results_by_id = _execute_tool_calls(
                        completion.tool_calls,
                        self._tools,
                        is_cancelled,
                        tool_calls_log,
                        self.report_date,
                    )
                    tool_batch_timed_out = False
                    for tc in completion.tool_calls:
                        result = results_by_id[tc.id]
                        try:
                            tool_batch_timed_out = tool_batch_timed_out or bool(
                                json.loads(result).get("timed_out")
                            )
                        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                            pass
                        tool_calls_log.append((tc.name, result))
                        self._emit("tool_start", build_tool_start_payload(tc.name, tc.arguments))
                        self._emit("tool_end", build_tool_end_payload(tc.name, time.monotonic(), result))
                        tool_msg = ChatMessage(
                            role="tool",
                            content=result,
                            tool_call_id=tc.id,
                            name=tc.name,
                        )
                        messages.append(tool_msg)
                        self.session_store.append(
                            session,
                            SessionMessageRecord(
                                role="tool",
                                content=result,
                                timestamp=datetime.now(),
                                tool_call_id=tc.id,
                                name=tc.name,
                            ),
                        )
                        record_tool_effects(tc.name, tc.arguments, result)
                    if tool_batch_timed_out:
                        self._emit("status", {"text": "部分数据源超时，基于已有结果整理结论…"})
                        break
                    continue

                raw_final = (completion.content or "").strip()
                final_text, _leftover = _parse_dsml_tool_calls(raw_final)
                if _leftover:
                    logger.warning("Stripped %d unparsable DSML tool call(s) from final text", len(_leftover))
                break

            if not final_text:
                _check_cancelled()
                self._emit("status", {"text": "整理已有工具结果…"})
                force_prompt = ChatMessage(
                    role="user",
                    content=(
                        "本轮工具阶段已经结束。不要再调用工具。"
                        "请只基于本轮用户问题、已有上下文和上方所有 tool 结果，直接输出最终结论。"
                        "必须给出可读的总结，不要写“请查看上方工具输出”。"
                    ),
                )
                forced_text = ""

                def _on_forced_delta(chunk: str) -> None:
                    nonlocal forced_text
                    forced_text += chunk
                    if not publish_after_verification:
                        self._emit("delta", {"text": chunk})

                try:
                    if is_lmstudio_qwen(getattr(client, "name", ""), getattr(client, "model", "")):
                        from trade_compass_agent.runtime.report_quality import report_portfolio_facts
                        summary_messages = [
                            ChatMessage(role="system", content=(
                                "你是执行报告整理器。仅依据给定任务和真实工具结果生成最终报告。"
                                "没有成交回执时必须明确本轮未成交，不得编造工具调用、交易或trade_id。"
                                "分别报告账户本金和当前可用现金；可用现金合计以portfolio_facts.total_available_cash为准，不得用本金额度替代。"
                                "该现金合计为null时须标注未确认，不能填零或其他确定金额。"
                                "数据不足则明确缺口；遵守原系统规则。\n" + messages[0].content
                            )),
                            ChatMessage(role="user", content=json.dumps({
                                "task": message, "tool_results": tool_calls_log,
                                "portfolio_facts": report_portfolio_facts(tool_calls_log),
                                "instruction": force_prompt.content,
                            }, ensure_ascii=False)),
                        ]
                        completion = client.stream_complete(
                            summary_messages, tools=None, on_delta=_on_forced_delta, is_cancelled=is_cancelled,
                        )
                    else:
                        completion = client.stream_complete(
                            [*messages, force_prompt], tools=None,
                            on_delta=_on_forced_delta, is_cancelled=is_cancelled,
                        )
                    _check_cancelled()
                    raw_forced = (completion.content or forced_text).strip()
                    final_text, _leftover = _parse_dsml_tool_calls(raw_forced)
                    if _leftover:
                        logger.warning(
                            "Stripped %d unparsable DSML tool call(s) from forced final text",
                            len(_leftover),
                        )
                except Exception:
                    logger.exception("Forced final summary failed after tool round limit")

            if not final_text:
                final_text = TOOL_ROUND_LIMIT_MESSAGE

            from trade_compass_agent.runtime.verifier import unverified_execution_claim
            from trade_compass_agent.runtime.report_quality import (
                assess_data_quality, report_facts, report_issues, report_portfolio_facts, report_settlement_facts, retain_rejected_report,
                review_report_evidence,
            )
            def review_call(system, user):
                _check_cancelled()
                review_messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
                if native:
                    def reject_review_tool(_tc):
                        raise AgentTurnError("报告复核阶段不能执行工具")
                    reviewed = client.run_agent(review_messages, tools=[], execute_tool=reject_review_tool,
                        on_delta=lambda _chunk: None, is_cancelled=is_cancelled, max_calls=1)
                else:
                    reviewed = client.stream_complete(review_messages, tools=None,
                        on_delta=lambda _chunk: None, is_cancelled=is_cancelled)
                return reviewed.content or ""

            if publish_after_verification and unverified_execution_claim(final_text, tool_calls_log):
                retain_rejected_report(self.config.data_dir, session.session_id, final_text,
                                       ["成交声明缺少匹配的本轮回执"])
                raise AgentTurnError("报告声称已成交，但缺少本轮成交回执；本轮执行未确认，请核对账本")
            issues = report_issues(final_text, tool_calls_log, reference_date=self.report_date)
            if self.verify_report_grounding and final_text != TOOL_ROUND_LIMIT_MESSAGE:
                try:
                    issues.extend(review_report_evidence(final_text, message, tool_calls_log, review_call))
                except AgentTurnError as exc:
                    retain_rejected_report(self.config.data_dir, session.session_id, final_text, [str(exc)])
                    raise
            if issues:
                retain_rejected_report(self.config.data_dir, session.session_id, final_text, issues)
                correction_limit = 2 if self.verify_report_grounding else 1
                correction_issues = list(issues)
                for correction_index in range(correction_limit):
                    correction = [ChatMessage(role="system", content=(
                        "只校正报告，不执行任何操作。以给定本轮事实修正标的名称、日期、高低价、数值关系和证据缺口，"
                        "保留有依据的结论与数据缺口；不能编造成交或补造数据。只输出最终完整报告，"
                        "仅改已指出的错处，保留其余正确内容；不要新增计算结果，无法核实的派生百分比直接删除并保留原始数值。"
                        "对问题涉及的整句或表格整行重新核对数值、单位、日期和来源归属，不能只替换数字或引用标签。"
                        "新闻中的历史估值与基本面工具的当前估值必须分别表述，不能把工具数值写成新闻曾披露的数值；"
                        "若无法清楚区分来源，删去冲突断言并保留待核实说明。先前指出的问题也不能再次引入。"
                        "历史日期的当日不能改成今日；FIFO净盈亏已扣分配费用，不能用价差乘数量替换。"
                        "不写校正过程、行号、审查步骤或开发说明。")),
                        ChatMessage(role="user", content=json.dumps({"report": final_text,
                            "issues": issues, "previous_issues": correction_issues, "facts": report_facts(tool_calls_log),
                            "portfolio_facts": report_portfolio_facts(tool_calls_log),
                            "settlement_facts": report_settlement_facts(tool_calls_log),
                            "report_date": self.report_date,
                            **({"task": message, "tool_results": tool_calls_log} if self.verify_report_grounding else {
                                "market_tool_results": [(name, raw) for name, raw in tool_calls_log
                                                        if name in {"get_market_pulse", "get_fund_flow"}]
                            })}, ensure_ascii=False))]
                    _check_cancelled()
                    if native:
                        def reject_tool(_tc):
                            raise AgentTurnError("报告校正阶段不能执行工具")
                        corrected = client.run_agent(correction, tools=[], execute_tool=reject_tool,
                            on_delta=lambda _chunk: None, is_cancelled=is_cancelled, max_calls=1)
                    else:
                        corrected = client.stream_complete(correction, tools=None,
                            on_delta=lambda _chunk: None, is_cancelled=is_cancelled)
                    candidate = (corrected.content or "").strip()
                    remaining = report_issues(candidate, tool_calls_log, reference_date=self.report_date)
                    if self.verify_report_grounding and candidate:
                        remaining.extend(review_report_evidence(candidate, message, tool_calls_log, review_call))
                    if not candidate or unverified_execution_claim(candidate, tool_calls_log):
                        retain_rejected_report(self.config.data_dir, session.session_id, candidate,
                                               remaining or ["校正报告为空或成交声明未核实"])
                        raise AgentTurnError("报告中的标的或数值关系未能核实，本轮结果未发布")
                    if not remaining:
                        final_text = candidate
                        break
                    retain_rejected_report(self.config.data_dir, session.session_id, candidate, remaining)
                    if correction_index + 1 == correction_limit:
                        raise AgentTurnError("报告中的标的或数值关系未能核实，本轮结果未发布")
                    final_text, issues = candidate, remaining
                    correction_issues = list(dict.fromkeys([*correction_issues, *remaining]))
            initial_data = ([("analyze_portfolio", json.dumps(self.initial_portfolio))]
                            if self.initial_portfolio is not None else [])
            quality = assess_data_quality([*initial_data, *tool_calls_log], planning_reference_date=self.planning_reference_date)
            if quality.warnings:
                final_text += "\n\n数据可用性：\n" + "\n".join(f"- {w}" for w in quality.warnings)
            if quality.execution_warnings:
                final_text += "\n\n未成交记录：\n" + "\n".join(f"- {w}" for w in quality.execution_warnings)
            if incomplete_reason:
                quality.degraded = True
                quality.warnings.append(incomplete_reason)
                final_text += "\n\n" + incomplete_reason
            final_text += "".join(build_report_evidence(final_text, tool_calls_log))
            if publish_after_verification:
                self._emit("delta", {"text": final_text})

            section_payload = [_section_to_dict(s) for s in sections]
            self.session_store.append(
                session,
                SessionMessageRecord(
                    role="assistant",
                    content=final_text,
                    timestamp=datetime.now(),
                    sections=section_payload or None,
                ),
            )
            response = TurnResponse(
                session_id=session.session_id,
                summary=final_text,
                sections=sections,
                turn_id=turn_id,
                warnings=quality.warnings,
                degraded=quality.degraded,
            )
            published_response = response
            if native_conversation:
                native_conversation.commit(session.messages, context=self.session_store.load_context(session))
            if not quality.degraded:
                from trade_compass_agent.llm.providers import llm_request_deadline
                try:
                    with llm_request_deadline(time.monotonic() + self.config.llm.timeout,
                                              is_cancelled=is_cancelled):
                        _check_cancelled()
                        curate_turn_insight(config=self.config, user_message=message, response=response)
                        _check_cancelled()
                except LLMExecutionCancelled:
                    _check_cancelled()  # User stop is distinct from parent workflow expiry.
                    raise
            self._nudge.on_user_turn()
            review_messages = messages + [ChatMessage(role="assistant", content=final_text)]
            try:
                if not quality.degraded:
                    self._maybe_background_review(review_messages, source_session_id=session.session_id, source_turn_id=turn_id)
            except Exception:
                logger.exception("Analysis completed; background review dispatch failed")
            self._update_session_summary(session, tool_calls_log, final_text)
            self._emit(
                "done",
                {
                    "session_id": response.session_id,
                    "summary": response.summary,
                    "sections": section_payload,
                    "ok": True,
                    "turn_id": turn_id,
                    "warnings": quality.warnings,
                    "degraded": quality.degraded,
                },
            )
            return response
        except (TurnInterruptedError, LLMExecutionCancelled) as exc:
            if isinstance(exc, LLMExecutionCancelled) and not (is_cancelled and is_cancelled()):
                if published_response is None:
                    self.session_store.append(session, SessionMessageRecord(
                        role="assistant", content="任务已超时或停止，已有记录已保留。", timestamp=datetime.now()))
                raise
            section_payload = [_section_to_dict(s) for s in sections]
            if published_response is not None:
                # The main answer already exists in history and its native cursor.
                # Stopping auxiliary learning must not append a second copy of it.
                response = replace(published_response, interrupted=True)
                self._update_session_summary(session, tool_calls_log, final_text)
            else:
                summary = _interrupt_summary(getattr(exc, "partial", "") or final_text)
                self.session_store.append(
                    session,
                    SessionMessageRecord(
                        role="assistant",
                        content=summary,
                        timestamp=datetime.now(),
                        sections=section_payload or None,
                    ),
                )
                response = TurnResponse(
                    session_id=session.session_id,
                    summary=summary,
                    sections=sections,
                    turn_id=turn_id,
                    interrupted=True,
                )
            self._emit(
                "interrupted",
                {
                    "session_id": response.session_id,
                    "summary": response.summary,
                    "sections": section_payload,
                    "ok": False,
                    "turn_id": turn_id,
                },
            )
            return response
        except Exception as exc:
            error_summary = str(exc) if isinstance(exc, AttachmentInputError) else f"分析过程中出现错误：{type(exc).__name__}"
            self.session_store.append(
                session,
                SessionMessageRecord(
                    role="assistant",
                    content=error_summary,
                    timestamp=datetime.now(),
                ),
            )
            raise


INTERRUPT_PLACEHOLDER = "[已停止]"


def _interrupt_summary(partial: str) -> str:
    text = partial.strip()
    if not text:
        return INTERRUPT_PLACEHOLDER
    if text.endswith("（已停止）"):
        return text
    return f"{text}\n\n（已停止）"


def _section_to_dict(section: TurnSection) -> dict:
    d: dict = {
        "title": section.title,
        "content": section.content,
        "specialist": section.specialist,
        "symbols": list(section.symbols),
        "kind": section.kind,
    }
    if section.forecast_data:
        d["forecast_data"] = section.forecast_data
    return d


def _section_from_tool(tool_name: str, result: str) -> TurnSection | None:
    if tool_name == "get_bars":
        return _compact_bars_summary(result)
    if tool_name == "get_market_pulse":
        return _compact_pulse_summary(result)
    if tool_name == "kline_forecast":
        return _compact_forecast_summary(result)
    return None


def _compact_bars_summary(result: str) -> TurnSection | None:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("error"):
        return None
    symbol = str(payload.get("symbol") or "")
    timeframe = str(payload.get("timeframe") or "1d")
    count = payload.get("count")
    bars = payload.get("bars") or []
    if not bars:
        return None
    latest = bars[-1]
    close = latest.get("close")
    if close is None:
        return None
    count_text = f"{count} 根" if isinstance(count, int) else f"{len(bars)} 根"
    return TurnSection(
        title=f"{symbol} 行情" if symbol else "行情",
        content=f"{symbol} {timeframe} {count_text}，最新收盘 {close}",
        symbols=[symbol] if symbol else [],
        kind="summary",
    )


def _compact_pulse_summary(result: str) -> TurnSection | None:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("error"):
        return None
    parts: list[str] = []
    limit_up = payload.get("limit_up") or {}
    count = limit_up.get("count")
    if isinstance(count, int):
        parts.append(f"涨停 {count} 家")
    sectors = payload.get("sectors") or []
    if sectors:
        top = sectors[0]
        name = top.get("name")
        change = top.get("change_pct")
        if name is not None and change is not None:
            parts.append(f"领涨 {name} {change:+.2f}%")
    if not parts:
        return None
    return TurnSection(title="市场脉搏", content="，".join(parts), kind="summary")


def _compact_forecast_summary(result: str) -> TurnSection | None:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("error"):
        return None
    symbol = str(payload.get("symbol") or "")
    if not symbol:
        return None
    summary = payload.get("forecast_summary") or {}
    change_pct = summary.get("change_pct", 0)
    direction = summary.get("direction", "sideways")
    direction_cn = {"up": "看涨", "down": "看跌", "sideways": "横盘"}.get(direction, direction)
    horizon = payload.get("horizon_bars", "?")
    content = f"{symbol} K线预测 {change_pct:+.2f}% {direction_cn}，预测 {horizon} 根"
    forecast_data = {
        "symbol": symbol,
        "forecast_bars": payload.get("forecast_bars") or [],
        "confidence_band": payload.get("confidence_band") or {},
        "model": payload.get("model") or "",
        "quality_status": payload.get("quality_status") or "experimental",
        "parameters": payload.get("parameters") or {},
    }
    return TurnSection(
        title=f"{symbol} K线预测",
        content=content,
        symbols=[symbol],
        kind="summary",
        forecast_data=forecast_data,
    )


import re

_DIRECTIONAL_PATTERN = re.compile(
    r"(买入|卖出|加仓|减仓|建仓|清仓|做多|做空|追涨|抄底|建议介入|可以买|适合买|建议卖)",
)
_SYMBOL_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])(\d{6})(?![A-Za-z0-9_]|\.\d)")


def _instrument_mentions(text: str) -> set[str]:
    """Keep instrument codes while excluding explicitly labelled model budgets."""
    numbers = r"\d+(?:\s*[/、,，]\s*\d+)*(?:\s*(?:逐轮)?(?:增至|至)\s*\d+)?"
    budgets = [rf"{numbers}\s*(?:输入\s*)?(?:tokens?|字符|窗口)",
               rf"(?:估算输入|输入\s*tokens?|输出预留|已加载窗口|上下文窗口|窗口上限|窗口)"
               rf"\s*(?:tokens?\s*)?(?:分别为|固定为?|为|从|[:：=])?\s*{numbers}"]
    for pattern in budgets:
        text = re.sub(pattern, lambda match: re.sub(r"\d", " ", match[0]), text, flags=re.IGNORECASE)
    return set(_SYMBOL_PATTERN.findall(text))


def build_report_evidence(analysis_text: str, tool_calls_log: list[tuple[str, str]]) -> list[str]:
    """Check the selected analysis before adding factual receipts to its text."""
    from trade_compass_agent.runtime.verifier import trade_receipt_section
    return [part for part in (trade_receipt_section(tool_calls_log),
            _check_data_gap(analysis_text, tool_calls_log), _build_provenance_footer(tool_calls_log)) if part]


def _check_data_gap(final_text: str, tool_calls_log: list[tuple[str, str]]) -> str | None:
    """If directional advice is given without supporting get_bars data, return a warning."""
    if not _DIRECTIONAL_PATTERN.search(final_text):
        return None
    symbols_mentioned = _instrument_mentions(final_text)
    if not symbols_mentioned:
        return None
    symbols_with_bars = set()
    for tool_name, result in tool_calls_log:
        if tool_name in ("get_bars", "kline_forecast", "batch_get_bars"):
            try:
                payload = json.loads(result)
                if not isinstance(payload, dict) or payload.get("error") or payload.get("ok") is False:
                    continue
                if tool_name == "batch_get_bars":
                    for sym, item in (payload.get("results") or {}).items():
                        if sym not in (payload.get("errors") or {}) and _has_bar_evidence(item):
                            symbols_with_bars.add(str(sym).strip())
                else:
                    sym = str(payload.get("symbol", "")).strip()
                    if sym and (_has_bar_evidence(payload) or (
                        tool_name == "kline_forecast" and payload.get("forecast_bars")
                    )):
                        symbols_with_bars.add(sym)
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
        elif tool_name == "dispatch_specialists":
            # Specialists internally fetch bars; extract covered symbols from output
            symbols_with_bars.update(_extract_symbols_from_specialist_result(result))
    ungrounded = symbols_mentioned - symbols_with_bars
    if ungrounded:
        return (
            f"\n\n⚠️ 数据覆盖不足：以下标的未获取到 K 线数据，"
            f"上述方向性建议仅供参考：{'、'.join(sorted(ungrounded))}"
        )
    return None


def _has_bar_evidence(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("error") or payload.get("ok") is False:
        return False
    if payload.get("bars_count", payload.get("count")) == 0:
        return False
    bars = payload.get("bars")
    if isinstance(bars, list):
        return bool(bars) and all(isinstance(b, dict) and _positive_price(b.get("close", b.get("C"))) for b in bars)
    count = payload.get("bars_count", 0)
    return isinstance(count, int) and count > 0 and _positive_price(payload.get("close"))


def _positive_price(value: object) -> bool:
    import math
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _extract_symbols_from_specialist_result(result: str) -> set[str]:
    """Extract stock symbols covered by specialist sub-calls."""
    covered: set[str] = set()
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return covered
    results_list = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results_list, list):
        return covered
    for item in results_list:
        if not isinstance(item, dict):
            continue
        output = item.get("output", "")
        task = item.get("task", "")
        # Symbols mentioned in task are considered covered if the specialist succeeded
        if output and '"error"' not in output[:200]:
            covered.update(_SYMBOL_PATTERN.findall(task))
            # Also extract from structured signal outputs
            try:
                out_parsed = json.loads(output)
                if isinstance(out_parsed, dict):
                    signals = out_parsed.get("signals", [])
                    if isinstance(signals, list):
                        for sig in signals:
                            if isinstance(sig, dict):
                                sym = sig.get("symbol", "")
                                if sym:
                                    covered.add(str(sym).strip())
            except (json.JSONDecodeError, TypeError):
                # Markdown report — extract 6-digit codes from output
                covered.update(_SYMBOL_PATTERN.findall(output[:5000]))
    return covered


def _build_provenance_footer(tool_calls_log: list[tuple[str, str]]) -> str:
    """Build a data-provenance footer from tool calls made during this turn."""
    if not tool_calls_log:
        return ""
    tools_used = []
    providers = set()
    latest_ts = None
    for tool_name, result in tool_calls_log:
        if tool_name not in tools_used:
            tools_used.append(tool_name)
        try:
            payload = json.loads(result)
            if isinstance(payload, dict):
                if tool_name == "batch_get_bars":
                    for item in (payload.get("results") or {}).values():
                        if _has_bar_evidence(item):
                            if item.get("source"):
                                providers.add(str(item["source"]))
                            ts = item.get("as_of")
                            if ts and (latest_ts is None or str(ts) > latest_ts):
                                latest_ts = str(ts)
                prov = payload.get("provider") or payload.get("provider_name")
                if prov:
                    providers.add(str(prov))
                ts = payload.get("timestamp")
                if ts and (latest_ts is None or str(ts) > latest_ts):
                    latest_ts = str(ts)
        except (json.JSONDecodeError, AttributeError):
            pass
    if not tools_used:
        return ""
    parts = []
    if providers:
        parts.append(f"数据来源：{', '.join(sorted(providers))}")
    if latest_ts:
        parts.append(f"数据时间：{latest_ts[:16]}")
    parts.append(f"工具：{', '.join(tools_used)}")
    return "\n\n---\n" + " | ".join(parts)
