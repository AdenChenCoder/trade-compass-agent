"""Tests for signal tracking and rule refinement."""


import pytest

from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.evaluation.rule_refinement import analyze_outcomes


@pytest.fixture
def tracker(tmp_path):
    return SignalTracker(tmp_path)


class TestSignalTracker:
    def test_track_new_signal(self, tracker):
        signal = tracker.track_signal({
            "signal_id": "sig-001",
            "symbol": "600519",
            "rating": "buy",
            "confidence": 0.8,
            "entry_price": 1800.0,
            "stop_loss": 1750.0,
            "target_price": 1900.0,
            "timestamp": "2025-01-01T09:30:00",
        })
        assert signal.signal_id == "sig-001"
        assert signal.status == "pending"

    def test_update_entry(self, tracker):
        tracker.track_signal({"signal_id": "sig-001", "symbol": "600519", "rating": "buy", "confidence": 0.7})
        tracker.update_entry("sig-001", 1800.0)
        active = tracker.get_active()
        assert len(active) == 1
        assert active[0].actual_entry == 1800.0
        assert active[0].status == "active"

    def test_update_exit_win(self, tracker):
        tracker.track_signal({"signal_id": "sig-001", "symbol": "600519", "rating": "buy", "confidence": 0.7})
        tracker.update_entry("sig-001", 1800.0)
        result = tracker.update_exit("sig-001", 1900.0, days_held=5)
        assert result is not None
        assert result.outcome == "win"
        assert result.actual_pnl > 0
        assert result.status == "closed"

    def test_update_exit_loss(self, tracker):
        tracker.track_signal({"signal_id": "sig-002", "symbol": "000001", "rating": "buy", "confidence": 0.6})
        tracker.update_entry("sig-002", 12.0)
        result = tracker.update_exit("sig-002", 10.0, days_held=3)
        assert result is not None
        assert result.outcome == "loss"
        assert result.actual_pnl < 0

    def test_get_stats_empty(self, tracker):
        stats = tracker.get_stats()
        assert stats["total"] == 0

    def test_get_stats_with_data(self, tracker):
        for i in range(5):
            tracker.track_signal({"signal_id": f"w-{i}", "symbol": "600519", "rating": "buy", "confidence": 0.7})
            tracker.update_entry(f"w-{i}", 100.0)
            tracker.update_exit(f"w-{i}", 110.0)

        for i in range(3):
            tracker.track_signal({"signal_id": f"l-{i}", "symbol": "000001", "rating": "buy", "confidence": 0.6})
            tracker.update_entry(f"l-{i}", 100.0)
            tracker.update_exit(f"l-{i}", 90.0)

        stats = tracker.get_stats()
        assert stats["total"] == 8
        assert stats["wins"] == 5
        assert stats["losses"] == 3
        assert abs(stats["win_rate"] - 0.625) < 0.01


class TestRuleRefinement:
    def test_insufficient_data(self, tmp_path):
        report = analyze_outcomes(tmp_path, min_signals=10)
        assert report.total_analyzed == 0
        assert report.insights == []

    def test_with_sufficient_data(self, tmp_path):
        tracker = SignalTracker(tmp_path)
        for i in range(15):
            conf = 0.9 if i < 10 else 0.5
            tracker.track_signal({
                "signal_id": f"s-{i}",
                "symbol": "600519",
                "rating": "strong_buy" if i < 10 else "buy",
                "confidence": conf,
            })
            tracker.update_entry(f"s-{i}", 100.0)
            exit_price = 110.0 if i < 5 else 92.0
            tracker.update_exit(f"s-{i}", exit_price)

        report = analyze_outcomes(tmp_path, min_signals=10)
        assert report.total_analyzed == 15
        assert report.confidence_bias != 0.0
