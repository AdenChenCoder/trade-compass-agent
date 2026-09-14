"""Consumer checks for valuation snapshots, bar evidence, and actual fills."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig
from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.runtime.loop import _build_provenance_footer, _check_data_gap
from trade_compass_agent.runtime.tools.portfolio import tool_analyze_portfolio
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.verifier import trade_receipt_section


NOW = datetime(2026, 9, 14, 10, 35)


@pytest.fixture
def valued_stack(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", agent=AgentConfig(llm_session_titles=False))
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl")
    for index, (side, quantity, price) in enumerate([("buy", 1000, 1.1113), ("buy", 2000, 1.2222), ("sell", 1500, 1.283)]):
        portfolio.record(PaperTrade(symbol="515220", account=AccountKind.ETF_ROTATION,
            side=side, quantity=quantity, price=price, timestamp=NOW - timedelta(days=1 if side == "buy" else 0, minutes=30-index),
            reason="test", trade_id=f"trade-{index}", decision_id=f"decision-{index}" if side == "buy" else None))
    def snapshot(self, provider=None):
        self._market_prices = {"515220": 1.5}
        return original(self)
    original = JsonPaperPortfolio.positions_with_market_prices
    monkeypatch.setattr(JsonPaperPortfolio, "positions_with_market_prices", snapshot)
    monkeypatch.setattr(JsonPaperPortfolio, "resolve_names", lambda *a, **k: None)
    monkeypatch.setattr("trade_compass_agent.runtime.tools.portfolio._market_now", lambda: NOW)
    monkeypatch.setattr("trade_compass_agent.runtime.tools.portfolio._fetch_exit_review_market_context", lambda *a: {})
    return SimpleNamespace(config=config, provider=object())


def test_api_and_tool_use_same_valuation_and_preserve_fifo_cost(client, valued_stack, monkeypatch):
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: valued_stack.config)
    monkeypatch.setattr("trade_compass_agent.data.create_market_data_provider", lambda *a, **k: object())
    path = valued_stack.config.data_dir / "paper_trades.jsonl"
    before = path.read_bytes()
    response = client.get("/api/portfolio")
    assert response.status_code == 200
    api = response.json()
    tool = json.loads(tool_analyze_portfolio(valued_stack))
    for summaries, positions in [(api["accounts"], api["positions_by_account"]["etf_rotation"]),
                                 (tool["account_summaries"], tool["positions"])]:
        account = next(s for s in summaries if s["account"] == "etf_rotation")
        assert account["market_value"] == sum(p["market_value"] for p in positions) == 2250
        assert account["unrealized_pnl"] == sum(p["unrealized_pnl"] for p in positions) == 416.7
        assert account["cost_basis"] == 1833.3  # Not rounded avg_cost 1.222 * 1500.
    assert path.read_bytes() == before


def test_empty_valuation_is_not_replaced_by_old_positions(valued_stack):
    from trade_compass_agent.web.serializers import to_portfolio_response
    portfolio = JsonPaperPortfolio(valued_stack.config.data_dir / "paper_trades.jsonl")
    response = to_portfolio_response(portfolio, costs=portfolio.costs, live_positions=[])
    assert all(not rows for rows in response.positions_by_account.values())
    assert all(account.market_value == 0 for account in response.accounts)
    assert any(account.realized_pnl != 0 for account in response.accounts)


@pytest.mark.parametrize("summary_only", [True, False])
def test_batch_evidence_retains_only_real_gaps_and_provider(summary_only):
    item = {"bars_count": 30, "source": "tencent", "as_of": "2026-09-14T15:00:00"}
    item.update({"close": 1.273} if summary_only else {"bars": [{"C": 1.273}]})
    calls = [("batch_get_bars", json.dumps({"results": {"515220": item, "515180": {"bars_count": 0}},
                                         "errors": {"513060": "timeout"}}))]
    warning = _check_data_gap("建议减仓515220，持有515180，买入513060。", calls)
    assert warning and "515220" not in warning and "515180" in warning and "513060" in warning
    assert "tencent" in _build_provenance_footer(calls)
    assert "2026-09-14T15:00" in _build_provenance_footer(calls)


@pytest.mark.parametrize("item", [{"bars": []}, {"bars": [{"close": 0}]}, {"bars": [{"close": 1}], "error": "failed"}])
def test_empty_or_failed_single_bars_cannot_hide_a_gap(item):
    warning = _check_data_gap("建议买入515220", [("get_bars", json.dumps({"symbol": "515220", **item}))])
    assert warning and "515220" in warning


def test_execution_history_distinguishes_one_fill_from_two_fifo_settlements(valued_stack):
    payload = json.loads(tool_analyze_portfolio(valued_stack))
    assert payload["recent_trades"][0]["quantity"] == 1500
    assert payload["recent_trades"][0]["trade_id"] == "trade-2"
    assert [r["quantity"] for r in payload["recent_closed_trades"]] == [1000, 500]
    assert {r["exit_trade_id"] for r in payload["recent_closed_trades"]} == {"trade-2"}
    decisions = json.loads(ToolRegistry(valued_stack).execute("search_decisions", {"symbol": "515220"}))
    assert len(decisions) == 2
    for decision in decisions:
        sell = next(t for t in decision["execution_trades"] if t["side"] == "sell")
        assert sell["trade_id"] == "trade-2" and sell["quantity"] == 1500


def test_receipt_table_deduplicates_ledger_and_execution_and_excludes_rejections(valued_stack):
    ledger = tool_analyze_portfolio(valued_stack)
    sell = json.loads(ledger)["recent_trades"][0]
    calls = [("analyze_portfolio", ledger), ("analyze_portfolio", ledger),
             ("place_paper_trade", json.dumps({**sell, "status": "executed"})),
             ("place_paper_trade", json.dumps({**sell, "trade_id": "rejected", "status": "rejected", "error": "stale quote"}))]
    table = trade_receipt_section(calls)
    assert table.count("trade-2") == 1 and "| 1500 | 1.283 | trade-2 |" in table
    assert "本轮回执" in table and "rejected" not in table
    assert "当日账本" in trade_receipt_section([("analyze_portfolio", ledger)])


@pytest.mark.parametrize("scheduled", [False, True])
def test_next_agent_reads_execution_quantity_and_persists_receipt_table(valued_stack, monkeypatch, scheduled):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime.loop import AgentLoop
    from trade_compass_agent.runtime.session import SessionStore
    class Client:
        count = 0
        def stream_complete(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                if scheduled:
                    assert any("最终答复必须是一份完整报告" in (m.content or "") for m in messages)
                return ChatCompletion(content="持仓分析：继续核查来源与信号。\n" * 150 if scheduled else "", model="test", provider="test", tool_calls=[
                    ToolCall(id="portfolio", name="analyze_portfolio", arguments="{}")])
            result = json.loads(messages[-1].content)
            assert result["recent_trades"][0]["quantity"] == 1500
            return ChatCompletion(content="持仓分析：已核对历史流水，实际卖出 1500 股。当前持仓继续观察。", model="test", provider="test")
    model = Client()
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda config: model)
    monkeypatch.setattr(AgentLoop, "_update_session_summary", lambda *a, **k: None)
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **k: None)
    store = SessionStore(valued_stack.config.data_dir / "agent_sessions")
    if scheduled:
        from trade_compass_agent.ops.agent_session import ScheduledAgentSession
        monkeypatch.setattr(AgentLoop, "from_config", lambda config, **kwargs:
                            AgentLoop(config=config, stack=valued_stack, session_store=store, **kwargs))
        text = ScheduledAgentSession(valued_stack.config, job_id="eod_review").run("核对今天的成交与持仓", timeout=10)
        assert "持仓分析" in text
        assert "继续核查来源与信号" not in text
    else:
        agent = AgentLoop(config=valued_stack.config, stack=valued_stack, session_store=store)
        text = agent.run_turn("核对今天的成交与持仓").summary
    assert "| 1500 | 1.283 | trade-2 |" in text
    assert text.count("trade-2") == 1
    assert "数据覆盖不足" not in text  # A factual receipt is not a new recommendation.
    saved = [json.loads(line) for p in (valued_stack.config.data_dir / "agent_sessions").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert any(r.get("role") == "assistant" and "| 1500 | 1.283 | trade-2 |" in (r.get("content") or "") for r in saved)
    if scheduled:
        assert any("继续核查来源与信号" in (r.get("content") or "") for r in saved)


@pytest.mark.parametrize("tool", ["analyze_portfolio", "batch_paper_trades", "place_paper_trade"])
def test_scheduled_consumers_use_final_report_and_keep_drafts_in_history(tmp_path, monkeypatch, client, tool):
    import asyncio
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession, run_agent_step
    from trade_compass_agent.ops.job_definition import StepContext
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.runtime.tools.builtin_operations import _build_morning_decision_context

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "vault")
    session = ScheduledAgentSession(config, job_id="morning_plan", run_date=NOW.date(), step_id="sector_flow")

    fill = {"status": "executed", "symbol": "515220", "side": "sell", "quantity": 1500,
            "price": 1.283, "account": "etf_rotation", "trade_id": "fill-1500",
            "timestamp": "2026-09-14T10:08:21"}
    payload = ({"recent_trades": [fill], "trades_as_of": "2026-09-14T10:35:00"} if tool == "analyze_portfolio"
               else {"results": [fill]} if tool == "batch_paper_trades" else fill)
    calls = [(tool, json.dumps(payload)), ("get_bars", json.dumps({"symbol": "515220", "bars": [{"close": 1.283}]}))]
    final = "成交数量已核对，实际为 1500 股；此前 1000 股结论已撤回。" + trade_receipt_section(calls)
    draft = "10:08 已卖出 1000 股。\n" + "下面按热点、行情和当前持仓分析。\n" * 200
    rows = [{"role": "user", "content": "核对今天交易"}, {"role": "assistant", "content": draft}]
    rows += [{"role": "tool", "name": name, "content": content} for name, content in calls]
    rows += [{"role": "assistant", "content": final}]
    path = config.data_dir / "agent_sessions" / f"{session.session_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    before = path.read_bytes()
    fake = SimpleNamespace(_tools=SimpleNamespace(schemas=[]),
                           run_turn=lambda *a, **k: SimpleNamespace(summary=final, interrupted=False))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.AgentLoop.from_config", lambda *a, **k: fake)
    output = asyncio.run(run_agent_step(StepContext(config=config, date=NOW.date()),
                                       "核对今天交易", "morning_plan", step_id="sector_flow"))
    result = output.data["analysis"]

    assert result == final and path.read_bytes() == before
    assert "10:08 已卖出 1000 股" not in result
    assert "实际为 1500 股" in result
    assert result.count("fill-1500") == 1 and "| 1500 | 1.283 |" in result
    assert "数据覆盖不足" not in result
    context = _build_morning_decision_context(candidates=[], l5_data={}, positions_data={},
                                            sector_data=output.data, ideas_data={}, risk_data={})
    excerpt = context["sector_context"]["analysis_excerpt"]
    assert "实际为 1500 股" in excerpt and "10:08 已卖出 1000 股" not in excerpt

    runs = SqliteRunStore(config.data_dir / "scheduler.db")
    run = runs.create_run("morning_plan")
    step = runs.create_step_run(run.id, "sector_flow")
    runs.complete_step(step, output=output.message, data_json=json.dumps(output.data))
    runs.complete_run(run, message="分析完成")
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    response = client.get(f"/api/jobs/runs/{run.id}")
    assert response.status_code == 200
    assert response.json()["analysis"] == final
    from trade_compass_agent.ops.delivery import DeliveryRouter
    assert DeliveryRouter(config)._build_rich_content(run) == final
