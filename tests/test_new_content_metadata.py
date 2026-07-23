from datetime import datetime

from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.memory.instrument_store import InstrumentStore
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.rules_store import RulesStore
from trade_compass_agent.portfolio.simulator import PaperPortfolio
from trade_compass_agent.web.api import _memory_response


def _trade(side: str, quantity: int, timestamp: datetime) -> PaperTrade:
    return PaperTrade(
        symbol="600519",
        account=AccountKind.SHORT_STOCK,
        side=side,
        quantity=quantity,
        price=100.0,
        timestamp=timestamp,
        reason="test",
    )


def test_position_opened_at_uses_oldest_remaining_lot() -> None:
    portfolio = PaperPortfolio()
    first_buy = datetime(2026, 7, 15, 10, 0)
    second_buy = datetime(2026, 7, 16, 10, 0)
    portfolio.record(_trade("buy", 100, first_buy))
    portfolio.record(_trade("buy", 100, second_buy))
    portfolio.record(_trade("sell", 100, datetime(2026, 7, 17, 10, 0)))

    position = portfolio.positions()[0]

    assert position.opened_at == second_buy


def test_rule_created_at_survives_edit_and_legacy_rules_stay_unmarked(tmp_path) -> None:
    store = RulesStore(tmp_path)
    store.add("新规则", actor="web")
    created = store.list_entries()[0]
    assert created.created_at

    store.replace(created.id, "编辑后的规则", actor="web")
    edited = store.list_entries()[0]
    assert edited.created_at == created.created_at

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "RULES.md").write_text("历史规则", encoding="utf-8")
    legacy = RulesStore(legacy_dir).list_entries()[0]
    assert legacy.created_at == ""


def test_memory_api_exposes_persisted_created_at(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    result = store.add("新知识", source="user_pin")
    assert result["ok"] is True

    payload = _memory_response("memory", store)

    assert payload.entries[0].created_at


def test_instrument_creation_time_is_persisted_without_backfilling_legacy_pages(
    tmp_path,
) -> None:
    store = InstrumentStore(tmp_path)
    store.update_section("600519", "笔记", "长期跟踪", name="贵州茅台")

    created_at = store.created_at("600519")
    assert datetime.fromisoformat(created_at).tzinfo is not None

    store.update_section("600519", "笔记", "继续跟踪", name="贵州茅台")
    assert store.created_at("600519") == created_at

    legacy_path = tmp_path / "instruments" / "000001.md"
    legacy_path.write_text("# 000001\n\n*最后更新: 2026-01-01*\n", encoding="utf-8")
    assert store.created_at("000001") == ""
