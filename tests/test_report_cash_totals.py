import json

from trade_compass_agent.runtime.report_quality import report_issues


def facts():
    return [
        (
            "analyze_portfolio",
            json.dumps(
                {
                    "positions": [],
                    "total_account_capital": 710000,
                    "account_balances": [
                        {"id": "short_stock", "available_cash": 9888.581, "capital": 10000},
                        {"id": "etf_rotation", "available_cash": 300000, "capital": 300000},
                        {"id": "mid_term", "available_cash": 400000, "capital": 400000},
                    ],
                }
            ),
        )
    ]


def test_initial_capital_cannot_replace_available_cash_total():
    wrong = "| 账户 | 本金 | 可用资金 |\n|---|---|---|\n| 合计 | 710,000.00 | 710,000.00 |\n当前可操作资金：710,000.00 元"
    assert report_issues(wrong, facts())
    correct = "| 账户 | 本金 | 可用资金 |\n|---|---|---|\n| 合计 | 710,000.00 | 709,888.58 |\n当前可操作资金：709,888.58 元"
    assert not report_issues(correct, facts())
    assert not report_issues(
        "short_stock 当前可操作资金：9,888.581 元；初始本金合计710000元。", facts()
    )


def test_unknown_balance_never_becomes_a_confirmed_zero_total():
    data = json.loads(facts()[0][1])
    data["account_balances"][0]["available_cash"] = None
    assert report_issues("当前可操作资金：710000元", [("analyze_portfolio", json.dumps(data))])
    assert report_issues("当前可操作资金：0元", [("analyze_portfolio", json.dumps(data))])
    assert not report_issues(
        "当前可操作资金待核实，账户本金710000元", [("analyze_portfolio", json.dumps(data))]
    )


def test_coarse_wan_currency_is_not_misread_as_individual_yuan():
    assert not report_issues("当前可操作资金：70.99万元", facts())
    assert not report_issues("|账户|可用现金（万元）|\n|合计|70.99|", facts())


def test_available_cash_can_equal_capital_when_no_profit_or_loss_exists():
    data = json.loads(facts()[0][1])
    for row in data["account_balances"]:
        row["available_cash"] = row["capital"]
    assert not report_issues("当前可操作资金：710000元", [("analyze_portfolio", json.dumps(data))])


def test_portfolio_tool_exposes_current_cash_separately_from_initial_capital(tmp_path):
    from datetime import datetime
    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.domain import AccountKind, PaperTrade
    from trade_compass_agent.portfolio import JsonPaperPortfolio
    from trade_compass_agent.portfolio.accounts import AccountStore
    from trade_compass_agent.runtime.market_stack import MarketStack
    from trade_compass_agent.runtime.tools.portfolio import tool_analyze_portfolio

    config = AppConfig(
        data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample"
    )
    AccountStore(config.data_dir / "accounts.json").update("short_stock", capital=10000)
    portfolio = JsonPaperPortfolio(
        config.data_dir / "paper_trades.jsonl", costs=config.trading_costs
    )
    for side, price, day in [("buy", 10, 16), ("sell", 9, 19)]:
        portfolio.record(
            PaperTrade(
                symbol="000001",
                account=AccountKind.SHORT_STOCK,
                side=side,
                quantity=100,
                price=price,
                timestamp=datetime(2026, 10, day, 10, 5),
                reason="test",
            )
        )
    data = json.loads(tool_analyze_portfolio(MarketStack.from_config(config)))
    assert data["total_available_cash"] == round(
        sum(row["available_cash"] for row in data["account_balances"]), 4
    )
    assert abs(data["total_account_capital"] - data["total_available_cash"] - 111.419) < 0.0001


def test_code_annotation_is_not_an_instrument_name():
    source = [
        (
            "sina_realtime_quote",
            json.dumps({"quotes": [{"symbol": "000001", "name": "Sample 000001", "price": 9}]}),
        )
    ]
    assert not report_issues("000001（代码 000001）持仓100股", source)
    assert report_issues("000001（代码 600825）持仓100股", source)


def test_decimal_money_and_timestamp_fraction_are_not_stock_symbols():
    from trade_compass_agent.runtime.loop import _check_data_gap

    text = "本轮未执行买入或卖出。total_account_capital=710000.0；capital=300000.0；capital=400000.0；取数12:19:35.512695。"
    assert _check_data_gap(text, facts()) is None
    assert _check_data_gap("建议买入000001.", []) is not None


def test_cash_total_repair_receives_account_facts_without_replaying_tools(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from trade_compass_agent.config import AppConfig, AgentConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    calls = []
    tools = []

    def stream(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ChatCompletion(
                content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")]
            )
        if len(calls) == 2:
            return ChatCompletion(
                content="本轮仅查询。当前可操作资金：710000.00 元，未进行任何交易。"
            )
        assert len(calls) == 3 and kwargs["tools"] is None
        assert (
            json.loads(messages[1].content)["portfolio_facts"]["total_available_cash"] == 709888.581
        )
        return ChatCompletion(
            content="本轮仅查询，未进行任何交易。初始本金710000元，当前可操作资金：709888.58 元。"
        )

    monkeypatch.setattr(
        loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream)
    )
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)

    def execute(self, name, args):
        tools.append(name)
        return facts()[0][1]

    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="disabled"),
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False),
    )
    result = loop.AgentLoop.from_config(config).run_turn(
        "只核对可用资金", session_id="scheduler-cash-total"
    )
    assert "709888.58" in result.summary and not report_issues(result.summary, facts())
    assert tools == ["analyze_portfolio"] and len(calls) == 3
