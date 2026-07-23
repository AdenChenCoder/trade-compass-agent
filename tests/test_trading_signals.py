"""Tests for the TradingSignal schema and emit_signal tool."""

import json
from unittest.mock import MagicMock

import pytest

from trade_compass_agent.domain.signals import (
    SignalRating,
    TradingSignal,
    parse_signal_rating,
    render_trading_signal,
)
from trade_compass_agent.runtime.tools.signals import tool_emit_signal


@pytest.fixture
def tmp_data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def mock_stack(tmp_data_dir):
    stack = MagicMock()
    stack.config.data_dir = tmp_data_dir
    return stack


class TestTradingSignalSchema:
    def test_create_signal_defaults(self):
        sig = TradingSignal(
            symbol="600519",
            rating=SignalRating.BUY,
            confidence=0.8,
            reasoning="MA20 above MA60, RSI at 55",
        )
        assert sig.symbol == "600519"
        assert sig.rating == SignalRating.BUY
        assert sig.confidence == 0.8
        assert sig.signal_id  # auto-generated
        assert sig.timestamp  # auto-generated

    def test_create_signal_full(self):
        sig = TradingSignal(
            symbol="000001",
            rating=SignalRating.STRONG_BUY,
            confidence=0.95,
            entry_price=15.5,
            stop_loss=14.0,
            target_price=18.0,
            risk_reward_ratio=1.67,
            reasoning="Breakout confirmed with volume surge",
            source_specialist="screener",
            source_tools=["get_bars", "compute_rsi", "compute_ma"],
        )
        assert sig.entry_price == 15.5
        assert sig.risk_reward_ratio == 1.67
        assert sig.source_tools == ["get_bars", "compute_rsi", "compute_ma"]

    def test_confidence_bounds(self):
        sig = TradingSignal(
            symbol="600519",
            rating=SignalRating.HOLD,
            confidence=0.0,
            reasoning="Neutral",
        )
        assert sig.confidence == 0.0

    def test_serialization(self):
        sig = TradingSignal(
            symbol="600519",
            rating=SignalRating.SELL,
            confidence=0.7,
            reasoning="Distribution pattern",
        )
        data = sig.model_dump()
        assert data["symbol"] == "600519"
        assert data["rating"] == "sell"
        restored = TradingSignal.model_validate(data)
        assert restored.rating == SignalRating.SELL


class TestRenderTradingSignal:
    def test_basic_render(self):
        sig = TradingSignal(
            symbol="600519",
            rating=SignalRating.BUY,
            confidence=0.85,
            entry_price=1850.0,
            stop_loss=1780.0,
            target_price=2000.0,
            reasoning="Trend continuation",
        )
        text = render_trading_signal(sig)
        assert "600519" in text
        assert "看多" in text
        assert "85%" in text
        assert "1850" in text
        assert "1780" in text
        assert "2000" in text


class TestParseSignalRating:
    def test_parse_from_rating_line(self):
        assert parse_signal_rating("**Rating**: strong_buy") == SignalRating.STRONG_BUY

    def test_parse_from_body(self):
        text = "Based on analysis, I recommend a buy for this stock."
        assert parse_signal_rating(text) == SignalRating.BUY

    def test_parse_priority(self):
        text = "This is a strong_sell signal"
        assert parse_signal_rating(text) == SignalRating.STRONG_SELL

    def test_parse_default_hold(self):
        assert parse_signal_rating("no clear direction") == SignalRating.HOLD


class TestEmitSignalTool:
    def test_emit_basic(self, mock_stack, tmp_data_dir):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="buy",
            confidence=0.8,
            reasoning="MA alignment bullish",
            source_specialist="intraday_tech",
            source_tools=["get_bars", "compute_ma"],
        )
        data = json.loads(result)
        assert data["status"] == "recorded"
        assert data["symbol"] == "600519"
        assert data["rating"] == "buy"
        assert data["signal_id"]

        signals_file = tmp_data_dir / "signals.jsonl"
        assert signals_file.exists()
        line = signals_file.read_text().strip()
        sig_data = json.loads(line)
        assert sig_data["symbol"] == "600519"

        audit_file = tmp_data_dir / "audit.jsonl"
        assert audit_file.exists()
        audit_data = json.loads(audit_file.read_text().strip())
        assert audit_data["event_type"] == "trading_signal"

    def test_emit_with_prices(self, mock_stack, tmp_data_dir):
        result = tool_emit_signal(
            mock_stack,
            symbol="000001",
            rating="strong_buy",
            confidence=0.9,
            entry_price=15.0,
            stop_loss=14.0,
            target_price=18.0,
            reasoning="Breakout with volume",
        )
        data = json.loads(result)
        assert data["risk_reward_ratio"] == 3.0
        assert data["entry_price"] == 15.0

    def test_emit_missing_symbol(self, mock_stack):
        result = tool_emit_signal(
            mock_stack,
            symbol="",
            rating="buy",
            confidence=0.5,
            reasoning="test",
        )
        data = json.loads(result)
        assert "error" in data

    def test_emit_missing_reasoning(self, mock_stack):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="buy",
            confidence=0.5,
            reasoning="",
        )
        data = json.loads(result)
        assert "error" in data

    def test_emit_invalid_rating(self, mock_stack):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="mega_buy",
            confidence=0.5,
            reasoning="test reason",
        )
        data = json.loads(result)
        assert "error" in data
        assert "invalid rating" in data["error"]

    def test_emit_clamps_confidence(self, mock_stack, tmp_data_dir):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="hold",
            confidence=1.5,
            reasoning="overconfident test",
        )
        data = json.loads(result)
        assert data["confidence"] == 1.0

    def test_source_tools_string_parsing(self, mock_stack, tmp_data_dir):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="sell",
            confidence=0.6,
            reasoning="breakdown",
            source_tools="get_bars, compute_rsi",
        )
        data = json.loads(result)
        assert data["status"] == "recorded"

        line = (tmp_data_dir / "signals.jsonl").read_text().strip()
        sig = json.loads(line)
        assert sig["source_tools"] == ["get_bars", "compute_rsi"]

    def test_source_skills_are_persisted_and_tracked(self, mock_stack, tmp_data_dir):
        result = tool_emit_signal(
            mock_stack,
            symbol="600519",
            rating="hold",
            confidence=0.6,
            reasoning="skill attribution test",
            source_skills="contextual-take-profit, portfolio-rebalance",
        )
        data = json.loads(result)
        assert data["source_skills"] == ["contextual-take-profit", "portfolio-rebalance"]

        signal = json.loads((tmp_data_dir / "signals.jsonl").read_text().strip())
        assert signal["source_skills"] == ["contextual-take-profit", "portfolio-rebalance"]

        tracked = json.loads((tmp_data_dir / "signal_tracking.jsonl").read_text().strip())
        assert tracked["source_skills"] == ["contextual-take-profit", "portfolio-rebalance"]
