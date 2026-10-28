import asyncio
import json
from datetime import date

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.job_definition import StepContext, StepOutput
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime.tools import builtin_operations as ops


def config_with_runs(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    with store._conn() as connection:
        for identity, status, stamp in [
            ("failed-intraday", "timed_out", "2026-10-21T14:35:00"),
            ("previous-day", "completed", "2026-10-20T15:30:00+00:00"),
            ("local-next-day", "failed", "2026-10-20T16:30:00+00:00"),
            ("current-review", "running", "2026-10-21T16:30:00"),
        ]:
            connection.execute("INSERT INTO job_runs(id,job_id,status,started_at,created_at) VALUES (?,?,?,?,?)",
                               (identity, "autonomous_trading", status, stamp, stamp))
    return config


def test_execution_context_uses_scheduler_day_and_preserves_statuses(tmp_path):
    config = config_with_runs(tmp_path)
    ctx = StepContext(config=config, date=date(2026, 10, 21), run_id="current-review")
    facts = ops._review_execution_context(ctx, days=1)
    assert facts["available"] is True
    assert {row["run_id"] for row in facts["recent_runs"]} == {"failed-intraday", "local-next-day"}
    assert facts["by_job"]["autonomous_trading"] == {"failed": 1, "timed_out": 1}
    assert facts["total_runs"] == 2 and facts["recent_runs_truncated"] is False
    assert facts["status_counts"] == {"failed": 1, "timed_out": 1}


def test_review_total_is_not_the_truncated_recent_detail_count(tmp_path):
    config = config_with_runs(tmp_path)
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    with store._conn() as connection:
        for i in range(12):
            connection.execute("INSERT INTO job_runs(id,job_id,status,started_at,created_at) VALUES (?,?,?,?,?)",
                               (f"extra-{i}", "autonomous_trading", "timed_out", "2026-10-21T15:00:00", "2026-10-21T15:00:00"))
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21), run_id="current-review"), days=1)
    assert facts["total_runs"] == 14
    assert sum(facts["by_job"]["autonomous_trading"].values()) == 14
    assert len(facts["recent_runs"]) == 12 and facts["recent_runs_truncated"] is True
    assert facts["status_counts"] == {"failed": 1, "timed_out": 13}


def test_weekly_context_includes_previous_days(tmp_path):
    config = config_with_runs(tmp_path)
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21), run_id="current-review"), days=7)
    assert len(facts["recent_runs"]) == 3


def test_missing_job_database_is_unknown_and_does_not_create_state(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21)), days=1)
    assert facts["available"] is False
    assert not (config.data_dir / "scheduler.db").exists()


def test_run_owned_receipts_prove_partial_activity_but_legacy_traces_stay_unknown(tmp_path):
    config = config_with_runs(tmp_path)
    sessions = config.data_dir / "agent_sessions"
    sessions.mkdir()
    (sessions / "scheduler-autonomous-execution-failed-intraday-2026-10-21.jsonl").write_text(
        json.dumps({"role": "tool", "name": "analyze_portfolio", "content": "{}"}) + "\n")
    (sessions / "scheduler-autonomous-2026-10-21.jsonl").write_text(
        json.dumps({"role": "tool", "name": "get_bars", "content": "{}"}) + "\n")
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21), run_id="current-review"), days=1)
    assert facts["observed_tool_receipts"] == 1
    assert facts["tool_receipts_by_job"] == {"autonomous_trading": 1}
    assert facts["runs_with_receipt_trace"] == 1 and facts["runs_without_receipt_trace"] == 1
    rows = {row["run_id"]: row for row in facts["recent_runs"]}
    assert rows["failed-intraday"]["observed_tool_receipts"] == 1
    assert rows["local-next-day"]["observed_tool_receipts"] is None


def test_corrupt_receipt_trace_is_unknown_and_is_not_rewritten(tmp_path):
    config = config_with_runs(tmp_path)
    path = config.data_dir / "agent_sessions/scheduler-autonomous-failed-intraday-2026-10-21.jsonl"
    path.parent.mkdir()
    path.write_text("invalid json")
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21), run_id="current-review"), days=1)
    assert facts["runs_with_receipt_trace"] == 0 and path.read_text() == "invalid json"


@pytest.mark.parametrize("operation,days", [(ops.agent_daily_journal, 1), (ops.agent_strategy_review, 7),
                                         (ops.agent_eod_reflection, 1)])
def test_review_consumer_receives_actual_failures_not_a_zero_trade_quality_claim(tmp_path, monkeypatch, operation, days):
    config = config_with_runs(tmp_path)
    captured = []

    async def run(ctx, prompt, job, **kwargs):
        captured.append(prompt)
        return StepOutput(message="review")

    monkeypatch.setattr(ops, "run_agent_step", run)
    ctx = StepContext(config=config, date=date(2026, 10, 21), run_id="current-review")
    asyncio.run(operation(ctx))
    assert "failed-intraday" in captured[0] and "timed_out" in captured[0]
    assert "零成交不能证明" in captured[0]
    assert "recent_runs 仅是截取的最近明细" in captured[0]
    assert "不等于已提交、已接受的止损委托" in captured[0]
    assert "连续天数须逐日核对" in captured[0]
    assert "输入与输出预留相加" in captured[0] and "待验证假设" in captured[0]
    assert json.loads(json.dumps(ops._review_execution_context(ctx, days=days)))["available"]


def test_execution_context_keeps_trigger_and_failure_reason_even_outside_recent_details(tmp_path):
    config = config_with_runs(tmp_path)
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    with store._conn() as connection:
        connection.execute("UPDATE job_runs SET trigger='scheduler',error='context exceeds 131072 tokens' "
                           "WHERE id='local-next-day'")
        connection.execute("UPDATE job_runs SET trigger='cli',error='deadline reached' WHERE id='failed-intraday'")
    facts = ops._review_execution_context(StepContext(config=config, date=date(2026, 10, 21), run_id="current-review"), days=1)
    assert facts["by_trigger"] == {"scheduler": {"failed": 1}, "cli": {"timed_out": 1}}
    rows = {row["run_id"]: row for row in facts["recent_runs"]}
    assert rows["local-next-day"]["trigger"] == "scheduler"
    assert rows["local-next-day"]["error"] == "context exceeds 131072 tokens"


@pytest.mark.parametrize("operation", [ops.agent_close_analysis, ops.agent_eod_reflection,
                                      ops.agent_daily_journal, ops.agent_strategy_review, ops.agent_premarket_briefing])
def test_review_steps_expose_fact_reads_without_trading_or_memory_mutations(tmp_path, monkeypatch, operation):
    seen = []

    async def run(ctx, prompt, job, **kwargs):
        seen.append(kwargs["tool_whitelist"])
        return StepOutput(message="review")

    monkeypatch.setattr(ops, "run_agent_step", run)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    asyncio.run(operation(StepContext(config=config, date=date(2026, 10, 21))))
    assert {"analyze_portfolio", "search_decisions", "get_bars"} <= seen[0]
    assert not seen[0] & {"place_paper_trade", "batch_paper_trades", "write_memory", "memory_write", "emit_signal"}
