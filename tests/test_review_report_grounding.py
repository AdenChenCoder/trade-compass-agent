import json
from report_contract_helpers import report_facts

import pytest

from trade_compass_agent.runtime.exceptions import AgentTurnError
from trade_compass_agent.runtime.report_quality import report_issues, review_report_evidence
from trade_compass_agent.runtime.report_publication import publish_report


@pytest.mark.parametrize("recover", [True, False])
def test_report_is_verified_before_publication_and_correction_never_replays_tools(recover):
    receipts = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "avg_cost": 9.21}]}))]
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        cost = next(f for f in report_facts(payload) if f["metric"] == "avg_cost")
        ref = cost["id"] if recover and len(calls) > 1 else "invented-cost"
        return json.dumps({"schema_version": 1, "items": [
            {"id": "cost", "type": "fact", "section": "positions", "ref": ref}]})
    result = publish_report(task="核对持仓成本", draft="成本8.79", tool_results=receipts, run_scope="run", call=call)
    assert (result.verification["status"] == "complete") is recover
    assert ("9.21" in result.text) is recover
    assert "8.79" not in result.text and len(calls) <= 4
    assert result.artifact["receipts"][0]["result"] == receipts[0][1]


@pytest.mark.parametrize("raw", ['{"valid":true,"issues":[{}]}', 'not json', '{"valid":false,"issues":[]}',
                                 '{"valid":false,"issues":[{"quote":"invented","reason":"missing"}]}'])
def test_invalid_review_cannot_mark_a_report_verified(raw):
    with pytest.raises(AgentTurnError, match="审查未完成"):
        review_report_evidence("原报告", "任务", [], lambda *_: raw)


@pytest.mark.parametrize("recover", [True, False])
def test_model_agreement_cannot_override_wrong_price_distance(recover):
    receipts = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "last_price": 8.92, "price_is_fresh": True}]}))]
    attempts = []
    def call(system, user):
        payload = json.loads(user)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        attempts.append(payload)
        fact = next(f for f in report_facts(payload) if f["metric"] == "last_price")
        item = {"id": "stop", "type": "condition", "section": "plan", "ref": fact["id"],
                "threshold": "8.50", "unit": "元/股", "comparison": "lt", "action": "review"}
        if not recover or len(attempts) == 1:
            item["distance_pct"] = 3.4  # Undeclared arithmetic cannot pass through a plan.
        return json.dumps({"schema_version": 1, "items": [item]})
    result = publish_report(task="保留止损计划阈值", draft="价格距离3.4%", tool_results=receipts, run_scope="run", call=call)
    assert (result.verification["status"] == "complete") is recover
    assert "3.4%" not in result.text
    assert ("8.50" in result.text) is recover


def test_later_discovered_fact_error_gets_one_more_readonly_correction():
    receipts = [("analyze_portfolio", json.dumps({"positions": [{"symbol": "601928", "avg_cost": 9.21}]}))]
    attempts = []
    def call(system, user):
        payload = json.loads(user)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        attempts.append(payload)
        cost = next(f for f in report_facts(payload) if f["metric"] == "avg_cost")
        items = [{"id": "cost", "type": "fact", "section": "positions",
                  "ref": cost["id"] if len(attempts) > 1 else "invented-cost"}]
        if len(attempts) < 3:
            items.append({"id": "news", "type": "fact", "section": "market", "ref": "unfetched-news"})
        else:
            items.append({"id": "news", "type": "gap", "section": "gaps", "text": "未取得对应日期的新闻原文"})
        return json.dumps({"schema_version": 1, "items": items})
    result = publish_report(task="核对持仓和当日新闻", draft="旧新闻当作今日跌幅", tool_results=receipts, run_scope="run", call=call)
    assert len(attempts) == 3
    assert "9.21" in result.text and "未取得对应日期" in result.text
    assert result.verification["status"] == "incomplete"


def test_correction_rechecks_whole_source_claim_and_remembers_earlier_errors():
    receipts = [("get_fundamentals", '{"symbol":"003021","pe_ttm":119.59}'),
                ("fetch_url", '{"url":"https://example.org/news","content":"历史新闻记载PE96.44"}')]
    attempts = []
    def call(system, user):
        payload = json.loads(user)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        attempts.append(payload)
        refs = {f["metric"]: f["id"] for f in report_facts(payload)}
        items = [{"id": "current", "type": "fact", "section": "market", "ref": refs["pe_ttm"]},
                 {"id": "historical", "type": "fact", "section": "market", "ref": "missing" if len(attempts) == 1 else refs["content"]}]
        if len(attempts) == 2:
            items[0]["ref"] = refs["content"]  # A repair may not replace an already valid observation.
        return json.dumps({"schema_version": 1, "items": items})
    result = publish_report(task="比较来源", draft="新闻PE119.59", tool_results=receipts, run_scope="run", call=call)
    assert len(attempts) == 2
    assert result.verification["status"] == "complete"
    assert result.artifact["document"]["items"][0]["ref"] != result.artifact["document"]["items"][1]["ref"]
    assert "119.59" in result.text and "历史新闻记载PE96.44" in result.text
    assert "新闻PE119.59" not in result.text


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
