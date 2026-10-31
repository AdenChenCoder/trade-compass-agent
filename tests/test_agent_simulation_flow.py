from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain import Bar
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.ops.agent_session import ScheduledAgentSession
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.accounts import AccountStore
from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
from trade_compass_agent.runtime import loop as loop_module
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.session import SessionStore
from trade_compass_agent.runtime.tools import portfolio as portfolio_tools
from trade_compass_agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("backend", ["api", "native"])
def test_scheduled_simulated_task_uses_real_tools_ledger_and_recoverable_archive(tmp_path, monkeypatch, backend):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    if backend == "native":
        config = replace(config, llm=replace(config.llm, provider="codex"))
    stack = MarketStack.from_config(config)
    now = [datetime(2026, 10, 30, 10, 5)]
    price = [100.0]

    class FixtureTransport:
        name = "acceptance_fixture"

        def get_bars(self, symbol, **kw):
            return [Bar(symbol=symbol, timestamp=now[0], open=price[0], high=price[0], low=price[0],
                        close=price[0], volume=1000, source="fixture")]

    stack = replace(stack, provider=FixtureTransport())
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: now[0])
    monkeypatch.setattr("trade_compass_agent.ops.trading_calendar.is_trading_day", lambda _: True)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    AccountStore(config.data_dir / "accounts.json").update("etf_rotation", capital=30000)
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    monkeypatch.setattr(loop_module.AgentLoop, "from_config", classmethod(
        lambda cls, cfg=None, **kw: cls(config=cfg or config, stack=stack,
                                      session_store=SessionStore(config.data_dir / "agent_sessions"), **kw)))
    receipts = []
    archive = "合成演练归档：模拟盈利不能作为真实投资规律或长期知识的证据。"
    steps = [
        ("get_bars", {"symbol": "511880", "timeframe": "1m", "limit": 1}),
        ("place_paper_trade", {"symbol": "511880", "side": "buy", "quantity": 100,
                              "account": "etf_rotation", "record_decision": False, "reason": "合成演练"}),
        ("place_paper_trade", {"symbol": "511880", "side": "sell", "quantity": 100,
                              "account": "etf_rotation", "record_decision": False, "reason": "合成演练"}),
        ("place_paper_trade", {"symbol": "511880", "side": "sell", "quantity": 100,
                              "account": "etf_rotation", "record_decision": False, "reason": "合成零持仓拒绝案例"}),
        ("analyze_portfolio", {}),
        ("write_memory", {"scope": "acceptance", "content": archive}),
    ]

    def arguments(index):
        if index == 2:
            price[0] = 101
            now[0] += timedelta(minutes=1)
        name, args = steps[index]
        return ToolCall(id=f"step-{index}", name=name, arguments=json.dumps(args, ensure_ascii=False))

    class ApiModel:
        def __init__(self):
            self.index = 0

        def stream_complete(self, messages, **kw):
            if messages[-1].role == "tool":
                receipts.append(json.loads(messages[-1].content))
            if self.index == len(steps):
                return ChatCompletion(content="本次仅为固定行情与模拟决策的隔离演练，交易及归档结果以回执为准。")
            call = arguments(self.index)
            self.index += 1
            return ChatCompletion(content=None, tool_calls=[call])

    class NativeModel(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, execute_tool, **kw):
            for index in range(len(steps)):
                receipts.append(json.loads(execute_tool(arguments(index))))
            return ChatCompletion(content="本次仅为固定行情与模拟决策的隔离演练，交易及归档结果以回执为准。")

    monkeypatch.setattr(loop_module, "create_chat_client", lambda _: NativeModel() if backend == "native" else ApiModel())
    scheduled = ScheduledAgentSession(config, job_id="autonomous_trading", step_id="simulation", run_date=now[0].date(),
                                      tool_whitelist={name for name, _ in steps})
    summary = scheduled.run("执行合成行情演练，遵循原有交易规则；只保存演练归档，禁止据此晋升长期知识。", timeout=30)
    assert "演练" in summary
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    assert len(portfolio.trades) == 2 and not portfolio.positions()
    assert receipts[1]["status"] == receipts[2]["status"] == "executed"
    assert receipts[3]["trade_rejected"] and "持仓" in receipts[3]["error"]
    settled = round(sum(row.pnl for row in portfolio.realized_trades()), 2)
    assert receipts[2]["realized_pnl"] == settled and settled < 100
    assert receipts[2]["realized_pnl_basis"] == "fifo_after_entry_and_exit_fees"
    assert len(receipts[4]["recent_trades"]) == 2
    balance = next(row for row in receipts[4]["account_balances"] if row["id"] == "etf_rotation")
    cash_changes = sum((1 if trade.side == "sell" else -1) * trade.price * trade.quantity
                       - portfolio.estimate_fee(trade) for trade in portfolio.trades)
    assert balance["available_cash"] == round(30000 + cash_changes, 4)
    stored = json.loads(ToolRegistry(stack).execute("search_memory", {"query": archive}))
    assert stored["results"] and archive in stored["results"][0]["content"]
    assert not MemoryStore(config.memory_dir).list_active()
    history = SessionStore(config.data_dir / "agent_sessions").load(scheduled.session_id)
    assert history and len([m for m in history.messages if m.role == "tool"]) == len(steps)


@pytest.mark.parametrize("sample", ["sample", "synthetic"])
def test_declared_sample_quote_cannot_become_an_autonomous_fill(tmp_path, monkeypatch, sample):
    from types import SimpleNamespace
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    now = datetime(2026, 10, 30, 10, 5)
    bar = Bar(symbol="511880", timestamp=now, open=100, high=100, low=100, close=100, volume=1000, source=sample)
    stack = SimpleNamespace(config=config, provider=SimpleNamespace(name="fixture", get_bars=lambda *a, **kw: [bar]))
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: now)
    monkeypatch.setattr("trade_compass_agent.ops.trading_calendar.is_trading_day", lambda _: True)
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    result = json.loads(ToolRegistry(stack).execute("place_paper_trade", {"symbol": "511880", "side": "buy",
        "quantity": 100, "account": "etf_rotation", "reason": "显式样例拒绝案例"}))
    assert result.get("code") == "sample_quote"
    assert not JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl").trades
