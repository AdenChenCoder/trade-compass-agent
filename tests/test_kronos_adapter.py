"""Unit tests for kronos_adapter and kline_forecast tool (mocked model)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trade_compass_agent.data.kronos_adapter import (
    ForecastBar,
    ForecastResult,
    _bars_to_df,
    _generate_future_timestamps,
    _infer_freq,
    forecast_kline,
    is_kronos_available,
)
from trade_compass_agent.domain.models import Bar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(n: int = 60, base_price: float = 100.0, start: datetime | None = None) -> list[Bar]:
    """Generate n synthetic daily bars."""
    rng = np.random.RandomState(42)
    bars: list[Bar] = []
    dt = start or datetime(2025, 1, 2)
    price = base_price
    for _ in range(n):
        change = rng.normal(0, 0.01) * price
        o = price + change * 0.3
        c = price + change
        h = max(o, c) + abs(rng.normal(0, 0.003)) * price
        l = min(o, c) - abs(rng.normal(0, 0.003)) * price
        v = max(1000, int(50000 + rng.normal(0, 5000)))
        bars.append(Bar(
            symbol="TEST",
            timestamp=dt,
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            close=round(c, 2),
            volume=v,
            amount=round(v * (o + c) / 2, 0),
        ))
        price = c
        dt += timedelta(days=1)
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
    return bars


def _make_mock_predictor(horizon: int, base_price: float = 100.0):
    """Return a mock predictor that produces a deterministic pred_df."""
    predictor = MagicMock()
    predictor.device = "cpu"

    def fake_predict(df, x_timestamp, y_timestamp, pred_len, **kw):
        rng = np.random.RandomState(hash(kw.get("T", 1.0)) % 2**31)
        rows = []
        p = base_price
        for _ in range(pred_len):
            delta = rng.normal(0, 0.005) * p
            o = p + delta * 0.3
            c = p + delta
            h = max(o, c) + abs(rng.normal(0, 0.002)) * p
            l = min(o, c) - abs(rng.normal(0, 0.002)) * p
            v = max(100, int(50000 + rng.normal(0, 3000)))
            rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v, "amount": v * p})
            p = c
        return pd.DataFrame(rows, index=y_timestamp[:pred_len])

    predictor.predict = MagicMock(side_effect=fake_predict)
    return predictor


# ---------------------------------------------------------------------------
# _bars_to_df
# ---------------------------------------------------------------------------

class TestBarsToDF:

    def test_returns_correct_columns(self):
        bars = _make_bars(5)
        df, ts = _bars_to_df(bars)
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]
        assert len(df) == 5
        assert len(ts) == 5

    def test_timestamps_match(self):
        bars = _make_bars(3)
        _, ts = _bars_to_df(bars)
        for i, b in enumerate(bars):
            assert ts.iloc[i] == b.timestamp

    def test_amount_fallback(self):
        bar = Bar(
            symbol="X", timestamp=datetime(2025, 1, 1),
            open=10.0, high=11.0, low=9.0, close=10.5,
            volume=1000, amount=0,
        )
        df, _ = _bars_to_df([bar])
        expected = 1000 * (10 + 11 + 9 + 10.5) / 4
        assert abs(df.iloc[0]["amount"] - expected) < 0.01


# ---------------------------------------------------------------------------
# _infer_freq
# ---------------------------------------------------------------------------

class TestInferFreq:

    def test_daily_bars(self):
        bars = _make_bars(10)
        freq = _infer_freq(bars)
        assert freq == timedelta(days=1)

    def test_single_bar_defaults_daily(self):
        bars = _make_bars(1)
        assert _infer_freq(bars) == timedelta(days=1)

    def test_intraday_bars(self):
        dt = datetime(2025, 6, 1, 9, 30)
        bars = []
        for i in range(10):
            bars.append(Bar(
                symbol="X", timestamp=dt + timedelta(minutes=i * 5),
                open=10, high=11, low=9, close=10, volume=100,
            ))
        freq = _infer_freq(bars)
        assert freq == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# _generate_future_timestamps
# ---------------------------------------------------------------------------

class TestGenerateFutureTimestamps:

    def test_daily_skips_weekends(self):
        friday = datetime(2025, 6, 6)  # Friday
        ts = _generate_future_timestamps(friday, timedelta(days=1), 3)
        assert ts.iloc[0].weekday() == 0  # Monday
        assert ts.iloc[1].weekday() == 1  # Tuesday
        assert ts.iloc[2].weekday() == 2  # Wednesday

    def test_correct_count(self):
        ts = _generate_future_timestamps(datetime(2025, 1, 2), timedelta(days=1), 5)
        assert len(ts) == 5

    def test_intraday_no_weekend_skip(self):
        friday = datetime(2025, 6, 6, 14, 30)
        ts = _generate_future_timestamps(friday, timedelta(minutes=30), 3)
        assert ts.iloc[0] == friday + timedelta(minutes=30)
        assert ts.iloc[1] == friday + timedelta(minutes=60)


# ---------------------------------------------------------------------------
# forecast_kline (mocked predictor)
# ---------------------------------------------------------------------------

class TestForecastKline:

    def test_too_few_bars_raises(self):
        bars = _make_bars(10)
        with pytest.raises(ValueError, match="at least 30"):
            forecast_kline(bars, "X", horizon=5)

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_returns_forecast_result(self, mock_get):
        bars = _make_bars(60)
        last_close = bars[-1].close
        mock_get.return_value = _make_mock_predictor(5, base_price=last_close)

        result = forecast_kline(bars, "TEST", horizon=5, sample_count=3)

        assert isinstance(result, ForecastResult)
        assert result.symbol == "TEST"
        assert result.horizon == 5
        assert result.lookback_used == 60
        assert len(result.mean_bars) == 5
        assert len(result.sample_paths) == 3
        assert len(result.confidence_upper) == 5
        assert len(result.confidence_lower) == 5

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_mean_bars_between_confidence_bounds(self, mock_get):
        bars = _make_bars(60)
        mock_get.return_value = _make_mock_predictor(5, base_price=bars[-1].close)

        result = forecast_kline(bars, "TEST", horizon=5, sample_count=5)

        for i, mb in enumerate(result.mean_bars):
            assert result.confidence_lower[i] <= mb.close <= result.confidence_upper[i]

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_forecast_bar_timestamps_are_weekdays(self, mock_get):
        bars = _make_bars(60)
        mock_get.return_value = _make_mock_predictor(10, base_price=bars[-1].close)

        result = forecast_kline(bars, "TEST", horizon=10, sample_count=2)

        for fb in result.mean_bars:
            assert fb.timestamp.weekday() < 5, f"{fb.timestamp} is a weekend"

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_truncates_to_max_context(self, mock_get):
        bars = _make_bars(200)
        mock_get.return_value = _make_mock_predictor(5, base_price=bars[-1].close)

        # small model has max_context=512, 200 bars should not be truncated
        result = forecast_kline(bars, "TEST", horizon=5, model_size="small", sample_count=1)
        assert result.lookback_used == 200

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_volumes_non_negative(self, mock_get):
        bars = _make_bars(60)
        mock_get.return_value = _make_mock_predictor(5, base_price=bars[-1].close)

        result = forecast_kline(bars, "TEST", horizon=5, sample_count=2)

        for fb in result.mean_bars:
            assert fb.volume >= 0

    @patch("trade_compass_agent.data.kronos_adapter._get_predictor")
    def test_model_id_from_registry(self, mock_get):
        bars = _make_bars(60)
        mock_get.return_value = _make_mock_predictor(3, base_price=bars[-1].close)

        result = forecast_kline(bars, "X", horizon=3, model_size="small", sample_count=1)
        assert result.model_id == "NeoQuasar/Kronos-small"


# ---------------------------------------------------------------------------
# is_kronos_available
# ---------------------------------------------------------------------------

class TestIsKronosAvailable:

    def test_returns_bool(self):
        assert isinstance(is_kronos_available(), bool)

    @patch.dict("sys.modules", {"torch": None})
    def test_false_when_torch_missing(self):
        with patch("builtins.__import__", side_effect=ImportError("no torch")):
            assert not is_kronos_available()


# ---------------------------------------------------------------------------
# tool_kline_forecast (mocked)
# ---------------------------------------------------------------------------

class TestToolKlineForecast:

    def test_missing_symbol_returns_error(self):
        import json
        from trade_compass_agent.runtime.tools.kline_forecast import tool_kline_forecast

        stack = MagicMock()
        result = json.loads(tool_kline_forecast(stack))
        assert "error" in result

    @patch("trade_compass_agent.data.kronos_adapter.forecast_kline")
    @patch("trade_compass_agent.data.kronos_adapter.is_kronos_available", return_value=True)
    def test_returns_forecast_json(self, mock_avail, mock_forecast):
        import json
        from trade_compass_agent.runtime.tools.kline_forecast import tool_kline_forecast

        bars = _make_bars(60)
        mock_forecast.return_value = ForecastResult(
            symbol="600519",
            model_id="NeoQuasar/Kronos-small",
            lookback_used=60,
            horizon=3,
            mean_bars=[
                ForecastBar(datetime(2025, 6, 1), 100, 102, 99, 101, 5000),
                ForecastBar(datetime(2025, 6, 2), 101, 103, 100, 102, 5100),
                ForecastBar(datetime(2025, 6, 3), 102, 104, 101, 103, 5200),
            ],
            confidence_upper=[102, 103, 104],
            confidence_lower=[99, 100, 101],
        )

        stack = MagicMock()
        stack.provider.get_bars.return_value = bars
        result = json.loads(tool_kline_forecast(stack, symbol="600519", horizon=3))

        assert result["symbol"] == "600519"
        assert len(result["forecast_bars"]) == 3
        assert "confidence_band" in result
        assert "disclaimer" in result
