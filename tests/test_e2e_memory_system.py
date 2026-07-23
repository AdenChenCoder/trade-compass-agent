"""End-to-end smoke tests for the memory and skill systems.

Verifies the full pipeline: ObservationStore → FTS5 search → recall tracking →
promotion scoring → WriteGate → MemoryStore (KNOWLEDGE.md) and SkillStore.
Uses a temp directory so production data is never touched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trade_compass_agent.config import MemoryGovernanceConfig
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.write_gate import SemanticWriteGate, quality_check
from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.memory.promotion import (
    rank_promotion_candidates,
    apply_promotions,
    BOOTSTRAP_MIN_SIGNAL,
    BOOTSTRAP_THRESHOLD,
    MIN_TOTAL_SIGNAL,
)
from trade_compass_agent.memory.tree.search import _sanitize_fts5_query


@pytest.fixture()
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture()
def obs_store(tmp_dir):
    return ObservationStore(tmp_dir / "obs.db")


@pytest.fixture()
def skill_store(tmp_dir):
    skill_dir = tmp_dir / "skills"
    skill_dir.mkdir()
    return SkillStore(skill_dir)


@pytest.fixture()
def mem_store(tmp_dir, skill_store):
    gate = SemanticWriteGate(skill_store=skill_store)
    return MemoryStore(tmp_dir, write_gate=gate)


# ── ObservationStore: append + search + recall ──────────────────────────

class TestObservationStoreE2E:
    def test_append_and_search(self, obs_store: ObservationStore):
        ok = obs_store.append("s1", "get_bars", "贵州茅台日K线数据 2026年6月最新走势", concepts=["贵州茅台", "日K线"])
        assert ok is True

        results = obs_store.search("茅台", track_recall=False)
        assert len(results) >= 1
        assert "茅台" in results[0].summary

    def test_dedup_blocks_duplicate(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_a", "完全相同的内容")
        ok = obs_store.append("s1", "tool_a", "完全相同的内容")
        assert ok is False

    def test_search_with_recall_tracking(self, obs_store: ObservationStore):
        obs_store.append("s1", "search_stock", "宁德时代新能源电池行业分析报告")
        results = obs_store.search("宁德时代", session_id="session_test")
        assert len(results) >= 1

        obs_id = results[0].id
        refreshed = [o for o in obs_store.recent(50) if o.id == obs_id]
        assert refreshed[0].recall_count == 1, "recall_count should be incremented by search"

    def test_recall_accumulates(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_x", "半导体行业ETF跟踪分析")
        r1 = obs_store.search("半导体", session_id="s1")
        obs_store.search("半导体", session_id="s2")
        obs_store.search("半导体", session_id="s3")
        assert len(r1) >= 1

        obs_id = r1[0].id
        refreshed = [o for o in obs_store.recent(50) if o.id == obs_id]
        assert refreshed[0].recall_count == 3, "3 searches should yield recall_count=3"
        assert refreshed[0].total_signal >= 3

    def test_daily_count_increment(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_y", "每日市场脉搏数据分析")
        obs = obs_store.recent(1)[0]
        obs_store.bump_daily([obs.id])
        obs_store.bump_daily([obs.id])
        refreshed = [o for o in obs_store.recent(50) if o.id == obs.id][0]
        assert refreshed.daily_count == 2

    def test_grounded_count_increment(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_z", "交易信号验证结果确认")
        obs = obs_store.recent(1)[0]
        obs_store.bump_grounded([obs.id])
        refreshed = [o for o in obs_store.recent(50) if o.id == obs.id][0]
        assert refreshed.grounded_count == 1

    def test_promotion_candidates_bootstrap(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_a", "市场趋势分析长期有效内容")
        obs_store.search("市场趋势")  # recall_count → 1
        candidates = obs_store.promotion_candidates(
            min_signal=BOOTSTRAP_MIN_SIGNAL, require_consolidated=False,
        )
        assert len(candidates) >= 1, "Bootstrap mode should find unconsolidated candidates"

    def test_promotion_candidates_normal_requires_consolidated(self, obs_store: ObservationStore):
        obs_store.append("s1", "tool_a", "未巩固的观察内容")
        obs_store.search("观察")
        obs_store.search("观察")
        obs_store.search("观察")  # signal=3
        candidates = obs_store.promotion_candidates(min_signal=3, require_consolidated=True)
        assert len(candidates) == 0, "Normal mode should skip unconsolidated"


# ── FTS5 Sanitizer: edge cases ──────────────────────────────────────────

class TestFTS5SanitizerE2E:
    def test_empty_input(self):
        assert _sanitize_fts5_query("") == ""

    def test_cjk_bigram(self):
        result = _sanitize_fts5_query("贵州茅台")
        assert result, "CJK input should produce a non-empty query"

    def test_operators_stripped(self):
        result = _sanitize_fts5_query("foo AND bar OR NOT baz")
        assert "AND" not in result.split()
        assert "OR" not in result.replace('" OR "', '').split()
        assert "NOT" not in result.split()

    def test_quotes_escaped(self):
        result = _sanitize_fts5_query('he said "hello"')
        assert result  # should not crash

    def test_mixed_cjk_ascii(self):
        result = _sanitize_fts5_query("ETF 半导体 analysis")
        assert result

    def test_punctuation_only(self):
        result = _sanitize_fts5_query("!@#$%")
        assert result == ""

    def test_fts5_query_actually_works_in_db(self, obs_store: ObservationStore):
        """Verify sanitized queries don't crash FTS5."""
        obs_store.append("s1", "t", "贵州茅台2026年财报分析")
        queries = ["茅台", "贵州茅台", 'test "quoted"', "A AND B", "!@#", "", "ETF分析"]
        for q in queries:
            results = obs_store.search(q, track_recall=False)
            assert isinstance(results, list), f"Query '{q}' should not crash"


# ── WriteGate + quality_check ────────────────────────────────────────────

class TestWriteGateE2E:
    def test_dedup_rejects_similar(self):
        gate = SemanticWriteGate()
        existing = ["贵州茅台是A股白酒龙头企业，市值超过2万亿"]
        admitted, reason = gate.should_admit(
            "贵州茅台是A股白酒行业龙头，市值超过两万亿", "memory", existing,
        )
        assert admitted is False, "Near-duplicate should be rejected"
        assert "重复" in reason or "similar" in reason.lower()

    def test_unique_admitted(self):
        gate = SemanticWriteGate()
        admitted, reason = gate.should_admit(
            "半导体行业受益于AI芯片需求增长，2026年Q2营收环比增长15%", "memory", [],
        )
        assert admitted is True

    def test_too_short_rejected(self):
        gate = SemanticWriteGate()
        admitted, reason = gate.should_admit("短", "memory", [])
        assert admitted is False

    def test_quality_check_rejects_ephemeral(self):
        ok, reason = quality_check("2026-06-17 茅台股价为1856元，现价1860")
        assert ok is False, "Ephemeral content should fail quality check"

    def test_quality_check_passes_durable(self):
        ok, reason = quality_check("均线交叉策略在震荡市中表现不佳，建议配合MACD确认信号")
        assert ok is True


# ── Full Promotion Pipeline ──────────────────────────────────────────────

class TestPromotionPipelineE2E:
    def _seed_observations(self, obs_store: ObservationStore, n_recalls: int = 4):
        """Seed an observation, consolidate it, and recall it multiple times."""
        obs_store.append("s1", "analyze", "均线交叉策略适用于趋势市而非震荡市场，需配合成交量确认方向")
        obs = obs_store.recent(1)[0]
        conn = obs_store._get_conn()
        conn.execute("UPDATE observations SET consolidated = 1 WHERE id = ?", (obs.id,))
        conn.commit()
        for i in range(n_recalls):
            obs_store.search("均线 趋势", session_id=f"sess_{i}")
        return obs.id

    def test_rank_produces_candidates(self, obs_store: ObservationStore):
        self._seed_observations(obs_store, n_recalls=4)
        candidates = rank_promotion_candidates(obs_store, min_signal=MIN_TOTAL_SIGNAL)
        assert len(candidates) >= 1, "Should produce at least one candidate"
        assert candidates[0].score > 0

    def test_apply_promotions_writes_knowledge(
        self, obs_store: ObservationStore, mem_store: MemoryStore,
    ):
        self._seed_observations(obs_store, n_recalls=4)
        candidates = rank_promotion_candidates(obs_store, min_signal=MIN_TOTAL_SIGNAL)
        assert len(candidates) >= 1

        promoted = apply_promotions(
            candidates, mem_store, obs_store, threshold=0.0,
            governance=MemoryGovernanceConfig(legacy_promotion_fallback=True),
        )
        assert len(promoted) >= 1, "At least one observation should be promoted"
        entries = mem_store.memory_entries
        assert len(entries) >= 1, "KNOWLEDGE.md should have entries after promotion"
        assert "均线" in entries[0], "Promoted content should appear in KNOWLEDGE.md"

    def test_promoted_not_re_selected(
        self, obs_store: ObservationStore, mem_store: MemoryStore,
    ):
        self._seed_observations(obs_store, n_recalls=4)
        candidates = rank_promotion_candidates(obs_store, min_signal=MIN_TOTAL_SIGNAL)
        apply_promotions(
            candidates, mem_store, obs_store, threshold=0.0,
            governance=MemoryGovernanceConfig(legacy_promotion_fallback=True),
        )

        candidates2 = rank_promotion_candidates(obs_store, min_signal=MIN_TOTAL_SIGNAL)
        assert len(candidates2) == 0, "Already promoted observations should not reappear"

    def test_bootstrap_mode(self, obs_store: ObservationStore, mem_store: MemoryStore):
        obs_store.append("s1", "research", "行业轮动策略：在经济复苏期配置周期股，衰退期转向防御品种")
        obs_store.search("行业轮动")  # signal=1, not consolidated
        candidates = rank_promotion_candidates(
            obs_store,
            min_signal=BOOTSTRAP_MIN_SIGNAL,
            bootstrap=True,
        )
        assert len(candidates) >= 1, "Bootstrap should find low-signal, unconsolidated candidates"

        promoted = apply_promotions(
            candidates, mem_store, obs_store, threshold=BOOTSTRAP_THRESHOLD,
            governance=MemoryGovernanceConfig(legacy_promotion_fallback=True),
        )
        assert len(promoted) >= 1, "Bootstrap threshold should allow promotion"


# ── MemoryStore: KNOWLEDGE.md persistence ─────────────────────────────────

class TestMemoryStoreE2E:
    def test_add_and_read(self, mem_store: MemoryStore):
        result = mem_store.add("均线交叉策略适用于趋势市场而非震荡市场，需配合成交量确认方向", target="memory")
        assert result.get("ok") is True

        entries = mem_store.memory_entries
        assert len(entries) == 1
        assert "均线" in entries[0]

    def test_knowledge_file_persists(self, tmp_dir, skill_store):
        gate = SemanticWriteGate(skill_store=skill_store)
        store1 = MemoryStore(tmp_dir, write_gate=gate)
        store1.add("长期有效的投资知识条目", target="memory")

        store2 = MemoryStore(tmp_dir, write_gate=gate)
        entries = store2.memory_entries
        assert len(entries) >= 1, "Entries should persist across MemoryStore instances"
        assert "投资知识" in entries[0]

    def test_user_profile_works(self, mem_store: MemoryStore):
        result = mem_store.add("用户偏好短线交易，风险承受能力中等", target="user")
        assert result.get("ok") is True
        entries = mem_store.user_entries
        assert len(entries) == 1


# ── SkillStore ─────────────────────────────────────────────────────────

_SKILL_TEMPLATE = """\
---
name: {name}
description: {description}
category: {category}
---

{body}
"""


class TestSkillStoreE2E:
    def _make_skill(self, skill_store, name, description="Test", body="content", category="test"):
        content = _SKILL_TEMPLATE.format(name=name, description=description, category=category, body=body)
        return skill_store.create(name=name, content=content)

    def test_create_and_list(self, skill_store: SkillStore):
        result = self._make_skill(skill_store, "test-skill", description="A test skill for E2E", body="## Steps\n1. Do thing A")
        assert result.get("ok") is True

        skills = skill_store.list_skills()
        names = {s.name for s in skills}
        assert "test-skill" in names

    def test_read_full(self, skill_store: SkillStore):
        self._make_skill(skill_store, "read-test", body="Full body content here")
        content = skill_store.read_full("read-test")
        assert content and "Full body content" in content

    def test_pin_unpin(self, skill_store: SkillStore):
        self._make_skill(skill_store, "pin-test")
        skill_store.pin("pin-test")
        rec = skill_store.get("pin-test")
        assert rec is not None and rec.usage.pinned is True

        skill_store.unpin("pin-test")
        rec = skill_store.get("pin-test")
        assert rec is not None and rec.usage.pinned is False

    def test_patch(self, skill_store: SkillStore):
        self._make_skill(skill_store, "patch-test", body="v1 original")
        result = skill_store.patch("patch-test", old_text="v1 original", new_text="v2 updated")
        assert result.get("ok") is True
        content = skill_store.read_full("patch-test")
        assert content and "v2 updated" in content
