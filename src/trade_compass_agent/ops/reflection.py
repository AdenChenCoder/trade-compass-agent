"""Deferred Reflection — cross-run learning for scheduled jobs.

Flow:
1. After a job runs → store a pending reflection with predictions/outputs
2. Before the next run of the same job → resolve pending reflections
   by comparing predictions against actual market data
3. Inject resolved reflections into the Agent context for the new run
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.memory.memory_store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class PendingReflection:
    job_id: str
    run_id: str
    run_date: str
    predictions: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    created_at: str = ""


@dataclass
class ResolvedReflection:
    job_id: str
    run_id: str
    run_date: str
    predictions: dict[str, Any] = field(default_factory=dict)
    actuals: dict[str, Any] = field(default_factory=dict)
    lesson: str = ""
    resolved_at: str = ""


class JobReflection:
    """Manages pending and resolved reflections for a job.

    Storage: {memory_dir}/reflections/{job_id}/pending.jsonl + resolved.jsonl
    """

    def __init__(self, memory_dir: Path) -> None:
        self.base_dir = memory_dir / "reflections"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _job_dir(self, job_id: str) -> Path:
        d = self.base_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def store_pending(
        self,
        job_id: str,
        run_id: str,
        *,
        predictions: dict[str, Any] | None = None,
        summary: str = "",
        run_date: date | None = None,
    ) -> None:
        d = run_date or date.today()
        pending = PendingReflection(
            job_id=job_id,
            run_id=run_id,
            run_date=d.isoformat(),
            predictions=predictions or {},
            summary=summary,
            created_at=datetime.now().isoformat(),
        )
        path = self._job_dir(job_id) / "pending.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(pending), ensure_ascii=False) + "\n")
        logger.debug("Stored pending reflection for %s run %s", job_id, run_id)

    def resolve_pending(
        self,
        job_id: str,
        resolve_fn: ResolveFunction | None = None,
        *,
        mem_store: "MemoryStore | None" = None,
        config: "AppConfig | None" = None,
    ) -> list[ResolvedReflection]:
        """Resolve all pending reflections for a job.

        resolve_fn(pending) -> (actuals_dict, lesson_str) or None to skip.
        If no resolve_fn, reflections are auto-resolved with empty actuals.
        """
        pending_path = self._job_dir(job_id) / "pending.jsonl"
        if not pending_path.exists():
            return []

        lines = pending_path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return []

        resolved_list: list[ResolvedReflection] = []
        remaining: list[str] = []

        _pending_fields = {f.name for f in PendingReflection.__dataclass_fields__.values()}
        for line in lines:
            raw = json.loads(line)
            filtered = {k: v for k, v in raw.items() if k in _pending_fields}
            pending = PendingReflection(**filtered)

            if resolve_fn:
                result = resolve_fn(pending)
                if result is None:
                    remaining.append(line)
                    continue
                actuals, lesson = result
            else:
                actuals, lesson = {}, f"Auto-resolved: {pending.summary}"

            feedback_actuals = actuals
            if mem_store is not None and config is not None:
                gov = config.memory.governance
                if gov.outcome_advisor_enabled:
                    from trade_compass_agent.ops.outcome_feedback import enrich_actuals_with_outcome_advisor

                    try:
                        from trade_compass_agent.llm.providers import ChatMessage, create_chat_client

                        client = create_chat_client(config)

                        def _llm_call(system_prompt: str, user_content: str) -> str:
                            return client.complete([
                                ChatMessage(role="system", content=system_prompt),
                                ChatMessage(role="user", content=user_content),
                            ]).content or ""

                        feedback_actuals = enrich_actuals_with_outcome_advisor(
                            pending,
                            actuals,
                            lesson,
                            _llm_call,
                            max_candidates=gov.outcome_advisor_max_candidates,
                        )
                    except Exception as exc:
                        logger.debug("Outcome advisor unavailable for %s/%s: %s", pending.job_id, pending.run_id, exc)

            resolved = ResolvedReflection(
                job_id=pending.job_id,
                run_id=pending.run_id,
                run_date=pending.run_date,
                predictions=pending.predictions,
                actuals=feedback_actuals,
                lesson=lesson,
                resolved_at=datetime.now().isoformat(),
            )
            if mem_store is not None and config is not None:
                from trade_compass_agent.ops.outcome_feedback import apply_outcome_feedback

                apply_outcome_feedback(pending, feedback_actuals, lesson, mem_store, config)
            resolved_list.append(resolved)

        # Write resolved and index to chunks.db for search_memory
        if resolved_list:
            resolved_path = self._job_dir(job_id) / "resolved.jsonl"
            with open(resolved_path, "a", encoding="utf-8") as f:
                for r in resolved_list:
                    f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
            self._index_resolved(job_id, resolved_list)

        # Update pending (keep unresolved)
        pending_path.write_text(
            "\n".join(remaining) + ("\n" if remaining else ""),
            encoding="utf-8",
        )

        logger.info("Resolved %d reflections for %s (%d still pending)", len(resolved_list), job_id, len(remaining))
        return resolved_list

    def get_context(self, job_id: str, *, limit: int = 5, sanitize: bool = True) -> str:
        """Get recent resolved reflections as a text context for Agent injection."""
        resolved_path = self._job_dir(job_id) / "resolved.jsonl"
        if not resolved_path.exists():
            return ""

        lines = resolved_path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return ""

        recent = lines[-limit:]
        parts = []
        prefix = "[历史-待验证] " if sanitize else ""
        for line in recent:
            r = json.loads(line)
            lesson = r.get("lesson", "")
            entry = f"[{r['run_date']}] {prefix}{lesson}"
            if r.get("predictions"):
                entry += f" | 预测: {json.dumps(r['predictions'], ensure_ascii=False)}"
            if r.get("actuals"):
                entry += f" | 实际: {json.dumps(r['actuals'], ensure_ascii=False)}"
            parts.append(entry)

        return "\n".join(parts) + (
            "\n\n[注：历史反思中的「减1/3」「减半仓」「考虑止盈」等表述仅为意图；"
            "止盈禁止仅凭涨幅判断，须 load_skill(contextual-take-profit) 8维评分；"
            "执行前须查 analyze_portfolio.rebalance_hint（止损）或 exit_review（止盈审查）验证可执行股数]"
        )

    def _index_resolved(self, job_id: str, resolved: list[ResolvedReflection]) -> None:
        """Index resolved reflection lessons into chunks.db for search_memory."""
        try:
            from trade_compass_agent.memory.tree.search import MemorySearchIndex
            index = MemorySearchIndex(self.base_dir.parent / "tree" / "chunks.db")
            for r in resolved:
                if not r.lesson:
                    continue
                content = f"[{r.run_date}] {r.lesson}"
                path = self._job_dir(job_id) / "resolved.jsonl"
                index.index_file(f"reflection-{job_id}", content, path)
        except Exception as exc:
            logger.debug("Failed to index reflections to chunks.db: %s", exc)

    def pending_count(self, job_id: str) -> int:
        path = self._job_dir(job_id) / "pending.jsonl"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").strip().splitlines())

    def clear(self, job_id: str) -> None:
        """Clear all reflections for a job (for testing)."""
        d = self._job_dir(job_id)
        for f in d.glob("*.jsonl"):
            f.unlink()


ResolveFunction = Any  # Callable[[PendingReflection], tuple[dict, str] | None]
