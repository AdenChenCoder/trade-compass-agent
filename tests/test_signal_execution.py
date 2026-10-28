import json

import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.evaluation.signal_execution import reconcile_signal_executions
from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.memory.decision_store import DecisionStore
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools.registry import ToolRegistry


def test_explicit_signal_partial_exit_fees_and_restart(client):
    config = load_app_config()
    registry = ToolRegistry(MarketStack.from_config())
    signal = json.loads(registry.execute("emit_signal", {
        "symbol": "600519", "rating": "buy", "confidence": 0.7,
        "reasoning": "测试信号", "account": "short_stock",
    }))
    signal_id = signal["signal_id"]
    tracker = SignalTracker(config.data_dir)
    body = {"symbol": "600519", "account": "short_stock", "side": "buy", "quantity": 200,
            "price": 10, "signal_id": signal_id}
    assert client.post("/api/portfolio/trades", json=body).status_code == 200
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    assert portfolio.trades[0].signal_id == signal_id
    # Partial exits cannot close the signal, even when the sell has no reference.
    assert client.post("/api/portfolio/trades", json={**body, "side": "sell", "quantity": 100, "signal_id": None}).status_code == 200
    record = tracker.get_active()[0]
    assert record.entry_quantity == 200
    assert record.resolved_quantity == 100
    assert record.outcome == ""
    assert record.net_pnl < 0  # Flat prices still incur both entry and exit fees.
    assert client.post("/api/portfolio/trades", json={**body, "side": "sell", "quantity": 100, "signal_id": None}).status_code == 200
    record = tracker.get_closed()[0]
    assert record.resolved_quantity == 200
    assert record.outcome == "loss"
    assert record.outcome_basis == "execution_net_pnl"
    assert len(record.exit_trade_ids) == 2
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    assert record.net_pnl == pytest.approx(sum(fill.pnl for fill in portfolio.realized_trades()), abs=0.01)
    before = tracker.path.read_bytes()
    reconcile_signal_executions(config.data_dir, portfolio)
    assert tracker.path.read_bytes() == before
    from trade_compass_agent.evaluation.rule_refinement import analyze_outcomes
    assert analyze_outcomes(config.data_dir).total_analyzed == 0
    assert analyze_outcomes(config.data_dir, outcome_basis="execution_net_pnl").total_analyzed == 1
    assert tracker.get_stats(outcome_basis="execution_net_pnl")["losses"] == 1


def test_cross_account_reference_rejected_and_unlinked_fill_not_guessed(client):
    registry = ToolRegistry(MarketStack.from_config())
    signal = json.loads(registry.execute("emit_signal", {
        "symbol": "600519", "rating": "buy", "confidence": 0.8,
        "reasoning": "仅针对短线账户", "account": "short_stock",
    }))
    body = {"symbol": "600519", "account": "mid_term", "side": "buy", "quantity": 100,
            "price": 10, "signal_id": signal["signal_id"]}
    assert client.post("/api/portfolio/trades", json=body).status_code == 400
    body.update(account="short_stock", signal_id=None)
    assert client.post("/api/portfolio/trades", json=body).status_code == 200
    records = SignalTracker(load_app_config().data_dir)._load_all()
    assert records[0].status == "pending"
    assert records[0].entry_trade_ids == []


def test_invalid_strategy_reference_does_not_persist_a_signal(client):
    config = load_app_config()
    response = json.loads(ToolRegistry(MarketStack.from_config()).execute("emit_signal", {
        "symbol": "600519", "rating": "buy", "confidence": 0.8, "reasoning": "无效关联",
        "strategy_id": "missing", "strategy_version": "a" * 16,
    }))
    assert "error" in response
    assert not (config.data_dir / "signals.jsonl").exists()


def test_agent_fill_keeps_strategy_and_skill_attribution_in_decision(client):
    from trade_compass_agent.evaluation.research import ResearchStore, StrategyDefinition
    from trade_compass_agent.runtime.tools.portfolio import tool_place_paper_trade

    config = load_app_config()
    strategy = ResearchStore(config.data_dir).save_strategy(StrategyDefinition(
        strategy_id="box-trend", name="趋势突破", hypothesis="研究假设", invalidation="跌破均线",
    ))
    stack = MarketStack.from_config()
    signal = json.loads(ToolRegistry(stack).execute("emit_signal", {
        "symbol": "600519", "rating": "buy", "confidence": 0.8, "reasoning": "可复查依据",
        "account": "short_stock", "strategy_id": "box-trend", "strategy_version": strategy["version"],
        "source_skills": ["strategy-research"],
    }))
    receipt = json.loads(tool_place_paper_trade(stack, symbol="600519", side="buy", quantity=100,
        price=10, account="short_stock", price_source="user_confirmed", signal_id=signal["signal_id"]))
    assert receipt["status"] == "executed"
    decision = DecisionStore(config.data_dir)._load_all()[0]
    assert decision.signal_id == signal["signal_id"]
    assert decision.strategy_version == strategy["version"]
    assert decision.source_skills == ["strategy-research"]
