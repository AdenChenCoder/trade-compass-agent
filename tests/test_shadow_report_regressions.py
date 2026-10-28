"""Real-model shadow failures, with valid-output counterexamples."""
import json
from types import SimpleNamespace

import pytest

from trade_compass_agent.runtime.report_quality import report_issues


IDENTITY = [("sina_realtime_quote", json.dumps({"quotes": [
    {"symbol": "300378", "name": "鼎捷数智", "price": 43.92}]}))]


@pytest.mark.parametrize("text", [
    "| 标的 | 评级 |\n| 300378 天娱数科 | Buy |",
    "| 优先级 | 标的 | 计划动作 |\n| 优先 | 300378 天娱数科 | 计划买入 |",
    "| 股票代码 | 股票名称 |\n| 300378 | 天娱数科 |",
])
def test_table_identity_columns_reject_unverified_wrong_names(text):
    assert report_issues(text, IDENTITY)


@pytest.mark.parametrize("text", [
    "| 标的 | 评级 |\n| 300378 鼎捷数智 | Buy |",
    "| 股票代码 | 股票名称 |\n| 300378 | 鼎捷数智 |",
    "| 指标 | 说明 |\n| 300378 实时RSI | 尚未计算 |",
    "| 标的 | 说明 |\n| 300378 继续持有 | 计划观察 |",
    "历史名称参考：300378（历史名称），此前记录待核实。",
    "信号归档：300378（买入/等回调）。",
    "信号归档：300378（持有/观察）。",
    "信号归档：300378（买入 / 等回调）。",
    "交易计划：对极端放量高波动标的（300378）执行等回调策略。",
])
def test_identity_checks_preserve_actions_indicators_and_historical_names(text):
    assert not report_issues(text, IDENTITY)


def pulse(day=None, time="15:00:00", price=3689.34):
    return [("get_market_pulse", json.dumps({"_meta": {"freshness": "mixed", "data_as_of": None},
        "indices": [{"name": "上证指数", "price": price, "date": day, "time": time}]}))]


@pytest.mark.parametrize("data", [pulse(), pulse("2026-10-23"), pulse("2026-10-26", "09:16:00"),
                                      pulse("2026-10-26", "09:30:00", 0)])
@pytest.mark.parametrize("text", ["市场整体平开震荡。", "大盘开盘窄幅震荡（上证3689.34，-0.03%）。"])
def test_unconfirmed_index_time_cannot_become_a_current_opening_fact(data, text):
    assert report_issues(text, data, reference_date="2026-10-26")


@pytest.mark.parametrize("text", [
    "市场快照：上证3689.34，时点待核实。", "如果市场平开震荡，再核验量能。",
    "预计明日大盘高开，仍需以实盘为准。", "历史参考（10/23）：市场平开震荡。",
    "今日尚未开盘，不能据快照判断开盘走势。", "开盘后须重新获取市场数据。",
    "历史参考：市场平开震荡。", "昨日市场平开震荡。",
    "## 10/23 历史回顾\n大盘开盘窄幅震荡。",
    "创业板-1.94%。开盘初段涨停池36只，按更冷一档处理。",
])
def test_unknown_time_does_not_block_history_uncertainty_or_future_conditions(text):
    assert not report_issues(text, pulse(), reference_date="2026-10-26")


def test_date_guard_allows_valid_same_day_market_data():
    assert not report_issues("大盘开盘窄幅震荡（上证3689.34，-0.03%）。",
                             pulse("2026-10-26", "09:35:00"), reference_date="2026-10-26")


@pytest.mark.parametrize("brackets", [("（", "）"), ("(", ")")])
def test_parenthesized_flow_amounts_keep_the_same_fact_boundary(brackets):
    left, right = brackets
    receipts = [("get_fund_flow", json.dumps({"industry_flow": [
        {"name": "金融行业", "net_inflow_yi": 18.44}]}))]
    assert report_issues(f"行业资金净流入：电子信息{left}162.42亿{right}。", receipts)
    assert not report_issues(f"行业资金净流入：金融行业{left}18.44亿{right}。", receipts)


@pytest.mark.parametrize("tool,receipt,wrong,correct", [
    ("get_fund_flow", {"industry_flow": [{"name": "金融行业", "net_inflow_yi": 18.44}],
                       "_meta": {"data_as_of": None, "freshness": "unknown"}},
     "行业资金净流入：电子信息（162.42亿）。", "行业资金净流入：金融行业18.44亿。快照时点未知。"),
    ("get_market_pulse", json.loads(pulse()[0][1]), "市场整体平开震荡。", "指数快照时点未知，无法确认今日开盘走势。"),
])
def test_plain_report_correction_gets_market_receipts_without_repeating_tools(tmp_path, monkeypatch, tool, receipt, wrong, correct):
    from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    reads, calls = [], []
    raw = json.dumps(receipt, ensure_ascii=False)

    def complete(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ChatCompletion(content=None, tool_calls=[ToolCall("read", tool, "{}")])
        if len(calls) == 2:
            return ChatCompletion(content=wrong)
        assert len(calls) == 3 and kwargs.get("tools") is None
        evidence = json.loads(messages[-1].content)
        assert evidence["market_tool_results"] == [[tool, raw]]
        assert evidence["report_date"] == "2026-10-26"
        return ChatCompletion(content=correct)

    def execute(self, name, args):
        reads.append(name)
        return raw

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=complete))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config, report_date="2026-10-26")
    response = agent.run_turn("核对当前市场", session_id="scheduler-market-correction")
    assert correct in response.summary and wrong not in response.summary
    assert reads == [tool] and len(calls) == 3
    assert wrong not in (config.data_dir / "agent_sessions/scheduler-market-correction.jsonl").read_text()
