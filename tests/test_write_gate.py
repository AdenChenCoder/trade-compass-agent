"""Tests for SemanticWriteGate and MemoryStore dedup."""

from pathlib import Path

from trade_compass_agent.memory.write_gate import (
    JACCARD_THRESHOLD,
    SemanticWriteGate,
    jaccard_similarity,
    contains_ephemeral_markers,
    quality_check,
)


class TestJaccardSimilarity:
    def test_identical_strings(self):
        assert jaccard_similarity("涨停家数超过三十家表明情绪偏强", "涨停家数超过三十家表明情绪偏强") == 1.0

    def test_similar_strings(self):
        a = "板块资金净流入超百亿时应关注核心标的买入机会"
        b = "板块净流入超百亿时需关注核心标的买入机会"
        sim = jaccard_similarity(a, b)
        assert sim > 0.5

    def test_different_strings(self):
        a = "涨停家数超过30家可作为市场情绪偏强指标"
        b = "PCB板块的供应链核心是鹏鼎控股"
        sim = jaccard_similarity(a, b)
        assert sim < 0.3

    def test_same_stock_different_content(self):
        a = "贵州茅台的核心竞争力在于品牌壁垒和定价权"
        b = "贵州茅台的销售渠道以直销和经销商并重"
        sim = jaccard_similarity(a, b)
        assert sim < 0.5

    def test_empty_strings(self):
        assert jaccard_similarity("", "abc") == 0.0
        assert jaccard_similarity("a", "") == 0.0


class TestEphemeralDetection:
    def test_date_not_blocked(self):
        assert not contains_ephemeral_markers("2026-06-12 买入了002938")

    def test_today_detected(self):
        assert contains_ephemeral_markers("今天买入了002938")

    def test_price_detected(self):
        assert contains_ephemeral_markers("现价 111.3 元")

    def test_percentage_detected(self):
        assert contains_ephemeral_markers("今天涨3.5%")

    def test_bug_detected(self):
        assert contains_ephemeral_markers("API报错了需要修复")

    def test_durable_content_passes(self):
        assert not contains_ephemeral_markers("涨停家数超过三十家可作为情绪指标")
        assert not contains_ephemeral_markers("sina批量报价接口比逐个获取快十倍")


class TestSemanticWriteGate:
    def setup_method(self):
        self.gate = SemanticWriteGate(skill_store=None)

    def test_threshold_is_0_5(self):
        assert JACCARD_THRESHOLD == 0.5

    def test_too_short_rejected(self):
        admitted, reason = self.gate.should_admit("短", "memory", [])
        assert not admitted
        assert "过短" in reason

    def test_duplicate_rejected(self):
        existing = ["板块资金净流入超百亿时应关注核心标的买入机会"]
        text = "板块净流入超百亿时需关注核心标的买入机会"
        admitted, reason = self.gate.should_admit(text, "memory", existing)
        assert not admitted
        assert "重复" in reason

    def test_paraphrase_rejected_at_0_5(self):
        existing = ["涨停家数超过三十家可作为市场情绪偏强的参考指标"]
        text = "涨停超过三十家可作为市场情绪偏强的指标"
        admitted, reason = self.gate.should_admit(text, "memory", existing)
        assert not admitted

    def test_ephemeral_admitted_at_write_time(self):
        """Ephemeral content is admitted at write time (quality check moved to promotion)."""
        admitted, _ = self.gate.should_admit(
            "今天大盘跌2.3%，市场情绪低迷", "memory", []
        )
        assert admitted

    def test_transient_admitted_at_write_time(self):
        """Transient content is admitted at write time (quality check moved to promotion)."""
        admitted, _ = self.gate.should_admit(
            "正在测试新的交易策略，临时记录", "memory", []
        )
        assert admitted

    def test_valid_entry_admitted(self):
        admitted, reason = self.gate.should_admit(
            "涨停家数超过三十家可作为市场情绪偏强的参考指标", "memory", []
        )
        assert admitted
        assert reason == "admitted"

    def test_unique_entry_passes_dedup(self):
        existing = ["涨停家数超过三十家可作为市场情绪偏强的参考指标"]
        admitted, reason = self.gate.should_admit(
            "sina批量报价接口比逐个获取快十倍以上", "memory", existing
        )
        assert admitted


class TestQualityCheck:
    """quality_check() is called at promotion time, not write time."""

    def test_ephemeral_rejected(self):
        ok, reason = quality_check("今天大盘跌2.3%，市场情绪低迷")
        assert not ok
        assert "时效性" in reason

    def test_transient_rejected(self):
        ok, reason = quality_check("正在测试新的交易策略，临时记录")
        assert not ok
        assert "持久性" in reason

    def test_durable_passes(self):
        ok, _ = quality_check("涨停家数超过三十家可作为市场情绪偏强的参考指标")
        assert ok

    def test_skill_body_coverage_rejected(self, tmp_path: Path):
        from trade_compass_agent.memory.skill_store import SkillStore

        skill_store = SkillStore(tmp_path / "skills")
        skill_store.create(
            "ma-cross",
            """\
---
name: ma-cross
description: 均线交叉交易流程
category: trading
---

## Steps
1. 均线交叉策略在震荡市中表现不佳，建议配合MACD确认信号
""",
        )

        ok, reason = quality_check(
            "均线交叉策略在震荡市中表现不佳，建议配合MACD确认信号",
            skill_store=skill_store,
        )

        assert not ok
        assert "skill" in reason


class TestMemoryStoreSupersede:
    """MemoryStore.add() should supersede or reinforce similar entries."""

    def test_similar_shorter_entry_reinforces(self, tmp_path: Path):
        from trade_compass_agent.memory.memory_store import MemoryStore

        store = MemoryStore(tmp_path, write_gate=SemanticWriteGate())
        r1 = store.add(
            "涨停家数超过三十家可作为市场情绪偏强的参考指标",
            target="memory",
            source="curator",
            confidence=0.85,
        )
        assert r1["ok"]

        r2 = store.add(
            "涨停超过三十家可作为市场情绪偏强的指标",
            target="memory",
            source="curator",
            confidence=0.85,
        )
        assert not r2["ok"]
        assert r2.get("merged") is True
        assert len(store.memory_entries) == 1

    def test_similar_longer_entry_supersedes(self, tmp_path: Path):
        from trade_compass_agent.memory.memory_store import MemoryStore

        store = MemoryStore(tmp_path, write_gate=SemanticWriteGate())
        short = "涨停家数超过三十家可作为市场情绪偏强指标"
        store.add(short, target="memory", source="curator", confidence=0.85)

        long = "涨停家数超过三十家可作为市场情绪偏强的参考指标，结合成交额放大更可靠"
        r2 = store.add(long, target="memory", source="curator", confidence=0.85)
        assert r2.get("ok") or r2.get("superseded")
        assert len(store.memory_entries) == 1
        assert "结合成交额" in store.memory_entries[0]

    def test_different_entry_added_normally(self, tmp_path: Path):
        from trade_compass_agent.memory.memory_store import MemoryStore

        store = MemoryStore(tmp_path, write_gate=SemanticWriteGate())
        store.add("涨停家数超过三十家可作为市场情绪偏强的参考指标", target="memory")
        r2 = store.add("sina批量报价接口比逐个获取快十倍以上", target="memory")
        assert r2["ok"]
        assert len(store.memory_entries) == 2
