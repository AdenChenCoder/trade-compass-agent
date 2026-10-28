from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import time
import threading
import sqlite3
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.providers import (
    ChatMessage, LLMRequest, OpenAIChatClient, create_llm_provider, llm_maintenance_mode, llm_request_deadline,
    _remaining_request_time, LLMExecutionCancelled,
)
from trade_compass_agent.ops.job_definition import JobDefinition, JobRegistry, StepOutput
from trade_compass_agent.ops.job_executor import JobExecutor
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.runtime.exceptions import AgentTurnError
from trade_compass_agent.runtime.tools import builtin_operations
from trade_compass_agent.runtime.workflows import engine


@pytest.fixture(autouse=True)
def mocked_loaded_window(monkeypatch):
    # Model transports below are mocked; never discover a real desktop model.
    monkeypatch.setattr("trade_compass_agent.llm.providers._lmstudio_loaded_context_window",
                        lambda *_args, **_kwargs: 131072)


def _sse(*choices):
    return "".join("data: " + json.dumps({"id": "test", "model": "qwen3.5-35b-a3b", "choices": [choice]})
                   + "\n\n" for choice in choices) + "data: [DONE]\n\n"


@pytest.mark.parametrize("model", ["qwen3.5-35b-a3b", "qwen3.6-35b-a3b", "qwen3.8-27b"])
def test_maintenance_uses_closed_thinking_template_in_raw_endpoint(monkeypatch, tmp_path, model):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "text": "[1,3,5]", "finish_reason": "stop"},
        ), headers={"content-type": "text/event-stream"})

    # A real SDK transport verifies serialization, rather than just inspecting a mock call.
    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenAIChatClient(model=model, api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    with llm_maintenance_mode():
        result = client.complete([ChatMessage(role="system", content="整理"), ChatMessage(role="user", content="记录")])
    assert json.loads(result.content) == [1, 3, 5]
    assert requests[0].url.path == "/v1/completions"
    body = json.loads(requests[0].content)
    assert body["prompt"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert "<|im_start|>system\n整理<|im_end|>" in body["prompt"]
    assert body["stream"] is True and body["max_tokens"] == 4096


@pytest.mark.parametrize("model", ["qwen3.5-35b-a3b", "qwen3.8-27b"])
def test_local_timeout_is_not_replayed_by_sdk(monkeypatch, tmp_path, model):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("model stalled", request=request)

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenAIChatClient(model=model, api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", max_retries=2,
                              usage_db_path=tmp_path / "usage.db")
    with pytest.raises(AgentTurnError, match="timed out"):
        client.complete([ChatMessage(role="user", content="test")])
    assert len(requests) == 1


def test_background_summary_also_uses_non_thinking_mode(monkeypatch, tmp_path):
    from trade_compass_agent.runtime import loop

    captured = {}
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **kwargs: captured.update(kwargs))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "text": '{"session_summary":"summary"}', "finish_reason": "stop"},
        ), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="lmstudio", model="qwen3.5-35b-a3b"))
    loop.AgentLoop.from_config(config)
    assert json.loads(captured["llm_call"]("summarize", "records"))["session_summary"] == "summary"
    assert calls[0].url.path == "/v1/completions"


def test_legacy_learning_provider_preserves_thinking(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "delta": {"reasoning_content": "private judgment"}, "finish_reason": None},
            {"index": 0, "delta": {"content": "summary"}, "finish_reason": "stop"},
        ), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    provider = create_llm_provider(provider="lmstudio", model="qwen3.5-35b-a3b", enabled=True, data_dir=tmp_path)
    assert provider.complete(LLMRequest(prompt="summarize", purpose="memory_compaction")).text == "summary"
    assert requests[0].url.path == "/v1/chat/completions" and len(requests) == 1


@pytest.mark.parametrize("model,penalty", [("qwen3.5-35b-a3b", 1.5), ("qwen3.6-35b-a3b", 1.5), ("qwen3.8-27b", 0.0)])
def test_memory_judgment_thinks_and_only_explicit_summary_disables_thinking(monkeypatch, tmp_path, model, penalty):
    requests = []

    def handler(request):
        requests.append(request)
        choices = ({"index": 0, "text": "summary", "finish_reason": "stop"},) if request.url.path == "/v1/completions" else (
            {"index": 0, "delta": {"reasoning_content": "private judgment"}, "finish_reason": None},
            {"index": 0, "delta": {"content": "judgment"}, "finish_reason": "stop"},
        )
        return httpx.Response(200, text=_sse(*choices), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="lmstudio", model=model))
    assert builtin_operations._memory_llm_call(config, "评估证据", "records") == "judgment"
    assert builtin_operations._summary_llm_call(config, "压缩已知事实", "records") == "summary"
    assert builtin_operations._memory_llm_call(config, "提炼经验", "records") == "judgment"
    assert [r.url.path for r in requests] == ["/v1/chat/completions", "/v1/completions", "/v1/chat/completions"]
    thinking = json.loads(requests[0].content)
    assert thinking["temperature"] == 1.0 and thinking["top_p"] == .95
    assert thinking["presence_penalty"] == penalty and thinking["top_k"] == 20 and thinking["repeat_penalty"] == 1.0
    summary = json.loads(requests[1].content)
    if "3.8" in model or "3.6" in model:
        assert summary["temperature"] == .7 and summary["presence_penalty"] == 1.5
        assert thinking["max_tokens"] == 32768
    else:
        assert summary["temperature"] == .3 and "presence_penalty" not in summary


@pytest.mark.parametrize("provider,model", [("lmstudio", "other-model"), ("openai", "qwen3.5-35b-a3b")])
def test_qwen_thinking_sampling_does_not_change_other_providers_or_models(monkeypatch, tmp_path, provider, model):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"},
        ), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenAIChatClient(model=model, api_key="test", name=provider,
                             base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    assert client.stream_complete([ChatMessage(role="user", content="question")]).content == "answer"
    body = json.loads(requests[0].content)
    assert body["temperature"] == .3 and "presence_penalty" not in body and "top_k" not in body


@pytest.mark.parametrize("weekly", [False, True])
def test_dream_diary_respects_configured_agent_budget(monkeypatch, tmp_path, weekly):
    from trade_compass_agent.memory.dream_diary import run_dream_diary

    captured = {}

    class Session:
        def __init__(self, config, *, job_id, **kwargs):
            captured["job_id"] = job_id

        def run(self, prompt, *, timeout):
            captured["timeout"] = timeout
            return "日记"

    monkeypatch.setattr("trade_compass_agent.ops.agent_session.ScheduledAgentSession", Session)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(agent_timeout=3600))
    assert run_dream_diary(config, "observations", weekly=weekly) == "日记"
    assert captured == {"job_id": "dreaming-weekly-diary" if weekly else "dreaming-diary", "timeout": 3600}


@pytest.mark.parametrize("weekly", [False, True])
def test_dreaming_routes_only_time_tree_compression_to_non_thinking(monkeypatch, tmp_path, weekly):
    from trade_compass_agent.llm.providers import _maintenance_mode
    from trade_compass_agent.memory.observation_store import ObservationStore
    from trade_compass_agent.memory.time_tree import TimeTree
    from trade_compass_agent.ops.job_definition import StepContext

    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(config.scheduler.timezone)).date()
    calls = []
    ObservationStore(config.data_dir / "observations.db").append("source", "analyze", "已观察事实")

    def memory_call(_config, system, content):
        calls.append((system, _maintenance_mode.get()))
        return "事实摘要"

    def patterns(_observations, _decisions, llm_call, **_kwargs):
        llm_call("pattern judgment", "evidence")
        return []

    def promotions(_candidates, _memory, _observations, *, llm_call, **_kwargs):
        llm_call("promotion judgment", "evidence")
        return []

    monkeypatch.setattr(builtin_operations, "_memory_llm_call", memory_call)
    monkeypatch.setattr("trade_compass_agent.memory.patterns.discover_patterns", patterns)
    monkeypatch.setattr("trade_compass_agent.memory.promotion.apply_promotions", promotions)
    monkeypatch.setattr("trade_compass_agent.memory.dream_diary.run_dream_diary", lambda *_args, **_kwargs: "日记")
    if weekly:
        TimeTree(config.data_dir / "time_tree.db").seal_day(
            today.isoformat(), obs_summaries=["已观察事实"], session_summaries=[],
            concepts=[], symbols=[], obs_ids=[], session_ids=[],
        )
    operation = builtin_operations.run_weekly_dreaming if weekly else builtin_operations.run_dreaming
    asyncio.run(operation(StepContext(config=config, date=today, job_id="weekly" if weekly else "postmarket")))
    assert any(non_thinking for _, non_thinking in calls)
    assert ("pattern judgment", False) in calls and ("promotion judgment", False) in calls
    assert all(non_thinking for system, non_thinking in calls if "judgment" not in system)


def test_stream_keeps_reasoning_private_and_preserves_tool_calls(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "delta": {"reasoning_content": "private reasoning"}, "finish_reason": None},
            {"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                "function": {"name": "analyze_portfolio", "arguments": "{}"}}]}, "finish_reason": "tool_calls"},
        ), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenAIChatClient(model="qwen3.5-35b-a3b", api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    deltas = []
    with llm_maintenance_mode():
        result = client.stream_complete([ChatMessage(role="user", content="检查账户")], tools=[{
            "type": "function", "function": {"name": "analyze_portfolio", "parameters": {"type": "object"}},
        }], on_delta=deltas.append)
    assert requests[0].url.path == "/v1/chat/completions"
    assert result.content is None and deltas == []
    assert result.tool_calls[0].name == "analyze_portfolio"
    assert json.loads(result.tool_calls[0].arguments) == {}


def test_nested_agent_deadline_cannot_extend_workflow_budget():
    with llm_request_deadline(time.monotonic() + .1):
        with llm_request_deadline(time.monotonic() + 300):
            assert 0 < _remaining_request_time() <= .1


@pytest.mark.parametrize("parent_event", [False, True])
def test_scheduled_commit_guard_honors_parent_budget_and_cancellation(tmp_path, monkeypatch, parent_event):
    from concurrent.futures import ThreadPoolExecutor
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.portfolio.trading_policy import TradeRejected

    cancelled = threading.Event()
    late_commit = tmp_path / "late-commit"

    class SlowAgent:
        _tools = SimpleNamespace(schemas=[])

        def run_turn(self, *_args, **_kwargs):
            time.sleep(.03)
            if parent_event:
                cancelled.set()
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(self._tools.trade_execution_guard).result()
            late_commit.touch()
            return SimpleNamespace(summary="committed", interrupted=False)

    monkeypatch.setattr("trade_compass_agent.runtime.loop.AgentLoop.from_config", lambda *_args, **_kwargs: SlowAgent())
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    with llm_request_deadline(time.monotonic() + (2 if parent_event else .01), is_cancelled=cancelled.is_set):
        with pytest.raises(TradeRejected if parent_event else TimeoutError):
            ScheduledAgentSession(config, job_id="postmarket").run("guard check", timeout=2)
    assert not late_commit.exists()


@pytest.mark.parametrize("model", ["qwen3.5-35b-a3b", "qwen3.8-27b"])
def test_deadline_interrupts_real_socket_read_without_waiting_for_read_timeout(tmp_path, model):
    released = threading.Event()
    requests = []

    class StalledHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            released.wait(2)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledHandler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    worker.start()
    client = OpenAIChatClient(model=model, api_key="lmstudio", name="lmstudio", timeout=900,
                              base_url=f"http://127.0.0.1:{server.server_port}/v1",
                              usage_db_path=tmp_path / "usage.db")
    started = time.monotonic()
    try:
        with pytest.raises(LLMExecutionCancelled):
            with llm_request_deadline(time.monotonic() + .15, is_cancelled=lambda: False):
                client.complete([ChatMessage(role="user", content="test")])
        assert time.monotonic() - started < .8
        assert requests == ["/v1/chat/completions"]
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        worker.join(1)


@pytest.mark.parametrize("finish,delta,message", [
    ("length", {"content": "unfinished"}, "输出上限"),
    ("stop", {"reasoning_content": "private reasoning"}, "最终答案"),
])
def test_incomplete_generation_cannot_be_published_as_success(monkeypatch, tmp_path, finish, delta, message):
    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=_sse(
            {"index": 0, "delta": delta, "finish_reason": finish},
        ), headers={"content-type": "text/event-stream"}))))
    client = OpenAIChatClient(model="qwen3.5-35b-a3b", api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    with pytest.raises(AgentTurnError, match=message):
        client.complete([ChatMessage(role="user", content="test")])


def test_empty_final_response_keeps_available_usage_in_failure_audit(monkeypatch, tmp_path):
    body = _sse({"index": 0, "delta": {"reasoning_content": "unfinished reasoning"}, "finish_reason": "stop"})
    usage_chunk = 'data: ' + json.dumps({"id": "test", "model": "qwen3.5-35b-a3b", "choices": [],
        "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6}}) + '\n\n'
    body = body.replace('data: [DONE]\n\n', usage_chunk + 'data: [DONE]\n\n')
    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=body,
            headers={"content-type": "text/event-stream"}))))
    client = OpenAIChatClient(model="qwen3.5-35b-a3b", api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    with pytest.raises(AgentTurnError, match="最终答案"):
        client.complete([ChatMessage(role="user", content="test")])
    with sqlite3.connect(tmp_path / "usage.db") as connection:
        assert connection.execute("select status,completion_tokens,sdk_attempts from llm_usage_calls").fetchone() == (
            "failed", 4, 1)


def test_builtin_schedules_allow_slow_models_with_matching_workflow_budgets(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    registry = JobRegistry()
    registry.from_config(config)
    for job in registry.all():
        manifest = engine.load_workflow_asset(engine.BUILTIN_WORKFLOW_DIR / job.workflow_id / "workflow.yaml")
        expected = {"autonomous_trading": 1800, "postmarket": 14400, "weekly": 10800}.get(job.id, 3600)
        assert job.timeout_seconds == manifest.timeout_seconds == expected


@pytest.mark.parametrize("text,receipts,rejected", [
    ("已完成买入000001一百股，交易已记录", [], True),
    ("执行成功：short_stock账户已完成000001买入100股", [], True),
    ("已执行模拟买入交易：000001一百股", [], True),
    ("本轮未成交，建议等待；没有调用交易工具", [], False),
    ("如果买入成功，成交后应检查trade_id", [], False),
    ("执行成功：交易风险分析完成，继续观察", [], False),
    ("已完成市场分析并评估买入条件，当前资金净买入为零", [], False),
    ("今日已成交100股", [("analyze_portfolio", '{"recent_trades":[{"trade_id":"old"}]}')], False),
    ("本轮已成交100股", [("analyze_portfolio", '{"recent_trades":[{"trade_id":"old"}]}')], True),
    ("本轮已成交100股", [("place_paper_trade", '{"status":"executed","trade_id":"new"}')], False),
    ("本轮已成交100股", [("place_paper_trade", '{"error":"rejected"}')], True),
])
def test_execution_claim_requires_current_receipt(text, receipts, rejected):
    from trade_compass_agent.runtime.verifier import unverified_execution_claim
    assert unverified_execution_claim(text, receipts) is rejected


def test_autonomous_hallucinated_fill_is_not_published_or_learned(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
    from trade_compass_agent.runtime import loop

    claim = "执行成功：short_stock账户已完成000001买入100股，交易已记录"
    def stream(*_args, on_delta=None, **_kwargs):
        if on_delta:
            on_delta(claim)
        return ChatCompletion(content=claim)
    client = SimpleNamespace(stream_complete=stream)
    monkeypatch.setattr(loop, "create_chat_client", lambda *_args: client)
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_kwargs: None)
    events = []
    monkeypatch.setattr(loop.AgentLoop, "_emit", lambda _self, event, data: events.append((event, data)))
    learned = []
    monkeypatch.setattr(loop, "curate_turn_insight", lambda **kwargs: learned.append(kwargs))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample")
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    with pytest.raises(AgentTurnError, match="缺少本轮成交回执"):
        ScheduledAgentSession(config, job_id="autonomous_trading").run("检查并执行交易", timeout=5)
    assert not learned and not (config.data_dir / "paper_trades.jsonl").exists()
    session = next((config.data_dir / "agent_sessions").glob("*.jsonl"))
    assert claim not in session.read_text()
    assert not any(event == "delta" and claim in data.get("text", "") for event, data in events)


@pytest.mark.parametrize("model", ["qwen3.5-35b-a3b", "qwen3.8-27b"])
@pytest.mark.parametrize("reasoning_finish", ["stop", "length"])
def test_reasoning_only_finish_uses_one_readonly_finalization_without_replaying_order(tmp_path, monkeypatch, model, reasoning_finish):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession
    from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.runtime.tools.registry import ToolRegistry

    requests = []
    orders = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            body = _sse({"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "order", "type": "function",
                "function": {"name": "place_paper_trade", "arguments": json.dumps({"symbol": "000001", "side": "buy",
                    "quantity": 100, "account": "short_stock", "price_source": "market_quote"})}}]}, "finish_reason": "tool_calls"})
        elif len(requests) == 2:
            body = _sse({"index": 0, "delta": {"reasoning_content": "private reasoning"}, "finish_reason": reasoning_finish})
        else:
            body = _sse(
                {"index": 0, "delta": {"reasoning_content": "private final judgment"}, "finish_reason": None},
                {"index": 0, "delta": {"content": "本轮已成交100股，成交ID actual-receipt"}, "finish_reason": "stop"},
            )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    def execute(_self, name, args):
        assert name == "place_paper_trade"
        orders.append(args)
        return json.dumps({"status": "executed", "trade_id": "actual-receipt", "symbol": "000001",
                           "quantity": 100, "price": 10, "account": "short_stock", "side": "buy"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_kwargs: None)
    monkeypatch.setattr(ToolRegistry, "_execute", execute)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="lmstudio", model=model))
    AutonomousTradingStore(config.data_dir).set_enabled(True)
    reply = ScheduledAgentSession(config, job_id="autonomous_trading").run("买入000001100股", timeout=5)
    assert len(orders) == 1 and "actual-receipt" in reply
    assert [r.url.path for r in requests] == ["/v1/chat/completions"] * 3
    finalization = json.loads(requests[-1].content)
    final_messages = json.dumps(finalization["messages"])
    assert "tools" not in finalization and "actual-receipt" in final_messages
    assert "private reasoning" not in final_messages and "private" not in reply
    with sqlite3.connect(config.data_dir / "llm_usage.db") as connection:
        assert connection.execute("select max(sdk_attempts) from llm_usage_calls").fetchone() == (1,)


def test_prejob_reflection_is_inside_budget_and_does_not_block_scheduler(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    stopped = threading.Event()

    def resolve(*_args, **_kwargs):
        try:
            while True:
                _remaining_request_time()
                time.sleep(.005)
        finally:
            stopped.set()

    reflection = SimpleNamespace(resolve_pending=resolve, get_context=lambda *_args, **_kwargs: "")
    job = JobDefinition(id="probe", name="probe", description="", schedule="", workflow_id="postmarket_archive",
                        agent_session="probe", trading_day_only=False, timeout_seconds=.08)

    async def run():
        heartbeat = asyncio.create_task(asyncio.sleep(.02, result="alive"))
        result = await JobExecutor(config, store, reflection=reflection).execute(job, trigger="cli")
        assert heartbeat.done() and await heartbeat == "alive"
        return result

    result = asyncio.run(run())
    assert result.status == "timed_out" and stopped.is_set()


@pytest.mark.parametrize("stalled", [False, True])
@pytest.mark.parametrize("summary", [False, True])
def test_job_deadline_stops_current_llm_and_prevents_memory_fallback(tmp_path, monkeypatch, stalled, summary):
    requests = []
    closed = []
    stream_closed = threading.Event()

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            if stalled:
                assert stream_closed.wait(1), "deadline must interrupt a stalled response"
                return
            for _ in range(100):
                time.sleep(.01)
                delta = {"text": " "} if summary else {"delta": {"reasoning_content": "thinking"}}
                yield _sse({"index": 0, **delta, "finish_reason": None}).replace("data: [DONE]\n\n", "").encode()

        def close(self):
            closed.append(True)
            stream_closed.set()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample",
                       llm=LLMConfig(provider="lmstudio", model="qwen3.5-35b-a3b"))

    async def maintenance(ctx):
        try:
            call = builtin_operations._summary_llm_call if summary else builtin_operations._memory_llm_call
            call(ctx.config, "JSON", "records")
        except Exception:
            (tmp_path / "incorrect-fallback").touch()
        (tmp_path / "late-write").touch()
        return StepOutput("done")

    monkeypatch.setattr(builtin_operations, "run_dreaming", maintenance)
    manifest = replace(engine.load_workflow_asset(engine.BUILTIN_WORKFLOW_DIR / "postmarket_archive/workflow.yaml"),
                       steps=(engine.WorkflowStep(id="dreaming", type="tool", uses="tool:builtin.run_dreaming",
                           with_inputs={"job_id": "probe", "as_of": "2026-10-16"}),), timeout_seconds=30)
    stack = engine.MarketStack.from_config(config)
    monkeypatch.setattr(engine, "run_workflow_asset_by_id", lambda _id, inputs, **kwargs:
                        engine.run_workflow_asset(manifest, inputs, stack=stack, **kwargs))
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    job = JobDefinition(id="probe", name="probe", description="", schedule="", workflow_id="postmarket_archive",
                        trading_day_only=False, timeout_seconds=.2)
    started = time.monotonic()
    run = asyncio.run(JobExecutor(config, store).execute(job, trigger="api"))
    assert time.monotonic() - started < .8
    assert run.status == "timed_out" and closed and len(requests) == 1
    assert not (tmp_path / "incorrect-fallback").exists()
    assert not (tmp_path / "late-write").exists()
    record = json.loads((config.data_dir / "workflow_runs" / run.id / "run.json").read_text())
    assert record["status"] == "timed_out"


def test_qwen38_vision_keeps_explicit_output_limit(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=_sse(
            {"index": 0, "delta": {"content": "图片中有三项"}, "finish_reason": "stop"},
        ), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr("trade_compass_agent.llm.providers._usage_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    client = OpenAIChatClient(model="qwen3.8-27b", api_key="lmstudio", name="lmstudio",
                              base_url="http://localhost:1234/v1", usage_db_path=tmp_path / "usage.db")
    messages = [{"role": "user", "content": [{"type": "text", "text": "数出图片中的项目"}]}]
    with llm_request_deadline(time.monotonic() + 30):
        assert client.complete_raw(messages, max_tokens=128).content == "图片中有三项"
    body = json.loads(requests[0].content)
    assert body["model"] == "qwen3.8-27b" and body["messages"] == messages
    assert body["max_tokens"] == 128 and body["presence_penalty"] == 0


@pytest.mark.parametrize("provider", ["lmstudio", "ollama"])
def test_local_provider_health_does_not_require_remote_credentials(client, tmp_path, monkeypatch, provider):
    from trade_compass_agent.config import invalidate_config_cache

    path = tmp_path / "local-health.yaml"
    path.write_text(f"llm:\n  provider: {provider}\n  model: qwen3.8-27b\n")
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    for key in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY"]:
        monkeypatch.setenv(key, "")
    (tmp_path / "data").mkdir(exist_ok=True)
    invalidate_config_cache()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["checks"]["llm"] == "ok"
    assert response.json()["status"] == "ok"
