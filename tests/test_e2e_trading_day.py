"""End-to-end integration test: simulate a complete trading day workflow.

Flow:
1. Premarket — scan positions and exit signals
2. Morning plan — screening → signal emission
3. Agent turn — analyze a symbol → emit signal → place trade
4. Close — check exit signals
5. EOD review — signal tracker status, P&L
6. Verify state: portfolio, signals, tracker are consistent
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.ops.tick_scheduler import TickScheduler
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools.portfolio import tool_place_paper_trade
from trade_compass_agent.runtime.tools.signals import tool_emit_signal


@pytest.fixture()
def trading_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up an isolated trading environment."""
    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"
    data_dir.mkdir()
    memory_dir.mkdir()
    (memory_dir / "skills").mkdir(parents=True)
    (memory_dir / "daily_reviews").mkdir(parents=True)
    (memory_dir / "weekly_reviews").mkdir(parents=True)

    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # Invalidate config cache
    import trade_compass_agent.config as cfg_mod
    cfg_mod._config_cache = None
    cfg_mod._config_cache_key = None

    config = load_app_config()
    stack = MarketStack.from_config(config)
    return {"config": config, "stack": stack, "data_dir": data_dir, "memory_dir": memory_dir}


def test_full_trading_day_flow(trading_env):
    """Simulate premarket → signal → trade → close → review."""
    config = trading_env["config"]
    stack = trading_env["stack"]
    data_dir = trading_env["data_dir"]

    # --- Phase 1: Premarket check (mock as trading day) ---
    # Premarket now includes Agent steps which will fail without LLM; verify it runs
    scheduler = TickScheduler(config)
    with patch("trade_compass_agent.ops.trading_calendar.is_trading_day", return_value=True):
        scheduler.run_job_now("premarket", trigger="test")
    runs = scheduler.run_store.recent_runs(limit=1, job_id="premarket")
    assert len(runs) == 1

    # --- Phase 2: Emit a signal via tool ---
    signal_result = tool_emit_signal(
        stack,
        symbol="600519",
        rating="buy",
        confidence=0.8,
        entry_price=1850.0,
        stop_loss=1800.0,
        target_price=2000.0,
        reasoning="MA多头排列，MACD金叉，成交量放大",
        source_specialist="intraday_tech",
        source_tools=["get_bars", "compute_macd", "compute_ma"],
    )
    signal_data = json.loads(signal_result)
    assert signal_data["status"] == "recorded"
    signal_id = signal_data["signal_id"]

    # Verify signal persisted
    signals_path = data_dir / "signals.jsonl"
    assert signals_path.exists()
    lines = signals_path.read_text().strip().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["symbol"] == "600519"

    # Verify signal tracker has it as "pending"
    tracker = SignalTracker(data_dir)
    tracking_path = data_dir / "signal_tracking.jsonl"
    assert tracking_path.exists()
    active = tracker.get_active()
    assert len(active) == 0  # still pending, not active
    all_records = tracker._load_all()
    assert len(all_records) == 1
    assert all_records[0].status == "pending"
    assert all_records[0].signal_id == signal_id

    # --- Phase 3: Place a buy trade (simulate as "yesterday" for T+1) ---
    from datetime import timedelta
    portfolio = JsonPaperPortfolio(
        data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    yesterday = datetime.now() - timedelta(days=1)
    portfolio.record(PaperTrade(
        symbol="600519",
        account=AccountKind.SHORT_STOCK,
        side="buy",
        quantity=100,
        price=1850.0,
        timestamp=yesterday,
        reason="根据信号入场",
    ))

    # Verify portfolio has the position
    positions = portfolio.positions()
    assert len(positions) == 1
    assert positions[0].symbol == "600519"
    assert positions[0].quantity == 100

    # Manually update tracker (normally done by tool_place_paper_trade)
    tracker.update_entry(signal_id, actual_entry=1850.0)

    # Signal tracker should now show "active"
    tracker2 = SignalTracker(data_dir)
    all_records2 = tracker2._load_all()
    active_signals = [r for r in all_records2 if r.status == "active"]
    assert len(active_signals) == 1
    assert active_signals[0].actual_entry == 1850.0

    # --- Phase 4: Close job ---
    with patch("trade_compass_agent.ops.trading_calendar.is_trading_day", return_value=True):
        scheduler.run_job_now("close", trigger="test")
    close_runs = scheduler.run_store.recent_runs(limit=1, job_id="close")
    assert len(close_runs) == 1

    # --- Phase 5: Place a sell trade (exit) — T+1 allows since buy was "yesterday" ---
    sell_result = tool_place_paper_trade(
        stack,
        symbol="600519",
        side="sell",
        quantity=100,
        price=1920.0,
        reason="目标价附近止盈",
        account="short_stock",
    )
    sell_data = json.loads(sell_result)
    assert sell_data.get("status") == "executed", f"Sell failed: {sell_data}"

    # Signal tracker should now show "closed"
    tracker3 = SignalTracker(data_dir)
    all_records3 = tracker3._load_all()
    closed_signals = [r for r in all_records3 if r.status == "closed"]
    assert len(closed_signals) == 1
    assert closed_signals[0].actual_exit == 1920.0
    assert closed_signals[0].outcome == "win"
    assert closed_signals[0].actual_pnl is not None
    assert closed_signals[0].actual_pnl > 0

    # --- Phase 6: Verify portfolio closed ---
    portfolio2 = JsonPaperPortfolio(
        data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    positions2 = portfolio2.positions()
    assert len(positions2) == 0  # all sold

    realized = portfolio2.realized_trades()
    assert len(realized) == 1
    assert realized[0].pnl > 0  # profitable trade

    # --- Phase 7: Signal tracker stats ---
    stats = tracker3.get_stats()
    assert stats["total"] == 1
    assert stats["wins"] == 1
    assert stats["win_rate"] == 1.0


def test_signal_deduplication(trading_env):
    """Verify that the same signal isn't tracked twice."""
    stack = trading_env["stack"]
    data_dir = trading_env["data_dir"]

    tool_emit_signal(
        stack,
        symbol="000001",
        rating="hold",
        confidence=0.6,
        reasoning="Test signal",
        source_specialist="test",
    )

    tracker = SignalTracker(data_dir)
    all_1 = tracker._load_all()
    assert len(all_1) == 1
    sig_id = all_1[0].signal_id

    # Try to track same signal_id again
    tracker.track_signal({"signal_id": sig_id, "symbol": "000001", "rating": "hold"})
    all_2 = tracker._load_all()
    assert len(all_2) == 1  # deduplicated


def test_premarket_skips_non_trading_day(trading_env):
    """Premarket job gracefully skips on non-trading days."""
    config = trading_env["config"]
    scheduler = TickScheduler(config)
    with patch("trade_compass_agent.ops.trading_calendar.is_trading_day", return_value=False):
        scheduler.run_job_now("premarket", trigger="test")
    runs = scheduler.run_store.recent_runs(limit=1, job_id="premarket")
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert "非交易日" in runs[0].message
