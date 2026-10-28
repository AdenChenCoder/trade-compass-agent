"""Distinguish an order rejection from an unavailable data provider."""

import json
from types import SimpleNamespace

from trade_compass_agent.runtime.report_quality import assess_data_quality


def test_rejections_remain_visible_separately_from_data_failures():
    reason = "T+1：当日买入的份额下一交易日才可卖出"
    results = [
        ("place_paper_trade", json.dumps({"error": reason, "trade_rejected": True})),
        ("place_paper_trade", json.dumps({"status": "executed", "trade_id": "other-order"})),
        ("get_bars", json.dumps({"symbol": "000001", "error": "timeout"})),
    ]
    quality = assess_data_quality(results)
    assert quality.execution_warnings == [reason]
    assert quality.degraded and len(quality.warnings) == 1
    assert "K线" in quality.warnings[0]


def test_rejected_quote_failure_keeps_its_actual_reason():
    reason = "无法获取 000001 的市场成交价: timeout"
    quality = assess_data_quality(
        [("place_paper_trade", json.dumps({"error": reason, "trade_rejected": True}))]
    )
    assert quality.execution_warnings == [reason]
    assert not quality.warnings and not quality.degraded
    unexpected = assess_data_quality([("place_paper_trade", '{"error":"provider unavailable"}')])
    assert unexpected.warnings


def test_final_report_and_saved_history_label_rejection_truthfully(tmp_path, monkeypatch):
    from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    reason = "T+1：当日买入的份额下一交易日才可卖出"
    completions = [
        ChatCompletion(
            content=None,
            tool_calls=[
                ToolCall(
                    "sell", "place_paper_trade", '{"symbol":"000001","side":"sell","quantity":100}'
                )
            ],
        ),
        ChatCompletion(
            content="本轮卖出未成交。工具确认当日买入受T+1限制，持仓与资金均未改变。下次执行前须核对可卖数量，本轮不重复下单，也不把历史买入视为新成交。"
        ),
    ]
    monkeypatch.setattr(
        loop,
        "create_chat_client",
        lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: completions.pop(0)),
    )
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(
        ToolRegistry,
        "execute",
        lambda *a, **k: json.dumps({"error": reason, "trade_rejected": True}),
    )
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(provider="disabled"),
        agent=AgentConfig(learning_enabled=False, llm_session_titles=False),
    )
    agent = loop.AgentLoop.from_config(config)
    result = agent.run_turn("尝试卖出当日刚买入的100股", session_id="scheduler-rejection-report")
    assert reason in result.summary and "未成交记录" in result.summary
    assert "暂不可用" not in result.summary and "数据可用性" not in result.summary
    assert not result.degraded
    saved = "\n".join(p.read_text() for p in config.data_dir.rglob("*.jsonl"))
    assert "未成交记录" in saved and "暂不可用" not in saved
