"""Tests for portfolio management tools."""

import json
from datetime import datetime, timedelta

import pytest

from trade_compass_agent.runtime.tools.portfolio import (
    tool_analyze_portfolio,
    tool_batch_paper_trades,
    tool_check_exit_signals,
    tool_place_paper_trade,
)


@pytest.fixture
def portfolio_stack(tmp_path, monkeypatch):
    """Create a MarketStack-like object with a config pointing to tmp data dir."""
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")

    from unittest.mock import MagicMock
    from trade_compass_agent.config import TradingCostConfig

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    memory_dir = tmp_path / "memory_vault"

    config = MagicMock()
    config.data_dir = data_dir
    config.memory_dir = memory_dir
    config.trading_costs = TradingCostConfig()

    stack = MagicMock()
    stack.config = config
    return stack


@pytest.fixture
def seeded_portfolio(portfolio_stack):
    """Seed some trades into the portfolio file."""
    trades_file = portfolio_stack.config.data_dir / "paper_trades.jsonl"
    yesterday = datetime.now() - timedelta(days=1)
    trades = [
        {
            "symbol": "600519",
            "account": "short_stock",
            "side": "buy",
            "quantity": 100,
            "price": 1800.0,
            "timestamp": yesterday.isoformat(),
            "reason": "test buy",
            "previous_close": 1790.0,
            "suspended": False,
            "is_st": False,
        },
        {
            "symbol": "000001",
            "account": "short_stock",
            "side": "buy",
            "quantity": 500,
            "price": 12.0,
            "timestamp": yesterday.isoformat(),
            "reason": "test buy 2",
            "previous_close": 11.8,
            "suspended": False,
            "is_st": False,
        },
    ]
    with trades_file.open("w") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    return portfolio_stack


class TestAnalyzePortfolio:
    def test_empty_portfolio(self, portfolio_stack):
        result = json.loads(tool_analyze_portfolio(portfolio_stack))
        assert result["total_positions"] == 0
        assert result["positions"] == []

    def test_with_positions(self, seeded_portfolio):
        result = json.loads(tool_analyze_portfolio(seeded_portfolio))
        assert result["total_positions"] == 2
        assert len(result["positions"]) == 2
        symbols = {p["symbol"] for p in result["positions"]}
        assert "600519" in symbols
        assert "000001" in symbols

    def test_position_warns_when_market_price_falls_back_to_trade_price(self, seeded_portfolio, monkeypatch):
        monkeypatch.setattr(
            "trade_compass_agent.portfolio.simulator.PaperPortfolio._try_sina_batch_quote",
            lambda self, symbols: None,
        )
        seeded_portfolio.provider.get_bars.return_value = []

        result = json.loads(tool_analyze_portfolio(seeded_portfolio))
        position = next(p for p in result["positions"] if p["symbol"] == "600519")

        assert position["last_price"] == 1800.0
        assert position["price_source"] == "last_trade"
        assert position["price_is_fresh"] is False
        assert "不要据此判断成本价持平" in position["price_warning"]

    def test_exit_review_suggested_flag(self, seeded_portfolio, monkeypatch):
        monkeypatch.setattr(
            "trade_compass_agent.runtime.tools.portfolio.pnl_exit_review_candidate",
            lambda pnl_pct, **kw: {"review_only": True, "trigger": "pnl_review"},
        )
        monkeypatch.setattr(
            "trade_compass_agent.runtime.tools.portfolio._fetch_exit_review_market_context",
            lambda symbols: {"industry_flow_top5": [{"name": "电子", "net_inflow_yi": 10.0}]},
        )
        result = json.loads(tool_analyze_portfolio(seeded_portfolio))
        flagged = [p for p in result["positions"] if p.get("exit_review_suggested")]
        assert len(flagged) == 2
        assert flagged[0].get("exit_review_context", {}).get("industry_flow_top5")


class TestPlacePaperTrade:
    def test_buy_success(self, portfolio_stack):
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519",
            side="buy",
            quantity=100,
            price=1800.0,
            reason="test",
            account="short_stock",
        ))
        assert result["status"] == "executed"
        assert result["quantity"] == 100

    def test_buy_creates_instrument_page_for_new_symbol(self, portfolio_stack):
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519",
            side="buy",
            quantity=100,
            price=1800.0,
            reason="test",
            account="short_stock",
        ))
        page = portfolio_stack.config.memory_dir / "instruments" / "600519.md"

        assert result["status"] == "executed"
        assert page.exists()
        assert "buy 100股 @1800.0" in page.read_text(encoding="utf-8")

    def test_odd_lot_size_now_allowed(self, portfolio_stack):
        """Lot size enforcement moved to agent skill — OMS no longer blocks."""
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519",
            side="buy",
            quantity=50,
            price=1800.0,
            reason="test",
            account="short_stock",
        ))
        assert result["status"] == "executed"

    def test_sell_no_position(self, portfolio_stack):
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519",
            side="sell",
            quantity=100,
            price=1800.0,
            reason="test exit",
            account="short_stock",
        ))
        assert "error" in result

    def test_sell_success(self, seeded_portfolio):
        result = json.loads(tool_place_paper_trade(
            seeded_portfolio,
            symbol="600519",
            side="sell",
            quantity=100,
            price=1850.0,
            reason="take profit",
            account="short_stock",
        ))
        assert result["status"] == "executed"

    def test_sell_does_not_parse_reported_pnl_from_reason(self, seeded_portfolio):
        result = json.loads(tool_place_paper_trade(
            seeded_portfolio,
            symbol="600519",
            side="sell",
            quantity=100,
            price=1800.0,
            reason="止损清仓：浮亏-21.62%",
            account="short_stock",
        ))

        assert result["status"] == "executed"
        assert result["price"] == 1800.0
        assert len((seeded_portfolio.config.data_dir / "paper_trades.jsonl").read_text().splitlines()) == 3

    def test_batch_sell_uses_actual_exit_price_when_reason_reports_loss(self, seeded_portfolio):
        result = json.loads(tool_batch_paper_trades(
            seeded_portfolio,
            trades=[{
                "symbol": "600519",
                "side": "sell",
                "quantity": 100,
                "price": 1410.84,
                "reason": "止损清仓：浮亏-21.62%",
                "account": "short_stock",
            }],
        ))

        assert result["executed"] == 1
        assert result["results"][0]["status"] == "executed"

    def test_batch_sell_does_not_parse_reported_pnl_from_reason(self, seeded_portfolio):
        result = json.loads(tool_batch_paper_trades(
            seeded_portfolio,
            trades=[{
                "symbol": "600519",
                "side": "sell",
                "quantity": 100,
                "price": 1800.0,
                "reason": "止损清仓：浮亏-21.62%",
                "account": "short_stock",
            }],
        ))

        assert result["executed"] == 1
        assert result["results"][0]["status"] == "executed"
        assert result["results"][0]["price"] == 1800.0

    def test_missing_params(self, portfolio_stack):
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="",
            side="buy",
            quantity=0,
            price=0,
        ))
        assert "error" in result


class TestCheckExitSignals:
    def test_no_positions(self, portfolio_stack):
        result = json.loads(tool_check_exit_signals(portfolio_stack))
        assert result["alerts"] == []
        assert "无持仓" in result["message"]

    def test_stop_loss_alert(self, seeded_portfolio):
        signals_file = seeded_portfolio.config.data_dir / "signals.jsonl"
        signal = {
            "symbol": "600519",
            "stop_loss": 1850.0,
            "target_price": 2000.0,
            "rating": "buy",
        }
        signals_file.write_text(json.dumps(signal) + "\n")

        result = json.loads(tool_check_exit_signals(seeded_portfolio))
        alerts = result["alerts"]
        triggered = [a for a in alerts if a["symbol"] == "600519"]
        assert len(triggered) == 1
        assert "止损" in triggered[0]["exit_reasons"][0]

    def test_no_alerts_healthy(self, seeded_portfolio):
        result = json.loads(tool_check_exit_signals(seeded_portfolio))
        assert result["positions_with_alerts"] == 0
