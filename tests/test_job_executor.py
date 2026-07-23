"""Tests for JobExecutor timeout behavior."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.agent_session import ScheduledAgentSession, run_agent_step
from trade_compass_agent.ops.job_definition import JobDefinition, StepContext, StepOutput
from trade_compass_agent.ops.job_executor import JobExecutor
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime.tools import builtin_operations
from trade_compass_agent.runtime.tools.builtin_operations import agent_morning_plan, run_l5_screener


def test_run_agent_step_does_not_block_event_loop(monkeypatch):
    """Agent work runs in a thread so asyncio timeouts stay accurate."""
    def slow_run(self, prompt: str, *, timeout: int = 300) -> str:
        import time
        time.sleep(0.15)
        return "analysis text"

    monkeypatch.setattr(ScheduledAgentSession, "run", slow_run)

    async def _run() -> None:
        ctx = StepContext(config=AppConfig(), date=date.today(), step_timeout_seconds=300)

        async def heartbeat() -> str:
            await asyncio.sleep(0.05)
            return "alive"

        agent_out, heartbeat_out = await asyncio.gather(
            run_agent_step(ctx, "prompt", "morning_plan"),
            heartbeat(),
        )
        assert heartbeat_out == "alive"
        assert agent_out.data["analysis"] == "analysis text"

    asyncio.run(_run())


def test_run_agent_step_respects_step_timeout(monkeypatch):
    def slow_run(self, prompt: str, *, timeout: int = 300) -> str:
        import time
        time.sleep(2.0)
        return "late"

    monkeypatch.setattr(ScheduledAgentSession, "run", slow_run)

    async def _run() -> None:
        ctx = StepContext(config=AppConfig(), date=date.today(), step_timeout_seconds=300)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run_agent_step(ctx, "prompt", "morning_plan"), timeout=0.1)

    asyncio.run(_run())


def test_job_executor_marks_degraded_primary_output(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    executor = JobExecutor(config, store)
    job = JobDefinition(
        id="morning_plan",
        name="Morning Plan",
        description="test",
        schedule="trading_day 09:05",
        workflow_id="morning_plan",
        agent_session=None,
    )

    monkeypatch.setattr(
        "trade_compass_agent.runtime.workflows.engine.run_workflow_asset_by_id",
        lambda *args, **kwargs: {
            "workflow_id": "morning_plan",
            "run_id": kwargs["run_id"],
            "primary_step_id": "agent_plan",
            "degraded": True,
            "error": "Agent 执行失败: read timed out",
            "warnings": ["primary output step failed"],
        },
    )

    run = asyncio.run(executor.execute(job, trigger="api"))

    assert run.status == "degraded"
    assert run.ok is False
    assert run.error == "Agent 执行失败: read timed out"


def test_l5_screener_returns_structured_signals_and_summary(tmp_path, monkeypatch):
    def fake_run_specialist(stack, specialist_id, task, *, config=None, on_event=None):
        assert specialist_id == "screener"
        assert "600519" in task
        return (
            "### 600519\n"
            "**Rating**: buy\n"
            "**Confidence**: 0.72\n"
            "**Entry Price**: 1500\n"
            "**Stop Loss**: 1420\n"
            "**Target Price**: 1680\n"
            "**Reasoning**: MA20 支撑良好，量能温和放大。\n"
        )

    monkeypatch.setattr("trade_compass_agent.runtime.specialists.run.run_specialist", fake_run_specialist)

    async def _run() -> None:
        ctx = StepContext(
            config=AppConfig(data_dir=tmp_path / "data", data_provider="sample"),
            date=date.today(),
            upstream={
                "screening": StepOutput(
                    message="screened",
                    data={"candidates": [{"symbol": "600519", "score": 1.23}]},
                )
            },
        )
        output = await run_l5_screener(ctx)

        assert output.message == "L5 审判产出 1 条信号"
        assert output.data["signals_emitted"] == 1
        assert output.data["signals"][0]["symbol"] == "600519"
        assert output.data["signals"][0]["rating"] == "buy"
        assert output.data["signals"][0]["screening_score"] == 1.23
        assert output.data["summary"]["by_rating"]["buy"] == 1
        assert output.data["summary"]["actionable_buys"] == ["600519"]
        assert output.data["summary"]["top_signals"][0]["reason"]
        assert "steps" not in output.data

    asyncio.run(_run())


def test_agent_morning_plan_can_fetch_needed_data_with_read_only_tools(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_agent_step(ctx, prompt, job_id, *, step_id=None, tool_whitelist=None):
        captured["prompt"] = prompt
        captured["job_id"] = job_id
        captured["step_id"] = step_id
        captured["tool_whitelist"] = tool_whitelist
        return StepOutput(message="Agent 分析完成", data={"analysis": "# 今日核心结论\n\n按上下文执行。" * 5})

    monkeypatch.setattr(builtin_operations, "run_agent_step", fake_run_agent_step)

    async def _run() -> None:
        ctx = StepContext(
            config=AppConfig(data_dir=tmp_path / "data", data_provider="sample"),
            date=date.today(),
            upstream={
                "screening": StepOutput(
                    message="screened",
                    data={"candidates": [{"symbol": "600519", "score": 1.23}]},
                ),
                "screener_ai": StepOutput(
                    message="l5",
                    data={
                        "signals_emitted": 1,
                        "signals": [
                            {
                                "symbol": "600519",
                                "rating": "buy",
                                "confidence": 0.72,
                                "reasoning": "L5 reason" * 80,
                            }
                        ],
                        "summary": {"actionable_buys": ["600519"]},
                    },
                ),
                "portfolio_check": StepOutput(
                    message="positions",
                    data={
                        "positions": [
                            {
                                "symbol": "600519",
                                "quantity": 100,
                                "pnl_pct": 3.2,
                                "price_source": "last_trade",
                                "price_is_fresh": False,
                            }
                        ]
                    },
                ),
                "sector_flow": StepOutput(message="sector", data={"analysis": "sector analysis" * 200}),
                "idea_generation": StepOutput(message="ideas", data={"ideas": [{"symbol": "600519", "score": 80}]}),
                "risk_review": StepOutput(message="risk", data={"report_markdown": "risk report" * 200}),
            },
        )
        await agent_morning_plan(ctx)

    asyncio.run(_run())

    prompt = str(captured["prompt"])
    assert captured["job_id"] == "morning_plan"
    assert captured["step_id"] == "agent_plan"
    tool_whitelist = captured["tool_whitelist"]
    assert "analyze_portfolio" in tool_whitelist
    assert "sina_realtime_quote" in tool_whitelist
    assert "get_bars" in tool_whitelist
    assert "batch_get_bars" in tool_whitelist
    assert "place_paper_trade" not in tool_whitelist
    assert "## decision_context" in prompt
    assert "如判断需要最新数据，可调用可用数据工具补齐" in prompt
    assert "禁止调用下单/交易执行工具" in prompt
    assert "price_source=last_trade" in prompt
    assert "l5_top_signals" in prompt
    assert "sector analysis" in prompt
    assert len(prompt) < 7000
