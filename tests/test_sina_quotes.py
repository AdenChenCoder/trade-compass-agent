"""Consumer checks for A-share quotes and the three supported US indices."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import requests

from trade_compass_agent.config import AppConfig
from trade_compass_agent.data.providers import ProviderError, to_sina_code
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.ops.job_definition import StepContext, StepOutput
from trade_compass_agent.runtime.tools import builtin_operations
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.tools.search import tool_sina_realtime_quote


# Public Sina responses captured 2026-09-16; 30/36-field shapes both occur.
DJI = 'var hq_str_gb_dji="道琼斯,52035.1719,-0.74,2026-09-16 02:11:45,-386.0300,52303.2383,52336.6094,51875.6484,54744.3281,45057.2812,198111750,413913231,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,0.00,0.0000,,Sep 15 02:11PM EDT,52421.1992,0,1,2026";'
IXIC = 'var hq_str_gb_ixic="纳斯达克,26002.3935,-0.70,2026-09-16 02:12:00,-184.0198,26140.2274,26172.1208,25949.4776,27190.2070,20690.2500,4586626304,5787254233,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,0.00,0.00,,Sep 15 02:12PM EDT,26186.4133,0,1,2026,0.0000,0.0000,0.0000,0.0000,0.0000,0.0000";'
INX = 'var hq_str_gb_inx="标普500指数,7589.1802,-0.40,2026-09-16 02:11:45,-30.8000,7612.2998,7617.2598,7572.6899,7816.7002,6316.9102,1460513262,2810740452,0,0.00,--,0.00,0.00,0.00,0.00,0,0,0.0000,0.00,0.0000,,Sep 15 02:11PM EDT,7619.9800,0,1,2026";'


def a_share() -> str:
    fields = ["贵州茅台", "1281", "1277.96", "1272.75", "1284.5", "1271.28", "1272.75", "1272.78", "1376172", "1756915149"]
    fields += ["0"] * 20 + ["2026-09-15", "15:00:00"]
    return 'var hq_str_sh600519="' + ",".join(fields) + '";'


def mock_response(monkeypatch, body):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.encoding = "gb18030"
        response._content = body.encode("gb18030")
        return response

    monkeypatch.setattr(requests, "get", get)
    return calls


def test_mixed_quotes_reach_agent_without_changing_a_share_fields(monkeypatch):
    calls = mock_response(monkeypatch, "\n".join([DJI, IXIC, INX, a_share()]))
    registry = ToolRegistry(SimpleNamespace(config=AppConfig()))
    result = json.loads(registry.execute("sina_realtime_quote", {"symbols": "600519,gb_dji,gb_ixic,gb_inx,hkHSI"}))
    assert result["count"] == 4
    assert set(result["errors"]) == {"hkHSI"}
    assert "error" not in result
    assert calls == ["https://hq.sinajs.cn/list=sh600519,gb_dji,gb_ixic,gb_inx"]
    quotes = {q["symbol"]: q for q in result["quotes"]}
    assert quotes["600519"] == {
        "symbol": "600519", "name": "贵州茅台", "open": 1281,
        "prev_close": 1277.96, "price": 1272.75, "high": 1284.5, "low": 1271.28,
        "volume": 1376172, "amount": 1756915149, "date": "2026-09-15", "time": "15:00:00",
    }
    dji = quotes["gb_dji"]
    assert dji["price"] == 52035.1719
    assert dji["open"] == 52303.2383
    assert dji["high"] == 52336.6094
    assert dji["low"] == 51875.6484
    assert dji["prev_close"] == 52421.1992
    assert dji["change_pct"] == -0.74
    assert dji["change"] == -386.03
    assert dji["volume"] == 198111750
    assert dji["amount"] is None
    assert dji["as_of"] == "2026-09-16T02:11:45+08:00"
    assert dji["market_time"] == "2026-09-15T14:11:45-04:00"
    assert dji["market_date"] == "2026-09-15"
    assert dji["price_type"] == "latest_quote"
    assert dji["warnings"]
    assert quotes["gb_ixic"]["price"] == 26002.3935
    assert quotes["gb_inx"]["price"] == 7589.1802


@pytest.mark.parametrize(("stamp", "expected"), [
    ("2026-01-16 02:11:45", "2026-01-15T13:11:45-05:00"),
    ("2026-09-16 04:00:00", "2026-09-15T16:00:00-04:00"),
])
def test_us_timezone_handles_winter_and_does_not_infer_close(monkeypatch, stamp, expected):
    mock_response(monkeypatch, DJI.replace("2026-09-16 02:11:45", stamp))
    quote = json.loads(tool_sina_realtime_quote(symbols="gb_dji"))["quotes"][0]
    assert quote["market_time"] == expected
    assert quote["price_type"] == "latest_quote"
    assert "close" not in quote


def test_missing_us_fields_are_unknown_not_zero(monkeypatch):
    mock_response(monkeypatch, DJI.replace("52303.2383", "--").replace("52421.1992", "").replace("198111750", "NaN"))
    quote = json.loads(tool_sina_realtime_quote(symbols="gb_dji"))["quotes"][0]
    assert quote["open"] is None
    assert quote["prev_close"] is None
    assert quote["volume"] is None


@pytest.mark.parametrize("bad", [
    'var hq_str_gb_dji="";',
    DJI.replace("52035.1719", "--"),
    DJI.replace("52035.1719", "NaN"),
    DJI.replace("52035.1719", "0"),
    DJI.replace("2026-09-16 02:11:45", "invalid date"),
])
def test_bad_quote_does_not_discard_valid_quotes(monkeypatch, bad):
    mock_response(monkeypatch, bad + "\n" + IXIC)
    result = json.loads(tool_sina_realtime_quote(symbols="gb_dji,gb_ixic,gb_inx"))
    assert result["count"] == 1
    assert result["quotes"][0]["symbol"] == "gb_ixic"
    assert set(result["errors"]) == {"gb_dji", "gb_inx"}


def test_empty_response_is_not_success(monkeypatch):
    mock_response(monkeypatch, "")
    result = json.loads(tool_sina_realtime_quote(symbols="gb_dji"))
    assert result["error"]
    assert result["count"] == 0
    assert result["errors"]["gb_dji"]


@pytest.mark.parametrize("symbols", ["", "usDJI,gb_aapl", "gb_dji&list=gb_aapl"])
def test_unsupported_input_never_reaches_network(monkeypatch, symbols):
    calls = mock_response(monkeypatch, DJI)
    result = json.loads(tool_sina_realtime_quote(symbols=symbols))
    assert result["error"]
    assert not calls


def test_aliases_preserve_caller_identity(monkeypatch):
    calls = mock_response(monkeypatch, a_share() + "\n" + DJI)
    result = json.loads(tool_sina_realtime_quote(symbols="600519,sh600519,GB_DJI"))
    assert {q["symbol"] for q in result["quotes"]} == {"600519", "sh600519", "GB_DJI"}
    assert len(calls) == 1
    with pytest.raises(ProviderError):
        to_sina_code("gb_dji")


@pytest.mark.parametrize("failure", [False, True])
def test_global_markets_passes_snapshot_and_gaps_to_analysis_and_output(tmp_path, monkeypatch, failure):
    if failure:
        def get(*args, **kwargs):
            raise requests.Timeout("Sina timeout")
        monkeypatch.setattr(requests, "get", get)
    else:
        mock_response(monkeypatch, DJI + "\n" + IXIC)
    agent = AsyncMock(return_value=StepOutput(message="done", data={"analysis": "report"}))
    monkeypatch.setattr(builtin_operations, "run_agent_step", agent)
    ctx = StepContext(config=AppConfig(data_dir=tmp_path), date=date(2026, 9, 14))
    result = asyncio.run(builtin_operations.check_global_markets(ctx))
    prompt = agent.call_args.args[1]
    snapshot = result.data["us_index_snapshot"]
    assert json.dumps(snapshot, ensure_ascii=False) in prompt
    assert "2026-09-14" in prompt
    assert "历史日期任务不能使用当前报价替代历史收盘" in prompt
    assert "不是已核实的收盘记录" in prompt
    assert "prev_close" in prompt
    assert result.data["analysis"] == "report"
    if failure:
        assert snapshot["error"]
    else:
        assert snapshot["count"] == 2
        assert snapshot["errors"]["gb_inx"]


def test_global_market_agent_can_verify_quotes_with_a_scoped_toolset(tmp_path, monkeypatch):
    from trade_compass_agent.runtime.workflows.engine import load_workflow_assets

    calls = mock_response(monkeypatch, "\n".join([DJI, IXIC, INX]))
    offered_tools = []

    class QuoteClient:
        name = "mock"
        model = "mock"

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            offered_tools.append({tool["function"]["name"] for tool in tools or []})
            if len(offered_tools) == 1:
                return ChatCompletion(content="", model=self.model, provider=self.name, tool_calls=[
                    ToolCall(id="quote", name="sina_realtime_quote",
                             arguments=json.dumps({"symbols": "gb_dji,gb_ixic,gb_inx"})),
                ])
            assert any(message.role == "tool" and message.name == "sina_realtime_quote"
                       and json.loads(message.content)["count"] == 3 for message in messages)
            return ChatCompletion(
                content="全球市场结论：指数为带时间的最新报价，收盘仍待核实。港股、汇率和商品存在数据缺口。",
                model=self.model, provider=self.name,
            )

    model = QuoteClient()
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda config: model)
    manifest = load_workflow_assets()["premarket_briefing"]
    step = next(step for step in manifest.steps if step.id == "global_market")
    assert step.timeout_seconds == 1800
    assert manifest.timeout_seconds == 3600
    ctx = StepContext(
        config=AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample"),
        date=date(2026, 9, 16), step_timeout_seconds=step.timeout_seconds,
    )

    output = asyncio.run(builtin_operations.check_global_markets(ctx))

    assert "全球市场结论" in output.data["analysis"]
    assert "数据缺口" in output.data["analysis"]
    assert output.data["us_index_snapshot"]["count"] == 3
    assert len(calls) == 2
    assert len(offered_tools) == 2
    for names in offered_tools:
        assert {"sina_realtime_quote", "web_search", "fetch_url", "load_skill", "search_market_flash"} <= names
        assert not names & {"place_paper_trade", "batch_paper_trades", "dispatch_specialists", "run_workflow"}
        assert len(names) <= 10
