"""Observed report defects must be blocked without rejecting valid research."""
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.runtime.report_quality import report_issues


def receipt(name, **data):
    return name, json.dumps(data, ensure_ascii=False)


IDENTITIES = [receipt("sina_realtime_quote", quotes=[
    {"symbol": "300378", "name": "鼎捷数智", "price": 43.54},
    {"symbol": "002354", "name": "天娱数科", "price": 12.3},
])]


@pytest.mark.parametrize("text", [
    "### 候选 3：300378 天娱数科 — 条件建仓",
    "| 300378 天娱数科 | 观察 |",
    "候选：300378 天娱数科 / 002354 鼎捷数智",
])
def test_wrong_explicit_code_name_pairs_are_rejected(text):
    assert report_issues(text, IDENTITIES)


@pytest.mark.parametrize("text", [
    "### 候选 3：300378 鼎捷数智 — 条件建仓",
    "| 300378 鼎捷数智 | 观察 |",
    "300378 当前持仓100股，继续持有。",
    "300378 继续持有",
    "300378（100股 @43.54）",
    "get_bars(300378) → compute_ma(300378)",
    "候选：300378 鼎捷数智 / 002354 天娱数科",
])
def test_valid_names_actions_and_tool_arguments_are_not_false_positives(text):
    assert not report_issues(text, IDENTITIES)


@pytest.mark.parametrize("text", [
    "| 300378 实时RSI | 74.69 |", "300378 当前风险 | 待复核",
    "## 候选小结：300378 鼎捷数智技术面最佳",
])
def test_indicator_labels_and_name_followed_by_analysis_are_not_names(text):
    assert not report_issues(text, IDENTITIES)


FLOW = [receipt("get_fund_flow", industry_flow=[
    {"name": "金融行业", "pct": 1.02, "net_inflow_yi": 18.44},
    {"name": "商业百货", "pct": .54, "net_inflow_yi": 8.59},
])]


@pytest.mark.parametrize("text", [
    "行业资金净流入：电子信息162亿、机械行业107亿 [get_fund_flow]",
    "行业资金净流入：金融行业184.4亿 [get_fund_flow]",
    "## 行业资金净流入（亿）\n| 行业 | 金额 |\n| 金融行业 | 184.4 |",
])
def test_fund_flow_claims_must_match_the_current_receipt(text):
    assert report_issues(text, FLOW, reference_date="2026-10-26")


@pytest.mark.parametrize("text", [
    "行业资金净流入：金融行业18.44亿、商业百货8.59亿 [get_fund_flow]",
    "行业资金净流入：金融行业18亿 [get_fund_flow]",
    "行业资金净流入：金融行业184400万元 [get_fund_flow]",
    "历史参考（10/23）：电子信息162亿 [get_fund_flow]，非本轮数据。",
    "如果金融行业净流入超过30亿，再重新评估。",
    "电子信息未进入本次返回的前二名，不能据此推断资金流出。",
    "## 10/23 历史资金参考\n行业资金净流入：电子信息162亿 [get_fund_flow]",
])
def test_fund_flow_history_rounding_and_conditional_analysis_remain_allowed(text):
    assert not report_issues(text, FLOW, reference_date="2026-10-26")


def test_fund_flow_categories_and_partial_reads_do_not_overwrite_each_other():
    calls = FLOW + [receipt("get_fund_flow", concept_flow=[
        {"name": "金融行业", "net_inflow_yi": 25.5},
    ])]
    assert not report_issues("行业资金净流入：金融行业18.44亿 [get_fund_flow]", calls)
    assert not report_issues("概念资金净流入：金融行业25.5亿 [get_fund_flow]", calls)
    assert report_issues("行业资金净流入：金融行业25.5亿 [get_fund_flow]", calls)


@pytest.mark.parametrize("text", [
    "行业资金流：金融行业净流出18.44亿", "## 行业资金流\n| 行业 | 净流出（亿元） |\n| 金融行业 | 18.44 |",
])
def test_outflow_sign_is_interpreted_correctly(text):
    negative = [receipt("get_fund_flow", industry_flow=[{"name": "金融行业", "net_inflow_yi": -18.44}])]
    assert not report_issues(text, negative)
    assert report_issues(text, FLOW)


def test_latest_flow_refresh_replaces_only_its_category():
    calls = FLOW + [receipt("get_fund_flow", industry_flow=[{"name": "金融行业", "net_inflow_yi": 20}])]
    assert not report_issues("行业资金净流入：金融行业20亿", calls)
    assert report_issues("行业资金净流入：金融行业18.44亿", calls)


@pytest.mark.parametrize("text", [
    "股权激励与融资融券概念净流入均超170亿。",
    "资金面：概念板块获资金净流入73.66亿（光伏概念）。",
])
def test_aggregate_prose_is_not_misclassified_as_a_single_sector(text):
    calls = [receipt("get_fund_flow", concept_flow=[{"name": "光伏概念", "net_inflow_yi": 73.66}])]
    assert not report_issues(text, calls)


def test_mixed_sector_stock_prose_preserves_aliases_units_and_signs():
    calls = [receipt("get_fund_flow", industry_flow=[{"name": "金融行业", "net_inflow_yi": 10.29}],
        concept_flow=[{"name": "央企50", "net_inflow_yi": 16.92}, {"name": "固态电池", "net_inflow_yi": 14.85}],
        main_force_top=[{"name": "科创50ETF华夏", "net_inflow_yi": 25.72},
                        {"name": "中际旭创", "net_inflow_yi": 12.058}])]
    text = "主力资金流向防御板块（金融+10.29亿、央企50+16.92亿）和宽基ETF（科创50ETF华夏+25.72亿）。"
    assert not report_issues(text, calls)
    assert not report_issues("固态电池概念净流入14.85亿。金融行业行业净流入10.29亿。", calls)
    assert not report_issues("概念：央企50+16.92亿主导资金流入，科技股（中际旭创+12亿）资金流入。", calls)
    assert report_issues("概念：央企50+16.92亿主导资金流入，科技股（中际旭创-12亿）资金流出。", calls)
    text = "行业资金净流入：电子信息162亿。主力个股：中际旭创12亿。"
    issues = report_issues(text, calls)
    assert len(issues) == 1 and "电子信息" in issues[0]


def test_date_search_keeps_old_history_accessible_without_using_it_as_today(tmp_path):
    from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory

    old = json.loads(tool_write_memory(tmp_path, "market-notes", "# 晨间计划 2026-07-29\n计划 计划 晨间 计划：昨日候选"))
    new = json.loads(tool_write_memory(tmp_path, "market-notes", "# 2026-10-26 盘前交易计划\n今日候选待复核"))
    for _ in range(2):  # Includes reopening the persisted index.
        hits = json.loads(tool_search_memory(tmp_path, "晨间计划 2026-10-26"))["results"]
        assert any(hit["path"] == new["path"] for hit in hits)
        assert all("2026-07-29" not in hit["content"] for hit in hits)
        assert json.loads(tool_search_memory(tmp_path, "2026-07-29"))["results"][0]["path"] == old["path"]
        assert json.loads(tool_search_memory(tmp_path, "2026-10-26"))["results"][0]["path"] == new["path"]
    assert json.loads(tool_search_memory(tmp_path, "晨间计划 2026-10-27"))["results"] == []


def test_market_pulse_preserves_quote_dates_and_never_presents_zero_as_a_price(monkeypatch):
    from trade_compass_agent.runtime.tools import market, search
    from trade_compass_agent.domain import LimitUpSummary, MarketPulse

    def no_network(*args, **kwargs):
        raise AssertionError("Unit test must not access live quotes")
    monkeypatch.setattr("requests.get", no_network)
    monkeypatch.setattr(search, "tool_sina_realtime_quote", lambda **_: json.dumps({"quotes": [
        {"symbol": "sh000001", "name": "上证指数", "price": 3690.47, "prev_close": 3657.9,
         "date": "2026-10-23", "time": "15:00:00"},
        {"symbol": "sz399001", "name": "深证成指", "price": 0, "prev_close": 11673.83,
         "date": "2026-10-26", "time": "09:09:00"},
    ]}))
    pulse = MarketPulse(datetime(2026, 10, 26, 8, 50), "akshare", [], LimitUpSummary(0, 0, [], []))
    stack = SimpleNamespace(market_pulse_provider=SimpleNamespace(get_market_pulse=lambda: pulse))
    data = json.loads(market.tool_get_market_pulse(stack))
    first, second = data["indices"]
    assert first["date"] == "2026-10-23" and first["price"] == 3690.47
    assert first["change_pct"] == pytest.approx(.89, abs=.01)
    assert second["price"] is None and second["change_pct"] is None
    assert data["_meta"]["freshness"] != "realtime"
    assert data["_meta"]["data_as_of"] is None  # Mixed components have no common verified time.
    assert data["warnings"]


def test_fund_flow_does_not_invent_observation_dates(monkeypatch):
    from trade_compass_agent.data import fund_flow

    monkeypatch.setattr(fund_flow.FundFlowProvider, "get_sector_flow",
                        lambda *a, **k: [fund_flow.SectorFlow("金融行业", 1.02, 18.44)])
    data = fund_flow.tool_get_fund_flow("industry")
    assert data["industry_flow"][0]["net_inflow_yi"] == 18.44
    assert data["_meta"]["data_as_of"] is None
    assert data["_meta"]["retrieved_at"] and data["_meta"]["source"] == "sina"
    assert data["_meta"]["freshness"] == "unknown"


def test_batch_bar_summary_separates_last_bar_from_five_bar_range():
    from trade_compass_agent.domain import Bar
    from trade_compass_agent.runtime.tools.batch import tool_batch_get_bars

    bars = [Bar("003021", datetime(2026, 10, 22, 15), 60, 90, 59.52, 83.39, 100, source="tushare"),
            Bar("003021", datetime(2026, 10, 23, 15), 85, 91.73, 85, 91.73, 200, source="tushare")]
    stack = SimpleNamespace(provider=SimpleNamespace(name="tushare", get_bars=lambda *a, **k: bars))
    row = json.loads(tool_batch_get_bars(stack, symbols="003021", limit=2))["results"]["003021"]
    assert row["low"] == 59.52  # Compatibility: do not silently change the old field.
    assert row["last_bar"]["low"] == 85 and row["last_bar"]["high"] == 91.73
    assert row["range_bars"] == 2 and row["high_low_basis"] == "recent_5_bars"
    assert row["_meta"]["data_as_of"].startswith("2026-10-23")


def test_batch_metadata_does_not_borrow_another_workers_provider():
    from trade_compass_agent.domain import Bar
    from trade_compass_agent.runtime.tools.batch import tool_batch_get_bars

    bars = [Bar("003021", datetime(2026, 10, 23, 15), 85, 91.73, 85, 91.73, 200, source="sina")]
    provider = SimpleNamespace(name="chain", last_resolved_provider="tushare", get_bars=lambda *a, **k: bars)
    row = json.loads(tool_batch_get_bars(SimpleNamespace(provider=provider), symbols="003021"))["results"]["003021"]
    assert row["_meta"]["sources"] == ["sina"] and row["_meta"]["is_cached"] is None


def test_rejected_stale_flow_cannot_enter_memory_but_corrected_write_succeeds():
    from trade_compass_agent.llm.providers import ToolCall
    from trade_compass_agent.runtime.loop import _execute_tool_calls

    writes = []
    tools = SimpleNamespace(execute=lambda name, args: writes.append(args) or '{"status":"saved"}')
    wrong = ToolCall("bad", "write_memory", json.dumps({"scope": "trade-log",
                     "content": "行业资金净流入：电子信息162亿 [get_fund_flow]"}))
    result = _execute_tool_calls([wrong], tools, None, FLOW, "2026-10-26")
    assert json.loads(result["bad"])["fact_check_failed"] and not writes
    right = ToolCall("good", "write_memory", json.dumps({"scope": "trade-log",
                     "content": "行业资金净流入：金融行业18.44亿 [get_fund_flow]"}))
    assert json.loads(_execute_tool_calls([right], tools, None, FLOW, "2026-10-26")["good"])["status"] == "saved"
    assert len(writes) == 1


@pytest.mark.parametrize("recover", [True, False])
def test_flow_guard_checks_publication_without_replaying_tools(tmp_path, monkeypatch, recover):
    from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.exceptions import AgentTurnError
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    wrong = "行业资金净流入：电子信息162亿 [get_fund_flow]。当前没有新成交。"
    correct = "行业资金净流入：金融行业18.44亿 [get_fund_flow]。快照时点未知，当前没有新成交。"
    valid = ChatCompletion(content='{"valid":true,"issues":[]}')
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("flow", "get_fund_flow", '{"category":"industry"}')]),
               ChatCompletion(content=wrong), valid, ChatCompletion(content=correct if recover else wrong), valid]
    if not recover:
        replies.extend([ChatCompletion(content=wrong), valid])
    reads = []

    def execute(self, name, args):
        reads.append(name)
        return FLOW[0][1]

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, verify_report_grounding=True, report_date="2026-10-26")
    if recover:
        assert correct in agent.run_turn("复核当前行业资金", session_id="scheduler-flow-check").summary
    else:
        with pytest.raises(AgentTurnError, match="未能核实"):
            agent.run_turn("复核当前行业资金", session_id="scheduler-flow-check")
    assert reads == ["get_fund_flow"] and not replies
    assert wrong not in (config.data_dir / "agent_sessions/scheduler-flow-check.jsonl").read_text()


def test_autonomous_task_reads_full_exact_day_plan_and_keeps_failure_visible(tmp_path, monkeypatch):
    import asyncio
    from datetime import date
    from trade_compass_agent.ops.job_definition import StepContext, StepOutput
    from trade_compass_agent.runtime.tools import builtin_operations

    monkeypatch.setattr("trade_compass_agent.ops.autonomous_trading.skip_reason", lambda *_: None)
    prompts = []

    async def capture(ctx, prompt, *args, **kwargs):
        prompts.append(prompt)
        return StepOutput(message="", data={"analysis": "本轮待复核"})

    monkeypatch.setattr(builtin_operations, "run_agent_step", capture)
    ctx = StepContext(config=SimpleNamespace(data_dir=tmp_path), date=date(2026, 10, 26), run_id="test")

    def run():
        asyncio.run(builtin_operations.agent_autonomous_trading(ctx))
        return json.loads(prompts[-1].split("\n")[-1])

    assert run()["status"] == "missing"
    directory = tmp_path / "workflows/morning_plan"
    directory.mkdir(parents=True)
    old = {"workflow_id": "morning_plan", "as_of": "2026-10-23", "data": {"analysis": "旧日建议"}}
    (directory / "2026-10-23.jsonl").write_text(json.dumps(old))
    assert run()["status"] == "missing"
    plan = {"workflow_id": "morning_plan", "as_of": "2026-10-26", "run_id": "first",
            "data": {"analysis": "完整研究依据。" * 300 + "尾部风险：价格缺口未消除前不得执行。"}}
    artifact = directory / "2026-10-26.jsonl"
    artifact.write_text(json.dumps(plan))
    for _ in range(2):
        assert run()["analysis"] == plan["data"]["analysis"]
    degraded = {**plan, "run_id": "second", "degraded": True, "error": "部分取数失败",
                "primary_output_failed": False, "warnings": ["持仓价格未更新"]}
    artifact.write_text(json.dumps(plan) + "\n" + json.dumps(degraded))
    result = run()
    assert result["run_id"] == "second" and result["status"] == "degraded"
    assert result["analysis"] == plan["data"]["analysis"] and result["warnings"] == ["持仓价格未更新"]
    failed = {**degraded, "run_id": "third", "primary_output_failed": True}
    artifact.write_text(artifact.read_text() + "\n" + json.dumps(failed))
    assert run()["status"] == "failed" and run()["analysis"] is None
    artifact.write_text(artifact.read_text() + '\n{"interrupted":')
    before = artifact.read_bytes()
    assert run()["status"] == "unavailable"
    assert artifact.read_bytes() == before
