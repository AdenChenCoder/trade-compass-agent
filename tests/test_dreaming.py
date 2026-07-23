"""Tests for the Dreaming mechanism (Phases 0-6)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from trade_compass_agent.memory.observation_store import Observation, ObservationStore
from trade_compass_agent.memory.dream_diary import PROCEDURE_EXTRACTION_PROMPT, run_procedure_extraction


# ═══════════════════════════════════════════════════════════════════════════
# Phase 0: Recall tracking
# ═══════════════════════════════════════════════════════════════════════════

class TestRecallTracking:
    def _make_store(self, tmp_path: Path) -> ObservationStore:
        store = ObservationStore(tmp_path / "obs.db")
        store.append("s1", "get_bars", "600519 突破 MA20", importance=7, concepts=["600519", "突破"])
        store.append("s1", "get_bars", "300750 放量上涨", importance=6, concepts=["300750", "放量"])
        store.append("s2", "search_decisions", "002415 止损卖出", importance=8, concepts=["002415", "止损"])
        return store

    def test_record_recall_increments_count(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        obs = store.recent(limit=3)
        obs_id = obs[0].id
        store.record_recall([obs_id], session_id="test-session")
        updated = store.recent(limit=3)
        target = [o for o in updated if o.id == obs_id][0]
        assert target.recall_count == 1
        assert target.last_recalled_at is not None

    def test_record_recall_tracks_days(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        obs = store.recent(limit=1)
        obs_id = obs[0].id
        store.record_recall([obs_id])
        store.record_recall([obs_id])
        updated = [o for o in store.recent(limit=3) if o.id == obs_id][0]
        assert updated.recall_count == 2
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in (updated.recall_days or [])

    def test_record_recall_tracks_sessions(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        obs = store.recent(limit=1)
        obs_id = obs[0].id
        store.record_recall([obs_id], session_id="sess-a")
        store.record_recall([obs_id], session_id="sess-b")
        store.record_recall([obs_id], session_id="sess-a")  # duplicate
        updated = [o for o in store.recent(limit=3) if o.id == obs_id][0]
        assert len(updated.unique_sessions_recalled or []) == 2


def test_procedure_extraction_prefers_patch_before_create():
    assert 'skill_manage(action="list")' in PROCEDURE_EXTRACTION_PROMPT
    assert 'skill_manage(action="view")' in PROCEDURE_EXTRACTION_PROMPT
    assert 'skill_manage(action="patch")' in PROCEDURE_EXTRACTION_PROMPT
    assert 'patch 比 create 优先' in PROCEDURE_EXTRACTION_PROMPT
    assert '才 skill_manage(action="create")' in PROCEDURE_EXTRACTION_PROMPT


def test_procedure_extraction_uses_dreaming_skill_actor(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, config, *, job_id, memory_actor=None, **kwargs):
            captured["job_id"] = job_id
            captured["memory_actor"] = memory_actor

        def run(self, prompt, *, timeout):
            return "ok"

    monkeypatch.setattr("trade_compass_agent.ops.agent_session.ScheduledAgentSession", FakeSession)

    assert run_procedure_extraction(config=object(), strong_patterns=[]) == "ok"
    assert captured == {"job_id": "dreaming-procedural", "memory_actor": "dreaming"}

    def test_mark_promoted(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        obs = store.recent(limit=1)
        store.mark_promoted([obs[0].id])
        updated = [o for o in store.recent(limit=3) if o.id == obs[0].id][0]
        assert updated.promoted_at is not None

    def test_promotion_candidates_filter(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        obs = store.recent(limit=3)
        # Mark one as consolidated + give recalls
        store.mark_consolidated([obs[0].id])
        store.record_recall([obs[0].id], session_id="s1")
        store.record_recall([obs[0].id], session_id="s2")
        candidates = store.promotion_candidates(min_signal=2)
        assert len(candidates) >= 1
        assert all(c.total_signal >= 2 for c in candidates)

    def test_concept_frequency(self, tmp_path: Path):
        store = self._make_store(tmp_path)
        freq = store.concept_frequency(lookback_days=1)
        assert "600519" in freq
        assert freq["600519"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: Multi-signal promotion scoring
# ═══════════════════════════════════════════════════════════════════════════

class TestPromotionScoring:
    def _make_obs(self, **kwargs) -> Observation:
        defaults = dict(
            id="test-obs-1", session_id="s1", tool_name="get_bars",
            summary="600519 突破 MA20 放量", raw_preview="", importance=8,
            concepts=["600519", "突破", "MA20"], created_at=datetime.now(timezone.utc).isoformat(),
            dedup_hash="abc123", consolidated=True,
            recall_count=5, recall_days=["2025-06-10", "2025-06-11", "2025-06-12"],
            last_recalled_at=datetime.now(timezone.utc).isoformat(),
            unique_sessions_recalled=["s1", "s2", "s3"],
            promoted_at=None,
        )
        defaults.update(kwargs)
        return Observation(**defaults)

    def test_compute_score_high_quality(self):
        from trade_compass_agent.memory.promotion import compute_promotion_score
        obs = self._make_obs()
        result = compute_promotion_score(obs)
        assert result.score > 0.6
        assert "recall_frequency" in result.dimension_scores

    def test_compute_score_low_quality(self):
        from trade_compass_agent.memory.promotion import compute_promotion_score
        obs = self._make_obs(
            recall_count=0, recall_days=[], last_recalled_at=None,
            unique_sessions_recalled=[], importance=3, concepts=[],
        )
        result = compute_promotion_score(obs)
        assert result.score < 0.3

    def test_gate_blocks_unpromoted(self):
        from trade_compass_agent.memory.promotion import _passes_gate
        obs = self._make_obs(recall_count=0, recall_days=[])
        assert not _passes_gate(obs)

    def test_gate_blocks_already_promoted(self):
        from trade_compass_agent.memory.promotion import _passes_gate
        obs = self._make_obs(promoted_at="2025-06-01T00:00:00")
        assert not _passes_gate(obs)

    def test_gate_blocks_unconsolidated(self):
        from trade_compass_agent.memory.promotion import _passes_gate
        obs = self._make_obs(consolidated=False)
        assert not _passes_gate(obs)

    def test_gate_passes_valid(self):
        from trade_compass_agent.memory.promotion import _passes_gate
        obs = self._make_obs()
        assert _passes_gate(obs)

    def test_rank_candidates_sorted_by_score(self, tmp_path: Path):
        from trade_compass_agent.memory.promotion import rank_promotion_candidates
        store = ObservationStore(tmp_path / "obs.db")
        for i in range(5):
            store.append(f"s{i}", "get_bars", f"Test obs {i}", importance=7 + i % 3, concepts=["test"])
        obs_list = store.recent(limit=5)
        for obs in obs_list:
            store.mark_consolidated([obs.id])
            store.record_recall([obs.id], session_id="s1")
            store.record_recall([obs.id], session_id="s2")
        candidates = rank_promotion_candidates(store, min_signal=2)
        if len(candidates) >= 2:
            assert candidates[0].score >= candidates[1].score


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: Cross-session pattern discovery
# ═══════════════════════════════════════════════════════════════════════════

class TestPatternDiscovery:
    def _make_store_with_data(self, tmp_path: Path):
        from trade_compass_agent.memory.decision_store import DecisionStore
        obs_store = ObservationStore(tmp_path / "obs.db")
        # Create observations across days
        for concept in ["600519", "半导体"]:
            obs_store.append("s1", "get_bars", f"{concept} 分析", importance=7, concepts=[concept, "突破"])
            obs_store.append("s2", "get_bars", f"{concept} 跟踪", importance=6, concepts=[concept, "放量"])
        dec_store = DecisionStore(tmp_path)
        return obs_store, dec_store

    def test_deterministic_fallback(self, tmp_path: Path):
        from trade_compass_agent.memory.patterns import discover_patterns
        obs_store, dec_store = self._make_store_with_data(tmp_path)
        patterns = discover_patterns(obs_store, dec_store, llm_call=None, lookback_days=7)
        # May return empty if not enough cross-day data, that's OK
        assert isinstance(patterns, list)

    def test_llm_pattern_parsing(self):
        from trade_compass_agent.memory.patterns import _parse_llm_patterns
        raw = json.dumps([{
            "theme": "半导体板块关注",
            "description": "持续关注半导体",
            "concepts": ["半导体", "600519"],
            "strength": 0.8,
            "significance": "板块轮动信号",
            "evidence_days": ["2025-06-10", "2025-06-11"],
        }])
        daily = {
            "2025-06-10": [{"id": "obs1", "summary": "半导体分析", "concepts": ["半导体"], "importance": 7, "tool": "get_bars"}],
            "2025-06-11": [{"id": "obs2", "summary": "600519 跟踪", "concepts": ["600519"], "importance": 6, "tool": "get_bars"}],
        }
        patterns = _parse_llm_patterns(raw, daily)
        assert len(patterns) == 1
        assert patterns[0].theme == "半导体板块关注"
        assert patterns[0].strength == 0.8
        assert len(patterns[0].evidence) == 2

    def test_persist_patterns(self, tmp_path: Path):
        from trade_compass_agent.memory.patterns import TradingPattern, persist_patterns
        patterns = [TradingPattern(
            id="p1", theme="test", description="test desc",
            concepts=["a"], days_seen=2, total_observations=5,
            strength=0.7, significance="test", first_seen="2025-06-10",
            last_seen="2025-06-11",
        )]
        path = persist_patterns(tmp_path, patterns)
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3: Trading insights
# ═══════════════════════════════════════════════════════════════════════════

class TestTradingInsights:
    def test_generate_insights_empty(self, tmp_path: Path):
        from trade_compass_agent.memory.decision_store import DecisionStore
        from trade_compass_agent.memory.insights import generate_insights
        obs_store = ObservationStore(tmp_path / "obs.db")
        dec_store = DecisionStore(tmp_path)
        insights = generate_insights(obs_store, dec_store, patterns=[])
        assert isinstance(insights, list)

    def test_hotness_spike_detection(self, tmp_path: Path):
        from trade_compass_agent.memory.insights import _detect_hotness_spikes
        obs_store = ObservationStore(tmp_path / "obs.db")
        # Add enough observations for a spike to be detected
        for i in range(10):
            obs_store.append(f"s{i}", "get_bars", f"半导体 observation {i}", importance=6, concepts=["半导体"])
        insights = _detect_hotness_spikes(obs_store, recent_days=1, baseline_days=7, min_growth=1.5)
        # May or may not detect depending on timing, just verify it runs
        assert isinstance(insights, list)

    def test_persist_insights(self, tmp_path: Path):
        from trade_compass_agent.memory.insights import InsightKind, TradingInsight, persist_insights
        insights = [TradingInsight(
            kind=InsightKind.HOTNESS_SPIKE, title="test", body="test body",
            evidence=[], actionable="do something", confidence=0.9,
        )]
        path = persist_insights(tmp_path, insights)
        assert path.exists()


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6: Time tree
# ═══════════════════════════════════════════════════════════════════════════

class TestTimeTree:
    def test_seal_day_no_llm(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        node = tree.seal_day(
            "2025-06-15",
            obs_summaries=["600519 突破 MA20", "半导体板块回调"],
            session_summaries=["用户问了持仓分析"],
            concepts=["600519", "半导体", "突破"],
            symbols=["600519"],
            obs_ids=["obs1", "obs2"],
            session_ids=["sess1"],
        )
        assert node.id == "2025-06-15"
        assert node.level == "day"
        assert node.source_count == 3
        assert node.sealed_at is not None
        assert "600519" in node.key_symbols

    def test_seal_week(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        day_nodes = []
        for i in range(5):
            d = f"2025-06-{10+i:02d}"
            node = tree.seal_day(d, [f"summary {d}"], [], [f"concept_{i}"], [], [f"obs_{i}"], [])
            day_nodes.append(node)
        week = tree.seal_week("2025-W24", day_nodes)
        assert week.level == "week"
        assert week.source_count == 5

    def test_recall_scope_today(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        today = datetime.now().strftime("%Y-%m-%d")
        tree.seal_day(today, ["test summary"], [], ["test"], [], ["obs1"], [])
        result = tree.recall("today")
        assert "test summary" in result

    def test_recall_not_found(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        result = tree.recall("2020-01-01")
        assert "No time node found" in result

    def test_concept_timeline(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        today = datetime.now().strftime("%Y-%m-%d")
        tree.seal_day(today, ["600519 analysis"], [], ["600519"], [], ["obs1"], [])
        results = tree.concept_timeline("600519", lookback_days=1)
        assert len(results) >= 1

    def test_maybe_cascade_no_trigger(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        tree.seal_day("2025-06-15", ["summary"], [], [], [], [], [])
        result = tree.maybe_cascade("2025-06-15")
        assert result["week"] is None  # only 1 day, need >= 5

    def test_maybe_cascade_triggers_week(self, tmp_path: Path):
        from trade_compass_agent.memory.time_tree import TimeTree
        tree = TimeTree(tmp_path / "time_tree.db")
        # Mon-Fri of 2025-W25: June 16-20
        for day in range(16, 21):
            tree.seal_day(f"2025-06-{day}", [f"day {day}"], [], [], [], [], [])
        result = tree.maybe_cascade("2025-06-20")
        assert result["week"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5: Dream diary helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestDreamDiary:
    def test_build_dreaming_summary(self):
        from trade_compass_agent.memory.dream_diary import build_dreaming_summary
        from trade_compass_agent.memory.time_tree import TimeNode
        day_node = TimeNode(id="2025-06-15", level="day", summary="test day")
        summary = build_dreaming_summary(day_node, [], [], [])
        assert "test day" in summary

    def test_append_dream_diary(self, tmp_path: Path):
        from trade_compass_agent.memory.dream_diary import append_dream_diary
        path = append_dream_diary(tmp_path, "今天学到了重要的一课。")
        assert path.exists()
        content = path.read_text()
        assert "今天学到了重要的一课" in content
        # Append again
        append_dream_diary(tmp_path, "第二天的反思。")
        content2 = path.read_text()
        assert "第二天的反思" in content2
