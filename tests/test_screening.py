"""Tests for the screening engine."""

import numpy as np
import pandas as pd
import pytest

from trade_compass_agent.screening.config import ScreeningConfig
from trade_compass_agent.screening.engine import run_screening
from trade_compass_agent.screening.factors import FactorScores, compute_factor_scores
from trade_compass_agent.screening.filters import layer1_filter
from trade_compass_agent.screening.resonance import apply_resonance_bonus
from trade_compass_agent.screening.triggers import apply_trigger_bonus
from trade_compass_agent.screening.universe import StockInfo, filter_st


def _make_df(close_values: list[float], volume: float = 1e6) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for testing."""
    n = len(close_values)
    close = np.array(close_values)
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": np.full(n, volume),
        "amount": np.full(n, volume * close.mean()),
    })


@pytest.fixture
def sample_stocks():
    return [
        StockInfo(symbol="600519", name="贵州茅台"),
        StockInfo(symbol="000001", name="平安银行"),
        StockInfo(symbol="300750", name="宁德时代"),
        StockInfo(symbol="000002", name="*ST万科A"),
        StockInfo(symbol="688001", name="华兴源创"),
    ]


@pytest.fixture
def sample_df_map():
    np.random.seed(42)
    base = 100.0
    days = 60

    uptrend = base * np.cumprod(1 + np.random.normal(0.002, 0.015, days))
    downtrend = base * np.cumprod(1 + np.random.normal(-0.002, 0.015, days))
    sideways = base * np.cumprod(1 + np.random.normal(0.0, 0.01, days))

    return {
        "600519": _make_df(uptrend.tolist(), volume=5e6),
        "000001": _make_df(sideways.tolist(), volume=3e6),
        "300750": _make_df(uptrend.tolist() + [uptrend[-1] * 1.05], volume=4e6),
        "000002": _make_df(downtrend.tolist(), volume=1e5),
        "688001": _make_df(sideways.tolist(), volume=2e6),
    }


@pytest.fixture
def cfg():
    return ScreeningConfig(
        min_market_cap_yi=0.0,
        min_avg_amount_wan=0.0,
        boards=["600", "000", "300", "688"],
        top_n=3,
    )


class TestFilterST:
    def test_removes_st(self, sample_stocks):
        filtered = filter_st(sample_stocks)
        names = [s.name for s in filtered]
        assert "*ST万科A" not in names
        assert "贵州茅台" in names

    def test_keeps_normal(self, sample_stocks):
        filtered = filter_st(sample_stocks)
        assert len(filtered) == 4


class TestL1Filter:
    def test_basic_pass(self, sample_stocks, sample_df_map, cfg):
        result = layer1_filter(sample_stocks, sample_df_map, cfg)
        assert "600519" in result.passed
        assert "300750" in result.passed

    def test_no_data_rejected(self, sample_stocks, cfg):
        result = layer1_filter(sample_stocks, {}, cfg)
        assert len(result.passed) == 0
        for sym, reason in result.rejected.items():
            assert reason in ("no_data", "board_excluded", "st_or_delisting")

    def test_market_cap_filter(self, sample_stocks, sample_df_map):
        cfg = ScreeningConfig(min_market_cap_yi=100.0, boards=["600", "000", "300", "688"])
        cap_map = {"600519": 200.0, "000001": 50.0, "300750": 150.0, "000002": 30.0, "688001": 80.0}
        result = layer1_filter(sample_stocks, sample_df_map, cfg, market_cap_map=cap_map)
        assert "600519" in result.passed
        assert "300750" in result.passed
        assert "000001" not in result.passed

    def test_liquidity_filter(self, sample_stocks, sample_df_map):
        cfg = ScreeningConfig(min_avg_amount_wan=100000.0, boards=["600", "000", "300", "688"])
        result = layer1_filter(sample_stocks, sample_df_map, cfg)
        assert len(result.passed) == 0


class TestFactorScoring:
    def test_scores_produced(self, sample_df_map, cfg):
        symbols = list(sample_df_map.keys())
        scores = compute_factor_scores(symbols, sample_df_map, cfg)
        assert len(scores) > 0
        assert all(0.0 <= s.composite <= 1.0 for s in scores)

    def test_uptrend_scores_higher(self, cfg):
        uptrend = _make_df((100 * np.cumprod(1 + np.full(60, 0.005))).tolist())
        downtrend = _make_df((100 * np.cumprod(1 + np.full(60, -0.005))).tolist())

        df_map = {"UP": uptrend, "DOWN": downtrend}
        scores = compute_factor_scores(["UP", "DOWN"], df_map, cfg)
        score_map = {s.symbol: s.composite for s in scores}
        assert score_map["UP"] > score_map["DOWN"]

    def test_empty_input(self, cfg):
        scores = compute_factor_scores([], {}, cfg)
        assert scores == []


class TestResonanceBonus:
    def test_bonus_applied(self, cfg):
        scores = [
            FactorScores("A", 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5),
            FactorScores("B", 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5),
        ]
        result = apply_resonance_bonus(
            scores,
            hot_industries=["半导体"],
            industry_map={"A": "半导体", "B": "银行"},
            cfg=cfg,
        )
        a_score = next(s for s in result if s.symbol == "A")
        b_score = next(s for s in result if s.symbol == "B")
        assert a_score.composite > b_score.composite

    def test_no_bonus_without_data(self, cfg):
        scores = [FactorScores("A", 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5)]
        result = apply_resonance_bonus(scores, cfg=cfg)
        assert result[0].composite == 0.5


class TestTriggerBonus:
    def test_volume_breakout_bonus(self, cfg):
        volume = np.full(60, 1e6)
        volume[-1] = 3e6  # 3x average
        close = (100 * np.cumprod(1 + np.random.normal(0.001, 0.01, 60))).tolist()
        df = _make_df(close)
        df["volume"] = volume

        scores = [FactorScores("A", 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5)]
        result = apply_trigger_bonus(scores, {"A": df}, cfg)
        assert result[0].composite > 0.5


class TestFullEngine:
    def test_end_to_end(self, sample_stocks, sample_df_map, cfg):
        stocks = filter_st(sample_stocks)
        result = run_screening(sample_df_map, cfg, stocks=stocks)
        assert result.universe_size == len(stocks)
        assert result.l1_passed > 0
        assert len(result.top_n) <= cfg.top_n
        assert result.top_n[0].composite >= result.top_n[-1].composite

    def test_empty_data(self, sample_stocks, cfg):
        result = run_screening({}, cfg, stocks=sample_stocks)
        assert result.l1_passed == 0
        assert len(result.top_n) == 0

    def test_respects_top_n(self, sample_df_map, cfg):
        cfg.top_n = 2
        stocks = [StockInfo(symbol=s, name=s) for s in sample_df_map.keys()]
        result = run_screening(sample_df_map, cfg, stocks=stocks)
        assert len(result.top_n) <= 2
