"""Tests for anti-hallucination infrastructure: compute_ma, verifier, data-gap guardrail."""

from __future__ import annotations

import json

from trade_compass_agent.data.providers import SampleProvider
from trade_compass_agent.runtime.verifier import verify_claims, _extract_claims


class TestComputeMa:
    def test_returns_ma_values(self):
        from trade_compass_agent.runtime.tools.ta import tool_compute_ma
        from unittest.mock import MagicMock

        stack = MagicMock()
        bars = SampleProvider().get_bars("600519", timeframe="1d", limit=120)
        stack.provider.get_bars.return_value = bars

        result = json.loads(tool_compute_ma(stack, symbol="600519", timeframe="1d", periods="5,10,20"))
        assert "ma" in result
        assert "MA5" in result["ma"]
        assert "MA10" in result["ma"]
        assert "MA20" in result["ma"]
        assert result["last_close"] > 0
        assert result["position_vs_ma"]
        assert result["trend"] in ("bullish_alignment", "bearish_alignment", "mixed")

    def test_invalid_periods_returns_error(self):
        from trade_compass_agent.runtime.tools.ta import tool_compute_ma
        from unittest.mock import MagicMock

        stack = MagicMock()
        result = json.loads(tool_compute_ma(stack, symbol="600519", periods="abc"))
        assert "error" in result


class TestVerifyClaims:
    def test_no_claims_passes(self):
        result = verify_claims("这是一段没有数值的文本", [])
        assert result.ok is True
        assert result.claims_checked == 0

    def test_correct_price_passes(self):
        tool_results = [
            ("get_bars", json.dumps({"symbol": "600519", "bars": [{"close": 1823.5, "timestamp": "2025-01-01"}]})),
        ]
        text = "600519 当前收盘价 1823.5，走势平稳。"
        result = verify_claims(text, tool_results)
        assert result.ok is True

    def test_wrong_price_flagged(self):
        tool_results = [
            ("get_bars", json.dumps({"symbol": "600519", "bars": [{"close": 1823.0, "timestamp": "2025-01-01"}]})),
        ]
        text = "600519 当前收盘价 2050.0，已经突破前高。"
        result = verify_claims(text, tool_results)
        assert result.ok is False
        assert len(result.violations) == 1
        assert result.violations[0].actual == 1823.0

    def test_prediction_context_skipped(self):
        tool_results = [
            ("get_bars", json.dumps({"symbol": "600519", "bars": [{"close": 1823.0, "timestamp": "2025-01-01"}]})),
        ]
        text = "目标价位 2100.0 是一个合理的预测。当前收盘价 1823.0。"
        result = verify_claims(text, tool_results)
        assert result.ok is True


class TestExtractClaims:
    def test_extracts_rsi(self):
        text = "当前 RSI(14)=72.5，处于超买区间"
        claims = _extract_claims(text)
        rsi_claims = [c for c in claims if c.kind == "rsi"]
        assert len(rsi_claims) == 1
        assert rsi_claims[0].value == 72.5

    def test_extracts_pe(self):
        text = "市盈率 25.3 较行业均值偏高"
        claims = _extract_claims(text)
        pe_claims = [c for c in claims if c.kind == "pe"]
        assert len(pe_claims) == 1
        assert pe_claims[0].value == 25.3


class TestDataGapGuardrail:
    def test_warning_on_ungrounded_recommendation(self):
        from trade_compass_agent.runtime.loop import _check_data_gap

        text = "建议买入 600519，当前趋势向好。"
        tool_calls_log: list[tuple[str, str]] = []
        warning = _check_data_gap(text, tool_calls_log)
        assert warning is not None
        assert "600519" in warning
        assert "数据覆盖不足" in warning

    def test_no_warning_when_bars_exist(self):
        from trade_compass_agent.runtime.loop import _check_data_gap

        text = "建议买入 600519，均线多头排列。"
        tool_calls_log = [
            ("get_bars", json.dumps({"symbol": "600519", "bars": [{"close": 1823}]})),
        ]
        warning = _check_data_gap(text, tool_calls_log)
        assert warning is None

    def test_no_warning_without_directional_advice(self):
        from trade_compass_agent.runtime.loop import _check_data_gap

        text = "600519 今日成交量放大，关注后续走势。"
        tool_calls_log: list[tuple[str, str]] = []
        warning = _check_data_gap(text, tool_calls_log)
        assert warning is None

    def test_no_warning_when_specialist_covers_symbols(self):
        from trade_compass_agent.runtime.loop import _check_data_gap

        text = "建议买入 002297，技术面看好。"
        specialist_result = json.dumps({
            "results": [
                {
                    "specialist": "intraday_tech",
                    "task": "分析 002297 的日内走势",
                    "output": json.dumps({"analysis": "均线多头排列"}),
                }
            ]
        })
        tool_calls_log = [
            ("dispatch_specialists", specialist_result),
        ]
        warning = _check_data_gap(text, tool_calls_log)
        assert warning is None

    def test_no_warning_when_screener_signal_covers_symbols(self):
        from trade_compass_agent.runtime.loop import _check_data_gap

        text = "建议买入 002491 和 600498。"
        specialist_result = json.dumps({
            "results": [
                {
                    "specialist": "screener",
                    "task": "002491,600498",
                    "output": json.dumps({
                        "signals": [
                            {"symbol": "002491", "direction": "long"},
                            {"symbol": "600498", "direction": "long"},
                        ],
                        "count": 2,
                    }),
                }
            ]
        })
        tool_calls_log = [
            ("dispatch_specialists", specialist_result),
        ]
        warning = _check_data_gap(text, tool_calls_log)
        assert warning is None


class TestProvenanceFooter:
    def test_builds_footer_from_tool_calls(self):
        from trade_compass_agent.runtime.loop import _build_provenance_footer

        tool_calls_log = [
            ("get_bars", json.dumps({"provider": "akshare", "timestamp": "2025-06-07T15:00:00"})),
            ("compute_rsi", json.dumps({"symbol": "600519"})),
        ]
        footer = _build_provenance_footer(tool_calls_log)
        assert "akshare" in footer
        assert "get_bars" in footer
        assert "compute_rsi" in footer

    def test_empty_when_no_tools(self):
        from trade_compass_agent.runtime.loop import _build_provenance_footer

        assert _build_provenance_footer([]) == ""

    def test_lists_all_tools_without_truncation(self):
        from trade_compass_agent.runtime.loop import _build_provenance_footer

        tool_calls_log = [(f"tool_{index}", "{}") for index in range(10)]
        footer = _build_provenance_footer(tool_calls_log)
        for index in range(10):
            assert f"tool_{index}" in footer
