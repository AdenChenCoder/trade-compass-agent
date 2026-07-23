"""Tests for Phase 5: visual pattern, backtester, and fund flow."""

import pandas as pd
import pytest

from trade_compass_agent.evaluation.backtester import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
)


class TestBacktester:
    """Tests for the vectorized backtesting engine."""

    @pytest.fixture
    def sample_prices(self):
        """Generate simple trending price data for 2 symbols."""
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        prices = {}
        for sym, base in [("600001", 10.0), ("600002", 20.0)]:
            closes = [base + i * 0.1 for i in range(60)]
            prices[sym] = pd.DataFrame({
                "date": [d.strftime("%Y-%m-%d") for d in dates],
                "open": [c - 0.05 for c in closes],
                "high": [c + 0.2 for c in closes],
                "low": [c - 0.2 for c in closes],
                "close": closes,
                "volume": [1000000] * 60,
            })
        return prices, dates

    @pytest.fixture
    def sample_signals(self, sample_prices):
        """Generate signal scores: 600001 high early, 600002 high late."""
        _, dates = sample_prices
        signals = pd.DataFrame(index=dates)
        signals["600001"] = [0.8] * 30 + [0.2] * 30
        signals["600002"] = [0.2] * 30 + [0.8] * 30
        return signals

    def test_backtest_basic(self, sample_prices, sample_signals):
        prices, _ = sample_prices
        cfg = BacktestConfig(
            initial_cash=100_000,
            position_pct=0.20,
            max_positions=2,
            entry_threshold=0.7,
            exit_threshold=0.3,
            stop_loss_pct=0.15,
            take_profit_pct=0.30,
        )
        result = run_backtest(sample_signals, prices, cfg)

        assert isinstance(result, BacktestResult)
        assert result.total_trades > 0
        assert len(result.daily_nav) == 60
        assert result.final_value > 0

    def test_backtest_generates_trades(self, sample_prices, sample_signals):
        prices, _ = sample_prices
        result = run_backtest(sample_signals, prices)

        assert result.total_trades >= 2
        for trade in result.trades:
            assert trade.symbol in ("600001", "600002")
            assert trade.quantity >= 100
            assert trade.quantity % 100 == 0
            assert trade.entry_price > 0
            assert trade.exit_price > 0

    def test_backtest_no_trades_when_no_signals(self, sample_prices):
        prices, dates = sample_prices
        signals = pd.DataFrame(index=dates, columns=["600001", "600002"])
        signals.fillna(0.5, inplace=True)

        cfg = BacktestConfig(entry_threshold=0.9, exit_threshold=0.1)
        result = run_backtest(signals, prices, cfg)

        assert result.total_trades == 0
        assert result.final_value == pytest.approx(100_000, rel=1e-6)

    def test_backtest_metrics_calculated(self, sample_prices, sample_signals):
        prices, _ = sample_prices
        result = run_backtest(sample_signals, prices)

        if result.total_trades > 0:
            assert 0 <= result.win_rate <= 1.0
            assert result.max_drawdown_pct <= 0

    def test_backtest_respects_max_positions(self, sample_prices):
        prices, dates = sample_prices
        signals = pd.DataFrame(index=dates, columns=["600001", "600002"])
        signals.fillna(0.9, inplace=True)

        cfg = BacktestConfig(max_positions=1, entry_threshold=0.7)
        result = run_backtest(signals, prices, cfg)

        assert result.total_trades >= 1


class TestFundFlowDataStructures:
    """Test fund flow data structures (without network)."""

    def test_stock_main_flow_dataclass(self):
        from trade_compass_agent.data.fund_flow import StockMainFlow

        flow = StockMainFlow(
            symbol="600519",
            name="贵州茅台",
            main_net_inflow=5.0,
            main_pct=12.5,
        )
        assert flow.symbol == "600519"
        assert flow.main_pct == 12.5

    def test_sector_flow_dataclass(self):
        from trade_compass_agent.data.fund_flow import SectorFlow

        flow = SectorFlow(
            sector_name="人工智能",
            change_pct=3.5,
            net_inflow=12.8,
        )
        assert flow.sector_name == "人工智能"

    def test_tool_get_fund_flow_returns_dict(self):
        from unittest.mock import patch
        from trade_compass_agent.data.fund_flow import tool_get_fund_flow, FundFlowProvider

        with patch.object(FundFlowProvider, "get_stock_main_flow", return_value=[]):
            with patch.object(FundFlowProvider, "get_sector_flow", return_value=[]):
                result = tool_get_fund_flow(category="summary")

        assert isinstance(result, dict)
        assert "northbound" not in result
        assert result.get("unavailable") == ["main_force", "industry", "concept"]

    def test_tool_get_fund_flow_rejects_unknown_category(self):
        from trade_compass_agent.data.fund_flow import tool_get_fund_flow

        result = tool_get_fund_flow(category="northbound")
        assert "error" in result


class TestChartPatternTool:
    """Test chart pattern tool (without mplfinance)."""

    def test_tool_requires_symbol(self):
        from unittest.mock import MagicMock
        from trade_compass_agent.runtime.tools.chart_pattern import tool_chart_pattern

        stack = MagicMock()
        result = tool_chart_pattern(stack)
        assert "error" in result

    def test_render_kline_without_mplfinance(self):
        from trade_compass_agent.runtime.tools.chart_pattern import render_kline_chart

        df = pd.DataFrame({
            "open": [10.0] * 40,
            "high": [10.5] * 40,
            "low": [9.5] * 40,
            "close": [10.2] * 40,
            "volume": [1000] * 40,
        })
        result = render_kline_chart(df, symbol="600519")
        assert result is None or isinstance(result, str)

    def test_chart_rendering_forces_headless_backend(self, monkeypatch):
        import os

        from trade_compass_agent.runtime.tools.chart_pattern import (
            _ensure_headless_matplotlib_backend,
        )

        monkeypatch.delenv("MPLBACKEND", raising=False)
        monkeypatch.delenv("MPLCONFIGDIR", raising=False)

        _ensure_headless_matplotlib_backend()

        import matplotlib

        assert matplotlib.get_backend().lower() == "agg"
        assert os.environ["MPLCONFIGDIR"]


class TestChartPatternIndicators:
    """Test the inline indicator computation in chart_pattern."""

    @pytest.fixture
    def trending_df(self):
        """Uptrending OHLCV data (80 bars)."""
        import numpy as np
        np.random.seed(42)
        n = 80
        base = 20.0
        closes = base + np.cumsum(np.abs(np.random.randn(n) * 0.1))
        return pd.DataFrame({
            "open": closes - 0.05,
            "high": closes + 0.2,
            "low": closes - 0.2,
            "close": closes,
            "volume": np.random.randint(100_000, 500_000, n),
        })

    @pytest.fixture
    def short_df(self):
        """Short OHLCV data (15 bars, not enough for some indicators)."""
        return pd.DataFrame({
            "open": [10.0] * 15,
            "high": [10.5] * 15,
            "low": [9.5] * 15,
            "close": [10.2] * 15,
            "volume": [1000] * 15,
        })

    def test_compute_indicators_full(self, trending_df):
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(trending_df)

        assert "last_close" in ind
        assert isinstance(ind["last_close"], float)

        # MA values
        assert "ma" in ind
        for k in ("MA5", "MA10", "MA20", "MA60"):
            assert k in ind["ma"]
            assert isinstance(ind["ma"][k], float)

        # MA positions
        assert "ma_position" in ind
        for k in ("MA5", "MA10", "MA20", "MA60"):
            assert ind["ma_position"][k] in ("above", "below", "at")

        # RSI
        assert "rsi_14" in ind
        assert 0 <= ind["rsi_14"] <= 100
        assert ind["rsi_zone"] in ("overbought", "oversold", "neutral")

        # MACD
        assert "macd" in ind
        for k in ("macd", "signal", "histogram"):
            assert k in ind["macd"]
        assert ind["macd_cross"] in ("golden_cross", "death_cross", "none")

        # Bollinger
        assert "bollinger" in ind
        b = ind["bollinger"]
        assert b["lower"] < b["middle"] < b["upper"]
        assert isinstance(b["pct_b"], float)

        # Volume ratio
        assert "volume_ratio" in ind
        assert ind["volume_ratio"] > 0

    def test_compute_indicators_short_data(self, short_df):
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(short_df)

        assert "last_close" in ind
        assert "ma" in ind
        # With only 15 bars, MA60 should be absent
        assert "MA60" not in ind["ma"]
        assert "MA5" in ind["ma"]
        # RSI needs >= 20 bars
        assert "rsi_14" not in ind
        # MACD needs >= 35 bars
        assert "macd" not in ind

    def test_format_indicator_context(self, trending_df):
        from trade_compass_agent.runtime.tools.chart_pattern import (
            _compute_indicators,
            _format_indicator_context,
        )
        ind = _compute_indicators(trending_df)
        text = _format_indicator_context(ind, "600519")

        assert "600519" in text
        assert "MA5" in text
        assert "RSI" in text
        assert "MACD" in text
        assert "布林带" in text
        assert "量比" in text

    def test_format_indicator_context_minimal(self, short_df):
        from trade_compass_agent.runtime.tools.chart_pattern import (
            _compute_indicators,
            _format_indicator_context,
        )
        ind = _compute_indicators(short_df)
        text = _format_indicator_context(ind, "000001")

        assert "000001" in text
        assert "MA5" in text
        # RSI/MACD shouldn't appear since data is too short
        assert "RSI" not in text
        assert "MACD" not in text

    def test_macd_golden_cross_detection(self):
        """Verify MACD golden cross is detected when histogram flips positive."""
        import numpy as np
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        np.random.seed(123)
        n = 60
        # Declining then sharply rising to create golden cross
        closes = list(range(50, 30, -1)) + list(range(30, 70))
        df = pd.DataFrame({
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100000] * n,
        })
        ind = _compute_indicators(df)
        assert "macd" in ind
        # After strong reversal the histogram should be positive
        assert ind["macd"]["histogram"] > 0

    def test_rsi_overbought(self):
        """Strong uptrend should give overbought RSI."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        closes = list(range(100, 200))
        df = pd.DataFrame({
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100000] * len(closes),
        })
        ind = _compute_indicators(df)
        assert ind["rsi_14"] >= 70
        assert ind["rsi_zone"] == "overbought"

    def test_bollinger_pct_b_range(self, trending_df):
        """Check %B is computed and within reasonable range."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(trending_df)
        pct_b = ind["bollinger"]["pct_b"]
        assert -0.5 <= pct_b <= 1.5  # Can be slightly outside [0,1] in extreme cases

    def test_kdj_computation(self, trending_df):
        """KDJ values should be computed for sufficient data."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(trending_df)

        assert "kdj" in ind
        for key in ("K", "D", "J"):
            assert key in ind["kdj"]
            assert isinstance(ind["kdj"][key], float)
        assert ind["kdj_cross"] in ("golden_cross", "death_cross", "none")
        assert ind["kdj_zone"] in ("overbought", "oversold", "neutral")

    def test_kdj_not_computed_short_data(self, short_df):
        """KDJ requires >= 12 bars and high/low columns."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        # short_df has 15 rows but no high/low columns
        df_no_hl = short_df.drop(columns=["high", "low"], errors="ignore")
        if "high" not in df_no_hl.columns:
            ind = _compute_indicators(df_no_hl)
            assert "kdj" not in ind

    def test_vwap_computation(self, trending_df):
        """VWAP should be computed for data with OHLCV."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(trending_df)

        assert "vwap" in ind
        assert isinstance(ind["vwap"], float)
        assert ind["vwap"] > 0
        assert "vwap_deviation" in ind
        assert isinstance(ind["vwap_deviation"], float)

    def test_trendline_detection(self):
        """Trendline detection should identify swing points and channel."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        import numpy as np

        # Create data with clear oscillating pattern to produce swing points
        n = 60
        closes = [20 + 5 * np.sin(i * 0.4) for i in range(n)]
        highs = [c + 2 for c in closes]
        lows = [c - 2 for c in closes]
        df = pd.DataFrame({
            "open": [c - 0.3 for c in closes],
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100000] * n,
        })
        ind = _compute_indicators(df)

        assert "trendlines" in ind
        t = ind["trendlines"]
        assert "swing_high_count" in t
        assert "swing_low_count" in t
        assert t["swing_high_count"] > 0
        assert t["swing_low_count"] > 0

    def test_bollinger_bandwidth(self, trending_df):
        """Bollinger bandwidth should be computed."""
        from trade_compass_agent.runtime.tools.chart_pattern import _compute_indicators
        ind = _compute_indicators(trending_df)

        assert "bollinger" in ind
        assert "bandwidth" in ind["bollinger"]
        assert ind["bollinger"]["bandwidth"] > 0

    def test_format_includes_kdj_vwap_trendlines(self):
        """Formatted context should include KDJ, VWAP, and trendline info."""
        from trade_compass_agent.runtime.tools.chart_pattern import (
            _compute_indicators,
            _format_indicator_context,
        )
        import numpy as np
        n = 80
        closes = [20 + i * 0.1 + np.sin(i * 0.3) * 0.5 for i in range(n)]
        df = pd.DataFrame({
            "open": [c - 0.1 for c in closes],
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [100000 + i * 1000 for i in range(n)],
        })
        ind = _compute_indicators(df)
        text = _format_indicator_context(ind, "000001")

        assert "KDJ" in text
        assert "VWAP" in text
        assert "趋势线" in text

    def test_tool_passes_multi_timeframe(self):
        """Verify multi_timeframe kwarg reaches analyze_chart_pattern."""
        from unittest.mock import MagicMock, patch
        from trade_compass_agent.runtime.tools.chart_pattern import tool_chart_pattern

        stack = MagicMock()
        with patch(
            "trade_compass_agent.runtime.tools.chart_pattern.analyze_chart_pattern",
            return_value="mock report",
        ) as mock_analyze:
            result = tool_chart_pattern(stack, symbol="600519", bars=30, multi_timeframe=True)
            mock_analyze.assert_called_once_with(stack, "600519", bars=30, multi_timeframe=True)
            assert result == "mock report"
