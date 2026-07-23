"""Tests for OMS integrity guardrails (system prompt, escalation, reconciliation, trade return)."""

import asyncio
import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from trade_compass_agent.config import TradingCostConfig


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def portfolio_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    memory_dir = tmp_path / "memory_vault"
    memory_dir.mkdir()
    (memory_dir / "instruments").mkdir()

    config = MagicMock()
    config.data_dir = data_dir
    config.memory_dir = memory_dir
    config.trading_costs = TradingCostConfig()

    stack = MagicMock()
    stack.config = config
    return stack


def _seed_buy(stack, symbol="600519", qty=100, price=1800.0, account="short_stock"):
    trades_file = stack.config.data_dir / "paper_trades.jsonl"
    trade = {
        "symbol": symbol,
        "account": account,
        "side": "buy",
        "quantity": qty,
        "price": price,
        "timestamp": (datetime.now() - timedelta(days=1)).isoformat(),
        "reason": "seed",
        "previous_close": price * 0.99,
        "suspended": False,
        "is_st": False,
    }
    with trades_file.open("a") as f:
        f.write(json.dumps(trade, ensure_ascii=False) + "\n")


# ── 1. System Prompt contains OMS guardrail rule ──────────────────────────


class TestSystemPromptGuardrail:
    def test_grounding_rules_contain_trade_irreplaceable(self):
        from trade_compass_agent.runtime.bootstrap import GROUNDING_RULES

        assert "交易操作不可替代" in GROUNDING_RULES
        assert "write_memory" in GROUNDING_RULES
        assert "status=executed" in GROUNDING_RULES


# ── 2. Critical tool failure escalation ───────────────────────────────────


class TestCriticalToolEscalation:
    def _make_registry(self, stack):
        from trade_compass_agent.runtime.tools.registry import ToolRegistry
        return ToolRegistry(stack)

    def test_first_exception_no_escalation(self, portfolio_stack):
        registry = self._make_registry(portfolio_stack)
        result = json.loads(registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": "bad", "price": 1800.0,
        })))
        assert "error" in result
        assert "escalation" not in result

    def test_second_consecutive_exception_has_escalation(self, portfolio_stack):
        registry = self._make_registry(portfolio_stack)
        registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": "bad", "price": 1800.0,
        }))
        result = json.loads(registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": "bad", "price": 1800.0,
        })))
        assert "escalation" in result
        assert "连续失败" in result["escalation"]

    def test_success_resets_counter(self, portfolio_stack):
        registry = self._make_registry(portfolio_stack)
        registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": "bad", "price": 1800.0,
        }))
        registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": 100, "price": 1800.0,
            "reason": "test",
        }))
        result = json.loads(registry.execute("place_paper_trade", json.dumps({
            "symbol": "600519", "side": "buy", "quantity": "bad", "price": 1800.0,
        })))
        assert "escalation" not in result

    def test_non_critical_tool_never_escalates(self, portfolio_stack):
        registry = self._make_registry(portfolio_stack)
        for _ in range(5):
            registry.execute("nonexistent_tool", None)
        result = json.loads(registry.execute("nonexistent_tool", None))
        assert "escalation" not in result

    def test_validation_error_not_counted(self, portfolio_stack):
        """Validation returns (not exceptions) should not trigger escalation."""
        registry = self._make_registry(portfolio_stack)
        for _ in range(5):
            registry.execute("place_paper_trade", json.dumps({
                "symbol": "", "side": "", "quantity": 0, "price": 0,
            }))
        result = json.loads(registry.execute("place_paper_trade", json.dumps({
            "symbol": "", "side": "", "quantity": 0, "price": 0,
        })))
        assert "escalation" not in result


# ── 3. Portfolio-memory reconciliation ────────────────────────────────────


class TestPortfolioMemoryReconcile:
    def _ctx(self, stack):
        from trade_compass_agent.ops.job_definition import StepContext
        return StepContext(config=stack.config, date=date.today())

    def test_consistent_state(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.builtin_operations import reconcile_portfolio_memory
        result = asyncio.run(reconcile_portfolio_memory(self._ctx(portfolio_stack)))
        assert result.data["discrepancies"] == []

    def test_memory_has_stale_position(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.builtin_operations import reconcile_portfolio_memory
        inst_dir = portfolio_stack.config.memory_dir / "instruments"
        (inst_dir / "600519.md").write_text(
            "# 600519\n## 笔记\n短线账户持仓，成本价1800，持仓100股\n"
        )
        result = asyncio.run(reconcile_portfolio_memory(self._ctx(portfolio_stack)))
        assert len(result.data["discrepancies"]) == 1
        assert "OMS无持仓" in result.data["discrepancies"][0]

    def test_oms_has_but_memory_missing(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.builtin_operations import reconcile_portfolio_memory
        _seed_buy(portfolio_stack)
        result = asyncio.run(reconcile_portfolio_memory(self._ctx(portfolio_stack)))
        assert len(result.data["discrepancies"]) == 1
        assert "无instrument页面" in result.data["discrepancies"][0]

    def test_closed_instrument_ignored(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.builtin_operations import reconcile_portfolio_memory
        inst_dir = portfolio_stack.config.memory_dir / "instruments"
        (inst_dir / "600519.md").write_text("# 600519\n## 笔记\n已平仓\n")
        result = asyncio.run(reconcile_portfolio_memory(self._ctx(portfolio_stack)))
        assert result.data["discrepancies"] == []

    def test_sync_instrument_pages_creates_and_updates_from_ledger(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.builtin_operations import sync_instrument_pages
        _seed_buy(portfolio_stack, symbol="600519", qty=100, price=1800.0)

        result = asyncio.run(sync_instrument_pages(self._ctx(portfolio_stack)))
        page = portfolio_stack.config.memory_dir / "instruments" / "600519.md"

        assert result.data["created"] == 1
        assert page.exists()
        assert "buy 100股 @1800 [short_stock]" in page.read_text(encoding="utf-8")

        second = asyncio.run(sync_instrument_pages(self._ctx(portfolio_stack)))
        text = page.read_text(encoding="utf-8")
        assert second.data["created"] == 0
        assert second.data["updated"] == 1
        assert text.count("buy 100股 @1800 [short_stock]") == 1


# ── 4. place_paper_trade returns position_after ───────────────────────────


class TestTradeResultPositionAfter:
    def test_buy_returns_position_after(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.portfolio import tool_place_paper_trade
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519", side="buy", quantity=100, price=1800.0,
            reason="test", account="short_stock",
        ))
        assert result["status"] == "executed"
        pa = result["position_after"]
        assert pa["quantity"] == 100
        assert pa["avg_cost"] > 0

    def test_sell_returns_closed_true(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.portfolio import tool_place_paper_trade
        _seed_buy(portfolio_stack)
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519", side="sell", quantity=100, price=1850.0,
            reason="exit", account="short_stock",
        ))
        assert result["status"] == "executed"
        pa = result["position_after"]
        assert pa["closed"] is True
        assert pa["quantity"] == 0

    def test_partial_sell_not_closed(self, portfolio_stack):
        from trade_compass_agent.runtime.tools.portfolio import tool_place_paper_trade
        _seed_buy(portfolio_stack, qty=200)
        result = json.loads(tool_place_paper_trade(
            portfolio_stack,
            symbol="600519", side="sell", quantity=100, price=1850.0,
            reason="reduce", account="short_stock",
        ))
        assert result["status"] == "executed"
        pa = result["position_after"]
        assert pa["closed"] is False
        assert pa["quantity"] == 100
