from __future__ import annotations

import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.providers import (
    ChatCompletion, ChatMessage, LLMExecutionCancelled, ToolCall, check_execution_active, llm_request_deadline,
)
from trade_compass_agent.runtime.exceptions import TurnInterruptedError
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.asset_runner import run_asset_specialist
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop


class _RecoveryChatClient:
    def __init__(self, first_arguments):
        self.calls = [
            ToolCall(id="failed", name="get_bars", arguments=first_arguments),
            ToolCall(id="recovered", name="get_bars", arguments='{"symbol":"000858"}'),
        ]
        self.receipts = []
        self.requests = 0

    def answer(self):
        assert "error" in json.loads(self.receipts[0])
        bars = json.loads(self.receipts[1])
        return ChatCompletion(content=f"首次查询失败，已改查 {bars['symbol']}，收盘价 {bars['close']}。")

    def complete(self, messages, *, tools=None):
        if self.requests:
            assert messages[-1].role == "tool"
            self.receipts.append(messages[-1].content)
        self.requests += 1
        if self.requests <= len(self.calls):
            return ChatCompletion(content="", tool_calls=[self.calls[self.requests - 1]])
        return self.answer()


class _RecoveryNativeClient(NativeAgentClient, _RecoveryChatClient):
    # The native callback contract is simulated; no executable or model is used.
    def __init__(self, first_arguments):
        _RecoveryChatClient.__init__(self, first_arguments)

    def run_agent(self, messages, *, tools, execute_tool, on_tool_interrupted=None):
        for call in self.calls:
            self.receipts.append(execute_tool(call))
        return self.answer()


@pytest.mark.parametrize("backend", ["api", "native"])
@pytest.mark.parametrize("failure", ["tool_error", "tool_timeout", "invalid_json", "non_object"])
def test_specialist_recovers_from_tool_error_with_visible_receipt(tmp_path, monkeypatch, backend, failure):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    stack = MarketStack.from_config(config)
    arguments = {"invalid_json": "{", "non_object": "[]"}.get(failure, '{"symbol":"600519"}')
    model = (_RecoveryNativeClient if backend == "native" else _RecoveryChatClient)(arguments)
    executions = []

    def execute(registry, profile, name, args):
        executions.append(args)
        if args.get("symbol") != "000858":
            if failure == "tool_timeout":
                raise TimeoutError("行情服务连接超时")
            raise ValueError("行情查询失败")
        return json.dumps({"symbol": "000858", "close": 120})

    monkeypatch.setattr("trade_compass_agent.runtime.specialists.asset_runner._execute_allowed_tool", execute)
    events = []
    result = run_asset_specialist(
        stack, load_specialist_profiles()["intraday_tech"], "查询行情，遇到数据错误后换一个标的继续。",
        config=config, client=model, on_event=events.append,
    )

    assert result == "首次查询失败，已改查 000858，收盘价 120。"
    assert executions == ([{"symbol": "600519"}] if failure in {"tool_error", "tool_timeout"} else []) + [{"symbol": "000858"}]
    assert [event.event for event in events] == ["tool_start", "tool_end", "tool_start", "tool_end"]
    assert [event.data["status"] for event in events if event.event == "tool_end"] == ["error", "ok"]
    assert all(event.data["specialist"] == "intraday_tech" for event in events)
    assert json.loads(model.receipts[0])["tool"] == "get_bars"


@pytest.mark.parametrize("backend", ["api", "native"])
@pytest.mark.parametrize("stage", ["model", "tool"])
@pytest.mark.parametrize("exception_type", [TurnInterruptedError, TimeoutError, LLMExecutionCancelled])
def test_specialist_does_not_recover_from_termination(backend, stage, exception_type):
    error = exception_type("stop")
    events = []
    executions = []
    requests = []
    tool_call = ToolCall(id="one", name="get_bars", arguments="{}")

    def execute_tool(name, args):
        executions.append(name)
        if exception_type is TimeoutError:
            check_execution_active()
        raise error

    def complete(messages, *, tools=None):
        requests.append("model")
        if stage == "model":
            if exception_type is TimeoutError:
                check_execution_active()
            raise error
        return ChatCompletion(content="", tool_calls=[tool_call])

    class TerminatingNativeClient(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, tools, execute_tool, on_tool_interrupted=None):
            complete(messages, tools=tools)
            execute_tool(tool_call)
            pytest.fail("A terminated tool must not produce a model receipt")

    client = TerminatingNativeClient() if backend == "native" else SimpleNamespace(complete=complete)
    deadline = llm_request_deadline(time.monotonic() - 1) if exception_type is TimeoutError else nullcontext()
    with deadline, pytest.raises(exception_type) as caught:
        run_react_loop(
            client=client, messages=[ChatMessage(role="user", content="检查行情")], tool_schemas=[],
            execute_tool=execute_tool, on_tool_event=lambda kind, data: events.append(kind),
        )
    if exception_type is TimeoutError:
        assert "deadline" in str(caught.value)
    else:
        assert caught.value is error
    assert requests == ["model"]
    assert executions == (["get_bars"] if stage == "tool" else [])
    assert events == (["tool_start"] if stage == "tool" else [])


def test_native_specialist_interrupted_tool_ends_with_error_without_replay(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    stack = MarketStack.from_config(config)
    calls = []
    tool_call = ToolCall(id="unfinished", name="get_bars", arguments='{"symbol":"600519"}')
    reason = "已达到工具阶段时限，以下仅为已获取信息的总结。"
    receipt = json.dumps({"error": "工具执行超时，结果未确认；请核对已提交操作，不要自动重试。",
                          "tool": tool_call.name, "execution_unconfirmed": True}, ensure_ascii=False)

    def execute(registry, profile, name, args):
        calls.append((name, args))
        check_execution_active()
        pytest.fail("The tool-phase deadline must interrupt execution")

    class InterruptedNativeClient(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, tools, execute_tool, on_tool_interrupted):
            try:
                with llm_request_deadline(time.monotonic() - 1):
                    execute_tool(tool_call)
            except TimeoutError:
                on_tool_interrupted(tool_call, receipt)
            return SimpleNamespace(content="已有日线不足以完成研究。", incomplete_reason=reason)

    monkeypatch.setattr("trade_compass_agent.runtime.specialists.asset_runner._execute_allowed_tool", execute)
    moments = iter([10.0, 10.75])
    clock = SimpleNamespace(monotonic=lambda: next(moments))
    monkeypatch.setattr("trade_compass_agent.runtime.specialists.nested_loop.time", clock)
    monkeypatch.setattr("trade_compass_agent.runtime.activity_events.time", clock)
    events = []
    result = run_asset_specialist(
        stack, load_specialist_profiles()["intraday_tech"], "分析行情", config=config,
        client=InterruptedNativeClient(), on_event=events.append,
    )

    assert calls == [("get_bars", {"symbol": "600519"})]
    assert [event.event for event in events] == ["tool_start", "tool_end"]
    assert events[-1].data["status"] == "error"
    assert events[-1].data["duration_ms"] == 750
    assert json.loads(events[-1].data["preview"])["execution_unconfirmed"]
    assert json.loads(result) == {
        "error": reason, "incomplete_reason": reason,
        "report_markdown": "已有日线不足以完成研究。", "warnings": [reason],
    }


@pytest.mark.parametrize("reason", ["已达到工具调用次数上限", "已达到工具阶段时限"])
def test_native_specialist_keeps_budget_limit_even_when_model_omits_it(reason):
    class IncompleteNativeClient(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, tools, execute_tool, on_tool_interrupted):
            return SimpleNamespace(content="全部研究已完成。", incomplete_reason=reason)

    result = run_react_loop(
        client=IncompleteNativeClient(), messages=[ChatMessage(role="user", content="分析行情")],
        tool_schemas=[], execute_tool=lambda *args: pytest.fail("No further tools are allowed"),
    )
    payload = json.loads(result)
    assert payload["error"] == payload["incomplete_reason"] == reason
    assert payload["warnings"] == [reason]
    assert payload["report_markdown"] == "全部研究已完成。"


@pytest.mark.parametrize("consumer", ["workflow", "dispatch"])
@pytest.mark.parametrize("incomplete", [True, False])
def test_structured_specialist_budget_status_reaches_real_consumers(tmp_path, monkeypatch, consumer, incomplete):
    from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists
    from trade_compass_agent.runtime.workflows.engine import load_workflow_assets, run_workflow_asset

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    stack = MarketStack.from_config(config)
    profile = load_specialist_profiles()["intraday_tech"]
    assert profile.output.mode == "structured_markdown"
    reason = "任务未全部完成：已达到工具预算。" if incomplete else ""
    report = "## 风险\n已取得的日线不足以确认走势。"

    class BudgetNativeClient(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, messages, *, tools, execute_tool, on_tool_interrupted):
            return ChatCompletion(content=report, incomplete_reason=reason)

    monkeypatch.setattr("trade_compass_agent.runtime.specialists.asset_runner.create_chat_client",
                        lambda config: BudgetNativeClient())
    if consumer == "workflow":
        output = run_workflow_asset(
            load_workflow_assets()["intraday_tech"], {"as_of": "2026-10-30", "task": "分析行情"},
            config=config, stack=stack, persist=False,
        )
        assert output["metadata"]["specialist_id"] == "intraday_tech"
        assert output["report_markdown"] == report
        assert bool(output.get("degraded")) is incomplete
    else:
        events = []
        dispatched = json.loads(tool_dispatch_specialists(
            stack, [{"specialist": "intraday_tech", "task": "分析行情"}], config=config, on_event=events.append,
        ))
        result = dispatched["results"][0]["output"]
        assert [event.data["status"] for event in events if event.event == "specialist_done"] == [
            "error" if incomplete else "ok",
        ]
        if not incomplete:
            assert result == report
            return
        output = json.loads(result)
        assert output["report_markdown"] == report

    if incomplete:
        assert output["incomplete_reason"] == reason
        assert reason in output["error"]
        assert reason in output["warnings"]
    else:
        assert "error" not in output
        assert "incomplete_reason" not in output
