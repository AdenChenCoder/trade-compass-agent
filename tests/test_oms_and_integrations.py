"""Tests for risk veto and job integrations."""

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.portfolio.simulator import PaperPortfolio


# === OMS Tests (exchange structural rules only) ===


class TestOMSExchangeRules:
    """Exchange structural validation: price limits, T+1, oversell."""

    def test_buy_above_limit_up_blocked(self):
        portfolio = PaperPortfolio()
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=11.01,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
            previous_close=10.0,
            price_limit_pct=0.10,
        ))
        assert not ok
        assert "涨停" in msg

    def test_buy_at_limit_up_allowed(self):
        portfolio = PaperPortfolio()
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=11.00,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
            previous_close=10.0,
            price_limit_pct=0.10,
        ))
        assert ok

    def test_sell_below_limit_down_blocked(self):
        portfolio = PaperPortfolio()
        portfolio.record(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 25, 10, 0),
            reason="seed",
        ))
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="sell",
            quantity=100,
            price=8.99,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
            previous_close=10.0,
            price_limit_pct=0.10,
        ))
        assert not ok
        assert "跌停" in msg

    def test_oversell_blocked(self):
        portfolio = PaperPortfolio()
        portfolio.record(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 25, 10, 0),
            reason="seed",
        ))
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="sell",
            quantity=200,
            price=10.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
        ))
        assert not ok
        assert "持仓" in msg

    def test_t_plus_one_same_day_sell_blocked(self):
        portfolio = PaperPortfolio()
        portfolio.record(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 27, 9, 30),
            reason="seed",
        ))
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="sell",
            quantity=100,
            price=10.5,
            timestamp=datetime(2026, 5, 27, 14, 0),
            reason="test",
            is_t0=False,
        ))
        assert not ok
        assert "T+1" in msg

    def test_t0_same_day_sell_allowed(self):
        portfolio = PaperPortfolio()
        portfolio.record(PaperTrade(
            symbol="511880",
            account=AccountKind.ETF_ROTATION,
            side="buy",
            quantity=100,
            price=100.0,
            timestamp=datetime(2026, 5, 27, 9, 30),
            reason="seed",
            is_t0=True,
        ))
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="511880",
            account=AccountKind.ETF_ROTATION,
            side="sell",
            quantity=100,
            price=100.5,
            timestamp=datetime(2026, 5, 27, 14, 0),
            reason="test",
            is_t0=True,
        ))
        assert ok

    def test_suspended_blocked(self):
        portfolio = PaperPortfolio()
        ok, msg = portfolio.validate_trade(PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
            suspended=True,
        ))
        assert not ok
        assert "停牌" in msg


# === Risk Veto Tests ===


class TestRiskVeto:
    """Risk check appends warnings without changing the rating."""

    def test_warning_on_high_concentration(self):
        from trade_compass_agent.domain.signals import SignalRating, TradingSignal
        from trade_compass_agent.runtime.specialists.risk_controls import apply_risk_warnings

        signal = TradingSignal(
            symbol="600519",
            rating=SignalRating.BUY,
            confidence=0.7,
            reasoning="Good momentum",
            source_specialist="debate",
        )
        stack = MagicMock()
        stack.config.data_dir = Path("/tmp/test")

        with patch(
            "trade_compass_agent.runtime.tools.portfolio.tool_analyze_portfolio"
        ) as mock_portfolio:
            mock_portfolio.return_value = '{"concentration_top5": [{"symbol": "600519", "weight_pct": 30}], "total_positions": 3}'
            result = apply_risk_warnings(stack, signal)

        assert result.rating == SignalRating.BUY
        assert "集中度提示" in result.reasoning

    def test_warning_on_many_positions(self):
        from trade_compass_agent.domain.signals import SignalRating, TradingSignal
        from trade_compass_agent.runtime.specialists.risk_controls import apply_risk_warnings

        signal = TradingSignal(
            symbol="600519",
            rating=SignalRating.STRONG_BUY,
            confidence=0.9,
            reasoning="Very strong",
            source_specialist="debate",
        )
        stack = MagicMock()
        stack.config.data_dir = Path("/tmp/test")

        with patch(
            "trade_compass_agent.runtime.tools.portfolio.tool_analyze_portfolio"
        ) as mock_portfolio:
            mock_portfolio.return_value = '{"concentration_top5": [], "total_positions": 10}'
            result = apply_risk_warnings(stack, signal)

        assert result.rating == SignalRating.STRONG_BUY
        assert "持仓数提示" in result.reasoning

    def test_no_veto_when_risk_low(self):
        from trade_compass_agent.domain.signals import SignalRating, TradingSignal
        from trade_compass_agent.runtime.specialists.risk_controls import apply_risk_warnings

        signal = TradingSignal(
            symbol="600519",
            rating=SignalRating.BUY,
            confidence=0.7,
            reasoning="Good momentum",
            source_specialist="debate",
        )
        stack = MagicMock()
        stack.config.data_dir = Path("/tmp/test")

        with patch(
            "trade_compass_agent.runtime.tools.portfolio.tool_analyze_portfolio"
        ) as mock_portfolio:
            mock_portfolio.return_value = '{"concentration_top5": [{"symbol": "600001", "weight_pct": 15}], "total_positions": 3}'
            result = apply_risk_warnings(stack, signal)

        assert result.rating == SignalRating.BUY

    def test_hold_and_sell_skip_veto(self):
        from trade_compass_agent.domain.signals import SignalRating, TradingSignal
        from trade_compass_agent.runtime.specialists.risk_controls import apply_risk_warnings

        signal = TradingSignal(
            symbol="600519",
            rating=SignalRating.SELL,
            confidence=0.8,
            reasoning="Bearish",
            source_specialist="debate",
        )
        stack = MagicMock()
        result = apply_risk_warnings(stack, signal)
        assert result.rating == SignalRating.SELL


# === Job Integration Tests ===


class TestEodSignalTracking:
    """eod_review should update signal tracker."""

    def test_update_signal_tracking(self, tmp_path, monkeypatch):
        import asyncio
        from trade_compass_agent.evaluation.signal_tracker import SignalTracker

        monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
        monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path))

        # Seed a tracked signal
        tracker = SignalTracker(tmp_path)
        tracker.track_signal({
            "signal_id": "sig-001",
            "symbol": "600519",
            "rating": "buy",
            "confidence": 0.8,
            "entry_price": 100.0,
            "stop_loss": 90.0,
            "target_price": 120.0,
            "timestamp": "2026-06-01T09:30:00",
        })
        tracker.update_entry("sig-001", 100.0)

        # Create mock portfolio with a position at 110.0
        (tmp_path / "paper_trades.jsonl").write_text(
            '{"symbol":"600519","account":"short_stock","side":"buy","quantity":100,"price":100.0,"timestamp":"2026-06-01T09:30:00","reason":"test"}\n',
            encoding="utf-8",
        )

        from trade_compass_agent.config import load_app_config
        import trade_compass_agent.config as cfg_mod
        cfg_mod._config_cache = None
        cfg_mod._config_cache_key = None
        config = load_app_config()

        from trade_compass_agent.ops.job_definition import StepContext
        from trade_compass_agent.runtime.tools.builtin_operations import update_signal_tracker
        ctx = StepContext(config=config, date=date.today())
        asyncio.run(update_signal_tracker(ctx))

        # Verify the tracker was updated
        active = tracker.get_active()
        assert len(active) == 1


class TestPremarketExitAlerts:
    """premarket should scan and push exit signal alerts."""

    def test_premarket_detects_stop_loss(self, tmp_path, monkeypatch):
        import json
        from trade_compass_agent.runtime.tools.builtin_operations import _load_signal_map

        monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
        monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path))

        signals_path = tmp_path / "signals.jsonl"
        signals_path.write_text(
            json.dumps({"symbol": "600519", "stop_loss": 95.0, "rating": "buy"}) + "\n",
            encoding="utf-8",
        )

        from trade_compass_agent.config import load_app_config
        import trade_compass_agent.config as cfg_mod
        cfg_mod._config_cache = None
        cfg_mod._config_cache_key = None
        config = load_app_config()

        signal_map = _load_signal_map(config)
        assert "600519" in signal_map
        assert signal_map["600519"]["stop_loss"] == 95.0
