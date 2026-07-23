"""Tests for DecisionStore — Decision Journal lifecycle."""

from pathlib import Path

import pytest

from trade_compass_agent.memory.decision_store import DecisionStore


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    return DecisionStore(tmp_path)


class TestStoreDecision:
    def test_creates_pending(self, store: DecisionStore) -> None:
        d = store.store_decision(
            symbol="002938", side="buy", quantity=300,
            price=22.5, account="short_stock", reasoning="回调到位",
        )
        assert d.status == "pending"
        assert d.symbol == "002938"
        assert d.price == 22.5

    def test_persists_to_file(self, store: DecisionStore) -> None:
        store.store_decision(symbol="600000", side="buy", quantity=100, price=10.0, account="a")
        assert store._file.exists()
        loaded = store._load_all()
        assert len(loaded) == 1
        assert loaded[0].symbol == "600000"


class TestResolve:
    def test_resolve_calculates_pnl(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=300, price=20.0, account="short_stock")
        resolved = store.resolve(symbol="002938", account="short_stock", sell_price=22.0)
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.outcome_pnl_pct == 10.0
        assert resolved.outcome_price == 22.0

    def test_resolve_matches_latest_pending(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=18.0, account="a")
        store.store_decision(symbol="002938", side="buy", quantity=200, price=20.0, account="a")
        resolved = store.resolve(symbol="002938", account="a", sell_price=21.0)
        assert resolved.price == 20.0  # most recent pending

    def test_resolve_no_match_returns_none(self, store: DecisionStore) -> None:
        store.store_decision(symbol="600000", side="buy", quantity=100, price=10.0, account="a")
        assert store.resolve(symbol="002938", account="a", sell_price=11.0) is None

    def test_resolve_different_account_no_match(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=10.0, account="a")
        assert store.resolve(symbol="002938", account="b", sell_price=11.0) is None


class TestReflection:
    def test_add_reflection(self, store: DecisionStore) -> None:
        d = store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
        store.resolve(symbol="002938", account="a", sell_price=22.0)
        assert store.add_reflection(d.id, "追高教训，应等回调")
        updated = store._load_all()
        assert updated[0].status == "reflected"
        assert "追高教训" in updated[0].reflection


class TestSearch:
    def test_filter_by_symbol(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
        store.store_decision(symbol="600000", side="buy", quantity=100, price=10.0, account="a")
        results = store.search(symbol="002938")
        assert len(results) == 1
        assert results[0].symbol == "002938"

    def test_filter_by_status(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
        store.resolve(symbol="002938", account="a", sell_price=22.0)
        store.store_decision(symbol="600000", side="buy", quantity=100, price=10.0, account="a")
        pending = store.search(status="pending")
        assert len(pending) == 1
        assert pending[0].symbol == "600000"


class TestStats:
    def test_empty_stats(self, store: DecisionStore) -> None:
        stats = store.stats()
        assert stats["total"] == 0

    def test_with_resolved(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
        store.store_decision(symbol="600000", side="buy", quantity=100, price=10.0, account="a")
        store.resolve(symbol="002938", account="a", sell_price=22.0)
        store.resolve(symbol="600000", account="a", sell_price=9.0)
        stats = store.stats()
        assert stats["resolved"] == 2
        assert stats["awaiting_reflection"] == 2
        assert stats["reflected"] == 0
        assert stats["win_rate"] == 50.0

    def test_with_reflected(self, store: DecisionStore) -> None:
        d = store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a")
        store.resolve(symbol="002938", account="a", sell_price=22.0)
        store.add_reflection(d.id, "入场逻辑正确")
        stats = store.stats()
        assert stats["awaiting_reflection"] == 0
        assert stats["reflected"] == 1
        assert stats["resolved"] == 1


class TestGetPastContext:
    def test_builds_context_string(self, store: DecisionStore) -> None:
        store.store_decision(symbol="002938", side="buy", quantity=100, price=20.0, account="a", reasoning="形态突破")
        store.resolve(symbol="002938", account="a", sell_price=22.0)
        ctx = store.get_past_context("002938")
        assert "002938" in ctx
        assert "形态突破" in ctx
        assert "+10.0%" in ctx
