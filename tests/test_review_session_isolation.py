"""Each review run gets fresh operational context while old history survives."""

import asyncio
from datetime import date

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.agent_session import ScheduledAgentSession
from trade_compass_agent.ops.job_definition import StepContext
from trade_compass_agent.runtime.tools import builtin_operations


@pytest.mark.parametrize(
    "job,step,operation",
    [
        ("postmarket", "agent_daily_journal", builtin_operations.agent_daily_journal),
        ("weekly", "agent_strategy_review", builtin_operations.agent_strategy_review),
        ("eod_review", "agent_eod_reflection", builtin_operations.agent_eod_reflection),
        ("close", "agent_close_analysis", builtin_operations.agent_close_analysis),
        ("premarket", "agent_briefing", builtin_operations.agent_premarket_briefing),
    ],
)
@pytest.mark.parametrize("identities", [("first-run", "second-run"), ("", "")])
def test_reviews_have_run_scoped_context_and_keep_legacy_history(
    tmp_path, monkeypatch, job, step, operation, identities
):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    legacy = config.data_dir / "agent_sessions" / f"scheduler-{job}-{step}-2026-10-21.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy history must remain accessible\n")
    seen = []

    def run(self, prompt, *, timeout):
        seen.append(self.session_id)
        return "fresh review"

    monkeypatch.setattr(ScheduledAgentSession, "run", run)
    outputs = [
        asyncio.run(operation(StepContext(config=config, date=date(2026, 10, 21), run_id=identity)))
        for identity in identities
    ]
    assert len(set(seen)) == 2
    assert all(
        value.startswith(f"scheduler-{job}-{step}-") and value.endswith("-2026-10-21")
        for value in seen
    )
    if all(identities):
        assert seen == [f"scheduler-{job}-{step}-{identity}-2026-10-21" for identity in identities]
    assert [value.data["session_id"] for value in outputs] == seen
    assert legacy.read_text() == "legacy history must remain accessible\n"
