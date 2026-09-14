"""Consumer checks for the global switch, execution feedback and shared account funds."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig
from trade_compass_agent.domain import AccountKind, Bar, PaperTrade
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.accounts import AccountStore
from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore, buying_power
from trade_compass_agent.runtime.tools import portfolio as portfolio_tools
from trade_compass_agent.runtime.tools.registry import ToolRegistry


NOW = datetime(2026, 9, 9, 10, 5)


def quote(price=10.0, timestamp=NOW):
    return Bar(symbol="000001", timestamp=timestamp, open=price, high=price, low=price,
               close=price, volume=1000)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", agent=AgentConfig(llm_session_titles=False))
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: NOW)
    monkeypatch.setattr("trade_compass_agent.ops.trading_calendar.is_trading_day", lambda day: True)
    monkeypatch.setattr(portfolio_tools, "_update_signal_tracker", lambda *args, **kwargs: None)
    AccountStore(config.data_dir / "accounts.json").update("short_stock", capital=1500)
    return SimpleNamespace(config=config, provider=SimpleNamespace(get_bars=lambda *args, **kwargs: [quote()]))


def order(**changes):
    return {"symbol": "000001", "side": "buy", "quantity": 100,
            "account": "short_stock", "reason": "市场分析后的交易决策", **changes}


def execute(stack, **changes):
    return json.loads(ToolRegistry(stack).execute("place_paper_trade", order(**changes)))


def ledger(stack):
    return JsonPaperPortfolio(stack.config.data_dir / "paper_trades.jsonl", costs=stack.config.trading_costs)


def test_switch_defaults_off_persists_and_rejects_autonomous_order(stack):
    store = AutonomousTradingStore(stack.config.data_dir)
    assert store.read() is False
    assert execute(stack)["code"] == "autonomous_trading_disabled"
    assert ledger(stack).trades == []
    store.set_enabled(True)
    assert AutonomousTradingStore(stack.config.data_dir).read() is True
    assert execute(stack)["status"] == "executed"
    store.set_enabled(False)
    assert execute(stack)["code"] == "autonomous_trading_disabled"
    assert len(ledger(stack).trades) == 1


def test_explicit_current_instruction_works_off_but_cannot_leak_to_next_turn(stack):
    registry = ToolRegistry(stack)
    instruction = "帮我买入000001一百股"
    registry.set_trade_context(instruction)
    result = json.loads(registry.execute("place_paper_trade", order(user_instruction=instruction)))
    assert result["status"] == "executed"
    registry.set_trade_context("分析一下市场")
    result = json.loads(registry.execute("place_paper_trade", order(user_instruction=instruction)))
    assert result["code"] == "invalid_user_instruction"
    assert len(ledger(stack).trades) == 1


def test_scheduled_context_cannot_claim_interactive_user_authorization(stack):
    registry = ToolRegistry(stack, memory_actor="scheduler")
    registry.set_trade_context("买入000001一百股")
    result = json.loads(registry.execute("place_paper_trade", order(user_instruction="买入000001一百股")))
    assert result["code"] == "invalid_user_instruction"
    assert ledger(stack).trades == []


def test_imports_cannot_bypass_switch_or_market_prices(stack):
    registry = ToolRegistry(stack)
    batch = {"trades": [order(price=10)]}
    assert json.loads(registry.execute("batch_paper_trades", batch))["code"] == "user_instruction_required"
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert execute(stack, price_source="user_confirmed", price=1)["code"] == "market_quote_required"
    registry.set_trade_context("同步真实账户买入000001一百股，成交价10元")
    batch["user_instruction"] = "同步真实账户买入000001一百股，成交价10元"
    assert json.loads(registry.execute("batch_paper_trades", batch))["executed"] == 1


def test_turning_off_during_quote_fetch_prevents_commit(stack):
    store = AutonomousTradingStore(stack.config.data_dir)
    store.set_enabled(True)

    def fetch(*args, **kwargs):
        store.set_enabled(False)
        return [quote()]

    stack.provider.get_bars = fetch
    assert execute(stack)["code"] == "autonomous_trading_disabled"
    assert ledger(stack).trades == []


def test_fee_inclusive_cash_and_concurrent_orders_cannot_overspend(stack):
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(stack), range(2)))
    assert sum(r.get("status") == "executed" for r in results) == 1
    failure = next(r for r in results if r.get("trade_rejected"))
    assert failure["code"] == "insufficient_funds"
    assert failure["required_cash"] > 1000
    assert failure["available_cash"] < 500
    portfolio = ledger(stack)
    assert len(portfolio.trades) == 1
    assert buying_power(portfolio, stack.config.data_dir, AccountKind.SHORT_STOCK) == failure["available_cash"]


def test_exact_gross_balance_is_not_enough_for_fees(stack):
    AccountStore(stack.config.data_dir / "accounts.json").update("short_stock", capital=1000)
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert execute(stack)["code"] == "insufficient_funds"
    assert ledger(stack).trades == []


def test_signal_index_failure_does_not_report_a_committed_fill_as_failed(stack, monkeypatch):
    def fail(*args):
        raise OSError("signal index unavailable")

    monkeypatch.setattr(portfolio_tools, "_update_signal_tracker", fail)
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert execute(stack)["status"] == "executed"
    assert len(ledger(stack).trades) == 1


def _concurrent_process_buy(data_dir):
    from pathlib import Path
    from trade_compass_agent.portfolio.trading_policy import buying_power

    path = Path(data_dir)
    portfolio = JsonPaperPortfolio(path / "paper_trades.jsonl")
    trade = PaperTrade(symbol="000001", account=AccountKind.SHORT_STOCK, side="buy",
                      quantity=100, price=10, timestamp=NOW, reason="concurrent process")

    def validate(current):
        if buying_power(current, path, trade.account) < 1000 + current.estimate_fee(trade):
            raise ValueError("insufficient funds")

    try:
        portfolio.record(trade, before_record=validate)
    except ValueError:
        return False
    return True


def test_separate_processes_share_the_buying_power_lock(stack):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_concurrent_process_buy, [str(stack.config.data_dir)] * 4))
    assert results.count(True) == 1
    assert len(ledger(stack).trades) == 1


def test_existing_import_and_sell_proceeds_change_cash_without_cross_account_funding(stack):
    portfolio = ledger(stack)
    imported = PaperTrade(symbol="000001", account=AccountKind.SHORT_STOCK, side="buy",
                          quantity=100, price=10, timestamp=NOW - timedelta(days=1),
                          reason="外部同步")
    portfolio.record(imported)
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert execute(stack)["code"] == "insufficient_funds"
    assert execute(stack, side="sell")["status"] == "executed"
    assert execute(stack)["status"] == "executed"
    assert len(ledger(stack).trades) == 3
    # Another account's unused 300k allocation never funds short_stock.
    assert execute(stack)["code"] == "insufficient_funds"


@pytest.mark.parametrize("changes,code", [
    ({"timestamp": NOW - timedelta(minutes=5)}, "stale_quote"),
    ({"timestamp": NOW + timedelta(minutes=5)}, "stale_quote"),
    ({"volume": 0}, "no_market_trades"),
])
def test_unusable_quote_returns_failure_without_filling(stack, changes, code):
    from dataclasses import replace
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    bar = replace(quote(), **changes)
    stack.provider.get_bars = lambda *args, **kwargs: [bar]
    assert execute(stack)["code"] == code
    assert ledger(stack).trades == []


def test_market_closed_and_t1_cannot_be_overridden_by_agent(stack, monkeypatch):
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert execute(stack)["status"] == "executed"
    rejected = execute(stack, side="sell", is_t0=True)
    assert rejected["trade_rejected"] is True
    assert "T+1" in rejected["error"]
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: NOW.replace(hour=12))
    assert execute(stack)["code"] == "market_closed"
    assert len(ledger(stack).trades) == 1


def test_ambiguous_account_does_not_combine_allocations(stack):
    AccountStore(stack.config.data_dir / "accounts.json").create("short_stock", "另一个短线", capital=100000)
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    assert "无法确定可用资金" in execute(stack)["error"]
    assert ledger(stack).trades == []


def test_api_switch_and_manual_sync_keep_existing_behavior(client, monkeypatch):
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: NOW)
    route = "/api/portfolio/autonomous-trading"
    assert client.get(route).json() == {"enabled": False}
    assert client.put(route, json={"enabled": True}).json() == {"enabled": True}
    assert client.get(route).json() == {"enabled": True}
    assert client.put(route, json={"enabled": "false"}).status_code == 422
    assert client.put(route, json={"enabled": False}).status_code == 200
    assert client.put("/api/accounts/short_stock", json={"capital": 100}).status_code == 200
    # Manual synchronization may exceed the allocation and sell the same day.
    for side in ("buy", "sell"):
        response = client.post("/api/portfolio/trades", json=order(side=side, price=10))
        assert response.status_code == 200
    accounts = client.get("/api/accounts").json()
    cash = next(a["available_cash"] for a in accounts if a["id"] == "short_stock")
    assert 0 < cash < 100  # Both executions contribute their fees.


@pytest.mark.parametrize("scheduled", [False, True])
def test_agent_receives_failed_trade_and_retries_in_existing_session(stack, monkeypatch, scheduled):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.runtime.loop import AgentLoop
    from trade_compass_agent.runtime.session import SessionStore

    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)

    class Client:
        count = 0

        def stream_complete(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                assert "全局 Agent 自主交易：开启" in messages[0].content
                quantity = 200
            elif self.count == 2:
                assert json.loads(messages[-1].content)["code"] == "insufficient_funds"
                quantity = 100
            else:
                assert json.loads(messages[-1].content)["status"] == "executed"
                return ChatCompletion(content="已根据可用资金调整为100股，模拟买入成交。", model="test", provider="test")
            return ChatCompletion(content="", model="test", provider="test", tool_calls=[
                ToolCall(id=f"trade-{self.count}", name="place_paper_trade", arguments=json.dumps(order(quantity=quantity))),
            ])

    model = Client()
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda config: model)
    monkeypatch.setattr(AgentLoop, "_update_session_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *args, **kwargs: None)
    session_store = SessionStore(stack.config.data_dir / "agent_sessions")
    agent = AgentLoop(config=stack.config, stack=stack, session_store=session_store,
                      memory_actor="scheduler" if scheduled else "agent")
    if scheduled:
        monkeypatch.setattr(AgentLoop, "from_config", lambda *args, **kwargs: agent)
        output = ScheduledAgentSession(stack.config, job_id="intraday").run("盘中分析并决定是否调仓", timeout=10)
    else:
        output = agent.run_turn("分析当前持仓和市场").summary
    assert "成交" in output
    assert len(ledger(stack).trades) == 1
    records = [json.loads(line) for path in (stack.config.data_dir / "agent_sessions").glob("*.jsonl")
               for line in path.read_text().splitlines()]
    results = [json.loads(r["content"]) for r in records if r.get("role") == "tool"]
    assert results[0]["code"] == "insufficient_funds"
    assert results[1]["status"] == "executed"


def test_intraday_scheduler_runs_real_workflow_agent_and_keeps_receipts(stack, monkeypatch):
    from datetime import date
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.ops.job_definition import JobRegistry
    from trade_compass_agent.ops.tick_scheduler import TickScheduler
    from trade_compass_agent.ops.session_cleanup import sweep_scheduler_sessions
    from trade_compass_agent.runtime.loop import AgentLoop
    from trade_compass_agent.runtime.session import SessionStore

    clock = [NOW]
    monkeypatch.setattr(portfolio_tools, "_market_now", lambda: clock[0])
    monkeypatch.setattr("trade_compass_agent.ops.job_executor._is_trading_day", lambda: True)
    stack.provider.get_bars = lambda *a, **kw: [quote(timestamp=clock[0])]
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    clients = []

    class Client:
        count = 0

        def stream_complete(self, messages, **kwargs):
            self.count += 1
            names = {s['function']['name'] for s in kwargs.get('tools') or []}
            assert 'place_paper_trade' in names
            assert 'schedule_task' not in names
            if len(clients) > 1:
                return ChatCompletion(content="本轮继续持有，当前可用资金不足以再买一手。", model="test", provider="test")
            if self.count == 1:
                return ChatCompletion(content="", model="test", provider="test", tool_calls=[
                    ToolCall(id='load-playbook', name='load_skill', arguments=json.dumps({'name': 'autonomous-paper-trading'}))])
            if self.count == 2:
                assert 'skill not found' not in messages[-1].content
                assert 'place_paper_trade' in messages[-1].content
                quantity = 200
            elif self.count == 3:
                assert json.loads(messages[-1].content)['code'] == 'insufficient_funds'
                quantity = 100
            else:
                result = json.loads(messages[-1].content)
                assert result['status'] == 'executed'
                return ChatCompletion(content=f"本轮实际成交100股，trade_id={result['trade_id']}。", model="test", provider="test")
            return ChatCompletion(content="", model="test", provider="test", tool_calls=[
                ToolCall(id=f'trade-{self.count}', name='place_paper_trade', arguments=json.dumps(order(quantity=quantity)))])

    def client_factory(config):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr('trade_compass_agent.runtime.loop.create_chat_client', client_factory)
    monkeypatch.setattr(AgentLoop, '_update_session_summary', lambda *a, **kw: None)
    monkeypatch.setattr(AgentLoop, '_maybe_background_review', lambda *a, **kw: None)
    monkeypatch.setattr(AgentLoop, 'from_config', lambda config, **kw: AgentLoop(
        config=config, stack=stack, session_store=SessionStore(config.data_dir / 'agent_sessions'),
        memory_actor=kw.get('memory_actor', 'scheduler')))

    def scheduler():
        value = TickScheduler(stack.config)
        job = value.registry.get('autonomous_trading')
        value.registry = JobRegistry()
        value.registry.register(job)
        monkeypatch.setattr(value.watch_plan_monitor, 'tick', lambda now: None)
        return value

    first = scheduler()
    first._tick()
    runs = first.run_store.recent_runs(job_id='autonomous_trading')
    assert len(runs) == 1
    assert runs[0].status == 'completed', runs[0].error
    assert '实际成交' in runs[0].message
    assert len(ledger(stack).trades) == 1
    trade_id = ledger(stack).trades[0].trade_id
    assert trade_id in runs[0].message
    artifacts = [json.loads(s) for s in Path(runs[0].artifact).read_text().splitlines()]
    assert artifacts[-1]['no_trade_disclaimer'] is False
    from fastapi.testclient import TestClient
    from trade_compass_agent.web.app import create_app
    monkeypatch.setattr('trade_compass_agent.web.api.load_app_config', lambda: stack.config)
    client = TestClient(create_app())
    detail = client.get(f'/api/jobs/runs/{runs[0].id}')
    assert detail.status_code == 200
    assert trade_id in json.dumps(detail.json(), ensure_ascii=False)
    first._tick()
    restarted = scheduler()
    restarted._tick()
    assert len(restarted.run_store.recent_runs(job_id='autonomous_trading')) == 1
    clock[0] = NOW.replace(minute=35)
    restarted._tick()
    assert len(restarted.run_store.recent_runs(job_id='autonomous_trading')) == 2
    sessions = list((stack.config.data_dir / 'agent_sessions').glob('scheduler-autonomous_trading-*.jsonl'))
    assert len(sessions) == 2
    all_records = [json.loads(s) for p in sessions for s in p.read_text().splitlines()]
    receipts = [json.loads(r['content']) for r in all_records if r.get('role') == 'tool' and r.get('name') == 'place_paper_trade']
    assert receipts[0]['code'] == 'insufficient_funds'
    assert receipts[1]['trade_id'] == trade_id
    sweep_scheduler_sessions(stack.config.data_dir, now=date(2027, 1, 1), force=True)
    assert all(p.exists() for p in sessions)
    AutonomousTradingStore(stack.config.data_dir).set_enabled(False)
    clock[0] = NOW.replace(hour=11)
    restarted._tick()
    assert len(restarted.run_store.recent_runs(job_id='autonomous_trading')) == 2
    assert restarted.list_jobs()[0].enabled is False


def test_stopped_intraday_run_cannot_commit_after_slow_quote(stack):
    from trade_compass_agent.portfolio.trading_policy import TradeRejected
    AutonomousTradingStore(stack.config.data_dir).set_enabled(True)
    active = [True]
    registry = ToolRegistry(stack, memory_actor='scheduler')

    def guard():
        if not active[0]:
            raise TradeRejected('本轮已停止', 'execution_inactive')

    def quote_after_timeout(*args, **kwargs):
        active[0] = False
        return [quote()]

    registry.trade_execution_guard = guard
    stack.provider.get_bars = quote_after_timeout
    result = json.loads(registry.execute('place_paper_trade', order()))
    assert result['code'] == 'execution_inactive'
    assert ledger(stack).trades == []
