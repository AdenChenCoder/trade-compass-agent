"""Tests for contradiction / structural checks (Step 1 + Step 4)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


from trade_compass_agent.memory.contradiction import (
    apply_conflict_reports,
    judge_at_promotion,
    scan_active_conflicts,
    structural_check,
)
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.observation_store import Observation
from trade_compass_agent.memory.promotion import PromotionCandidate


def test_structural_check_rejects_empty() -> None:
    ok, err = structural_check("")
    assert not ok
    assert "Empty" in err


def test_structural_check_rejects_raw_tool_output() -> None:
    raw = "[get_bars] symbol=600519; close=100"
    ok, err = structural_check(raw)
    assert not ok
    assert "Raw tool" in err


def test_structural_check_accepts_normal_text(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "v")
    ok, err = structural_check("100股减1/3", store)
    assert ok
    assert err == ""


def test_judge_at_promotion_superseede_verdict() -> None:
    obs = Observation(
        id="obs1",
        session_id="s1",
        tool_name="get_bars",
        summary="测试观察",
        raw_preview="",
        importance=7,
        concepts=["600519"],
        created_at="2026-06-16T12:00:00+00:00",
        dedup_hash="abc",
    )
    candidate = PromotionCandidate(observation=obs, score=0.8, dimension_scores={})

    def fake_llm(system: str, user: str) -> str:
        return json.dumps({
            "verdict": "SUPERSEDE",
            "reason": "与旧条冲突",
            "refined": "A股卖出须100股整数倍",
            "conflicts_with": "100股减",
        })

    result = judge_at_promotion(
        "100股减1/3",
        [candidate],
        existing_knowledge="100股减1/3",
        skills_summary="",
        llm_call=fake_llm,
    )
    assert result.verdict == "SUPERSEDE"
    assert result.conflicts_with.startswith("100股减")


def test_scan_active_conflicts_min_lot_fixture(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vault")
    store.add("100股减1/3", source="promotion", confidence=0.85)
    active = store.list_active("memory", min_confidence=0.5)

    def fake_llm(system: str, user: str) -> str:
        return json.dumps([{
            "verdict": "SUPERSEDE",
            "entry_prefix": "100股减",
            "reason": "与 min-lot 硬约束冲突",
            "refined": "A股卖出须100股整数倍",
            "conflicts_with": "100股减",
        }])

    reports = scan_active_conflicts(active, "min-lot=100", "", fake_llm)
    assert len(reports) == 1
    assert reports[0].verdict == "SUPERSEDE"


def test_apply_conflict_reports_supersede(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vault")
    store.add("100股减1/3", source="promotion", confidence=0.85)

    from trade_compass_agent.memory.contradiction import ConflictReport

    reports = [
        ConflictReport(
            verdict="SUPERSEDE",
            entry_prefix="100股减",
            reason="min-lot",
            refined_text="A股卖出须100股整数倍",
            conflicts_with="100股减",
        ),
    ]
    applied = apply_conflict_reports(reports, store)
    assert len(applied) == 1
    assert "整数倍" in store.memory_entries[0]
    meta = store.get_active_meta("memory")[0]
    assert meta["source"] == "curator"


def test_archive_inactive_skips_user_pin(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "vault")
    store.add("用户钉住的规则", source="user_pin", confidence=1.0)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    store._meta["memory"][0]["last_accessed"] = old
    store._meta["memory"][0]["created_at"] = old
    store._save_meta()

    archived = store.archive_inactive("memory", stale_days=90)
    assert archived == []
    assert store.get_active_meta("memory")[0]["status"] == "active"
