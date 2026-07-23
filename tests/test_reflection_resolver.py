"""Tests for market-aware reflection resolution."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from dataclasses import replace

import pytest

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.ops.reflection import JobReflection, PendingReflection
from trade_compass_agent.ops.reflection_resolver import (
    extract_alerts,
    extract_position_snapshots,
    resolve_all_job_reflections,
    resolve_pending_with_market,
)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory_vault"
    data_dir.mkdir(parents=True)
    memory_dir.mkdir(parents=True)
    cfg = load_app_config()
    return replace(cfg, data_dir=data_dir, memory_dir=memory_dir)


class TestExtractHelpers:
    def test_extract_position_snapshots(self):
        predictions = {
            "portfolio_scan": {"positions": [{"symbol": "600519", "pnl_pct": 5.0}]},
        }
        assert extract_position_snapshots(predictions)[0]["symbol"] == "600519"

    def test_extract_alerts(self):
        predictions = {
            "exit_check": {"alerts": ["600183 盈利21.6%，考虑止盈"]},
            "portfolio_scan": {"alerts": ["600183 盈利21.6%，考虑止盈", "512400 亏损-47.9%"]},
        }
        alerts = extract_alerts(predictions)
        assert len(alerts) == 2
        assert "512400" in alerts[1]


class TestResolvePendingWithMarket:
    def test_skips_same_day_pending(self, config: AppConfig):
        pending = PendingReflection(
            job_id="close",
            run_id="r1",
            run_date=date.today().isoformat(),
            predictions={"mark_to_market": {"positions": [{"symbol": "600183", "pnl_pct": 10.0}]}},
            summary="收盘检查",
        )
        assert resolve_pending_with_market(pending, config) is None

    def test_resolves_without_positions(self, config: AppConfig):
        pending = PendingReflection(
            job_id="weekly",
            run_id="r1",
            run_date="2026-06-01",
            predictions={"weekly_summary": {"turns": 3}},
            summary="周度摘要",
        )
        actuals, lesson = resolve_pending_with_market(pending, config, as_of=date(2026, 6, 10))
        assert actuals == {}
        assert "无持仓快照" in lesson

    def test_compares_portfolio_pnl(self, config: AppConfig, monkeypatch: pytest.MonkeyPatch):
        pending = PendingReflection(
            job_id="close",
            run_id="r1",
            run_date="2026-06-01",
            predictions={
                "mark_to_market": {
                    "positions": [
                        {"symbol": "600183", "pnl_pct": 10.0},
                        {"symbol": "601138", "pnl_pct": -5.0},
                    ],
                },
                "exit_check": {"alerts": ["600183 盈利10.0%，考虑止盈"]},
            },
            summary="收盘检查",
        )

        pos_600183 = MagicMock(symbol="600183", avg_cost=100.0, last_price=125.0)

        monkeypatch.setattr(
            "trade_compass_agent.portfolio.JsonPaperPortfolio",
            lambda *a, **k: MagicMock(
                positions_with_market_prices=lambda provider: [pos_600183],
            ),
        )
        monkeypatch.setattr(
            "trade_compass_agent.runtime.market_stack.MarketStack.from_config",
            lambda cfg: MagicMock(provider=MagicMock()),
        )

        actuals, lesson = resolve_pending_with_market(pending, config, as_of=date(2026, 6, 10))
        assert actuals["positions"][0]["symbol"] == "600183"
        assert actuals["positions"][0]["actual_pnl_pct"] == 25.0
        assert actuals["positions"][1]["status"] == "closed"
        assert "600183" in lesson
        assert "601138" in lesson


class TestResolveAllJobReflections:
    def test_resolves_backlog_for_all_jobs(self, config: AppConfig, monkeypatch: pytest.MonkeyPatch):
        reflection = JobReflection(config.memory_dir)
        reflection.store_pending(
            "close",
            "run-close",
            predictions={"mark_to_market": {"positions": [{"symbol": "600183", "pnl_pct": 5.0}]}},
            summary="收盘",
            run_date=date(2026, 6, 1),
        )
        reflection.store_pending(
            "eod_review",
            "run-eod",
            predictions={"pnl_review": {"positions": [{"symbol": "002491", "pnl_pct": 12.0}]}},
            summary="复盘",
            run_date=date(2026, 6, 1),
        )

        pos = MagicMock(symbol="600183", avg_cost=100.0, last_price=110.0)
        pos2 = MagicMock(symbol="002491", avg_cost=100.0, last_price=115.0)
        monkeypatch.setattr(
            "trade_compass_agent.portfolio.JsonPaperPortfolio",
            lambda *a, **k: MagicMock(
                positions_with_market_prices=lambda provider: [pos, pos2],
            ),
        )
        monkeypatch.setattr(
            "trade_compass_agent.runtime.market_stack.MarketStack.from_config",
            lambda cfg: MagicMock(provider=MagicMock()),
        )

        results = resolve_all_job_reflections(config.memory_dir, config)
        assert "close" in results
        assert "eod_review" in results
        assert reflection.pending_count("close") == 0
        assert reflection.pending_count("eod_review") == 0
        assert reflection.get_context("close")
        assert reflection.get_context("eod_review")
