"""Tests for AccountStore CRUD operations."""


import pytest

from trade_compass_agent.domain import AccountKind
from trade_compass_agent.portfolio.accounts import AccountStore


@pytest.fixture
def store(tmp_path):
    return AccountStore(tmp_path / "accounts.json")


def test_default_seed(store):
    accounts = store.list()
    assert len(accounts) == 3
    ids = [a.id for a in accounts]
    assert "short_stock" in ids
    assert "etf_rotation" in ids
    assert "mid_term" in ids
    mid_term = store.get("mid_term")
    assert mid_term is not None
    assert mid_term.name == "中长线账户"
    assert mid_term.description == "中长线持仓账户"


def test_get_existing(store):
    acct = store.get("short_stock")
    assert acct is not None
    assert acct.kind == AccountKind.SHORT_STOCK
    assert acct.capital == 300_000


def test_get_nonexistent(store):
    assert store.get("nonexistent") is None


def test_create(store):
    acct = store.create(
        kind=AccountKind.LONG_TERM,
        name="长线账户",
        description="长线持仓",
        capital=500_000,
    )
    assert acct.kind == AccountKind.LONG_TERM
    assert acct.name == "长线账户"
    assert acct.capital == 500_000
    assert len(acct.id) == 8

    all_accounts = store.list()
    assert len(all_accounts) == 4


def test_update(store):
    updated = store.update("short_stock", name="短线A", capital=200_000)
    assert updated is not None
    assert updated.name == "短线A"
    assert updated.capital == 200_000

    refreshed = store.get("short_stock")
    assert refreshed.name == "短线A"


def test_update_nonexistent(store):
    assert store.update("nonexistent", name="x") is None


def test_delete(store):
    assert store.delete("mid_term") is True
    assert len(store.list()) == 2
    assert store.get("mid_term") is None


def test_delete_nonexistent(store):
    assert store.delete("nonexistent") is False
