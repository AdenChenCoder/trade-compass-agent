from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from trade_compass_agent.domain import Bar
from trade_compass_agent.runtime.tools.ta import (
    tool_compute_bollinger,
    tool_compute_macd,
    tool_compute_rsi,
    tool_compute_volume_ratio,
)


def _make_bars(n: int = 60, base: float = 10.0) -> list[Bar]:
    import random

    random.seed(42)
    bars = []
    price = base
    for i in range(n):
        change = random.uniform(-0.03, 0.03)
        price *= 1 + change
        bars.append(
            Bar(
                symbol="600519",
                timestamp=datetime(2024, 1, 1 + i // 4, 9 + i % 4),
                open=round(price * 0.999, 2),
                high=round(price * 1.01, 2),
                low=round(price * 0.99, 2),
                close=round(price, 2),
                volume=float(random.randint(100000, 500000)),
            )
        )
    return bars


@pytest.fixture
def mock_stack():
    stack = MagicMock()
    stack.provider.get_bars.return_value = _make_bars(60)
    stack.config = MagicMock()
    return stack


def test_compute_rsi(mock_stack):
    result = json.loads(tool_compute_rsi(mock_stack, symbol="600519"))
    assert "current_rsi" in result
    assert 0 <= result["current_rsi"] <= 100
    assert result["interpretation"] in (
        "extremely_overbought", "overbought", "neutral", "oversold", "extremely_oversold"
    )
    assert len(result["recent_5"]) <= 5


def test_compute_macd(mock_stack):
    result = json.loads(tool_compute_macd(mock_stack, symbol="600519"))
    assert "current" in result
    assert "macd" in result["current"]
    assert "signal" in result["current"]
    assert "histogram" in result["current"]
    assert result["cross"] in ("golden_cross", "death_cross", "none")
    assert result["trend"] in ("bullish", "bearish")


def test_compute_bollinger(mock_stack):
    result = json.loads(tool_compute_bollinger(mock_stack, symbol="600519"))
    assert "current" in result
    assert result["current"]["upper"] >= result["current"]["middle"]
    assert result["current"]["middle"] >= result["current"]["lower"]
    assert "bandwidth" in result
    assert result["position"] in (
        "above_upper", "near_upper", "middle", "near_lower", "below_lower"
    )


def test_compute_volume_ratio(mock_stack):
    result = json.loads(tool_compute_volume_ratio(mock_stack, symbol="600519"))
    assert result["volume_ratio"] > 0
    assert result["interpretation"] in (
        "extreme_expansion", "strong_expansion", "moderate_expansion",
        "normal", "shrinkage", "extreme_shrinkage"
    )


def test_rsi_no_bars(mock_stack):
    mock_stack.provider.get_bars.return_value = []
    with pytest.raises(ValueError, match="no bars"):
        tool_compute_rsi(mock_stack, symbol="000000")
