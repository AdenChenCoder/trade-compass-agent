from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import replace

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.providers import ChatMessage, create_chat_client, create_llm_provider
from trade_compass_agent.runtime.exceptions import AgentUnavailableError, TurnInterruptedError
from trade_compass_agent.runtime.loop import AgentLoop


# A real child process exercises framing, callbacks, cancellation and cleanup.
# No vendor login or inference is used in these regression tests.
FAKE_CODEX = r'''
import json, os, sys, uuid
def emit(v):
    print(json.dumps(v), flush=True)
def request_tool():
    emit({'id':'tool-request','method':'item/tool/call','params':{
        'tool':os.environ.get('FAKE_TOOL','get_bars'),
        'arguments':json.loads(os.environ.get('FAKE_ARGUMENTS','{"symbol":"600519","limit":3}')),
        'callId': 'call-test' if tool_number == 1 else 'call-test-' + str(tool_number)}})
for line in sys.stdin:
    msg=json.loads(line)
    method=msg.get('method')
    if os.environ.get('FAKE_PROTOCOL'):
        with open(os.environ['FAKE_PROTOCOL'],'a') as f:
            f.write(json.dumps(msg)+'\n')
    if method == 'initialize':
        if os.environ.get('FAKE_EXPECT_PROXY'):
            assert os.environ.get('HTTPS_PROXY') == os.environ['FAKE_EXPECT_PROXY']
        assert msg['params']['clientInfo']['title'] == 'Trade Compass'
        emit({'id':msg['id'],'result':{'userAgent':'codex/0.163.0'}})
    elif method == 'config/read':
        emit({'id':msg['id'],'result':{'config':{'mcp_servers':{'unrelated':{'enabled':True}}}}})
    elif method == 'thread/start':
        params=msg['params']
        assert params['sandbox']=='read-only'
        assert params['config']['mcp_servers.unrelated.enabled'] is False
        tools=params['dynamicTools']
        thread_id=str(uuid.uuid4())
        with open('fake-thread.json','w') as f:
            json.dump({'id':thread_id,'tools':tools},f)
        emit({'id':msg['id'],'result':{'thread':{'id':thread_id}}})
    elif method == 'thread/resume':
        if os.environ.get('FAKE_MISSING_RESUME'):
            emit({'id':msg['id'],'error':{'code':-32600,'message':'no rollout found for thread id'}})
            continue
        if os.environ.get('FAKE_BAD_RESUME'):
            emit({'id':msg['id'],'error':{'code':-32600,'message':'authentication unavailable'}})
            continue
        with open('fake-thread.json') as f:
            state=json.load(f)
        assert msg['params']['threadId']==state['id']
        tools=state['tools']
        emit({'id':msg['id'],'result':{'thread':{'id':state['id']}}})
    elif method == 'turn/start':
        if os.environ.get('FAKE_RECORD'):
            with open(os.environ['FAKE_RECORD'],'a') as f:
                f.write(json.dumps(msg['params'])+'\n')
        emit({'id':msg['id'],'result':{'turn':{'id':'turn-test'}}})
        if os.environ.get('FAKE_STALL') or (tools and os.environ.get('FAKE_STALL_TOOLS')):
            continue
        if tools:
            tool_number=1
            request_tool()
            continue
        if os.environ.get('FAKE_FAIL_SUMMARY'):
            emit({'method':'turn/completed','params':{'turn':{'status':'failed'}}})
            continue
        emit({'method':'item/agentMessage/delta','params':{'delta':'原生回答'}})
        emit({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'原生回答'}}})
        emit({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
    elif msg.get('id')=='tool-request':
        assert 'result' in msg
        if tool_number < int(os.environ.get('FAKE_TOOL_COUNT','1')):
            tool_number += 1
            request_tool()
            continue
        emit({'method':'item/agentMessage/delta','params':{'delta':'基于工具结果的回答'}})
        emit({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'基于工具结果的回答'}}})
        emit({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
'''


@pytest.fixture
def native_config(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_CODEX)
    binary.chmod(0o700)
    return AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                     data_provider="sample", llm=LLMConfig(provider="codex", model="",
                     cli_path=str(binary), api_key_env="", timeout=5, agent_timeout=5))


def test_native_text_and_learning_do_not_require_api_key(native_config):
    client = create_chat_client(native_config)
    assert isinstance(client, NativeAgentClient)
    assert client.complete([ChatMessage(role="user", content="你好")]).content == "原生回答"
    provider = create_llm_provider(provider="codex", model="", enabled=True, config=native_config)
    from trade_compass_agent.llm.providers import LLMRequest
    assert provider.complete(LLMRequest(prompt="摘要", purpose="learning")).text == "原生回答"


def test_native_codex_child_uses_system_proxy_with_only_no_proxy_exported(native_config, monkeypatch):
    from trade_compass_agent.llm import native_environment

    for key in list(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            monkeypatch.delenv(key)
    monkeypatch.setattr(native_environment.sys, "platform", "darwin")
    monkeypatch.setattr(native_environment.request, "getproxies_macosx_sysconf",
                        lambda: {"https": "http://127.0.0.1:8123"}, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,.eastmoney.com")
    monkeypatch.setenv("FAKE_EXPECT_PROXY", "http://127.0.0.1:8123")
    result = create_chat_client(native_config).complete([ChatMessage(role="user", content="test")])
    assert result.content == "原生回答"
    assert "HTTPS_PROXY" not in os.environ


@pytest.mark.parametrize("value", ["http://explicit-proxy:8124", ""])
def test_native_environment_preserves_explicit_proxy_and_bypass(monkeypatch, value):
    from trade_compass_agent.llm import native_environment

    monkeypatch.setenv("https_proxy", value)
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setattr(native_environment.sys, "platform", "darwin")
    monkeypatch.setattr(native_environment.request, "getproxies_macosx_sysconf",
                        lambda: pytest.fail("Explicit proxy must prevent system fallback"), raising=False)
    env = native_environment.native_agent_environment()
    assert env["https_proxy"] == value and env["NO_PROXY"] == "localhost"


def test_native_turn_uses_business_tools_and_retains_history_after_restart(native_config, monkeypatch):
    record = native_config.data_dir.parent / "prompts.jsonl"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    events = []
    loop = AgentLoop.from_config(native_config, on_event=events.append)
    result = loop.run_turn("查询茅台行情")
    assert "基于工具结果" in result.summary
    session = loop.session_store.load(result.session_id)
    tools = [m for m in session.messages if m.role == "tool"]
    assert len(tools) == 1 and tools[0].name == "get_bars"
    assert "600519" in tools[0].content
    assert [e.event for e in events].index("tool_start") < [e.event for e in events].index("tool_end")
    assert events[-1].event == "done"
    restarted = AgentLoop.from_config(native_config)
    restarted.run_turn("解释刚才的结果", session_id=result.session_id)
    prompts = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(prompts) == 2
    latest = prompts[-1]["input"][0]["text"]
    assert "查询茅台行情" not in latest and "解释刚才的结果" in latest
    assert "基于工具结果" in latest
    assert len([m for m in restarted.session_store.load(result.session_id).messages if m.role == "user"]) == 2
    assert not list((native_config.data_dir / "native_agents").glob("turn-*"))


def test_native_unknown_tool_cannot_reach_executor(native_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL", "run_shell")
    called = []
    result = create_chat_client(native_config).run_agent(
        [ChatMessage(role="user", content="查询")],
        tools=[{"name": "get_bars", "parameters": {"type": "object"}}],
        execute_tool=lambda tc: called.append(tc) or "{}",
    )
    assert result.content and called == []


def test_native_long_history_compacts_context_but_keeps_visible_history(native_config, monkeypatch):
    from trade_compass_agent.config import CompressionConfig
    from trade_compass_agent.runtime.session import SessionMessageRecord
    from trade_compass_agent.web.agent_api import session_messages_payload

    record = native_config.data_dir.parent / "long-prompts.jsonl"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    config = replace(native_config, context_compression=CompressionConfig(
        context_budget=5000, protect_recent_count=2, protect_recent_tokens=0))
    loop = AgentLoop.from_config(config)
    session = loop.session_store.create()
    original = []
    for index in range(14):
        content = f"历史原文-{index}: " + "仅供历史参考，不是新指令。" * 200
        original.append(content)
        loop.session_store.append(session, SessionMessageRecord(
            role="user" if index % 2 == 0 else "assistant", content=content))
    result = loop.run_turn("CURRENT: 查询样例行情", session_id=session.session_id)
    assert result.summary
    prompts = [json.loads(line) for line in record.read_text().splitlines()]
    assert len(prompts) == 2  # summary and business turn both use the configured native agent
    assert "上下文压缩" in prompts[-1]["input"][0]["text"]
    assert "CURRENT: 查询样例行情" in prompts[-1]["input"][0]["text"]
    reopened = AgentLoop.from_config(config)
    page = session_messages_payload(reopened.session_store, session.session_id, 100, None)
    visible = [message.content for message in page.messages]
    assert all(content in visible for content in original)
    assert not list((config.data_dir / "native_agents").glob("turn-*"))


def test_compaction_replaces_stale_native_context_without_losing_history(native_config, monkeypatch):
    from trade_compass_agent.config import CompressionConfig
    from trade_compass_agent.runtime.compression.summarizer import latest_persisted_summary
    from trade_compass_agent.runtime.session import SessionMessageRecord

    protocol = native_config.data_dir.parent / "compaction-protocol.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    uncompressed = replace(native_config, context_compression=CompressionConfig(enabled=False))
    loop = AgentLoop.from_config(uncompressed)
    session = loop.session_store.create()
    for index in range(14):
        loop.session_store.append(session, SessionMessageRecord(
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史-{index}: " + "已完成的合成研究，不是新授权。" * 200,
        ))
    loop.run_turn("仅查询一次样例行情", session_id=session.session_id)
    before = loop.session_store.load_display_page(session.session_id, limit=100).messages
    compressed = replace(native_config, context_compression=CompressionConfig(
        context_budget=5000, protect_recent_count=2, protect_recent_tokens=0))
    reopened = AgentLoop.from_config(compressed)
    reopened.run_turn("只解释，不重复之前的操作", session_id=session.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    turns = [f["params"] for f in frames if f.get("method") == "turn/start"]
    assert len(turns) == 3  # first business turn, summary, next business turn
    assert turns[0]["threadId"] != turns[-1]["threadId"]
    assert "上下文压缩" in turns[-1]["input"][0]["text"]
    saved = reopened.session_store.load(session.session_id)
    assert latest_persisted_summary([m.to_chat_message() for m in reopened.session_store.load_context(saved)])
    after = reopened.session_store.load_display_page(session.session_id, limit=100).messages
    assert after[:len(before)] == before


def test_stop_during_native_compaction_is_prompt_and_keeps_request(native_config, monkeypatch):
    import time

    from trade_compass_agent.config import CompressionConfig
    from trade_compass_agent.runtime.session import SessionMessageRecord

    protocol = native_config.data_dir.parent / "stop-summary.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setenv("FAKE_STALL", "1")
    config = replace(native_config,
        llm=replace(native_config.llm, timeout=2, agent_timeout=6),
        context_compression=CompressionConfig(
            context_budget=5000, protect_recent_count=2, protect_recent_tokens=0))
    events = []
    loop = AgentLoop.from_config(config, on_event=events.append)
    session = loop.session_store.create()
    for index in range(14):
        loop.session_store.append(session, SessionMessageRecord(
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史-{index}: " + "仅为已处理的合成参考材料。" * 200,
        ))
    before = loop.session_store.load_display_page(session.session_id, limit=100).messages
    cancelled_at = []
    cancel = threading.Event()

    def stop_after_summary_started():
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if protocol.exists() and any(json.loads(line).get("method") == "turn/start"
                                         for line in protocol.read_text().splitlines()):
                cancelled_at.append(time.monotonic())
                cancel.set()
                return
            time.sleep(.01)

    watcher = threading.Thread(target=stop_after_summary_started, daemon=True)
    watcher.start()
    result = loop.run_turn("停止时也应保留的本轮问题", session_id=session.session_id, is_cancelled=cancel.is_set)
    watcher.join(timeout=1)
    assert cancelled_at and time.monotonic() - cancelled_at[0] < 1
    assert result.interrupted and events[-1].event == "interrupted"
    after = loop.session_store.load_display_page(session.session_id, limit=100).messages
    assert after[:len(before)] == before
    assert [m.content for m in after[len(before):]] == ["停止时也应保留的本轮问题", "[已停止]"]
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    assert sum(f.get("method") == "turn/start" for f in frames) == 1


def test_compaction_deadline_has_terminal_http_and_preserves_history(native_config, monkeypatch, client):
    from trade_compass_agent.config import CompressionConfig
    from trade_compass_agent.runtime.session import SessionMessageRecord
    from trade_compass_agent.web import agent_api

    protocol = native_config.data_dir.parent / "deadline-summary.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setenv("FAKE_STALL", "1")
    config = replace(native_config,
        llm=replace(native_config.llm, timeout=5, agent_timeout=1),
        context_compression=CompressionConfig(
            context_budget=5000, protect_recent_count=2, protect_recent_tokens=0))
    monkeypatch.setattr(agent_api, "load_app_config", lambda: config)
    loop = AgentLoop.from_config(config)
    session = loop.session_store.create()
    for index in range(14):
        loop.session_store.append(session, SessionMessageRecord(
            role="user" if index % 2 == 0 else "assistant",
            content=f"历史-{index}: " + "仅为已处理的合成参考材料。" * 200,
        ))
    before = client.get(f"/api/agent/sessions/{session.session_id}/messages").json()
    response = client.post("/api/agent/turn", json={
        "session_id": session.session_id, "message": "预算到限也应保留的本轮问题"})
    assert response.status_code == 504 and "超时" in response.json()["detail"]
    after = client.get(f"/api/agent/sessions/{session.session_id}/messages").json()
    assert after["messages"][:len(before["messages"])] == before["messages"]
    assert after["messages"][-2]["content"] == "预算到限也应保留的本轮问题"
    assert "超时" in after["messages"][-1]["content"] and not after["has_active_turn"]
    with client.stream("GET", f"/api/agent/stream?session_id={session.session_id}") as stream:
        events = [line for line in stream.iter_lines() if line.startswith("event:")]
    assert events[-1] == "event: error" and "event: done" not in events
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    assert sum(f.get("method") == "turn/start" for f in frames) == 1


def test_legacy_ready_cursor_still_resumes_matching_application_context(native_config, monkeypatch):
    from trade_compass_agent.llm.native_session import NativeConversation

    protocol = native_config.data_dir.parent / "legacy-context.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    loop = AgentLoop.from_config(native_config)
    first = loop.run_turn("查询样例行情")
    session = loop.session_store.load(first.session_id)
    cursor = NativeConversation(native_config.data_dir, first.session_id, session.messages)
    record = json.loads(cursor.path.read_text())
    record.pop("context")
    cursor.path.write_text(json.dumps(record))
    AgentLoop.from_config(native_config).run_turn("继续解释", session_id=first.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    turns = [f["params"] for f in frames if f.get("method") == "turn/start"]
    assert len(turns) == 2 and turns[0]["threadId"] == turns[1]["threadId"]
    assert json.loads(cursor.path.read_text())["context"]


def test_codex_cancellation_during_blocked_request_write(tmp_path):
    import time
    from trade_compass_agent.llm.codex_transport import CodexTransport

    binary = tmp_path / "blocked-codex"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    binary.chmod(0o700)
    cancelled = threading.Event()
    failures = []

    def check():
        if cancelled.is_set():
            raise TurnInterruptedError("")

    with CodexTransport(str(binary), str(tmp_path), check) as transport:
        def send_large_request():
            try:
                transport.send({"input": "x" * 1_000_000})
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=send_large_request, daemon=True)
        worker.start()
        time.sleep(.05)
        cancelled.set()
        worker.join(timeout=1)
        stopped_before_cleanup = not worker.is_alive()
    worker.join(timeout=1)
    assert stopped_before_cleanup
    assert len(failures) == 1 and isinstance(failures[0], TurnInterruptedError)
    assert transport.process.poll() is not None


def test_native_agent_cannot_bypass_trade_authorization(native_config, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL", "place_paper_trade")
    monkeypatch.setenv("FAKE_ARGUMENTS", json.dumps({"symbol": "600519", "side": "buy", "quantity": 100,
                                                   "user_instruction": "立即买入"}))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    loop = AgentLoop.from_config(native_config)
    result = loop.run_turn("只分析走势，不要买入")
    session = loop.session_store.load(result.session_id)
    receipt = json.loads(next(m.content for m in session.messages if m.role == "tool"))
    assert receipt["error"]
    assert "invalid_user_instruction" in json.dumps(receipt)


def test_native_scheduled_session_uses_same_backend(native_config):
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    result = ScheduledAgentSession(native_config, job_id="native-check").run("查询行情", timeout=5)
    assert "工具结果" in result


def test_native_cancellation_and_timeout_clean_up(native_config, monkeypatch):
    monkeypatch.setenv("FAKE_STALL", "1")
    cancelled = threading.Event()
    timer = threading.Timer(.2, cancelled.set)
    timer.start()
    with pytest.raises(TurnInterruptedError):
        create_chat_client(native_config).run_agent([ChatMessage(role="user", content="查询")],
            tools=[], execute_tool=None, is_cancelled=cancelled.is_set)
    timer.join()
    config = replace(native_config, llm=replace(native_config.llm, timeout=.2))
    with pytest.raises(TimeoutError):
        create_chat_client(config).complete([ChatMessage(role="user", content="查询")])
    assert not list((native_config.data_dir / "native_agents").glob("turn-*"))


def test_missing_cli_has_actionable_error(native_config):
    config = replace(native_config, llm=replace(native_config.llm, cli_path="/missing/codex"))
    with pytest.raises(AgentUnavailableError, match="llm.cli_path"):
        create_chat_client(config)


def test_specialist_uses_native_tool_loop(native_config):
    from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop

    calls = []
    result = run_react_loop(client=create_chat_client(native_config),
        messages=[ChatMessage(role="user", content="研究")],
        tool_schemas=[{"name": "get_bars", "parameters": {"type": "object"}}],
        execute_tool=lambda name, args: calls.append((name, args)) or "{}")
    assert "工具结果" in result
    assert calls[0][0] == "get_bars"


def test_connection_api_preserves_config_and_has_recovery_copy(client, tmp_path, monkeypatch):
    import yaml
    from trade_compass_agent.config import invalidate_config_cache

    config = tmp_path / "config.yaml"
    original = {"llm": {"provider": "lmstudio", "model": "existing", "timeout": 42},
                "watchlists": {"stocks": ["600519"]}}
    config.write_text(yaml.safe_dump(original))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config))
    invalidate_config_cache()
    response = client.patch("/api/agent/connection", json={"provider": "codex", "model": ""})
    assert response.status_code == 200
    assert response.json()["provider"] == "codex"
    assert next(o for o in response.json()["options"] if o["provider"] == "lmstudio")["model"] == "existing"
    assert yaml.safe_load(config.read_text())["llm"]["timeout"] == 42
    assert yaml.safe_load(config.read_text())["watchlists"] == original["watchlists"]
    assert yaml.safe_load(config.with_suffix(".llm-backup.yaml").read_text()) == original
    client.patch("/api/agent/connection", json={"provider": "lmstudio", "model": "existing"})
    assert yaml.safe_load(config.with_suffix(".llm-backup.yaml").read_text()) == original
    assert client.patch("/api/agent/connection", json={"provider": "arbitrary_command"}).status_code == 422
    invalidate_config_cache()


def test_switching_back_restores_custom_connection_without_exposing_credentials(tmp_path, monkeypatch):
    import yaml
    from trade_compass_agent.config import update_llm_connection
    from trade_compass_agent.llm.connection import connection_settings

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"llm": {"provider": "openai", "model": "custom-model",
                                            "api_key_env": "MY_EXISTING_KEY", "cli_path": ""}}))
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(config))
    monkeypatch.setenv("MY_EXISTING_KEY", "secret-not-in-settings")
    native = update_llm_connection("codex", "")
    options = connection_settings(native)
    assert "secret-not-in-settings" not in json.dumps(options)
    previous = next(o for o in options["options"] if o["provider"] == "openai")
    restored = update_llm_connection("openai", previous["model"])
    assert restored.llm.model == "custom-model" and restored.llm.api_key_env == "MY_EXISTING_KEY"


def test_connection_check_does_not_confuse_installed_and_authenticated(native_config, monkeypatch):
    from types import SimpleNamespace
    from trade_compass_agent.llm.connection import check_connection

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert check_connection(native_config)["status"] == "unauthenticated"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: SimpleNamespace(returncode=0))
    status = check_connection(native_config)
    assert status["status"] == "authenticated" and "额度将在执行任务时确认" in status["message"]


def test_native_resumes_vendor_thread_after_application_restart(native_config, monkeypatch):
    protocol = native_config.data_dir.parent / 'protocol.jsonl'
    monkeypatch.setenv('FAKE_PROTOCOL', str(protocol))
    first = AgentLoop.from_config(native_config).run_turn('查询茅台行情')
    second = AgentLoop.from_config(native_config).run_turn('解释刚才的结果', session_id=first.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    starts = [f for f in frames if f.get('method') == 'thread/start']
    resumes = [f for f in frames if f.get('method') == 'thread/resume']
    turns = [f['params'] for f in frames if f.get('method') == 'turn/start']
    assert len(starts) == len(resumes) == 1
    assert starts[0]['params']['ephemeral'] is False
    assert turns[0]['threadId'] == turns[1]['threadId'] == resumes[0]['params']['threadId']
    assert '查询茅台行情' not in turns[1]['input'][0]['text']
    assert '解释刚才的结果' in turns[1]['input'][0]['text']
    assert second.summary


def test_missing_vendor_thread_rebuilds_from_visible_history_without_replaying_turn(native_config, monkeypatch):
    protocol = native_config.data_dir.parent / 'missing.jsonl'
    monkeypatch.setenv('FAKE_PROTOCOL', str(protocol))
    first = AgentLoop.from_config(native_config).run_turn('查询茅台行情')
    monkeypatch.setenv('FAKE_MISSING_RESUME', '1')
    result = AgentLoop.from_config(native_config).run_turn('继续解释', session_id=first.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    turns = [f['params'] for f in frames if f.get('method') == 'turn/start']
    assert len(turns) == 2 and turns[0]['threadId'] != turns[1]['threadId']
    assert '查询茅台行情' in turns[-1]['input'][0]['text']
    assert 'get_bars' in turns[-1]['input'][0]['text']
    assert result.summary


def test_resume_auth_failure_is_not_hidden_by_new_thread_or_turn_retry(native_config, monkeypatch):
    protocol = native_config.data_dir.parent / 'auth.jsonl'
    monkeypatch.setenv('FAKE_PROTOCOL', str(protocol))
    first = AgentLoop.from_config(native_config).run_turn('查询茅台行情')
    monkeypatch.setenv('FAKE_BAD_RESUME', '1')
    loop = AgentLoop.from_config(native_config)
    with pytest.raises(AgentUnavailableError):
        loop.run_turn('继续解释', session_id=first.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    assert sum(f.get('method') == 'thread/start' for f in frames) == 1
    assert sum(f.get('method') == 'turn/start' for f in frames) == 1
    assert any('基于工具结果' in m.content for m in loop.session_store.load(first.session_id).messages)


@pytest.mark.parametrize('change', ['model', 'history', 'cancel'])
def test_changed_context_or_incomplete_turn_does_not_resume_stale_native_state(native_config, monkeypatch, change):
    from trade_compass_agent.runtime.session import SessionMessageRecord

    protocol = native_config.data_dir.parent / 'changed.jsonl'
    monkeypatch.setenv('FAKE_PROTOCOL', str(protocol))
    loop = AgentLoop.from_config(native_config)
    first = loop.run_turn('查询茅台行情')
    config = native_config
    if change == 'model':
        config = replace(config, llm=replace(config.llm, model='another-model'))
    elif change == 'history':
        session = loop.session_store.load(first.session_id)
        loop.session_store.append(session, SessionMessageRecord(role='assistant', content='另一个模型的回答'))
    else:
        monkeypatch.setenv('FAKE_STALL', '1')
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        timer.start()
        result = loop.run_turn('取消本轮', session_id=first.session_id, is_cancelled=cancel.is_set)
        timer.join()
        assert result.interrupted
        monkeypatch.delenv('FAKE_STALL')
    result = AgentLoop.from_config(config).run_turn('继续解释', session_id=first.session_id)
    frames = [json.loads(line) for line in protocol.read_text().splitlines()]
    turns = [f['params'] for f in frames if f.get('method') == 'turn/start']
    assert turns[0]['threadId'] != turns[-1]['threadId']
    assert result.summary


def test_native_session_rejects_concurrent_writer(native_config):
    from trade_compass_agent.llm.native_session import native_session_guard

    with native_session_guard(native_config.data_dir, 'same-session'):
        with pytest.raises(AgentUnavailableError, match='已有任务'):
            AgentLoop.from_config(native_config).run_turn('查询', session_id='same-session')


@pytest.mark.parametrize('connection_status,expected', [('authenticated','ok'), ('unauthenticated','degraded'), ('missing','degraded'), ('unknown','degraded')])
def test_health_uses_native_authentication_instead_of_api_key(client, native_config, monkeypatch, connection_status, expected):
    monkeypatch.setattr('trade_compass_agent.config.load_app_config', lambda: native_config)
    native_config.data_dir.mkdir(parents=True, exist_ok=True)
    def check(config, *, timeout):
        assert config.llm.provider == 'codex' and timeout <= 1
        return {'status': connection_status}
    monkeypatch.setattr('trade_compass_agent.llm.connection.check_connection', check)
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == expected
    assert response.json()['checks']['llm'] == ('ok' if expected == 'ok' else connection_status)
