from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trade_compass_agent.data.providers import ProviderError
from trade_compass_agent.config import AgentConfig, AppConfig, LLMConfig
from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.session import (
    SessionMessageRecord,
    SessionStore,
    derive_session_title,
)
from trade_compass_agent.runtime.tools.registry import ToolRegistry


def test_session_store_creates_directory(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "data" / "agent_sessions"
    assert not sessions_dir.exists()

    store = SessionStore(sessions_dir)
    session = store.create()

    assert sessions_dir.is_dir()
    assert (sessions_dir / f"{session.session_id}.jsonl").exists()


def test_session_store_pages_display_messages_from_latest(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create()
    for index in range(120):
        store.append(
            session,
            SessionMessageRecord(role="user", content=f"message-{index}"),
        )
        if index % 10 == 0:
            store.append(
                session,
                SessionMessageRecord(role="tool", content=f"tool-{index}"),
            )

    latest = store.load_display_page(session.session_id, limit=50)
    assert latest is not None
    assert [item.content for item in latest.messages] == [
        f"message-{index}" for index in range(70, 120)
    ]
    assert latest.start_index == 70
    assert latest.next_before == 70
    assert latest.total_messages == 120

    middle = store.load_display_page(session.session_id, limit=50, before=70)
    assert middle is not None
    assert [item.content for item in middle.messages] == [
        f"message-{index}" for index in range(20, 70)
    ]
    assert middle.start_index == 20
    assert middle.next_before == 20

    oldest = store.load_display_page(session.session_id, limit=50, before=20)
    assert oldest is not None
    assert [item.content for item in oldest.messages] == [
        f"message-{index}" for index in range(20)
    ]
    assert oldest.start_index == 0
    assert oldest.next_before is None


def test_tool_registry_returns_error_json_on_provider_failure() -> None:
    stack = MagicMock()
    stack.config.memory_dir = Path("/tmp/memory")
    stack.config.data = MagicMock(cninfo_enabled=True)
    stack.cninfo_provider.name = "failing"
    stack.cninfo_provider.get_events.side_effect = ProviderError("cninfo failed for 00519: '00519'")

    registry = ToolRegistry(stack)
    result = registry.execute("get_events", {"symbol": "00519", "limit": 5})
    payload = json.loads(result)

    assert payload.get("events") == [] or payload.get("tool") == "get_events"
    assert "error" in payload
    assert "cninfo failed" in payload["error"]


def test_scheduled_agent_session_fails_on_tool_round_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    class EmptyChatClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            return self.stream_complete(messages, tools=tools)

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            return ChatCompletion(content="", model="mock", provider="mock")

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: EmptyChatClient(),
    )

    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"
    data_dir.mkdir()
    memory_dir.mkdir()
    config = AppConfig(
        data_dir=data_dir,
        memory_dir=memory_dir,
        data_provider="sample",
        agent=AgentConfig(max_tool_rounds=1),
    )

    session = ScheduledAgentSession(config, job_id="eod_review")

    with pytest.raises(AgentUnavailableError, match="tool round limit"):
        session.run("请复盘今天表现", timeout=5)


def test_scheduled_agent_session_forces_summary_on_tool_round_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    class ForcedSummaryClient:
        name = "mock"
        model = "mock"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, *, tools=None):
            return self.stream_complete(messages, tools=tools)

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            self.calls += 1
            if self.calls == 1:
                return ChatCompletion(content="", model="mock", provider="mock")
            content = "盘后复盘结论：基于已有工具结果，今日应关注止盈纪律和风险敞口。"
            if on_delta:
                on_delta(content)
            return ChatCompletion(content=content, model="mock", provider="mock")

    chat_client = ForcedSummaryClient()
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: chat_client,
    )

    data_dir = tmp_path / "data"
    memory_dir = tmp_path / "memory"
    data_dir.mkdir()
    memory_dir.mkdir()
    config = AppConfig(
        data_dir=data_dir,
        memory_dir=memory_dir,
        data_provider="sample",
        agent=AgentConfig(max_tool_rounds=1),
    )

    session = ScheduledAgentSession(config, job_id="eod_review")

    text = session.run("请复盘今天表现", timeout=5)

    assert "盘后复盘结论" in text
    assert chat_client.calls == 2


def test_scheduled_agent_session_uses_extended_llm_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    captured_configs: list[AppConfig] = []

    class SuccessfulChatClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            return ChatCompletion(content="定时任务分析完成。", model="mock", provider="mock")

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            result = self.complete(messages, tools=tools)
            if on_delta and result.content:
                on_delta(result.content)
            return result

    def create_client(config: AppConfig):
        captured_configs.append(config)
        return SuccessfulChatClient()

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", create_client)
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        llm=LLMConfig(timeout=60.0, max_retries=2),
    )

    text = ScheduledAgentSession(config, job_id="morning_plan").run("生成计划", timeout=5)

    assert text == "定时任务分析完成。"
    assert captured_configs
    assert all(item.llm.timeout == 180.0 for item in captured_configs)
    assert config.llm.timeout == 60.0


def test_scheduler_response_prefers_substantive_pre_tool_report(tmp_path: Path) -> None:
    from trade_compass_agent.ops.agent_session import _select_scheduler_response_text

    session_file = tmp_path / "scheduler-morning_plan-agent_plan-2026-06-29.jsonl"
    full_report = (
        "## 今日交易计划\n\n"
        "1. 持仓处理：600498 清仓，002491 减半仓，其余持有。\n"
        "2. 新机会：300323、000703、300200 只在回调到支撑位后再评估。\n"
        "3. 风控：电子/通信集中度偏高，先处理风险再考虑新增仓位。\n"
        "4. 数据缺口：个股资金流向、龙虎榜、筹码数据待补齐。\n"
    ) * 3
    records = [
        {"type": "meta", "created_at": "2026-06-29T09:00:00"},
        {"role": "user", "content": "请生成 morning plan", "timestamp": "2026-06-29T09:00:00"},
        {
            "role": "assistant",
            "content": full_report,
            "timestamp": "2026-06-29T09:00:10",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "emit_signal", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "{\"ok\": true}", "timestamp": "2026-06-29T09:00:11", "name": "emit_signal"},
        {"role": "assistant", "content": "已记录交易信号。", "timestamp": "2026-06-29T09:00:12"},
    ]
    session_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    selected = _select_scheduler_response_text("已记录交易信号。", session_file)

    assert selected == full_report.strip()


def test_scheduler_response_keeps_final_summary_when_no_better_report(tmp_path: Path) -> None:
    from trade_compass_agent.ops.agent_session import _select_scheduler_response_text

    session_file = tmp_path / "scheduler-eod_review-agent_plan-2026-06-29.jsonl"
    final_summary = "## 收盘复盘\n\n今日按计划执行，风险敞口可控，明日继续关注核心持仓。" * 10
    records = [
        {"role": "user", "content": "请复盘", "timestamp": "2026-06-29T15:00:00"},
        {"role": "assistant", "content": final_summary, "timestamp": "2026-06-29T15:00:10"},
    ]
    session_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    selected = _select_scheduler_response_text(final_summary, session_file)

    assert selected == final_summary


def test_list_recent_excludes_scheduler_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    store = SessionStore(sessions_dir)
    user = store.create()
    store.append(user, SessionMessageRecord(role="user", content="hello"))

    scheduler_id = "scheduler-premarket-2026-06-15"
    (sessions_dir / f"{scheduler_id}.jsonl").write_text(
        '{"type":"meta","created_at":"2026-06-15T08:00:00"}\n'
        '{"role":"user","content":"cron","timestamp":"2026-06-15T08:00:00"}\n',
        encoding="utf-8",
    )

    listed = store.list_recent(10)
    assert len(listed) == 2

    filtered = store.list_recent(10, exclude_prefix="scheduler-")
    assert len(filtered) == 1
    assert filtered[0].session_id == user.session_id


def test_session_store_recreates_directory(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "data" / "agent_sessions"
    store = SessionStore(sessions_dir)
    session = store.create()
    session_file = sessions_dir / f"{session.session_id}.jsonl"
    session_file.unlink()
    sessions_dir.rmdir()
    store.append(session, SessionMessageRecord(role="user", content="hi"))
    assert sessions_dir.is_dir()
    assert session_file.exists()


def test_session_store_compacts_model_context_without_replacing_transcript(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    store = SessionStore(sessions_dir)
    session = store.get_or_create("channel-feishu_bot-u1")
    store.append(session, SessionMessageRecord(role="user", content="old question"))
    store.append(session, SessionMessageRecord(role="assistant", content="old answer"))

    archive = store.replace_context(
        session,
        [
            SessionMessageRecord(role="user", content="[上下文压缩 — 仅参考] summary"),
            SessionMessageRecord(role="user", content="recent question"),
        ],
    )

    transcript = store.load(session.session_id)
    assert transcript is not None
    assert [message.content for message in transcript.messages] == [
        "old question",
        "old answer",
    ]
    assert [message.content for message in store.load_context(transcript)] == [
        "[上下文压缩 — 仅参考] summary",
        "recent question",
    ]
    assert archive is not None and archive.exists()
    assert "old question" in archive.read_text(encoding="utf-8")


def test_session_store_appends_to_transcript_and_compacted_context(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "agent_sessions")
    session = store.get_or_create("channel-feishu_bot-u1")
    store.append(session, SessionMessageRecord(role="user", content="old question"))
    store.replace_context(
        session,
        [SessionMessageRecord(role="user", content="[上下文压缩 — 仅参考] summary")],
    )

    store.append(session, SessionMessageRecord(role="assistant", content="new answer"))

    transcript = store.load(session.session_id)
    assert transcript is not None
    assert [message.content for message in transcript.messages] == [
        "old question",
        "new answer",
    ]
    assert [message.content for message in store.load_context(transcript)] == [
        "[上下文压缩 — 仅参考] summary",
        "new answer",
    ]


def test_session_store_migrates_daily_channel_files_to_stable_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "agent_sessions"
    sessions_dir.mkdir()
    stable_id = "channel-feishu_bot-u1"
    (sessions_dir / f"{stable_id}.jsonl").write_text(
        '{"type":"meta","created_at":"2026-07-13T08:00:00"}\n'
        '{"role":"user","content":"before rollover"}\n',
        encoding="utf-8",
    )
    daily = sessions_dir / f"{stable_id}-2026-07-14.jsonl"
    daily.write_text(
        '{"type":"meta","created_at":"2026-07-14T08:00:00"}\n'
        '{"role":"user","content":"after rollover"}\n',
        encoding="utf-8",
    )

    session = SessionStore(sessions_dir).get_or_create(stable_id)

    assert [message.content for message in session.messages] == [
        "before rollover",
        "after rollover",
    ]
    assert not daily.exists()
    assert (sessions_dir / "archive" / "daily" / daily.name).exists()


def test_mcp_probe_survives_run_sync_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from trade_compass_agent.runtime.mcp.client import McpClientRegistry

    monkeypatch.setattr(
        "trade_compass_agent.runtime.mcp.client.merge_mcp_server_specs",
        lambda: {"bad": {"url": "http://127.0.0.1:1"}},
    )

    def boom():
        raise RuntimeError("probe thread crashed")

    monkeypatch.setattr("trade_compass_agent.runtime.mcp.client._run_sync", lambda _coro: boom())

    registry = McpClientRegistry()
    servers = registry.probe(force=True)
    assert len(servers) == 1
    assert servers[0].status == "error"
    assert "probe thread crashed" in (servers[0].error or "")


def test_agent_turn_survives_cninfo_provider_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def __init__(self) -> None:
            self._calls = 0

        def complete(self, messages, *, tools=None):
            self._calls += 1
            if self._calls == 1:
                return ChatCompletion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call_events",
                            name="get_events",
                            arguments='{"symbol":"00519","limit":5}',
                        )
                    ],
                    model="mock",
                    provider="mock",
                )
            return ChatCompletion(
                content="00519 短线暂无公告数据，请核对是否为 6 位 A 股代码。",
                model="mock",
                provider="mock",
            )

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            completion = self.complete(messages, tools=tools)
            if on_delta and not completion.tool_calls and completion.content:
                on_delta(completion.content)
            return completion

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: MockChatClient(),
    )

    response = client.post(
        "/api/agent/turn",
        json={"message": "00519 短线怎么看"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["summary"]


def _mock_chat_client() -> type:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def complete(self, messages, *, tools=None):
            return ChatCompletion(
                content="测试回复内容。",
                model="mock",
                provider="mock",
            )

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            completion = self.complete(messages, tools=tools)
            if on_delta and completion.content:
                on_delta(completion.content)
            return completion

    return MockChatClient


def test_agent_session_persists_and_loads(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _mock_chat_client()(),
    )

    turn = client.post(
        "/api/agent/turn",
        json={"message": "今天大盘资金情况"},
    )
    assert turn.status_code == 200
    session_id = turn.json()["session_id"]
    assert session_id

    detail = client.get(f"/api/agent/sessions/{session_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["session_id"] == session_id
    assert body["updated_at"]
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert "今天大盘资金情况" in body["messages"][0]["content"]
    assert body["messages"][0]["timestamp"]
    assert body["messages"][1]["role"] == "assistant"
    assert "测试回复内容" in body["messages"][1]["content"]
    assert body["messages"][1]["timestamp"]

    listed = client.get("/api/agent/sessions")
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert any(item["session_id"] == session_id for item in sessions)


def test_list_agent_sessions_hides_scheduler_sessions(
    client: TestClient,
    tmp_path: Path,
) -> None:
    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    user_session_id = created.json()["session_id"]

    sessions_dir = tmp_path / "data" / "agent_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    scheduler_id = "scheduler-premarket-2026-06-15"
    (sessions_dir / f"{scheduler_id}.jsonl").write_text(
        '{"type":"meta","created_at":"2026-06-15T08:00:00"}\n'
        '{"role":"user","content":"cron","timestamp":"2026-06-15T08:00:00"}\n',
        encoding="utf-8",
    )

    listed = client.get("/api/agent/sessions")
    assert listed.status_code == 200
    session_ids = [item["session_id"] for item in listed.json()["sessions"]]
    assert user_session_id in session_ids
    assert scheduler_id not in session_ids


def test_agent_session_load_returns_404_for_missing(
    client: TestClient,
) -> None:
    response = client.get("/api/agent/sessions/does-not-exist")
    assert response.status_code == 404


def test_post_new_session_get_returns_empty_messages(
    client: TestClient,
) -> None:
    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    body = created.json()
    session_id = body["session_id"]
    assert session_id
    assert body["updated_at"]

    detail = client.get(f"/api/agent/sessions/{session_id}")
    assert detail.status_code == 200
    loaded = detail.json()
    assert loaded["session_id"] == session_id
    assert loaded["messages"] == []
    assert loaded["updated_at"]


def test_agent_session_messages_endpoint_returns_latest_page(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "data" / "agent_sessions")
    session = store.create()
    for index in range(65):
        store.append(
            session,
            SessionMessageRecord(role="assistant", content=f"answer-{index}"),
        )

    latest = client.get(f"/api/agent/sessions/{session.session_id}/messages?limit=20")
    assert latest.status_code == 200
    body = latest.json()
    assert [item["content"] for item in body["messages"]] == [
        f"answer-{index}" for index in range(45, 65)
    ]
    assert body["page"] == {
        "start_index": 45,
        "total_messages": 65,
        "next_before": 45,
    }

    older = client.get(
        f"/api/agent/sessions/{session.session_id}/messages?limit=20&before=45"
    )
    assert older.status_code == 200
    assert [item["content"] for item in older.json()["messages"]] == [
        f"answer-{index}" for index in range(25, 45)
    ]


def test_derive_session_title_truncates_long_message() -> None:
    title = derive_session_title("这是一段很长的问题" * 10, max_len=20)
    assert len(title) <= 20
    assert title.endswith("…")


def test_agent_session_auto_title_on_first_turn(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: _mock_chat_client()(),
    )

    turn = client.post(
        "/api/agent/turn",
        json={"message": "600519 短线怎么看"},
    )
    assert turn.status_code == 200
    session_id = turn.json()["session_id"]

    detail = client.get(f"/api/agent/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "600519 短线怎么看"

    listed = client.get("/api/agent/sessions")
    item = next(s for s in listed.json()["sessions"] if s["session_id"] == session_id)
    assert item["title"] == "600519 短线怎么看"


def test_agent_session_patch_and_delete(
    client: TestClient,
) -> None:
    created = client.post("/api/agent/sessions")
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    patched = client.patch(
        f"/api/agent/sessions/{session_id}",
        json={"title": "自定义标题"},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "自定义标题"

    deleted = client.delete(f"/api/agent/sessions/{session_id}")
    assert deleted.status_code == 204

    missing = client.get(f"/api/agent/sessions/{session_id}")
    assert missing.status_code == 404


def test_agent_turn_600519_returns_200(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MockChatClient:
        name = "mock"
        model = "mock"

        def __init__(self) -> None:
            self._calls = 0

        def complete(self, messages, *, tools=None):
            self._calls += 1
            if self._calls == 1:
                return ChatCompletion(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call_bars",
                            name="get_bars",
                            arguments='{"symbol":"600519","timeframe":"1d","limit":60}',
                        ),
                        ToolCall(
                            id="call_events",
                            name="get_events",
                            arguments='{"symbol":"600519","limit":5}',
                        ),
                    ],
                    model="mock",
                    provider="mock",
                )
            return ChatCompletion(
                content="600519 贵州茅台短线观察：关注均线支撑与量能。",
                model="mock",
                provider="mock",
            )

        def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
            completion = self.complete(messages, tools=tools)
            if on_delta and not completion.tool_calls and completion.content:
                on_delta(completion.content)
            return completion

    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.create_chat_client",
        lambda config: MockChatClient(),
    )

    response = client.post(
        "/api/agent/turn",
        json={"message": "600519 短线怎么看"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "600519" in body["summary"]
