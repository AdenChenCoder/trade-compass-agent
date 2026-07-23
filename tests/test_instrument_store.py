"""Tests for InstrumentStore — per-symbol knowledge pages."""

from pathlib import Path

import pytest

from trade_compass_agent.memory.instrument_store import InstrumentStore


@pytest.fixture
def store(tmp_path: Path) -> InstrumentStore:
    return InstrumentStore(tmp_path)


class TestRecall:
    def test_recall_nonexistent(self, store: InstrumentStore) -> None:
        assert store.recall("999999") is None

    def test_recall_after_update(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "PCB龙头", name="鹏鼎控股")
        page = store.recall("002938")
        assert page is not None
        assert "002938" in page
        assert "鹏鼎控股" in page
        assert "PCB龙头" in page


class TestUpdateSection:
    def test_creates_page_if_not_exists(self, store: InstrumentStore) -> None:
        result = store.update_section("600519", "关注理由", "白酒龙头", name="贵州茅台")
        assert result["ok"]
        assert store.exists("600519")

    def test_updates_existing_section(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "初始内容", name="鹏鼎控股")
        store.update_section("002938", "关注理由", "PCB + iPhone供应链")
        page = store.recall("002938")
        assert "PCB + iPhone供应链" in page
        assert "初始内容" not in page

    def test_invalid_section(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "test", name="test")
        result = store.update_section("002938", "不存在的段落", "content")
        assert not result["ok"]
        assert "not found" in result["error"]

    def test_updates_key_levels(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关键价位", "- 支撑: 100\n- 压力: 125", name="鹏鼎控股")
        page = store.recall("002938")
        assert "支撑: 100" in page
        assert "压力: 125" in page


class TestAppendTrade:
    def test_appends_to_history(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "test", name="鹏鼎控股")
        result = store.append_trade("002938", "buy", 200, 111.3, "回调至支撑位")
        assert result["ok"]
        page = store.recall("002938")
        assert "buy 200股 @111.3" in page
        assert "回调至支撑位" in page

    def test_creates_page_for_new_symbol(self, store: InstrumentStore) -> None:
        result = store.append_trade("600000", "buy", 100, 10.5, name="浦发银行")
        assert result["ok"]
        assert store.exists("600000")

    def test_multiple_trades(self, store: InstrumentStore) -> None:
        store.append_trade("002938", "buy", 200, 111.3, name="鹏鼎控股")
        store.append_trade("002938", "sell", 100, 120.5, "达目标价")
        page = store.recall("002938")
        assert "buy 200股" in page
        assert "sell 100股" in page


class TestReplaceTradeHistory:
    def test_replaces_only_trade_history(self, store: InstrumentStore) -> None:
        store.update_section("600519", "关注理由", "白酒龙头", name="贵州茅台")
        store.replace_trade_history("600519", ["- 2026-07-06 buy 100股 @1800 [short_stock]"])
        store.replace_trade_history("600519", ["- 2026-07-07 sell 100股 @1850 [short_stock]"])

        page = store.recall("600519")
        assert page is not None
        assert "白酒龙头" in page
        assert "2026-07-06 buy" not in page
        assert "2026-07-07 sell 100股" in page


class TestListInstruments:
    def test_empty(self, store: InstrumentStore) -> None:
        assert store.list_instruments() == []

    def test_lists_all(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "a", name="A")
        store.update_section("600519", "关注理由", "b", name="B")
        instruments = store.list_instruments()
        assert "002938" in instruments
        assert "600519" in instruments
        assert len(instruments) == 2


class TestExists:
    def test_not_exists(self, store: InstrumentStore) -> None:
        assert not store.exists("999999")

    def test_exists_after_create(self, store: InstrumentStore) -> None:
        store.update_section("002938", "关注理由", "test", name="test")
        assert store.exists("002938")
