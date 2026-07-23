"""Tests for P0/P1/P2 fixes: cooldown, dispatch, nested_loop, follow_through, ETF, SSE."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


from trade_compass_agent.risk.cooldown import CooldownTracker


class TestCooldownCorruptJSON:
    def test_empty_file_returns_default(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        path.write_text("", encoding="utf-8")
        tracker = CooldownTracker(path)
        assert tracker.state.consecutive_losses == 0
        assert tracker.state.cooling is False

    def test_invalid_json_returns_default(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        path.write_text("{invalid json!!!", encoding="utf-8")
        tracker = CooldownTracker(path)
        assert tracker.state.consecutive_losses == 0

    def test_truncated_json_returns_default(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        path.write_text('{"consecutive_losses": 2, "co', encoding="utf-8")
        tracker = CooldownTracker(path)
        assert tracker.state.consecutive_losses == 0

    def test_valid_json_loads_correctly(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        path.write_text(json.dumps({
            "consecutive_losses": 5,
            "cooling": True,
            "updated_at": "2025-01-01T00:00:00",
        }), encoding="utf-8")
        tracker = CooldownTracker(path)
        assert tracker.state.consecutive_losses == 5
        assert tracker.state.cooling is True


class TestCooldownBreakeven:
    def test_breakeven_does_not_increment_losses(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        tracker = CooldownTracker(path, threshold=3)
        tracker.record_loss()
        tracker.record_loss()
        assert tracker.state.consecutive_losses == 2
        assert not tracker.is_active()


class TestCooldownThreadSafety:
    def test_concurrent_record_loss_no_data_loss(self, tmp_path: Path):
        path = tmp_path / "cooldown.json"
        tracker = CooldownTracker(path, threshold=100)
        errors = []

        def _record_n(n: int):
            try:
                for _ in range(n):
                    tracker.record_loss()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_record_n, args=(10,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert tracker.state.consecutive_losses == 50


class TestDispatchErrorBoundary:
    def test_single_specialist_failure_does_not_kill_batch(self):
        from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists

        stack = MagicMock()
        stack.config = MagicMock()

        with patch(
            "trade_compass_agent.runtime.tools.dispatch.run_specialist",
            side_effect=[Exception("LLM exploded"), "analysis complete"],
        ):
            result = json.loads(tool_dispatch_specialists(
                stack,
                [
                    {"specialist": "intraday_tech", "task": "scan 600519"},
                    {"specialist": "equity_research", "task": "research 600519"},
                ],
            ))

        assert len(result["results"]) == 2
        assert "error" in result["results"][0]["output"]
        assert result["results"][1]["output"] == "analysis complete"

    def test_invalid_tasks_type_returns_error(self):
        from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists

        stack = MagicMock()
        result = json.loads(tool_dispatch_specialists(stack, "not a list"))
        assert "error" in result

    def test_non_dict_items_handled_gracefully(self):
        from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists

        stack = MagicMock()
        stack.config = MagicMock()
        result = json.loads(tool_dispatch_specialists(stack, ["invalid_item"]))
        assert result["results"][0]["error"] == "invalid task item"


class TestNestedLoopErrorHandling:
    def test_llm_error_returns_json_error(self):
        from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop
        from trade_compass_agent.llm.providers import ChatMessage

        client = MagicMock()
        client.complete.side_effect = Exception("API timeout")

        result = run_react_loop(
            client=client,
            messages=[ChatMessage(role="user", content="test")],
            tool_schemas=[],
            execute_tool=lambda n, a: "{}",
            max_rounds=3,
        )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "API timeout" in parsed["error"]

    def test_tool_execution_error_captured(self):
        from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop
        from trade_compass_agent.llm.providers import ChatMessage, ChatCompletion, ToolCall

        call1 = ChatCompletion(
            content="",
            tool_calls=[ToolCall(id="tc1", name="bad_tool", arguments="{}")],
        )
        call2 = ChatCompletion(content="Final answer", tool_calls=None)
        client = MagicMock()
        client.complete.side_effect = [call1, call2]

        def _bad_exec(name, args):
            raise ValueError("tool exploded")

        result = run_react_loop(
            client=client,
            messages=[ChatMessage(role="user", content="test")],
            tool_schemas=[{"type": "function", "function": {"name": "bad_tool"}}],
            execute_tool=_bad_exec,
            max_rounds=3,
        )

        assert result == "Final answer"


class TestFollowThroughBearish:
    def test_bearish_action_inverts_returns(self):
        from trade_compass_agent.evaluation.follow_through import FollowThroughEvaluator
        from trade_compass_agent.domain import AuditEvent, Bar

        bars = []
        base_date = datetime(2025, 1, 1)
        for i in range(10):
            bars.append(Bar(
                symbol="600519",
                timestamp=base_date + timedelta(days=i),
                open=100.0 - i,
                high=101.0 - i,
                low=99.0 - i,
                close=100.0 - i,
                volume=1000,
            ))

        provider = MagicMock()
        provider.get_bars.return_value = bars

        event = AuditEvent(
            id="evt1",
            event_type="recommendation",
            timestamp=base_date,
            summary="avoid 600519",
            payload={"symbol": "600519", "action": "avoid"},
        )

        evaluator = FollowThroughEvaluator(provider)
        report = evaluator.evaluate([event])

        assert len(report.results) == 1
        result = report.results[0]
        assert result.return_1d is not None
        assert result.return_1d > 0

    def test_next_day_entry_avoids_lookahead(self):
        from trade_compass_agent.evaluation.follow_through import FollowThroughEvaluator
        from trade_compass_agent.domain import AuditEvent, Bar

        bars = []
        base_date = datetime(2025, 1, 1)
        for i in range(10):
            bars.append(Bar(
                symbol="600519",
                timestamp=base_date + timedelta(days=i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1000,
            ))

        provider = MagicMock()
        provider.get_bars.return_value = bars

        event = AuditEvent(
            id="evt1",
            event_type="recommendation",
            timestamp=base_date,
            summary="buy 600519",
            payload={"symbol": "600519", "grade_out": "buy"},
        )

        evaluator = FollowThroughEvaluator(provider)
        report = evaluator.evaluate([event])

        result = report.results[0]
        assert result.entry_close == 101.0


class TestETFClassification:
    def test_etf_prefixes(self):
        from trade_compass_agent.data.providers import infer_instrument_kind
        from trade_compass_agent.domain import InstrumentKind

        assert infer_instrument_kind("510300") == InstrumentKind.ETF
        assert infer_instrument_kind("159915") == InstrumentKind.ETF
        assert infer_instrument_kind("512100") == InstrumentKind.ETF
        assert infer_instrument_kind("588000") == InstrumentKind.ETF

    def test_stock_prefixes_not_misclassified(self):
        from trade_compass_agent.data.providers import infer_instrument_kind
        from trade_compass_agent.domain import InstrumentKind

        assert infer_instrument_kind("600519") == InstrumentKind.STOCK
        assert infer_instrument_kind("000001") == InstrumentKind.STOCK
        assert infer_instrument_kind("300750") == InstrumentKind.STOCK
        assert infer_instrument_kind("100001") == InstrumentKind.STOCK


class TestSSEReplay:
    def test_replay_without_last_event_id_returns_all(self):
        from trade_compass_agent.runtime.stream_buffer import SessionStreamBuffer
        from trade_compass_agent.runtime.types import TurnEvent

        buffer = SessionStreamBuffer(capacity=10)
        evt1 = buffer.append(TurnEvent(event="status", data={"text": "a"}, id="e1"))
        evt2 = buffer.append(TurnEvent(event="delta", data={"text": "b"}, id="e2"))

        replay = buffer.replay_after(None)
        assert len(replay) == 2
        assert replay[0] == evt1
        assert replay[1] == evt2
