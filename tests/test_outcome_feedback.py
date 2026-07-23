"""Tests for outcome-based KNOWLEDGE confidence feedback."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.memory.memory_store import ENTRY_DELIMITER, MemoryStore
from trade_compass_agent.ops.outcome_feedback import (
    apply_outcome_feedback,
    enrich_actuals_with_outcome_advisor,
    find_implicated_entries,
    is_disproven,
    parse_alert_symbols,
)
from trade_compass_agent.ops.reflection import JobReflection, PendingReflection


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    cfg = load_app_config()
    return replace(
        cfg,
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory_vault",
    )


@pytest.fixture
def mem_store(config: AppConfig) -> MemoryStore:
    store = MemoryStore(config.memory_dir)
    store.add("600183 突破20日线后趋势延续性较好", source="promotion")
    _stamp_promotion_meta(store)
    return store


def _stamp_promotion_meta(
    store: MemoryStore,
    *,
    idx: int = 0,
    job_id: str = "close",
    run_id: str = "",
    promoted_at: str = "2026-06-01T09:30:00+08:00",
    source_obs_ids: list[str] | None = None,
) -> None:
    row = store._meta["memory"][idx]
    row["promoted_by_job_id"] = job_id
    row["promoted_by_run_id"] = run_id
    row["promoted_at"] = promoted_at
    row["source_obs_ids"] = source_obs_ids or []
    store._save_meta()


class TestParseAlertSymbols:
    def test_extracts_six_digit_codes(self):
        alerts = ["600183 盈利21.6%，考虑止盈", "512400 亏损-47.9%"]
        assert parse_alert_symbols(alerts) == {"600183", "512400"}


class TestIsDisproven:
    def test_pnl_deviation(self, config: AppConfig):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }
        disproven, reasons = is_disproven(pending, actuals, config)
        assert disproven is True
        assert reasons[0]["signal"] == "pnl_deviation"

    def test_direction_wrong(self, config: AppConfig):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 4.0,
                    "actual_pnl_pct": -0.5,
                    "delta_pnl_pct": -4.5,
                },
            ],
        }
        disproven, reasons = is_disproven(pending, actuals, config)
        assert disproven is True
        assert reasons[0]["signal"] == "direction_wrong"

    def test_not_disproven_small_delta(self, config: AppConfig):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": 8.0,
                    "delta_pnl_pct": -2.0,
                },
            ],
        }
        disproven, _ = is_disproven(pending, actuals, config)
        assert disproven is False


class TestApplyOutcomeFeedback:
    def test_lowers_confidence_by_symbol(self, config: AppConfig, mem_store: MemoryStore):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }
        meta_before = mem_store.get_active_meta("memory")[0]
        assert meta_before["confidence"] == 0.85

        results = apply_outcome_feedback(pending, actuals, "lesson", mem_store, config)
        assert len(results) == 1
        assert results[0]["confidence"] == pytest.approx(0.55)
        assert results[0]["delta"] == pytest.approx(-0.30)
        assert results[0]["match_reason"] == "promoted_by_job_id+symbol+window"
        assert results[0]["outcome_reason"] == "outcome:pnl_deviation:promoted_by_job_id+symbol+window:close:2026-06-01"
        assert results[0]["implicated_symbols"] == ["600183"]
        assert results[0]["implicated_targets"] == ["600183"]
        assert results[0]["outcome_report"]["match_reason"] == "promoted_by_job_id+symbol+window"
        assert results[0]["outcome_report"]["delta"] == pytest.approx(-0.30)
        assert "pnl_deviation" in results[0]["explanation"]

        meta_after = mem_store.get_active_meta("memory")[0]
        assert meta_after["disproof_count"] == 1
        assert len(meta_after["adjustments"]) == 1
        assert meta_after["adjustments"][0]["reason"] == results[0]["outcome_reason"]

    def test_archives_after_second_disproof(self, config: AppConfig, mem_store: MemoryStore):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }
        apply_outcome_feedback(pending, actuals, "lesson", mem_store, config)
        apply_outcome_feedback(pending, actuals, "lesson", mem_store, config)

        assert mem_store.get_active_meta("memory") == []
        archived = mem_store._meta["memory"][0]
        assert archived["status"] == "archived"
        assert archived["confidence"] == 0.0
        assert archived["disproof_count"] == 2

    def test_no_change_when_not_disproven(self, config: AppConfig, mem_store: MemoryStore):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": 9.0,
                    "delta_pnl_pct": -1.0,
                },
            ],
        }
        results = apply_outcome_feedback(pending, actuals, "lesson", mem_store, config)
        assert results == []
        assert mem_store.get_active_meta("memory")[0]["confidence"] == 0.85

    def test_find_by_promoted_run_id(self, config: AppConfig, mem_store: MemoryStore):
        mem_store._meta["memory"][0]["promoted_by_run_id"] = "run-abc"
        mem_store._save_meta()
        pending = PendingReflection(job_id="close", run_id="run-abc", run_date="2026-06-01")
        entries = find_implicated_entries(mem_store, pending, {"999999"})
        assert len(entries) == 1

    def test_find_by_source_obs_ids(self, config: AppConfig, mem_store: MemoryStore):
        mem_store._meta["memory"][0]["source_obs_ids"] = ["obs-1"]
        mem_store._save_meta()
        pending = PendingReflection(
            job_id="close",
            run_id="r1",
            run_date="2026-06-01",
            predictions={"evidence": [{"source_obs_ids": ["obs-1"]}]},
        )
        entries = find_implicated_entries(mem_store, pending, {"999999"})
        assert len(entries) == 1

    def test_does_not_lower_confidence_by_symbol_only(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("600183 旧行情规律，缺少本次 promotion 归因", source="promotion")
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert results == []
        assert store.get_active_meta("memory")[0]["confidence"] == pytest.approx(0.85)

    def test_does_not_adjust_user_pinned_memory(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("600183 用户确认的长期偏好", source="user_pin")
        row = store._meta["memory"][0]
        row["promoted_by_run_id"] = "r1"
        row["promoted_by_job_id"] = "close"
        row["promoted_at"] = "2026-06-01T09:30:00+08:00"
        store._save_meta()
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert results == []
        assert store.get_active_meta("memory")[0]["confidence"] == 1.0

    def test_does_not_cross_adjust_different_job_window(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("600183 close job 规律", source="promotion")
        _stamp_promotion_meta(store, job_id="close")
        store.add("600183 open job 规律", source="promotion")
        _stamp_promotion_meta(store, idx=1, job_id="open")
        pending = PendingReflection(job_id="open", run_id="r-open", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": -5.0,
                    "delta_pnl_pct": -15.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert len(results) == 1
        assert results[0]["entry_text"] == "600183 open job 规律"
        metas = store.get_active_meta("memory")
        assert metas[0]["confidence"] == pytest.approx(0.85)
        assert metas[1]["confidence"] == pytest.approx(0.55)

    def test_mild_deviation_uses_base_delta(self, config: AppConfig, mem_store: MemoryStore):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "positions": [
                {
                    "symbol": "600183",
                    "predicted_pnl_pct": 10.0,
                    "actual_pnl_pct": 4.0,
                    "delta_pnl_pct": -6.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", mem_store, config)
        assert len(results) == 1
        assert results[0]["delta"] == pytest.approx(-0.15)
        assert results[0]["confidence"] == pytest.approx(0.70)

    def test_structured_missed_upside_outcome(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("半导体板块出现放量后适合重点关注", source="promotion")
        _stamp_promotion_meta(store, job_id="close")
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "outcomes": [
                {
                    "type": "missed_upside",
                    "target": "半导体",
                    "action": "watch",
                    "executed": False,
                    "actual_return_pct": 8.0,
                    "threshold_pct": 5.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert len(results) == 1
        assert results[0]["outcome_signals"] == ["missed_upside"]
        assert results[0]["implicated_symbols"] == ["半导体"]
        assert results[0]["confidence"] == pytest.approx(0.70)

    def test_multiple_independent_outcomes_increase_delta(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("半导体与新能源双主线可继续关注", source="promotion")
        _stamp_promotion_meta(store, job_id="close")
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")
        actuals = {
            "outcomes": [
                {
                    "type": "missed_upside",
                    "target": "半导体",
                    "actual_return_pct": 6.0,
                    "threshold_pct": 5.0,
                },
                {
                    "type": "missed_upside",
                    "target": "新能源",
                    "actual_return_pct": 6.0,
                    "threshold_pct": 5.0,
                },
            ],
        }

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert len(results) == 1
        assert results[0]["delta"] == pytest.approx(-0.225)
        assert results[0]["confidence"] == pytest.approx(0.625)
        assert results[0]["outcome_report"]["signals"][0]["signal"] == "missed_upside"

    def test_advisor_candidates_are_sanitized_and_gated(self, config: AppConfig):
        store = MemoryStore(config.memory_dir)
        store.add("半导体板块突破后应继续跟踪", source="promotion")
        _stamp_promotion_meta(store, job_id="close")
        pending = PendingReflection(
            job_id="close",
            run_id="r1",
            run_date="2026-06-01",
            predictions={"summary": "半导体板块值得关注"},
        )

        def fake_llm(system_prompt: str, user_content: str) -> str:
            assert "cannot change memory" in system_prompt
            assert "半导体" in user_content
            return """
            ```json
            {
              "outcomes": [
                {
                  "type": "missed_upside",
                  "target": "半导体",
                  "actual_return_pct": 9.0,
                  "threshold_pct": 5.0,
                  "explanation": "advisor candidate only"
                },
                {"type": "unsupported", "target": "半导体", "actual_return_pct": 99}
              ]
            }
            ```
            """

        actuals = enrich_actuals_with_outcome_advisor(
            pending,
            {},
            "半导体后续上涨",
            fake_llm,
            max_candidates=5,
        )
        assert actuals["outcomes"] == [
            {
                "type": "missed_upside",
                "target": "半导体",
                "symbol": "",
                "advisor_source": "llm",
                "actual_return_pct": 9.0,
                "threshold_pct": 5.0,
                "advisor_explanation": "advisor candidate only",
            },
        ]

        results = apply_outcome_feedback(pending, actuals, "lesson", store, config)
        assert len(results) == 1
        assert results[0]["outcome_report"]["signals"][0]["advisor_source"] == "llm"

    def test_advisor_respects_zero_max_candidates(self, config: AppConfig):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")

        actuals = enrich_actuals_with_outcome_advisor(
            pending,
            {},
            "lesson",
            lambda *_: '{"outcomes":[{"type":"missed_upside","target":"半导体","actual_return_pct":9}]}',
            max_candidates=0,
        )
        assert actuals == {}

    def test_invalid_advisor_output_is_noop(self, config: AppConfig):
        pending = PendingReflection(job_id="close", run_id="r1", run_date="2026-06-01")

        actuals = enrich_actuals_with_outcome_advisor(
            pending,
            {"positions": []},
            "lesson",
            lambda *_: "not json",
        )
        assert actuals == {"positions": []}

    def test_reflection_hook_uses_advisor_when_enabled(
        self,
        config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ):
        cfg = replace(
            config,
            memory=replace(
                config.memory,
                governance=replace(config.memory.governance, outcome_advisor_enabled=True),
            ),
        )
        reflection = JobReflection(cfg.memory_dir)
        store = MemoryStore(cfg.memory_dir)
        store.add("半导体板块突破后应继续跟踪", source="promotion")
        _stamp_promotion_meta(store, job_id="close")
        reflection.store_pending(
            "close",
            "run-close",
            predictions={"summary": "半导体板块值得关注"},
            summary="收盘",
            run_date=__import__("datetime").date(2026, 6, 1),
        )

        class FakeClient:
            def complete(self, messages):
                assert len(messages) == 2
                return __import__("types").SimpleNamespace(
                    content='{"outcomes":[{"type":"missed_upside","target":"半导体","actual_return_pct":8,"threshold_pct":5}]}'
                )

        monkeypatch.setattr(
            "trade_compass_agent.llm.providers.create_chat_client",
            lambda app_config: FakeClient(),
        )

        resolved = reflection.resolve_pending(
            "close",
            resolve_fn=lambda pending: ({}, "半导体随后上涨"),
            mem_store=store,
            config=cfg,
        )

        assert resolved[0].actuals["outcomes"][0]["advisor_source"] == "llm"
        meta = store._meta["memory"][0]
        assert meta["disproof_count"] == 1
        assert meta["confidence"] == pytest.approx(0.70)


class TestReflectionHook:
    def test_resolve_pending_triggers_feedback(self, config: AppConfig, monkeypatch: pytest.MonkeyPatch):
        reflection = JobReflection(config.memory_dir)
        mem_store = MemoryStore(config.memory_dir)
        mem_store._atomic_write(
            mem_store._memory_file,
            ENTRY_DELIMITER.join(["600183 趋势规律"]),
        )
        mem_store._meta["memory"] = [{
            "text": "600183 趋势规律",
            "confidence": 0.85,
            "source": "promotion",
            "status": "active",
            "disproof_count": 0,
            "adjustments": [],
            "promoted_by_job_id": "close",
            "promoted_at": "2026-06-01T09:30:00+08:00",
        }]
        mem_store._save_meta()

        reflection.store_pending(
            "close",
            "run-close",
            predictions={"mark_to_market": {"positions": [{"symbol": "600183", "pnl_pct": 10.0}]}},
            summary="收盘",
            run_date=__import__("datetime").date(2026, 6, 1),
        )

        pos = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
            symbol="600183", avg_cost=100.0, last_price=80.0,
        )
        monkeypatch.setattr(
            "trade_compass_agent.portfolio.JsonPaperPortfolio",
            lambda *a, **k: __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
                positions_with_market_prices=lambda provider: [pos],
            ),
        )
        monkeypatch.setattr(
            "trade_compass_agent.runtime.market_stack.MarketStack.from_config",
            lambda cfg: __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(provider=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()),
        )

        from trade_compass_agent.ops.reflection_resolver import make_market_resolve_fn

        reflection.resolve_pending(
            "close",
            resolve_fn=make_market_resolve_fn(config),
            mem_store=mem_store,
            config=config,
        )

        meta = mem_store._meta["memory"][0]
        assert meta["disproof_count"] == 1
        assert meta["confidence"] == pytest.approx(0.55)
