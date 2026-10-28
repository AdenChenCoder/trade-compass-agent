"""Reports distinguish historical dates, instrument names and net settlements."""
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.runtime.report_quality import report_issues, review_report_evidence


def evidence():
    return [
        ("sina_realtime_quote", json.dumps({"quotes": [{"symbol": "003021", "name": "兆威机电",
             "date": "2026-10-26", "price": 96.26, "high": 98.01, "low": 91.12}]})),
        ("get_bars", json.dumps({"symbol": "003021", "timeframe": "1d",
             "_meta": {"retrieved_at": "2026-10-26T07:30:00+00:00", "timezone": "Asia/Shanghai"},
             "bars": [{"timestamp": "2026-10-23T15:00:00", "high": 91.73, "low": 85},
                      {"timestamp": "2026-10-26T15:00:00", "high": 98.01, "low": 91.12}]})),
    ]


@pytest.mark.parametrize("text", [
    "实际买入价88.44，非10/23盘中最高（当日最高91.73）[get_bars: 10/23 open 85.00 / high 91.73]",
    "10/23建仓，当日最高91.73；今日最高98.01。",
    "10月23日建仓，当日最高91.73；本日低点91.12。",
    "2026-10-23建仓，91.73（当日最高）；今日高点98.01。",
    "### 10/23 建仓复盘\n- 当日最高91.73\n### 今日行情\n- 当日最高98.01",
    "## 10/23 建仓复盘\n### 价格依据\n- 当日低点85.00\n## 当前状态\n- 今日低点91.12",
    "MA5/20维持多头排列，当日最高98.01。",
])
def test_verified_historical_extremes_and_current_extremes_can_coexist(text):
    assert not report_issues(text, evidence(), reference_date="2026-10-26")


@pytest.mark.parametrize("text", [
    "10/23建仓，当日最高98.01。", "10/23最高价98.01。",
    "10/23建仓；今日最高91.73。", "今日最高91.73；10/23最高91.73。",
    "### 10/23 建仓复盘\n- 当日最高98.01",
    "10/22建仓，当日最高91.73。",
])
def test_historical_errors_missing_dates_and_wrong_current_prices_are_still_rejected(text):
    assert report_issues(text, evidence(), reference_date="2026-10-26")


@pytest.mark.parametrize("text", [
    "get_bars(003021) → compute_ma(003021)",
    "get_fund_flow(003021) → recall_instrument(003021)",
    "### 模式：研究关注 003021（strength=0.75, 2天）",
    "003021（100 股 @88.44），继续观察。",
    "003021（兆威机电）继续观察", "兆威机电（003021）继续观察",
    "003021（兆威机电, strength=0.75）继续观察",
])
def test_tool_arguments_and_metadata_are_not_company_name_claims(text):
    assert not report_issues(text, evidence(), reference_date="2026-10-26")


@pytest.mark.parametrize("text", [
    "get_bars(003021)；003021（错误公司）继续观察。",
    "错误公司（003021）继续观察。",
    "003021（错误公司 100股）继续观察。",
    "003021（strength=0.75, 2天）；错误公司（003021）。",
    "003021（错误公司, strength=0.75）。", "003021（name=错误公司）。",
    "003021（strength=0.75, name=错误公司）。",
])
def test_metadata_or_tool_calls_do_not_hide_an_explicit_name_mismatch(text):
    assert report_issues(text, evidence(), reference_date="2026-10-26")


def test_interactive_report_uses_evidence_date_for_historical_headings():
    assert not report_issues("## 10/23 建仓复盘\n当日最高91.73。", evidence())
    assert report_issues("## 10/23 建仓复盘\n当日最高98.01。", evidence())


def settled_portfolio():
    return {"recent_closed_trades": [
        {"symbol": "601928", "account": "short_stock", "quantity": 300,
         "entry_trade_id": "buy-1", "exit_trade_id": "sell-1", "pnl": -150.06,
         "entry": 9.21, "exit": 8.75, "fees": 12.06, "closed_at": "2026-10-22T09:36:00"},
        {"symbol": "601928", "account": "short_stock", "quantity": 200,
         "entry_trade_id": "buy-1", "exit_trade_id": "sell-2", "pnl": -153.65,
         "entry": 9.21, "exit": 8.49, "fees": 9.65, "closed_at": "2026-10-22T10:36:00"},
    ]}


def test_financial_reviewer_receives_allocated_fees_and_net_settlement_scope():
    def review(system, user):
        payload = json.loads(user)
        facts = payload["verified_fifo_settlements"]
        assert facts["basis"] == "fifo_after_entry_and_exit_fees"
        assert facts["scope"] == "returned_allocations_only"
        assert sum(row["fees"] for row in facts["allocations"]) == pytest.approx(21.71)
        assert facts["covered_totals"][0]["pnl"] == -303.71
        assert facts["covered_totals"][0]["quantity"] == 500
        assert "价差乘数量" in system and "高位不等于最高" in system
        return '{"valid":true,"issues":[]}'

    calls = [("analyze_portfolio", json.dumps(settled_portfolio()))]
    assert review_report_evidence("601928两笔FIFO净结算合计亏损303.71元。", "复盘", calls, review) == []


def test_repeated_or_partial_settlements_never_become_full_account_totals():
    from trade_compass_agent.runtime.report_quality import report_settlement_facts

    payload = settled_portfolio()
    payload["recent_closed_trades"] = [payload["recent_closed_trades"][0]] * 2
    facts = report_settlement_facts([("analyze_portfolio", json.dumps(payload))])
    assert len(facts["allocations"]) == 1 and facts["scope"] == "returned_allocations_only"
    assert facts["covered_totals"][0]["quantity"] == 300
    assert facts["covered_totals"][0]["pnl"] == -150.06
    assert not report_settlement_facts([("analyze_portfolio", json.dumps(payload)), ("analyze_portfolio", '{"error":"timeout"}')])


def test_settlements_from_other_accounts_and_entries_are_not_merged():
    from trade_compass_agent.runtime.report_quality import report_settlement_facts

    payload = settled_portfolio()
    row = payload["recent_closed_trades"][0]
    payload["recent_closed_trades"].extend([{**row, "account": "mid_term"}, {**row, "entry_trade_id": "other-buy"}])
    facts = report_settlement_facts([("analyze_portfolio", json.dumps(payload))])
    assert len(facts["covered_totals"]) == 3


@pytest.mark.parametrize("recover", [True, False])
def test_historical_price_guard_is_preserved_through_report_publication(tmp_path, monkeypatch, recover):
    from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.exceptions import AgentTurnError
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    wrong = "003021在10/23建仓，当日最高98.01。"
    correct = "003021在10/23建仓，当日最高91.73；今日最高98.01。get_bars(003021)为核对来源。"
    valid = ChatCompletion(content='{"valid":true,"issues":[]}')
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("bars", "get_bars", '{"symbol":"003021"}')]),
               ChatCompletion(content=wrong), valid,
               ChatCompletion(content=correct if recover else wrong), valid]
    if not recover:
        replies.extend([ChatCompletion(content=wrong), valid])
    reads = []

    def execute(self, name, args):
        reads.append(name)
        return evidence()[1][1]

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, verify_report_grounding=True, report_date="2026-10-26")
    if recover:
        assert correct in agent.run_turn("核对历史和今日价格", session_id="scheduler-date-scope").summary
    else:
        with pytest.raises(AgentTurnError, match="未能核实"):
            agent.run_turn("核对历史和今日价格", session_id="scheduler-date-scope")
    assert reads == ["get_bars"] and not replies
    public = (config.data_dir / "agent_sessions/scheduler-date-scope.jsonl").read_text()
    assert wrong not in public


def test_report_correction_receives_the_same_settlements_without_replaying_tools(tmp_path, monkeypatch):
    from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    reads = []
    wrong = "601928可用现金合计0元；已结算净亏损303.71元。"
    correct = "601928已结算净亏损303.71元；全部账户可用现金合计100元。"
    calls = []

    def stream(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ChatCompletion(content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")])
        if len(calls) == 2:
            return ChatCompletion(content=wrong)
        payload = json.loads(messages[1].content)
        if "report_lines" in payload:
            assert payload["verified_fifo_settlements"]["covered_totals"][0]["pnl"] == -303.71
            if payload["report"] == wrong:
                return ChatCompletion(content='{"valid":false,"issues":[{"line":1,"reason":"可用现金合计应为100"}]}')
            return ChatCompletion(content='{"valid":true,"issues":[]}')
        assert kwargs.get("tools") is None
        assert payload["settlement_facts"]["covered_totals"][0]["pnl"] == -303.71
        return ChatCompletion(content=correct)

    def execute(self, name, args):
        reads.append(name)
        return json.dumps({**settled_portfolio(), "positions": [],
                           "account_balances": [{"id": "short_stock", "available_cash": 100}]})

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    result = loop.AgentLoop.from_config(config, verify_report_grounding=True).run_turn("核对净结算与现金", session_id="scheduler-settlement")
    assert correct in result.summary and wrong not in result.summary and reads == ["analyze_portfolio"]
