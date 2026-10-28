"""Consumer regressions for the October 22–24 degraded tasks."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.reassessment import review_candidates
from trade_compass_agent.runtime.report_quality import assess_data_quality, report_issues
from trade_compass_agent.runtime.tools.registry import ToolRegistry


@pytest.mark.parametrize("text", [
    "2笔601928止损合计-303.71 < 8笔盈利合计+1,219.04。",
    "-303.71 < +1,219.04", "1,219.04 > 303.71", "3万元 < 4亿元",
    "价格8.50 < 8.92", "-3 > -4", "3 <= 4",
])
def test_valid_comparisons_do_not_block_publication(text):
    assert not report_issues(text, [])


@pytest.mark.parametrize("text", [
    "303.71 > 1,219.04", "-3 < -4", "3亿元 < 4万元", "9.14 < 9.12",
])
def test_explicit_false_comparisons_still_block_publication(text):
    assert report_issues(text, [])


@pytest.mark.parametrize("name", ["N通则", "C通则", "*ST通则", "ST通则"])
def test_exchange_prefix_is_part_of_the_verified_instrument_name(name):
    facts = [("sina_realtime_quote", json.dumps({"quotes": [{"symbol": "301718", "name": name}]}))]
    assert not report_issues(f"{name}（301718）：未纳入评估", facts)
    assert report_issues("错误公司（301718）：未纳入评估", facts)
    if name == "*ST通则":
        assert report_issues("ST通则（301718）：未纳入评估", facts)


def test_multi_symbol_bars_error_is_actionable_without_contacting_providers(tmp_path):
    provider = Mock()
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    registry = ToolRegistry(SimpleNamespace(config=config, provider=provider))
    payload = json.loads(registry.execute("get_bars", {
        "symbol": "003017,002535,002351,301153", "timeframe": "1d", "limit": 10,
    }))
    assert payload["error_type"] == "invalid_arguments"
    assert payload["suggested_tool"] == "batch_get_bars"
    assert payload["suggested_arguments"] == {
        "symbols": "003017,002535,002351,301153", "timeframe": "1d", "limit": 10,
    }
    provider.get_bars.assert_not_called()


@pytest.mark.parametrize("resolved", [[], ["003017"], ["003017", "002535"]])
def test_corrected_batch_clears_only_resolved_bars_queries(resolved):
    calls = [("get_bars", json.dumps({
        "symbol": "003017,002535", "symbols": ["003017", "002535"], "timeframe": "1d",
        "error_type": "invalid_arguments", "error": "use batch_get_bars",
    })), ("batch_get_bars", json.dumps({
        "timeframe": "1d", "results": {symbol: {"bars_count": 10, "close": 9} for symbol in resolved},
    }))]
    quality = assess_data_quality(calls)
    assert quality.degraded is (len(resolved) < 2)
    for symbol in resolved:
        assert not any(symbol in warning for warning in quality.warnings)
    for symbol in {"003017", "002535"} - set(resolved):
        assert any(symbol in warning for warning in quality.warnings)


def test_quote_retry_cannot_hide_failure_for_another_symbol():
    calls = [("sina_realtime_quote", json.dumps({
        "quotes": [{"symbol": "gb_ixic", "price": 10}], "errors": {"gb_dji": "timeout"},
    })), ("sina_realtime_quote", json.dumps({"quotes": [{"symbol": "gb_ixic", "price": 11}]}))]
    assert assess_data_quality(calls).degraded
    calls.append(("sina_realtime_quote", json.dumps({"quotes": [{"symbol": "gb_dji", "price": 9}]})))
    assert not assess_data_quality(calls).degraded


def test_another_timeframe_does_not_clear_failed_daily_query():
    calls = [("get_bars", '{"symbol":"003017","timeframe":"1d","error":"timeout"}'),
             ("batch_get_bars", '{"timeframe":"5m","results":{"003017":{"bars_count":10,"close":9}}}')]
    assert assess_data_quality(calls).degraded


def test_batch_failure_can_recover_through_single_symbol_reads():
    calls = [("batch_get_bars", '{"timeframe":"1d","results":{},"errors":{"003017":"timeout","002535":"timeout"}}'),
             ("get_bars", '{"symbol":"003017","timeframe":"1d","bars":[{"close":9}]}')]
    quality = assess_data_quality(calls)
    assert quality.degraded and len(quality.warnings) == 1 and "002535" in quality.warnings[0]
    calls.append(("get_bars", '{"symbol":"002535","timeframe":"1d","bars":[{"close":9}]}'))
    assert not assess_data_quality(calls).degraded


def test_invalid_quote_scope_is_reported_without_losing_successful_quotes(tmp_path, monkeypatch):
    fields = ["贵州茅台", "9", "9", "9", "10", "8", "9", "9", "100", "900"]
    fields += ["0"] * 20 + ["2026-10-23", "15:00:00"]
    get = Mock(return_value=SimpleNamespace(text='var hq_str_sh600519="' + ",".join(fields) + '";', raise_for_status=lambda: None))
    monkeypatch.setattr("requests.get", get)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    payload = json.loads(ToolRegistry(SimpleNamespace(config=config)).execute(
        "sina_realtime_quote", {"symbols": "600519,恒生指数"}))
    assert payload["quotes"][0]["symbol"] == "600519"
    assert payload["invalid_symbols"] == ["恒生指数"] and payload["retry_hint"]
    assert get.call_count == 1 and "恒生" not in get.call_args.args[0]
    quality = assess_data_quality([("sina_realtime_quote", json.dumps(payload))])
    assert quality.degraded and "参数有误" in quality.warnings[0]


@pytest.mark.parametrize("recover", [True, False])
def test_review_repairs_exact_identity_with_previous_output_and_preserves_failed_snapshot(tmp_path, recover):
    store = MemoryStore(tmp_path / "memory")
    store.add("趋势收益仍需不同会话验证", source="scheduler")
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        if len(calls) == 2:
            assert payload["previous_reviews"] == []
            assert "invented-id" not in json.dumps(payload)
            assert payload["validation_error"]["expected_entry_ids"] == [payload["candidates"][0]["entry_id"]]
        return json.dumps({"reviews": [{
            "entry_id": payload["candidates"][0]["entry_id"] if recover and len(calls) == 2 else "invented-id",
            "decision": "defer", "reason": "缺少独立证据", "refs": [],
        }]})

    result = review_candidates(store, call)
    assert result["ok"] is recover and len(calls) == 2
    rows = MemoryStore(tmp_path / "memory").get_entries_with_meta()
    assert len(rows) == 1 and rows[0].status == "candidate"
    assert rows[0].version == (2 if recover else 1)


def test_valid_report_is_published_without_unnecessary_correction(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop

    report = "N通则（301718）：新股波动较大，未纳入评估。\n2笔止损合计-303.71 < 8笔盈利合计+1,219.04。"
    replies = [ChatCompletion(content=None, tool_calls=[ToolCall("quote", "sina_realtime_quote", '{"symbols":"301718"}')]),
               ChatCompletion(content=report)]
    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=lambda *a, **k: replies.pop(0)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(ToolRegistry, "execute", lambda *a: json.dumps({"quotes": [{"symbol": "301718", "name": "N通则"}]}))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    result = loop.AgentLoop.from_config(config).run_turn("按原始回执写报告", session_id="regression-report")
    assert report in result.summary and not result.degraded and not replies
    saved = (config.data_dir / "agent_sessions/regression-report.jsonl").read_text()
    assert "N通则" in saved and "分析过程中出现错误" not in saved


def test_agent_uses_suggested_batch_and_publishes_recovered_report(tmp_path, monkeypatch):
    from datetime import datetime
    from trade_compass_agent.domain import Bar
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.runtime import loop

    calls = []

    def stream(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return ChatCompletion(content=None, tool_calls=[ToolCall("bad", "get_bars", '{"symbol":"003017,002535"}')])
        if len(calls) == 2:
            error = json.loads(messages[-1].content)
            assert error["error_type"] == "invalid_arguments"
            return ChatCompletion(content=None, tool_calls=[ToolCall("retry", error["suggested_tool"], json.dumps(error["suggested_arguments"]))])
        assert len(json.loads(messages[-1].content)["results"]) == 2
        return ChatCompletion(content="003017、002535 日线均已取得，依据已返回的收盘数据继续观察，本轮没有成交。")

    monkeypatch.setattr(loop, "create_chat_client", lambda *_: SimpleNamespace(stream_complete=stream))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="disabled"), agent=AgentConfig(learning_enabled=False, llm_session_titles=False))
    agent = loop.AgentLoop.from_config(config)
    reads = []

    def get_bars(symbol, **kwargs):
        reads.append(symbol)
        return [Bar(symbol=symbol, timestamp=datetime(2026, 10, 23, 15), open=9, high=10, low=8, close=9, volume=100)]

    monkeypatch.setattr(agent._tools, "stack", SimpleNamespace(config=config, provider=SimpleNamespace(get_bars=get_bars)))
    result = agent.run_turn("核对这两只标的的日线", session_id="recovered-bars")
    assert not result.degraded and not result.warnings and sorted(reads) == ["002535", "003017"]
    assert len(calls) == 3 and "数据可用性" not in result.summary
