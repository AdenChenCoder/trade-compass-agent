"""Report labels must not turn verified identities into factual errors."""
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime import loop
from trade_compass_agent.runtime.report_quality import report_issues
from trade_compass_agent.runtime.tools.registry import ToolRegistry


def receipts():
    return [
        ("analyze_portfolio", json.dumps({"positions": [
            {"symbol": "300406", "name": "九强生物"},
            {"symbol": "002415", "name": "海康威视"},
        ]}, ensure_ascii=False)),
        ("get_fund_flow", json.dumps({
            "_meta": {"data_as_of": None, "freshness": "unknown"},
            "note": "行情时点未知，不能作为今日或实时资金流。",
            "industry_flow": [{"name": "电子信息", "net_inflow_yi": 173.3}],
            "concept_flow": [{"name": "大飞机", "net_inflow_yi": 28.57}],
        }, ensure_ascii=False)),
    ]


@pytest.mark.parametrize("text", [
    "#### 2. 300406 九强生物持仓表现",
    "### 持仓：300406 九强生物详细复核",
    "### 持仓：002415 海康威视技术分析",
    "#### 300406 九强生物 — 持仓表现",
    "| 标的 |\n| 300406 九强生物 |",
])
def test_heading_description_is_not_part_of_verified_company_name(text):
    assert report_issues(text, receipts(), reference_date="2026-11-02") == []


@pytest.mark.parametrize("text", [
    "#### 持仓：300406 错误公司持仓表现",
    "#### 持仓：300406 海康威视持仓表现",
    "#### 持仓：300406 九强生物科技",
    "300406（九强生物科技）",
    "| 标的 |\n| 300406 海康威视 |",
])
def test_heading_suffix_cannot_hide_an_actual_identity_mismatch(text):
    assert any("名称应为九强生物" in issue for issue in report_issues(text, receipts()))


@pytest.mark.parametrize("prefix", ["N", "C", "*ST"])
def test_descriptive_heading_keeps_exchange_name_prefix(prefix):
    tools = [("analyze_portfolio", json.dumps({"positions": [
        {"symbol": "300406", "name": prefix + "九强"}]}))]
    assert not report_issues(f"### 持仓：300406 {prefix}九强持仓表现", tools)
    assert report_issues("### 持仓：300406 九强持仓表现", tools)


@pytest.mark.parametrize("text", [
    "行业资金流向：电子信息板块净流入+173.3亿元。",
    "行业资金流向：电子信息行业板块净流入+173.3亿元。",
    "概念资金流向：大飞机概念以28.57亿居前。",
    "概念资金流向：大飞机概念板块净流入28.57亿。",
    "## 行业资金\n| 板块 | 净流入(亿元) |\n| 电子信息板块 | +173.3 |",
])
def test_flow_classification_suffix_keeps_verified_amount(text):
    assert report_issues(text, receipts()) == []


@pytest.mark.parametrize("text", [
    "行业资金流向：电子信息板块净流入+999.0亿元。",
    "行业资金流向：电子信息板块净流出173.3亿元。",
    "概念资金流向：大飞机概念以28.00亿居前。",
    "行业资金流向：未知板块净流入173.3亿元。",
])
def test_flow_alias_still_checks_amount_sign_and_missing_sector(text):
    assert report_issues(text, receipts())


def test_same_label_in_different_categories_uses_declared_scope():
    tools = [("get_fund_flow", json.dumps({
        "industry_flow": [{"name": "电子信息", "net_inflow_yi": 173.3}],
        "concept_flow": [{"name": "电子信息", "net_inflow_yi": 28.57}],
    }))]
    assert not report_issues("行业资金：电子信息板块净流入173.3亿。", tools)
    assert not report_issues("概念资金：电子信息板块净流入28.57亿。", tools)
    assert report_issues("概念资金：电子信息板块净流入173.3亿。", tools)


@pytest.mark.parametrize("claim,wrong", [
    ("# 收盘分析 2026-11-02（周日）", True),
    ("# 收盘分析 2026-11-02（周一）", False),
    ("明日（11/03 周一）关注点", True),
    ("明日（11/03 周二）关注点", False),
    ("下周一（11/03）再核对", True),
    ("历史记录曾写2026-11-02（周日），此处仅引用原文。", False),
    ("2026-10-30（星期五）历史收盘价12.46", False),
    ("如果2026-11-03（周一）放量，则复核。", True),
])
def test_calendar_weekday_is_checked_before_model_correction(claim, wrong):
    assert bool(report_issues(claim, [], reference_date="2026-11-02")) is wrong


def test_relative_weekday_checks_preserve_year_boundary():
    assert not report_issues("明日（01/01 周五）关注点", [], reference_date="2026-12-31")
    assert not report_issues("昨日（12/31 周四）复盘", [], reference_date="2027-01-01")
    assert report_issues("明日（01/01 周四）关注点", [], reference_date="2026-12-31")


@pytest.mark.parametrize("valid", [True, False])
def test_scheduled_report_publication_preserves_identity_and_raw_evidence(tmp_path, monkeypatch, valid):
    report = ("#### 2. 300406 九强生物持仓表现\n"
              "行业资金流向：电子信息板块净流入+173.3亿元。\n"
              "该资金快照时点未知，不作为今日或实时资金流。")
    if not valid:
        report = report.replace("九强生物", "错误公司")
    tools = dict(receipts())
    reads, requests = [], []
    responses = [ChatCompletion(content=None, tool_calls=[
        ToolCall("portfolio", "analyze_portfolio", "{}"),
        ToolCall("flow", "get_fund_flow", "{}"),
    ]), ChatCompletion(content=report)]

    def stream(messages, **kwargs):
        requests.append(kwargs)
        from report_contract_helpers import report_reply
        reply = report_reply(messages)
        if reply is not None:
            return reply
        return responses.pop(0)

    def execute(self, name, args):
        reads.append(name)
        return tools[name]

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"),
                       agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, verify_report_grounding=True, report_date="2026-11-02")
    session_id = "scheduler-report-identity"
    result = agent.run_turn("核对持仓与资金快照并写报告", session_id=session_id)
    assert "九强生物" in result.summary and "错误公司" not in result.summary
    assert "173.3亿元" in result.summary and "时点未提供" in result.summary
    assert result.verification["status"] == "complete" and len(requests) == 4
    artifact = json.loads((config.data_dir / "report_artifacts" /
                           (result.verification["artifact_id"] + ".json")).read_text())
    assert any(row["tool"] == "get_fund_flow" and row["result"] == tools["get_fund_flow"]
               for row in artifact["receipts"])
    assert not responses
    assert sorted(reads) == ["analyze_portfolio", "get_fund_flow"]
    assert all(request.get("tools") is None for request in requests[2:])
    public = [json.loads(line) for line in
              (config.data_dir / "agent_sessions" / f"{session_id}.jsonl").read_text().splitlines()]
    assert public[-1]["content"] == result.summary
    assert public[-1]["verification"] == result.verification
