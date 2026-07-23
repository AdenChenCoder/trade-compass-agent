"""Tests for decision reflection curation."""

from pathlib import Path

import pytest

from trade_compass_agent.memory.decision_store import DecisionStore
from trade_compass_agent.ops import curate_decisions, generate_decision_reflection


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    return DecisionStore(tmp_path)


def test_curate_decisions_rule_based(tmp_path: Path) -> None:
    store = DecisionStore(tmp_path)
    d = store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a", reasoning="突破")
    store.resolve(symbol="002938", account="a", sell_price=22.0)

    reflected = curate_decisions(tmp_path, max_reflect=5)
    assert reflected == [d.id]

    updated = store.search(status="reflected")
    assert len(updated) == 1
    assert updated[0].reflection
    assert "盈利" in updated[0].reflection


def test_generate_decision_reflection_manual() -> None:
    from trade_compass_agent.memory.decision_store import TradeDecision

    d = TradeDecision(
        id="abc",
        symbol="600549",
        side="buy",
        quantity=100,
        price=90.0,
        account="a",
        reasoning="板块强势",
        market_context="",
        decided_at="2026-06-22T00:00:00+00:00",
        status="resolved",
        outcome_pnl_pct=-11.49,
        holding_days=2,
    )
    text = generate_decision_reflection(d, manual_text="  追高入场，应等回调  ")
    assert text == "追高入场，应等回调"


def test_generate_decision_reflection_llm_fallback(store: DecisionStore) -> None:
    store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
    store.resolve(symbol="002938", account="a", sell_price=18.0)

    def _boom(_system: str, _user: str) -> str:
        raise RuntimeError("llm down")

    resolved = store.search(status="resolved")[0]
    text = generate_decision_reflection(resolved, llm_call=_boom)
    assert "亏损" in text
