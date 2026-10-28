import json

import pytest

from trade_compass_agent.runtime.report_quality import report_issues


def bars(day="2026-10-21", retrieved="2026-10-21T10:43:59+00:00"):
    return ("get_bars", json.dumps({"symbol": "601928", "timeframe": "1d",
        "_meta": {"retrieved_at": retrieved, "timezone": "Asia/Shanghai"},
        "bars": [{"timestamp": "2026-10-20T15:00:00", "low": 8.89, "high": 9.20},
                 {"timestamp": day + "T15:00:00", "low": 8.90, "high": 9.11,
                  "open": 9.08, "close": 8.92}]}))


@pytest.mark.parametrize("text", ["股权登记日2026-10-22（今日）", "除息日2026-10-23（明日）",
                                 "明日（10-23）除息", "今日（2026-10-22）登记完成"])
def test_relative_event_dates_use_report_day_not_generation_clock(text):
    assert report_issues(text, [], reference_date="2026-10-21")


@pytest.mark.parametrize("text", ["股权登记日2026-10-22（明日）", "明日（10-22）关注走势",
                                 "除息日2026-10-23，尚未到登记日", "此前记录曾写“今日（2026-10-22）”，该称呼有误。"])
def test_dated_future_events_and_explicit_old_quotes_are_not_current_date_claims(text):
    assert not report_issues(text, [], reference_date="2026-10-21")


def test_relative_dates_cross_the_year_boundary():
    assert not report_issues("明日（01-01）", [], reference_date="2026-12-31")
    assert not report_issues("昨日（12-31）", [], reference_date="2027-01-01")


@pytest.mark.parametrize("claim", ["高股息精选概念今日跌0.74%", "今日高股息精选概念下跌0.74%"])
def test_old_news_movement_cannot_be_relabelled_as_current_market_data(claim):
    news = ("search_stock_news", json.dumps({"news": [{"title": "高股息精选概念下跌0.74%，6股主力资金净流出超亿元",
                                                     "time": "2026-10-20 16:36:00"}]}))
    assert report_issues(claim, [news], reference_date="2026-10-21")
    assert not report_issues(claim, [news], reference_date="2026-10-20")


def test_news_history_quotes_and_matching_current_sources_remain_valid():
    rows = [{"title": "高股息精选概念下跌0.74%，6股主力资金净流出超亿元", "time": "2026-10-20 16:36:00"}]
    news = ("search_stock_news", json.dumps({"news": rows}))
    assert not report_issues("今日复核旧闻：10月20日高股息精选概念跌0.74%。", [news], reference_date="2026-10-21")
    assert not report_issues("2026-10-20发布的新闻显示高股息精选概念当日跌0.74%，并非2026-10-21实时数据。",
                             [news], reference_date="2026-10-21")
    rows.append({**rows[0], "time": "2026-10-21 16:36:00"})
    assert not report_issues("高股息精选概念今日跌0.74%", [("search_stock_news", json.dumps({"news": rows}))],
                             reference_date="2026-10-21")


@pytest.mark.parametrize("text", [
    "601928 支撑8.89（今日低）", "601928 今日最低价：8.89元",
    "601928 今日最高价9.20元", "若跌破8.89（今日低）则复核",
])
def test_today_price_must_match_the_current_dated_bar(text):
    assert report_issues(text, [bars()])


@pytest.mark.parametrize("text", [
    "601928 今日最低价8.90元，今日最高价9.11元",
    "601928 支撑8.89（昨日低），今日低点8.90",
    "601928 若跌破8.89则复核；计划止损8.61元",
    "601928 2026-10-20低点8.89元",
    "601928 原文误写「今日低点8.89」，更正：今日低点8.90",
])
def test_history_plans_and_correct_current_facts_remain_valid(text):
    assert not report_issues(text, [bars()])


def test_previous_trading_day_is_not_today_even_when_retrieved_today():
    assert report_issues("601928 今日低点8.90", [bars(day="2026-10-20")])


def test_dates_use_the_evidence_timezone_including_near_midnight():
    assert not report_issues("601928 今日低点8.90", [
        bars(retrieved="2026-10-20T16:30:00+00:00")])


def test_cross_midnight_archive_uses_trusted_job_date_not_fetch_date():
    evidence = [bars(retrieved="2026-10-21T16:30:00+00:00")]
    assert not report_issues("601928 当日低点8.90", evidence, reference_date="2026-10-21")
    assert report_issues("601928 当日低点8.89", evidence, reference_date="2026-10-21")
    assert report_issues("601928 今日低点8.90", evidence, reference_date="2026-10-22")
    assert report_issues("601928 今日低点8.90", evidence)


def test_intra_day_bars_do_not_claim_to_be_full_day_extremes():
    name, raw = bars()
    value = json.loads(raw)
    value["timeframe"] = "30m"
    assert not report_issues("601928 今日低点8.89", [(name, json.dumps(value))])


@pytest.mark.parametrize("text", [
    "601928最低价8.90略低于MA60（8.919），收盘8.92微幅高于MA60。",
    "情景2：明日收盘跌破MA60（收<8.90）但守MA20。",
    "情景3：跌破MA20（<8.60）则复核。",
    "连续2日跌破MA20（8.606）→止损评估。",
    "|收盘跌破MA20 8.61|转谨慎，评估止损|",
    "今日收盘已高于MA20，明日收盘价跌破MA20则复核。",
    "原始成交价为9.21；买入时已高于MA5/MA10。",
    "601928止损触发：收盘价跌破MA20（约8.58）或盘中放量破位。",
])
def test_intraday_price_comparisons_and_future_scenarios_are_not_current_close_claims(text):
    evidence = [bars(), ("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "last_price": 8.92,
                   "price_is_fresh": True}]})),
                ("compute_ma", json.dumps({"symbol": "601928", "ma": {"MA20": 8.606, "MA60": 8.919}}))]
    assert not report_issues(text, evidence)


def test_current_close_comparison_still_rejects_a_false_breakdown():
    evidence = [bars(), ("compute_ma", json.dumps({"symbol": "601928", "ma": {"MA60": 8.919}}))]
    assert report_issues("601928现价8.92明显低于MA60。", evidence)


def test_other_instruments_history_does_not_use_current_positions_price():
    evidence = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "last_price": 8.92,
                   "price_is_fresh": True}]})),
                ("compute_ma", json.dumps({"symbol": "601928", "ma": {"MA60": 8.919}}))]
    assert not report_issues("601928 当前持仓。\n| 515180 | 9/29清仓 | 跌破MA60 |", evidence)
    assert report_issues("601928 当前持仓。\n601928 当前跌破MA60。", evidence)
    assert report_issues("601928当前跌破MA60，现金261894.26元，采样时间03:01:02.654321。", evidence)


@pytest.mark.parametrize("text", [
    "| MA20 安全边际 | 8.606 | 距离止损位8.50仍有约1.8%空间 |",
    "601928止损位8.50距当前价仍有3.4%空间。",
    "601928 MA20为8.606，距离现价+2.7%。",
])
def test_price_distance_percentages_must_follow_the_actual_prices(text):
    evidence = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "last_price": 8.92,
                   "price_is_fresh": True}]}))]
    assert any("距离不支持" in issue for issue in report_issues(text, evidence))


@pytest.mark.parametrize("text", [
    "| MA20 安全边际 | 8.606 | 距离止损位8.50约1.2%空间 |",
    "601928止损位8.50距当前价约4.7%空间。",
    "601928 MA20为8.606，距离现价约3.65%。",
    "601928 MA60为8.919，距离现价-0.01%。",
    "601928市场成交占比1.8%，不是价格距离。",
    "601928止损8.50距当前价有空间，持仓占比1.8%。",
])
def test_correct_or_unrelated_percentages_are_not_price_distance_errors(text):
    evidence = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "last_price": 8.92,
                   "price_is_fresh": True}]}))]
    assert not report_issues(text, evidence)


def test_write_memory_is_checked_after_reads_in_the_same_batch():
    from trade_compass_agent.llm.providers import ToolCall
    from trade_compass_agent.runtime.loop import _execute_tool_calls

    executed = []

    class Tools:
        def execute(self, name, arguments):
            executed.append(name)
            return bars()[1] if name == "get_bars" else '{"path":"saved.md"}'

    result = _execute_tool_calls([
        ToolCall("read", "get_bars", '{"symbol":"601928"}'),
        ToolCall("write", "write_memory", json.dumps({"scope": "trade-log",
                 "content": "601928 支撑8.89（今日低）"})),
    ], Tools(), None)
    assert executed == ["get_bars"]
    assert json.loads(result["write"])["fact_check_failed"] is True


def test_rejected_memory_can_be_corrected_without_replaying_reads():
    from trade_compass_agent.llm.providers import ToolCall
    from trade_compass_agent.runtime.loop import _execute_tool_calls
    from trade_compass_agent.runtime.report_quality import assess_data_quality

    executed = []

    class Tools:
        def execute(self, name, arguments):
            executed.append(name)
            return json.dumps({"scope": "trade-log", "path": "saved.md"})

    evidence = [bars()]
    wrong = _execute_tool_calls([ToolCall("bad", "write_memory", json.dumps({
        "scope": "trade-log", "content": "601928 今日低点8.89"}))], Tools(), None, evidence)
    evidence.append(("write_memory", wrong["bad"]))
    quality = assess_data_quality(evidence)
    assert quality.degraded and "未保存" in " ".join(quality.warnings)
    correct = _execute_tool_calls([ToolCall("good", "write_memory", json.dumps({
        "scope": "trade-log", "content": "601928 今日低点8.90，昨日低点8.89"}))], Tools(), None, evidence)
    evidence.append(("write_memory", correct["good"]))
    assert executed == ["write_memory"]
    assert not assess_data_quality(evidence).degraded


@pytest.mark.parametrize("archive_date", [None, "2026-10-21"])
def test_agent_consumer_saves_only_the_corrected_memory(tmp_path, monkeypatch, archive_date):
    from types import SimpleNamespace
    from trade_compass_agent.config import AppConfig, AgentConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    calls = []
    original = ToolRegistry.execute

    def stream(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ChatCompletion(content=None, tool_calls=[ToolCall("read", "get_bars", '{"symbol":"601928"}')])
        if len(calls) in {2, 3}:
            if len(calls) == 3:
                assert json.loads(messages[-1].content)["fact_check_failed"]
                assert not list((tmp_path / "memory" / "tree" / "trade-log").glob("*.md"))
            value = "8.89" if len(calls) == 2 else "8.90"
            return ChatCompletion(content=None, tool_calls=[ToolCall("memo" + value, "write_memory", json.dumps({
                "scope": "trade-log", "content": "601928 今日低点" + value}))])
        return ChatCompletion(content="601928 今日低点8.90，昨日低点8.89。本轮仅记录行情。")

    def execute(self, name, arguments):
        evidence = bars(retrieved="2026-10-21T16:30:00+00:00") if archive_date else bars()
        return evidence[1] if name == "get_bars" else original(self, name, arguments)

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
        llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config)
    agent.report_date = archive_date
    result = agent.run_turn("核对日记并记录", session_id="scheduler-journal-facts")
    chunks = list((config.memory_dir / "tree" / "trade-log").glob("*.md"))
    assert len(chunks) == 1 and "今日低点8.90" in chunks[0].read_text()
    assert "今日低点8.89" not in chunks[0].read_text()
    assert len(calls) == 4 and not result.degraded
