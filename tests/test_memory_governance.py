"""Tests for memory governance (Step 1 anti-poisoning)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trade_compass_agent.config import MemoryGovernanceConfig
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.skill_store import SkillStore
from trade_compass_agent.memory.write_gate import SemanticWriteGate
from trade_compass_agent.runtime.bootstrap import build_system_prompt
from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
from trade_compass_agent.runtime.tools.self_improve import MEMORY_WRITE_SCHEMA, SKILL_MANAGE_SCHEMA


@pytest.fixture
def mem_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        tmp_path / "vault",
        min_inject_confidence=0.5,
    )


def test_agent_add_low_confidence_not_in_snapshot(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content="突破MA20放量是短线入场信号",
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )
    assert result["ok"] is True
    assert result["confidence"] == 0.4

    mem_store.load_from_disk()
    snapshot = mem_store.format_for_system_prompt()
    assert "突破MA20" not in snapshot
    assert mem_store.memory_entries  # still on disk


def test_direct_agent_add_defaults_below_injection_threshold(mem_store: MemoryStore) -> None:
    text = "内部直写的低信任候选规则"

    result = mem_store.add(text)

    assert result["ok"] is True
    assert result["source"] == "agent"
    assert result["confidence"] < mem_store.min_inject_confidence
    mem_store.load_from_disk()
    assert text not in mem_store.format_for_system_prompt()


def test_direct_agent_add_cannot_force_high_confidence(mem_store: MemoryStore) -> None:
    text = "内部直写试图强行注入的规则"

    result = mem_store.add(text, source="agent", confidence=0.9)

    assert result["ok"] is True
    assert result["confidence"] < mem_store.min_inject_confidence
    mem_store.load_from_disk()
    assert text not in mem_store.format_for_system_prompt()


def test_promotion_confidence_injects_snapshot(mem_store: MemoryStore) -> None:
    mem_store.add(
        "高波动标的止损宜收紧",
        source="promotion",
        confidence=0.85,
    )
    mem_store.load_from_disk()
    snapshot = mem_store.format_for_system_prompt()
    assert "高波动" in snapshot


def test_agent_replace_rejected(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="replace",
            content="new",
            old_text="old",
            actor="agent",
        )
    )
    assert result["ok"] is False


def test_user_pin_high_confidence(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="pin",
            content="用户偏好短线波段",
            actor="user",
        )
    )
    assert result["ok"] is True
    mem_store.load_from_disk()
    assert "用户偏好" in mem_store.format_for_system_prompt()


def test_user_forget_archives(mem_store: MemoryStore) -> None:
    mem_store.add("临时笔记条目", source="promotion", confidence=0.85)
    mem_store.load_from_disk()
    assert "临时笔记" in mem_store.format_for_system_prompt()

    result = json.loads(
        tool_memory_write(
            mem_store,
            action="forget",
            content="临时笔记",
            actor="user",
        )
    )
    assert result["ok"] is True

    mem_store.load_from_disk()
    assert "临时笔记" not in mem_store.format_for_system_prompt()


def test_list_active_shows_confidence(mem_store: MemoryStore) -> None:
    mem_store.add("低信任草稿", source="agent", confidence=0.4)
    listed = json.loads(tool_memory_write(mem_store, action="list", actor="agent"))
    assert listed["count"] >= 1
    assert any(e["confidence"] == 0.4 for e in listed["entries"])


def test_agent_add_cannot_spoof_promotion_source(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content="伪装晋升来源的低信任条目",
            source="promotion",
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is True
    assert result["source"] == "agent"
    assert result["confidence"] == 0.4


def test_agent_add_rejects_skill_covered_knowledge(tmp_path: Path) -> None:
    skill_store = SkillStore(tmp_path / "vault" / "skills")
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
    store = MemoryStore(
        tmp_path / "vault",
        write_gate=SemanticWriteGate(skill_store=skill_store),
        min_inject_confidence=0.5,
    )

    result = json.loads(
        tool_memory_write(
            store,
            action="add",
            content="均线交叉策略在震荡市中表现不佳，建议配合MACD确认信号",
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is False
    assert "skill" in result["error"]
    assert store.memory_entries == []


def test_agent_add_rejects_procedural_knowledge(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content=(
                "止盈审查：须 load_skill(contextual-take-profit)，"
                "按卖分/持分评分表决定减仓。"
            ),
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is False
    assert result["suggested_target"] == "skill"
    assert "procedural" in result["error"]
    assert mem_store.memory_entries == []


def test_agent_add_rejects_ordered_steps_as_procedural(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content="1. 先确认趋势是否破位\n2. 再检查资金流向\n3. 最后决定是否减仓",
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is False
    assert result["suggested_target"] == "skill"
    assert result["reason"] == "contains ordered execution steps"


def test_agent_add_allows_declarative_knowledge(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content="追高入场会显著增加后续止损压力，突破回踩确认入场更稳",
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is True


def test_prompt_and_tool_schema_explain_knowledge_skill_boundary(tmp_path: Path) -> None:
    prompt = build_system_prompt(memory_dir=tmp_path, skills=[])

    assert "Knowledge / Skill 边界规则" in prompt
    assert "触发条件、执行步骤、工具调用顺序" in prompt
    assert "这些过程性内容必须用 skill_manage" in MEMORY_WRITE_SCHEMA["description"]
    assert "而不是 write_knowledge" in SKILL_MANAGE_SCHEMA["description"]


def test_agent_add_cannot_supersede_high_trust_entry(mem_store: MemoryStore) -> None:
    original = "涨停家数超过三十家可作为市场情绪偏强指标"
    mem_store.add(original, source="promotion", confidence=0.85)

    longer = "涨停家数超过三十家可作为市场情绪偏强的参考指标，结合成交额放大更可靠"
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="add",
            content=longer,
            actor="agent",
            governance=MemoryGovernanceConfig(),
        )
    )

    assert result["ok"] is False
    assert mem_store.memory_entries == [original]
    assert mem_store.get_active_meta("memory")[0]["confidence"] == 0.85


def test_promotion_upgrades_low_trust_duplicate(mem_store: MemoryStore) -> None:
    text = "突破MA20放量是短线入场信号"
    tool_memory_write(
        mem_store,
        action="add",
        content=text,
        actor="agent",
        governance=MemoryGovernanceConfig(),
    )

    result = mem_store.add(text, source="promotion", confidence=0.85)

    assert result["ok"] is True
    assert result.get("duplicate") is True
    assert len(mem_store.memory_entries) == 1
    mem_store.load_from_disk()
    assert text in mem_store.format_for_system_prompt()


def test_agent_cannot_pin(mem_store: MemoryStore) -> None:
    result = json.loads(
        tool_memory_write(
            mem_store,
            action="pin",
            content="用户确认固定的高信任规则",
            actor="agent",
        )
    )

    assert result["ok"] is False
    assert "Only user actor" in result["error"]
    mem_store.load_from_disk()
    assert "用户确认固定" not in mem_store.format_for_system_prompt()


def test_low_trust_reinforce_cannot_cross_injection_threshold(mem_store: MemoryStore) -> None:
    text = "后台反复出现的低信任候选规则"
    mem_store.add(text, source="agent", confidence=0.4)

    first = mem_store.reinforce(text[:50])
    second = mem_store.reinforce(text[:50])

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["confidence"] < mem_store.min_inject_confidence
    mem_store.load_from_disk()
    assert text not in mem_store.format_for_system_prompt()


def test_trusted_reinforce_can_cross_injection_threshold(mem_store: MemoryStore) -> None:
    text = "人工整理过但暂未达到阈值的规则"
    mem_store.add(text, source="curator", confidence=0.45)

    result = mem_store.reinforce(text[:50])

    assert result["ok"] is True
    assert result["confidence"] >= mem_store.min_inject_confidence
    mem_store.load_from_disk()
    assert text in mem_store.format_for_system_prompt()


def test_archive_stale_soft_archives(mem_store: MemoryStore) -> None:
    text = "低置信过期条目仍需保留审计"
    mem_store.add(text, source="promotion", confidence=0.1)

    archived = mem_store.archive_stale("memory")

    assert archived == [text]
    assert text in mem_store.memory_entries
    mem_store.load_from_disk()
    assert text not in mem_store.format_for_system_prompt()
