"""Consolidation worker — promotes Working observations to Episodic/Semantic tiers.

Runs as a daemon thread, periodically:
1. Reads unconsolidated observations from ObservationStore
2. Groups them by session
3. Compresses groups into session summaries (Episodic tier)
4. Extracts stable facts and proposes Semantic writes (via LLM)
5. Marks observations as consolidated

Design: fire-and-forget daemon, tolerates failures gracefully.
Consolidation moves observations through bounded memory tiers.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from typing import Callable

from trade_compass_agent.memory.observation_store import ObservationStore, Observation
from trade_compass_agent.memory.session_summary_store import SessionSummaryStore
from trade_compass_agent.memory.memory_store import MemoryStore

logger = logging.getLogger(__name__)

CONSOLIDATION_INTERVAL_SECONDS = 300  # 5 minutes
MIN_OBSERVATIONS_TO_CONSOLIDATE = 3
MAX_BATCH_SIZE = 30

CONSOLIDATION_PROMPT = """\
You are a memory consolidation agent for a trading assistant.

Given a batch of recent observations (tool results captured during trading sessions),
write a 1-2 sentence summary of what was discussed/analyzed.
Focus on: symbols mentioned, market conditions observed, decisions made.

Respond in JSON:
{
  "session_summary": "..."
}

Observations:
"""


class ConsolidationWorker:
    """Daemon thread that consolidates Working → Episodic/Semantic."""

    def __init__(
        self,
        obs_store: ObservationStore,
        session_store: SessionSummaryStore,
        memory_store: MemoryStore,
        llm_call: Callable[[str, str], str] | None = None,
    ) -> None:
        self._obs = obs_store
        self._sessions = session_store
        self._memory = memory_store
        self._llm_call = llm_call
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the consolidation daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="consolidation-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Consolidation worker started")

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._stop_event.set()

    def run_once(self) -> int:
        """Run a single consolidation pass. Returns number of observations processed."""
        all_observations = self._obs.unconsolidated(limit=MAX_BATCH_SIZE)
        # Mark low-importance as consolidated without processing
        low_importance = [obs for obs in all_observations if obs.importance < 5]
        if low_importance:
            self._obs.mark_consolidated([obs.id for obs in low_importance])

        observations = [obs for obs in all_observations if obs.importance >= 5]
        if len(observations) < MIN_OBSERVATIONS_TO_CONSOLIDATE:
            return 0

        # Group by session
        by_session: dict[str, list[Observation]] = defaultdict(list)
        for obs in observations:
            by_session[obs.session_id].append(obs)

        processed = 0
        for session_id, session_obs in by_session.items():
            try:
                self._consolidate_session(session_id, session_obs)
                processed += len(session_obs)
            except Exception as exc:
                logger.warning("Consolidation failed for session %s: %s", session_id, exc)

        return processed

    def _consolidate_session(self, session_id: str, observations: list[Observation]) -> None:
        """Consolidate a batch of observations from one session."""
        obs_text = "\n".join(
            f"- [{obs.tool_name}] {obs.summary}" for obs in observations
        )

        if self._llm_call:
            try:
                result = self._llm_call(
                    CONSOLIDATION_PROMPT,
                    obs_text,
                )
                self._process_llm_result(session_id, observations, result)
            except Exception as exc:
                logger.warning("LLM consolidation failed: %s", exc)
                self._fallback_consolidation(session_id, observations)
        else:
            self._fallback_consolidation(session_id, observations)

        # Mark as consolidated
        self._obs.mark_consolidated([obs.id for obs in observations])

    def _process_llm_result(
        self, session_id: str, observations: list[Observation], raw: str
    ) -> None:
        """Parse LLM response and write to Episodic/Semantic tiers."""
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = raw.strip()
            if json_str.startswith("```"):
                lines = json_str.split("\n")
                json_str = "\n".join(lines[1:-1])
            data = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            self._fallback_consolidation(session_id, observations)
            return

        # Update Episodic tier
        summary = data.get("session_summary", "")
        if summary:
            tools = list(dict.fromkeys(obs.tool_name for obs in observations))
            self._sessions.upsert(
                session_id=session_id,
                summary=summary,
                turn_count=len(observations),
                tools_used=tools,
                started_at=observations[0].created_at if observations else None,
            )

        # NOTE: Semantic tier writes removed in v3.0 memory redesign.
        # Facts are no longer auto-promoted here. Promotion to KNOWLEDGE.md
        # requires passing through SemanticWriteGate (weekly curator only).

    def _fallback_consolidation(self, session_id: str, observations: list[Observation]) -> None:
        """Simple concatenation fallback when LLM unavailable."""
        summaries = [obs.summary for obs in observations[:10]]
        combined = "; ".join(s[:100] for s in summaries)
        tools = list(dict.fromkeys(obs.tool_name for obs in observations))
        self._sessions.upsert(
            session_id=session_id,
            summary=combined[:500],
            turn_count=len(observations),
            tools_used=tools,
            started_at=observations[0].created_at if observations else None,
        )

    def _run_loop(self) -> None:
        """Main daemon loop."""
        while not self._stop_event.is_set():
            try:
                processed = self.run_once()
                if processed:
                    logger.info("Consolidated %d observations", processed)
                # Also cleanup old observations
                self._obs.cleanup_old()
            except Exception as exc:
                logger.error("Consolidation error: %s", exc, exc_info=True)

            self._stop_event.wait(timeout=CONSOLIDATION_INTERVAL_SECONDS)


_singleton: ConsolidationWorker | None = None
_singleton_lock = threading.Lock()


def shared_consolidation_worker(
    obs_store: ObservationStore,
    session_store: SessionSummaryStore,
    memory_store: MemoryStore,
    llm_call: Callable[[str, str], str] | None = None,
) -> ConsolidationWorker:
    """Process-wide consolidation daemon (one thread regardless of AgentLoop count)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ConsolidationWorker(
                obs_store=obs_store,
                session_store=session_store,
                memory_store=memory_store,
                llm_call=llm_call,
            )
            _singleton.start()
        return _singleton
