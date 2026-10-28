from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatMessage, OpenAIChatClient, llm_request_deadline
from trade_compass_agent.llm.usage import (
    LLMCallAudit,
    LLMUsageRecord,
    SqliteLLMUsageStore,
    current_llm_usage_context,
    llm_usage_context,
    mark_llm_transport_attempt,
)
from trade_compass_agent.ops.agent_session import ScheduledAgentSession
from trade_compass_agent.runtime.exceptions import AgentTurnError


def _response(*, prompt=120, completion=30, cached=40, total=150):
    return SimpleNamespace(
        id="chatcmpl-audit",
        model="deepseek-flash-actual",
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="完成",
            reasoning_content=None,
            tool_calls=None,
        ))],
    )


def _client_with_fake_api(monkeypatch, tmp_path: Path, response) -> OpenAIChatClient:
    api = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response),
        ),
    )
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: api)
    return OpenAIChatClient(
        model="deepseek-flash",
        api_key="test-key",
        name="deepseek",
        usage_db_path=tmp_path / "llm_usage.db",
    )


def test_background_deadline_bounds_each_model_request(monkeypatch, tmp_path: Path):
    observed = []
    api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: (observed.append(kwargs), _response())[1],
    )))
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: api)
    client = OpenAIChatClient(model="deepseek-flash", api_key="test-key", name="deepseek",
                              max_retries=0, usage_db_path=tmp_path / "usage.db")
    with llm_request_deadline(time.monotonic() + .5):
        client.complete([ChatMessage(role="user", content="test")])
    assert len(observed) == 1
    assert 0 < observed[0]["timeout"] <= .5
    with pytest.raises(TimeoutError):
        with llm_request_deadline(time.monotonic() - 1):
            client.complete([ChatMessage(role="user", content="test")])
    assert len(observed) == 1


def _record(store: SqliteLLMUsageStore, *, record_id: str, usage_date: str, model: str,
            job_id: str, session_id: str, tokens: tuple[int, int, int, int],
            status: str = "completed", sdk_attempts: int = 1,
            billed_amount: float | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    prompt, completion, cached, total = tokens
    store.record(LLMUsageRecord(
        id=record_id,
        usage_date=usage_date,
        started_at=now,
        finished_at=now,
        provider="deepseek",
        model=model,
        operation="chat_stream",
        job_id=job_id,
        run_id=f"run-{record_id}",
        session_id=session_id,
        attempt=1,
        sdk_attempts=sdk_attempts,
        status=status,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        total_tokens=total,
        billed_amount=billed_amount,
        billed_currency="CNY" if billed_amount is not None else "",
        cost_source="provider_reported" if billed_amount is not None else "unavailable",
        provider_request_id=f"provider-{record_id}",
        duration_ms=10,
        error_type="" if status == "completed" else "TimeoutError",
    ))


def test_persists_provider_usage_and_task_attribution_without_prompt_content(tmp_path, monkeypatch):
    client = _client_with_fake_api(monkeypatch, tmp_path, _response())

    with llm_usage_context(
        job_id="background-review",
        run_id="run-1",
        session_id="scheduler-background-review-1",
        attempt=2,
    ):
        result = client.complete([ChatMessage(role="user", content="sensitive prompt text")])

    assert result.content == "完成"
    records = SqliteLLMUsageStore(tmp_path / "llm_usage.db").recent()
    assert len(records) == 1
    record = records[0]
    assert (record.provider, record.model, record.job_id) == (
        "deepseek", "deepseek-flash-actual", "background-review",
    )
    assert (record.run_id, record.session_id, record.attempt) == (
        "run-1", "scheduler-background-review-1", 2,
    )
    assert (record.prompt_tokens, record.completion_tokens, record.cached_tokens, record.total_tokens) == (
        120, 30, 40, 150,
    )
    assert b"sensitive prompt text" not in (tmp_path / "llm_usage.db").read_bytes()


def test_counts_openai_sdk_internal_retries(tmp_path, monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(500, headers={"retry-after-ms": "0"}, json={"error": {"message": "retry"}})
        return httpx.Response(200, json={
            "id": "chatcmpl-retried",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        event_hooks={"request": [lambda _request: mark_llm_transport_attempt()]},
    )
    monkeypatch.setattr(
        "trade_compass_agent.llm.providers._usage_http_client",
        lambda: http_client,
    )
    client = OpenAIChatClient(
        model="deepseek-flash",
        api_key="test-key",
        max_retries=2,
        usage_db_path=tmp_path / "llm_usage.db",
    )

    assert client.complete([ChatMessage(role="user", content="retry")]).content == "ok"

    record = SqliteLLMUsageStore(tmp_path / "llm_usage.db").recent()[0]
    assert len(requests) == 3
    assert record.sdk_attempts == 3
    assert record.total_tokens == 12


def test_stream_usage_uses_provider_final_chunk(tmp_path, monkeypatch):
    chunks = [
        SimpleNamespace(
            id="chatcmpl-stream",
            model="deepseek-flash-actual",
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=None, content="完成"))],
        ),
        SimpleNamespace(
            id="chatcmpl-stream",
            model="deepseek-flash-actual",
            usage=SimpleNamespace(
                prompt_tokens=60,
                completion_tokens=8,
                total_tokens=68,
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            ),
            choices=[],
        ),
    ]
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return iter(chunks)

    api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: api)
    client = OpenAIChatClient(
        model="deepseek-flash",
        api_key="test-key",
        usage_db_path=tmp_path / "llm_usage.db",
    )

    result = client.stream_complete([ChatMessage(role="user", content="stream")])

    assert result.content == "完成"
    assert calls[0]["stream_options"] == {"include_usage": True}
    record = SqliteLLMUsageStore(tmp_path / "llm_usage.db").recent()[0]
    assert record.operation == "chat_stream"
    assert (record.prompt_tokens, record.completion_tokens, record.cached_tokens, record.total_tokens) == (
        60, 8, 20, 68,
    )


def test_failed_request_is_audited_when_provider_has_no_usage(tmp_path, monkeypatch):
    def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    api = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: api)
    client = OpenAIChatClient(
        model="deepseek-flash",
        api_key="test-key",
        usage_db_path=tmp_path / "llm_usage.db",
    )

    with pytest.raises(AgentTurnError, match="provider unavailable"):
        client.complete([ChatMessage(role="user", content="fail")])

    record = SqliteLLMUsageStore(tmp_path / "llm_usage.db").recent()[0]
    assert record.status == "failed"
    assert record.error_type == "RuntimeError"
    assert record.total_tokens is None


def test_concurrent_usage_writes_are_not_lost(tmp_path):
    db_path = tmp_path / "llm_usage.db"

    def write_one(index: int) -> None:
        with llm_usage_context(job_id="background-review", run_id=f"run-{index}"):
            audit = LLMCallAudit(
                db_path=db_path,
                provider="deepseek",
                model="deepseek-flash",
                operation="chat",
            )
            with audit:
                audit.mark_invocation()
            audit.finish(status="completed")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_one, range(24)))

    records = SqliteLLMUsageStore(db_path).recent(limit=100)
    assert len(records) == 24
    assert {record.run_id for record in records} == {f"run-{index}" for index in range(24)}


def test_usage_api_aggregates_by_date_model_and_job(client, tmp_path):
    store = SqliteLLMUsageStore(tmp_path / "data" / "llm_usage.db")
    _record(store, record_id="1", usage_date="2026-09-15", model="flash", job_id="background-review",
            session_id="review-1", tokens=(100, 20, 50, 120), sdk_attempts=3)
    _record(store, record_id="2", usage_date="2026-09-16", model="flash", job_id="autonomous_trading",
            session_id="trade-1", tokens=(80, 10, 0, 90), billed_amount=0.02)
    _record(store, record_id="3", usage_date="2026-09-16", model="chat", job_id="",
            session_id="interactive-1", tokens=(40, 10, 0, 50), status="failed")

    response = client.get("/api/llm/usage?date_from=2026-09-15&date_to=2026-09-16")

    assert response.status_code == 200
    body = response.json()
    assert body["total"]["request_count"] == 3
    assert body["total"]["sdk_attempt_count"] == 5
    assert body["total"]["total_tokens"] == 260
    assert body["total"]["provider_reported_costs"] == {"CNY": 0.02}
    assert {row["key"] for row in body["by_date"]} == {"2026-09-15", "2026-09-16"}
    assert {row["key"] for row in body["by_model"]} == {"chat", "flash"}
    assert {row["key"] for row in body["by_job"]} == {
        "background-review", "autonomous_trading", "interactive",
    }
    assert len(body["records"]) == 3


def test_usage_api_returns_zero_totals_before_first_model_call(client):
    response = client.get("/api/llm/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["total"]["request_count"] == 0
    assert body["total"]["total_tokens"] == 0
    assert body["by_date"] == body["by_model"] == body["by_job"] == body["records"] == []


def test_scheduled_session_binds_job_run_session_and_attempt(tmp_path, monkeypatch):
    seen = []

    def run_turn(*_args, **_kwargs):
        seen.append(current_llm_usage_context())
        return SimpleNamespace(summary="完成", interrupted=False)

    fake_agent = SimpleNamespace(
        _tools=SimpleNamespace(schemas=[], _exclude_tools=set()),
        _memory_store=SimpleNamespace(),
        _skill_store=SimpleNamespace(),
        run_turn=run_turn,
    )
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.AgentLoop.from_config",
        lambda *_args, **_kwargs: fake_agent,
    )
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    scheduled = ScheduledAgentSession(
        config,
        job_id="background-review",
        step_id="request-2",
        run_id="run-2",
        attempt=2,
    )

    assert scheduled.run("复盘", timeout=2) == "完成"
    assert len(seen) == 1
    assert seen[0] == current_llm_usage_context().__class__(
        job_id="background-review",
        run_id="run-2",
        session_id=scheduled.session_id,
        attempt=2,
    )
