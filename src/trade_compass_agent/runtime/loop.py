from __future__ import annotations

import json
import time
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, field

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.runtime.exceptions import TurnInterruptedError
from trade_compass_agent.runtime.activity_events import (
    build_tool_end_payload,
    build_tool_start_payload,
)
from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
from trade_compass_agent.runtime.context import ContextBuilder, _sanitize_tool_calls
from trade_compass_agent.runtime.compression.budget import TokenBudget
from trade_compass_agent.runtime.compression.trim import trim_tool_results
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
from trade_compass_agent.runtime.background_review import ReviewNudgeTracker, spawn_background_review
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
) -> dict[str, str]:
    """Execute tool calls with selective parallelism.

    Tier A (PARALLEL_SAFE_TOOLS) run concurrently via a thread pool.
    All other tools run sequentially in their original order, after
    the parallel batch completes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, str] = {}

    parallel = [(i, tc) for i, tc in enumerate(tool_calls) if tc.name in PARALLEL_SAFE_TOOLS]
    sequential = [(i, tc) for i, tc in enumerate(tool_calls) if tc.name not in PARALLEL_SAFE_TOOLS]

    def _run_parallel(tc):
        try:
            result = run_with_timeout(
                lambda: tools.execute(tc.name, tc.arguments),
                timeout=_PARALLEL_TOOL_TIMEOUT_SECONDS,
                description=f"tool:{tc.name}",
            )
        except TimeoutError as exc:
            result = json.dumps({"error": str(exc), "timed_out": True}, ensure_ascii=False)
        return tc.id, result

    if len(parallel) > 1:
        workers = min(_MAX_PARALLEL_WORKERS, len(parallel))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool_par") as pool:
            futures = {pool.submit(_run_parallel, tc): tc for _, tc in parallel}
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
    skill_actor: str = "user"
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

    def _maybe_background_review(self, messages: list) -> None:
        """Check nudge thresholds and spawn background review if needed."""
        review_memory, review_skills = self._nudge.should_review()
        if not review_memory and not review_skills:
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

        snapshot = [
            {"role": getattr(m, "role", "user"), "content": getattr(m, "content", str(m))}
            for m in messages[-20:]
            if getattr(m, "role", "user") != "tool"
            and not (getattr(m, "role", "") == "assistant" and not getattr(m, "content", ""))
        ]

        spawn_background_review(
            messages_snapshot=snapshot,
            review_memory=review_memory,
            review_skills=review_skills,
            llm_call=llm_call,
            memory_write=memory_write,
            skill_manage=skill_manage,
            memory_store=self._memory_store,
            config=self.config,
        )
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
        def _check_cancelled() -> None:
            if is_cancelled and is_cancelled():
                raise TurnInterruptedError(final_text)

        client = create_chat_client(self.config)
        self._nudge.reset_turn_flags()
        skills_cfg = load_agent_skills_config()
        skills = discover_skills(memory_dir=self.config.memory_dir, skills_config=skills_cfg)
        context = ContextBuilder(
            memory_dir=self.config.memory_dir,
            skills=skills,
            skills_config=skills_cfg,
            memory_store=self._memory_store,
            rules_enabled=self.config.rules.enabled,
            rules_char_limit=self.config.rules.char_limit,
            compression_config=self.config.context_compression,
        )
        session = self.session_store.get_or_create(session_id)
        if turn_id:
            self._emit(
                "turn_started",
                {"turn_id": turn_id, "session_id": session.session_id},
            )
        enriched = enrich_user_message(
            message,
            parse_attachments(attachments),
            config=self.config,
            memory_dir=self.config.memory_dir,
        )
        history = [m.to_chat_message() for m in self.session_store.load_context(session)]
        messages = context.build_messages(history, enriched)

        # Phase 0: token budget check — Phase 1 trim (in ContextBuilder) + Phase 2 summarize
        _budget = TokenBudget(self.config)
        if _budget.enabled:
            _est_tokens = _budget.estimate(
                messages, tools_schemas=self._tools.schemas,
                system_prompt_tokens=context.system_prompt_tokens,
            )
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
                        return client.complete(msgs).content or ""

                    compressed, _summary_text, summarized, saved = summarize_middle_turns(
                        messages,
                        llm_call=_summarize_llm,
                        protect_recent_count=_budget.protect_recent_count,
                        protect_recent_tokens=_budget.protect_recent_tokens,
                        previous_summary=latest_persisted_summary(history),
                    )
                    if summarized > 0:
                        compacted_messages = _sanitize_tool_calls(compressed)
                        if not compacted_messages or compacted_messages[-1].role != "user":
                            raise RuntimeError("compacted context lost the current user message")
                        durable_history = [
                            SessionMessageRecord(
                                role=item.role,
                                content=item.content or "",
                                timestamp=datetime.now(),
                                tool_call_id=item.tool_call_id,
                                name=item.name,
                                tool_calls=list(item.tool_calls or []),
                            )
                            for item in compacted_messages[1:-1]
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

        user_record = SessionMessageRecord(role="user", content=enriched, timestamp=datetime.now())
        self.session_store.append(session, user_record)
        if not session.title:
            user_count = sum(1 for item in session.messages if item.role == "user")
            if user_count == 1:
                if self.config.agent.llm_session_titles:
                    self.session_store.set_title(
                        session,
                        suggest_session_title(message, self.config),
                    )
                else:
                    self.session_store.maybe_set_title_from_first_message(session, message)

        sections: list[TurnSection] = []
        final_text = ""
        max_rounds = self.config.agent.max_tool_rounds
        tool_calls_log: list[tuple[str, str]] = []

        try:
            for _round in range(max_rounds):
                _check_cancelled()
                self._emit("status", {"text": "思考中…"})

                def _on_delta(chunk: str) -> None:
                    nonlocal final_text
                    final_text += chunk
                    self._emit("delta", {"text": chunk})

                is_last_round = (_round == max_rounds - 1)
                completion = client.stream_complete(
                    messages,
                    tools=None if is_last_round else self._tools.schemas,
                    on_delta=_on_delta,
                    is_cancelled=is_cancelled,
                )
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
                        self._nudge.on_tool_iteration()
                        if should_capture(tc.name, result):
                            summary = extract_summary(tc.name, result)
                            from trade_compass_agent.memory.observation_store import estimate_importance, extract_concepts
                            self._obs_store.append(
                                session_id=session.session_id,
                                tool_name=tc.name,
                                summary=summary,
                                raw_preview=result[:2000],
                                importance=estimate_importance(tc.name, summary),
                                concepts=extract_concepts(summary),
                            )
                        if tc.name == "write_knowledge":
                            self._nudge.on_memory_write()
                        elif tc.name == "skill_manage":
                            self._nudge.on_skill_write()
                        section = _section_from_tool(tc.name, result)
                        if section:
                            sections.append(section)
                            self._emit("section", _section_to_dict(section))
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
                        "工具调用轮次已耗尽。不要再调用工具。"
                        "请只基于本轮用户问题、已有上下文和上方所有 tool 结果，直接输出最终结论。"
                        "必须给出可读的总结，不要写“请查看上方工具输出”。"
                    ),
                )
                forced_text = ""

                def _on_forced_delta(chunk: str) -> None:
                    nonlocal forced_text
                    forced_text += chunk
                    self._emit("delta", {"text": chunk})

                try:
                    completion = client.stream_complete(
                        [*messages, force_prompt],
                        tools=None,
                        on_delta=_on_forced_delta,
                        is_cancelled=is_cancelled,
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

            gap_warning = _check_data_gap(final_text, tool_calls_log)
            if gap_warning:
                final_text += gap_warning
            provenance = _build_provenance_footer(tool_calls_log)
            if provenance:
                final_text += provenance

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
            )
            curate_turn_insight(config=self.config, user_message=message, response=response)
            self._nudge.on_user_turn()
            self._maybe_background_review(messages)
            self._update_session_summary(session, tool_calls_log, final_text)
            self._emit(
                "done",
                {
                    "session_id": response.session_id,
                    "summary": response.summary,
                    "sections": section_payload,
                    "ok": True,
                    "turn_id": turn_id,
                },
            )
            return response
        except TurnInterruptedError as exc:
            summary = _interrupt_summary(exc.partial or final_text)
            section_payload = [_section_to_dict(s) for s in sections]
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
            error_text = str(exc)
            # Reactive compression: context overflow → trim and retry once
            if TokenBudget.is_context_overflow_error(error_text) and _budget.enabled:
                logger.warning(
                    "context overflow detected, attempting aggressive trim: %s",
                    type(exc).__name__,
                )
                try:
                    system_msg = messages[0] if messages and messages[0].role == "system" else None
                    middle = messages[1:] if system_msg else messages
                    trimmed, pruned, saved = trim_tool_results(
                        middle,
                        protect_recent_count=5,   # aggressive: only keep 5 recent
                        protect_recent_tokens=4000,  # ~4K token tail
                        protect_head_count=1,
                    )
                    if pruned > 0:
                        logger.info(
                            "emergency trim: %d messages pruned, ~%s chars saved; retrying",
                            pruned, f"{saved:,}",
                        )
                        self._emit("status", {"text": "上下文过长，压缩后重试…"})
                        messages = [system_msg] + trimmed if system_msg else trimmed
                        if system_msg is None and messages[0].role != "system":
                            messages.insert(0, ChatMessage(role="system", content=context.system_prompt))
                        # Retry the entire tool-calling loop with compressed context
                        return self.run_turn(
                            message,
                            session_id=session_id,
                            attachments=attachments,
                            turn_id=turn_id,
                            is_cancelled=is_cancelled,
                        )
                except Exception as trim_err:
                    logger.error("emergency trim failed: %s", trim_err)

            error_summary = f"分析过程中出现错误：{type(exc).__name__}"
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
_SYMBOL_PATTERN = re.compile(r"\b(\d{6})\b")


def _check_data_gap(final_text: str, tool_calls_log: list[tuple[str, str]]) -> str | None:
    """If directional advice is given without supporting get_bars data, return a warning."""
    if not _DIRECTIONAL_PATTERN.search(final_text):
        return None
    symbols_mentioned = set(_SYMBOL_PATTERN.findall(final_text))
    if not symbols_mentioned:
        return None
    symbols_with_bars = set()
    for tool_name, result in tool_calls_log:
        if tool_name in ("get_bars", "kline_forecast"):
            try:
                payload = json.loads(result)
                sym = str(payload.get("symbol", ""))
                if sym and not payload.get("error"):
                    symbols_with_bars.add(sym.strip())
            except (json.JSONDecodeError, AttributeError):
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
