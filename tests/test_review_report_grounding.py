import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.exceptions import AgentTurnError
from trade_compass_agent.runtime.report_quality import report_issues, review_report_evidence
from trade_compass_agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("recover", [True, False])
def test_report_is_verified_before_publication_and_correction_never_replays_tools(tmp_path, monkeypatch, recover):
    wrong = "601928 持仓成本8.79，明日分红0.1元。"
    correct = "601928 持仓成本9.21。公告仅有标题，分红金额和日期待核实。"
    review = json.dumps({"valid": False, "issues": [{"quote": "持仓成本8.79", "reason": "持仓avg_cost为9.21，筹码均价不能作为持仓成本"}]})
    completions = [ChatCompletion(content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")]),
        ChatCompletion(content=wrong), ChatCompletion(content=review),
        ChatCompletion(content=correct if recover else wrong),
        ChatCompletion(content=json.dumps({"valid": True, "issues": []}) if recover else review)]
    if not recover:
        completions.extend([ChatCompletion(content=wrong), ChatCompletion(content=review)])
    requests, tools = [], []

    def stream(messages, **kwargs):
        requests.append((messages, kwargs))
        return completions.pop(0)

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)

    def execute(self, name, args):
        tools.append(name)
        return json.dumps({"positions": [{"symbol": "601928", "avg_cost": 9.21, "price_is_fresh": True,
                                          "last_price": 8.92}], "recent_trades": []})

    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, verify_report_grounding=True)
    if recover:
        result = agent.run_turn("核对持仓并写收盘报告", session_id="scheduler-grounding")
        assert correct in result.summary and wrong not in result.summary
    else:
        with pytest.raises(AgentTurnError, match="未能核实"):
            agent.run_turn("核对持仓并写收盘报告", session_id="scheduler-grounding")
    assert tools == ["analyze_portfolio"] and not completions
    assert all(request[1].get("tools") is None for request in requests[-3:])
    public = (config.data_dir / "agent_sessions/scheduler-grounding.jsonl").read_text()
    assert wrong not in public


@pytest.mark.parametrize("raw", ['{"valid":true,"issues":[{}]}', 'not json', '{"valid":false,"issues":[]}',
                                 '{"valid":false,"issues":[{"quote":"invented","reason":"missing"}]}'])
def test_invalid_review_cannot_mark_a_report_verified(raw):
    with pytest.raises(AgentTurnError, match="审查未完成"):
        review_report_evidence("原报告", "任务", [], lambda *_: raw)


@pytest.mark.parametrize("recover", [True, False])
def test_model_agreement_cannot_override_wrong_price_distance(tmp_path, monkeypatch, recover):
    wrong = "601928止损位8.50距当前价仍有3.4%空间。"
    correct = "601928当前价8.92，止损计划阈值8.50；本轮未成交。"
    verdict = ChatCompletion(content='{"valid":true,"issues":[]}')
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")]),
               ChatCompletion(content=wrong), verdict,
               ChatCompletion(content=correct if recover else wrong), verdict]
    if not recover:
        replies.extend([ChatCompletion(content=wrong), verdict])
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", lambda *a: json.dumps({"positions": [{"symbol": "601928",
                         "last_price": 8.92, "price_is_fresh": True}]}))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, verify_report_grounding=True)
    if recover:
        assert correct in agent.run_turn("核对价格距离", session_id="distance-report").summary
    else:
        with pytest.raises(AgentTurnError, match="未能核实"):
            agent.run_turn("核对价格距离", session_id="distance-report")
    assert wrong not in (config.data_dir / "agent_sessions/distance-report.jsonl").read_text()


def test_later_discovered_fact_error_gets_one_more_readonly_correction(tmp_path, monkeypatch):
    first = "601928成本8.79；高股息精选概念今日跌0.74%。"
    second = "601928成本9.21；高股息精选概念今日跌0.74%。"
    final = "601928成本9.21；10月20日旧新闻中的概念跌幅不能当成今日行情。"
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("read", "analyze_portfolio", "{}")]),
               ChatCompletion(content=first),
               ChatCompletion(content='{"valid":false,"issues":[{"line":1,"reason":"成本应为9.21"}]}'),
               ChatCompletion(content=second),
               ChatCompletion(content='{"valid":false,"issues":[{"line":1,"reason":"新闻日期是10月20日"}]}'),
               ChatCompletion(content=final), ChatCompletion(content='{"valid":true,"issues":[]}')]
    tool_reads = []
    def execute(*args):
        tool_reads.append(args[1])
        return json.dumps({"positions": [{"symbol": "601928", "avg_cost": 9.21}]})
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    result = loop.AgentLoop.from_config(config, verify_report_grounding=True).run_turn("核对持仓与新闻", session_id="scheduler-two-facts")
    assert result.summary.startswith(final) and tool_reads == ["analyze_portfolio"] and not replies
    saved = (config.data_dir / "agent_sessions/scheduler-two-facts.jsonl").read_text()
    assert first not in saved and second not in saved


def test_correction_rechecks_whole_source_claim_and_remembers_earlier_errors(tmp_path, monkeypatch):
    first = "新闻明确PE119.59，成交量2699万手。"
    second = "新闻明确PE119.59，成交量2698.65万股。"
    final = "新闻记载PE96.44；本轮基本面工具返回PE119.59。日线成交量26986525股。"
    replies = [
        ChatCompletion(content=None, tool_calls=[ToolCall("read", "get_fundamentals", '{"symbol":"003021"}')]),
        ChatCompletion(content=first),
        ChatCompletion(content='{"valid":false,"issues":[{"line":1,"reason":"成交量单位错误，回执为26986525股"}]}'),
        ChatCompletion(content=second),
        ChatCompletion(content='{"valid":false,"issues":[{"line":1,"reason":"新闻PE为96.44，119.59来自基本面工具，来源归属错误"}]}'),
        ChatCompletion(content=final), ChatCompletion(content='{"valid":true,"issues":[]}'),
    ]
    correction_payloads, tools = [], []

    def stream(messages, **kwargs):
        if messages[0].content.startswith("只校正报告"):
            assert kwargs.get("tools") is None
            correction_payloads.append(json.loads(messages[-1].content))
        return replies.pop(0)

    def execute(*args):
        tools.append(args[1])
        return '{"symbol":"003021","pe":119.59}'

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    result = loop.AgentLoop.from_config(config, verify_report_grounding=True).run_turn(
        "新闻PE96.44；日线成交量26986525股。核对基本面后写报告。", session_id="source-correction")
    assert result.summary.startswith(final) and tools == ["get_fundamentals"] and not replies
    assert len(correction_payloads) == 2
    last = correction_payloads[-1]
    assert any("成交量单位错误" in issue for issue in last["previous_issues"])
    assert any("来源归属错误" in issue for issue in last["issues"])
    public = (config.data_dir / "agent_sessions/source-correction.jsonl").read_text()
    assert final in public and first not in public and second not in public


def test_different_dated_source_values_can_be_reported_with_explicit_attribution():
    report = "历史新闻记载PE96.44；本轮基本面工具返回PE119.59。"
    assert review_report_evidence(report, "旧新闻PE96.44", [("get_fundamentals", '{"pe":119.59}')],
        lambda *_: '{"valid":true,"issues":[]}') == []


@pytest.mark.parametrize("text", [
    "10/26日K线：低开93.0（前收91.73）、高98.01、低91.12、收96.26。",
    "高开9.30（昨收9.50）", "平开9.30（前收9.50）",
    "低开9.30（前收9.30）",
])
def test_explicit_opening_direction_must_match_the_same_sentence_prices(text):
    assert any("开盘" in issue for issue in report_issues(text, []))


@pytest.mark.parametrize("text", [
    "10/26日K线：高开93.0（前收91.73）、高98.01、低91.12、收96.26。",
    "低开9.30（昨收9.50）", "平开9.30（前收9.30）",
    "低开，93.0是盘中价，前收91.73。", "低开0（前收91.73）",
    "如果低开93.0（前收91.73），先核对实际行情。",
])
def test_valid_opening_direction_or_unrelated_prices_are_preserved(text):
    assert not report_issues(text, [])


def test_format_repair_rechecks_the_same_facts_without_forcing_a_success():
    calls = []

    def judge(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if len(calls) == 1:
            return '{"valid":false,"issues":[{"quote":"缩写…","reason":"缺少依据"}]}'
        return '{"valid":false,"issues":[{"quote":"原报告","reason":"缺少日期依据"}]}'

    assert review_report_evidence("原报告", "原任务", [("get_bars", "原回执")], judge) == ["原报告：缺少日期依据"]
    assert len(calls) == 2 and calls[1]["report"] == calls[0]["report"]
    assert calls[1]["task"] == "原任务" and calls[1]["tool_results"] == [["get_bars", "原回执"]]
    assert "validation_error" in calls[1]


def test_report_lines_are_owned_by_the_reviewer_input_not_rewritten_by_the_model():
    result = review_report_evidence("首行\n\n持仓成本8.79", "任务", [],
        lambda *_: '{"valid":false,"issues":[{"line":3,"reason":"持仓成本应为9.21"}]}')
    assert result == ["持仓成本8.79：持仓成本应为9.21"]


def test_context_token_counts_cannot_be_published_as_character_counts():
    task = "上下文预算不足：估算输入 101031、输出预留 32768、已加载窗口 131072。"
    issues = review_report_evidence("输入101,031字符，窗口131072字符。", task, [],
                                    lambda *_: '{"valid":true,"issues":[]}')
    assert len(issues) == 1 and "单位是token" in issues[0]
    assert review_report_evidence("输入101031 token加预留32768超过131072 token窗口。", task, [],
                                  lambda *_: '{"valid":true,"issues":[]}') == []


def test_known_unit_issue_does_not_skip_other_semantic_errors_before_correction():
    task = "估算输入 101031、输出预留 32768、已加载窗口 131072。"
    issues = review_report_evidence("输入101031字符。\n并发冲突已确诊。", task, [], lambda *_:
        '{"valid":false,"issues":[{"line":2,"reason":"缺少并发诊断证据"}]}')
    assert len(issues) == 2 and any("缺少并发诊断证据" in issue for issue in issues)


def test_character_counts_unrelated_to_context_budget_are_not_rejected():
    task = "估算输入 101031、输出预留 32768、已加载窗口 131072。"
    assert review_report_evidence("日记限400字符，字符数不含标点。", task, [],
                                  lambda *_: '{"valid":true,"issues":[]}') == []


def test_model_budgets_are_not_reported_as_instruments_without_market_data():
    from trade_compass_agent.runtime.loop import _check_data_gap

    text = "601928计划卖出需核对行情。估算输入token分别为101031/112024/154226，已加载窗口131072 token。"
    warning = _check_data_gap(text, [])
    assert "601928" in warning
    assert not any(value in warning for value in ["101031", "112024", "154226", "131072"])
    assert "131072" not in _check_data_gap("601928计划卖出；上下文不足，窗口131072。", [])


@pytest.mark.parametrize("line", [0, 2, 4, True, "3"])
def test_review_cannot_reference_an_empty_missing_or_non_numeric_report_line(line):
    with pytest.raises(AgentTurnError, match="审查未完成"):
        review_report_evidence("首行\n\n末行", "任务", [], lambda *_: json.dumps({
            "valid": False, "issues": [{"line": line, "reason": "缺少依据"}]}))


def test_market_period_schema_lists_only_supported_periods():
    from trade_compass_agent.data.providers import ALL_TIMEFRAMES
    from trade_compass_agent.runtime.tools.registry import BASE_TOOL_SCHEMAS

    for schema in BASE_TOOL_SCHEMAS:
        timeframe = schema["function"]["parameters"].get("properties", {}).get("timeframe")
        if timeframe:
            assert set(timeframe["enum"]) == ALL_TIMEFRAMES and "1w" not in timeframe["enum"]
