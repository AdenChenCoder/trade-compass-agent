"""Decision outcomes are rebuilt from the authoritative paper-trade ledger."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from trade_compass_agent.config import TradingCostConfig
from trade_compass_agent.domain import AccountKind, Bar, PaperTrade
from trade_compass_agent.memory.decision_reconciler import reconcile_decisions
from trade_compass_agent.memory.decision_store import DecisionStore
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.runtime.tools.portfolio import (
    tool_batch_paper_trades,
    tool_place_paper_trade,
)


def _trade(
    *,
    trade_id: str,
    decision_id: str | None,
    side: str,
    quantity: int,
    price: float,
    timestamp: datetime,
    symbol: str = "600703",
) -> PaperTrade:
    return PaperTrade(
        symbol=symbol,
        account=AccountKind.SHORT_STOCK,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=timestamp,
        reason=f"{side} test",
        is_t0=True,
        trade_id=trade_id,
        decision_id=decision_id,
        price_source="test",
    )


def test_reconcile_one_sell_across_two_buy_decisions(tmp_path):
    data_dir = tmp_path / "data"
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    opened = datetime(2026, 6, 23, 9, 30)
    portfolio.record(_trade(
        trade_id="buy-1", decision_id="decision-1", side="buy",
        quantity=200, price=19.285, timestamp=opened,
    ))
    portfolio.record(_trade(
        trade_id="buy-2", decision_id="decision-2", side="buy",
        quantity=100, price=22.02, timestamp=opened + timedelta(days=2),
    ))
    portfolio.record(_trade(
        trade_id="sell-1", decision_id=None, side="sell",
        quantity=300, price=16.86, timestamp=opened + timedelta(days=15),
    ))

    result = reconcile_decisions(data_dir, TradingCostConfig())
    decisions = {d.id: d for d in DecisionStore(data_dir).search(limit=20)}

    assert result.changed == 2
    assert decisions["decision-1"].status == "resolved"
    assert decisions["decision-1"].resolved_quantity == 200
    assert decisions["decision-1"].outcome_pnl_pct == -12.57
    assert decisions["decision-2"].status == "resolved"
    assert decisions["decision-2"].resolved_quantity == 100
    assert decisions["decision-2"].outcome_pnl_pct == -23.43
    assert decisions["decision-1"].outcome_trade_ids == ["sell-1"]
    assert decisions["decision-1"].outcome_source == "trade_ledger_fifo"


def test_partial_sell_is_idempotent_across_restart_then_resolves(tmp_path):
    data_dir = tmp_path / "data"
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    opened = datetime(2026, 6, 1, 9, 30)
    portfolio.record(_trade(
        trade_id="buy-1", decision_id="decision-1", side="buy",
        quantity=200, price=10.0, timestamp=opened,
    ))
    portfolio.record(_trade(
        trade_id="sell-1", decision_id=None, side="sell",
        quantity=50, price=12.0, timestamp=opened + timedelta(days=1),
    ))

    reconcile_decisions(data_dir, TradingCostConfig())
    first = DecisionStore(data_dir).search(limit=10)[0]
    assert first.status == "partial"
    assert first.resolved_quantity == 50
    assert first.outcome_price == 12.0
    assert first.outcome_pnl_pct == 20.0

    second_run = reconcile_decisions(data_dir, TradingCostConfig())
    after_restart = DecisionStore(data_dir).search(limit=10)[0]
    assert second_run.changed == 0
    assert after_restart.outcome_trade_ids == ["sell-1"]
    assert after_restart.resolved_quantity == 50

    reloaded = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    reloaded.record(_trade(
        trade_id="sell-2", decision_id=None, side="sell",
        quantity=150, price=8.0, timestamp=opened + timedelta(days=2),
    ))
    reconcile_decisions(data_dir, TradingCostConfig())
    resolved = DecisionStore(data_dir).search(limit=10)[0]
    assert resolved.status == "resolved"
    assert resolved.resolved_quantity == 200
    assert resolved.outcome_price == 9.0
    assert resolved.outcome_pnl_pct == -10.0
    assert resolved.outcome_trade_ids == ["sell-1", "sell-2"]


def test_legacy_trades_match_existing_decision_without_ids(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    store = DecisionStore(data_dir)
    decision = store.store_decision(
        symbol="600519", side="buy", quantity=100, price=1800.0,
        account="short_stock", reasoning="legacy buy",
    )
    trades = [
        {
            "symbol": "600519", "account": "short_stock", "side": "buy",
            "quantity": 100, "price": 1800.0,
            "timestamp": "2026-06-01T09:30:00", "reason": "legacy buy",
        },
        {
            "symbol": "600519", "account": "short_stock", "side": "sell",
            "quantity": 100, "price": 1440.0,
            "timestamp": "2026-06-10T09:30:00", "reason": "legacy sell",
            "is_t0": True,
        },
    ]
    (data_dir / "paper_trades.jsonl").write_text(
        "\n".join(json.dumps(item) for item in trades) + "\n",
        encoding="utf-8",
    )

    reconcile_decisions(data_dir, TradingCostConfig())
    migrated = next(d for d in DecisionStore(data_dir).search(limit=10) if d.id == decision.id)

    assert migrated.entry_trade_id
    assert migrated.status == "resolved"
    assert migrated.outcome_price == 1440.0
    assert migrated.outcome_pnl_pct == -20.0
    assert migrated.reconciliation_status == "confirmed"
    persisted_trades = [
        json.loads(line)
        for line in (data_dir / "paper_trades.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(item["trade_id"] for item in persisted_trades)
    assert persisted_trades[0]["decision_id"] == decision.id


def test_reconcile_recovers_missing_decision_from_linked_buy_trade(tmp_path):
    data_dir = tmp_path / "data"
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    portfolio.record(_trade(
        trade_id="buy-1", decision_id="decision-1", side="buy",
        quantity=100, price=10.0, timestamp=datetime(2026, 6, 1, 9, 30),
    ))

    reconcile_decisions(data_dir, TradingCostConfig())
    recovered = DecisionStore(data_dir).search(limit=10)

    assert len(recovered) == 1
    assert recovered[0].id == "decision-1"
    assert recovered[0].entry_trade_id == "buy-1"
    assert recovered[0].status == "pending"
    assert recovered[0].reasoning == "buy test"


def test_trade_tools_reconcile_multiple_buy_decisions(tmp_path):
    from unittest.mock import MagicMock

    stack = MagicMock()
    stack.config.data_dir = tmp_path / "data"
    stack.config.memory_dir = tmp_path / "memory"
    stack.config.trading_costs = TradingCostConfig()

    for quantity, price in ((200, 10.0), (100, 20.0)):
        result = json.loads(tool_place_paper_trade(
            stack,
            symbol="159999",
            side="buy",
            quantity=quantity,
            price=price,
            reason="linked buy",
            account="short_stock",
            is_t0=True,
        ))
        assert result["status"] == "executed"
        assert result["trade_id"]
        assert result["decision_id"]

    sold = json.loads(tool_place_paper_trade(
        stack,
        symbol="159999",
        side="sell",
        quantity=300,
        price=15.0,
        reason="full exit",
        account="short_stock",
        is_t0=True,
    ))
    decisions = sorted(DecisionStore(stack.config.data_dir).search(limit=10), key=lambda d: d.price)

    assert sold["status"] == "executed"
    assert [decision.status for decision in decisions] == ["resolved", "resolved"]
    assert [decision.outcome_pnl_pct for decision in decisions] == [50.0, -25.0]


def test_batch_position_import_does_not_fabricate_decision(tmp_path):
    from unittest.mock import MagicMock

    stack = MagicMock()
    stack.config.data_dir = tmp_path / "data"
    stack.config.memory_dir = tmp_path / "memory"
    stack.config.trading_costs = TradingCostConfig()

    result = json.loads(tool_batch_paper_trades(
        stack,
        trades=[{
            "symbol": "510300",
            "side": "buy",
            "quantity": 100,
            "price": 4.0,
            "reason": "existing position import",
            "account": "etf_rotation",
        }],
    ))

    assert result["executed"] == 1
    assert result["results"][0]["decision_id"] is None
    assert DecisionStore(stack.config.data_dir).search(limit=10) == []


def test_decisions_api_recovers_ledger_and_exposes_audit_fields(client, tmp_path):
    data_dir = tmp_path / "data"
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    opened = datetime(2026, 6, 1, 9, 30)
    portfolio.record(_trade(
        trade_id="buy-api", decision_id="decision-api", side="buy",
        quantity=100, price=10.0, timestamp=opened,
    ))
    portfolio.record(_trade(
        trade_id="sell-api", decision_id=None, side="sell",
        quantity=100, price=9.0, timestamp=opened + timedelta(days=1),
    ))

    response = client.get("/api/decisions")
    assert response.status_code == 200
    decision = next(item for item in response.json()["decisions"] if item["id"] == "decision-api")

    assert decision["outcome_price"] == 9.0
    assert decision["resolved_quantity"] == 100
    assert decision["outcome_pnl_pct"] == -10.0
    assert decision["outcome_trade_ids"] == ["sell-api"]
    assert decision["outcome_source"] == "trade_ledger_fifo"
    assert decision["reconciliation_status"] == "confirmed"


def test_market_quote_execution_uses_server_price_and_records_provenance(tmp_path):
    from unittest.mock import MagicMock

    stack = MagicMock()
    stack.config.data_dir = tmp_path / "data"
    stack.config.memory_dir = tmp_path / "memory"
    stack.config.trading_costs = TradingCostConfig()
    quote_time = datetime(2026, 7, 16, 10, 5)
    stack.provider.get_bars.return_value = [
        Bar(
            symbol="600519",
            timestamp=quote_time,
            open=1410.0,
            high=1412.0,
            low=1409.0,
            close=1410.84,
            volume=1000,
        )
    ]

    result = json.loads(tool_place_paper_trade(
        stack,
        symbol="600519",
        side="buy",
        quantity=100,
        price=1800.0,
        price_source="market_quote",
        reason="paper market order",
        account="short_stock",
        is_t0=True,
    ))
    persisted = json.loads((stack.config.data_dir / "paper_trades.jsonl").read_text().splitlines()[0])

    assert result["status"] == "executed"
    assert result["price"] == 1410.84
    assert result["requested_price"] == 1800.0
    assert result["price_as_of"] == quote_time.isoformat()
    assert persisted["price"] == 1410.84
    assert persisted["price_source"] == "market_quote"
    assert persisted["requested_price"] == 1800.0


def test_market_quote_failure_rejects_before_ledger_write(tmp_path):
    from unittest.mock import MagicMock

    stack = MagicMock()
    stack.config.data_dir = tmp_path / "data"
    stack.config.memory_dir = tmp_path / "memory"
    stack.config.trading_costs = TradingCostConfig()
    stack.provider.get_bars.side_effect = RuntimeError("quote unavailable")

    result = json.loads(tool_place_paper_trade(
        stack,
        symbol="600519",
        side="buy",
        quantity=100,
        price_source="market_quote",
        reason="paper market order",
        account="short_stock",
    ))

    assert result["trade_rejected"] is True
    assert "quote unavailable" in result["error"]
    assert not (stack.config.data_dir / "paper_trades.jsonl").exists()


def test_agent_trade_schema_defaults_to_server_market_quote():
    from trade_compass_agent.runtime.tools.registry import BASE_TOOL_SCHEMAS

    schema = next(
        item["function"] for item in BASE_TOOL_SCHEMAS
        if item["function"]["name"] == "place_paper_trade"
    )

    assert schema["parameters"]["properties"]["price_source"]["default"] == "market_quote"
    assert "price" not in schema["parameters"]["required"]


def test_changed_ledger_outcome_archives_stale_reflection(tmp_path):
    data_dir = tmp_path / "data"
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl")
    opened = datetime(2026, 6, 1, 9, 30)
    portfolio.record(_trade(
        trade_id="buy-1", decision_id="decision-1", side="buy",
        quantity=100, price=10.0, timestamp=opened,
    ))
    portfolio.record(_trade(
        trade_id="sell-1", decision_id=None, side="sell",
        quantity=100, price=9.0, timestamp=opened + timedelta(days=1),
    ))
    store = DecisionStore(data_dir)
    store.store_decision(
        symbol="600703", side="buy", quantity=100, price=10.0,
        account="short_stock", reasoning="old decision",
        decision_id="decision-1", entry_trade_id="buy-1",
    )
    store.resolve(symbol="600703", account="short_stock", sell_price=10.0)
    store.add_reflection("decision-1", "旧的 0% 复盘")

    reconcile_decisions(data_dir, TradingCostConfig())
    corrected = DecisionStore(data_dir).search(limit=10)[0]

    assert corrected.status == "resolved"
    assert corrected.outcome_pnl_pct == -10.0
    assert corrected.reflection is None
    assert corrected.reflection_stale is True
    assert corrected.reflection_history == ["旧的 0% 复盘"]
