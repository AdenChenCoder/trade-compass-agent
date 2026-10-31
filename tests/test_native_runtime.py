from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.connection import check_connection
from trade_compass_agent.llm.codex_transport import CodexTransport
from trade_compass_agent.llm import native_runtime
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.native_runtime import prepare_native_runtime
from trade_compass_agent.llm.native_session import NativeConversation, fingerprint
from trade_compass_agent.llm.providers import ChatMessage
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.loop import AgentLoop
from trade_compass_agent.portability import create_portable_export, import_portable_export
from trade_compass_agent.recovery import RecoveryLayout, create_backup, restore_backup
from test_native_agents import FAKE_CODEX


def test_agent_continuation_uses_private_runtime_without_inheriting_desktop_identity(tmp_path, monkeypatch):
    source = native_runtime.host_codex_home()
    source.mkdir()
    (source / "auth.json").write_text('{"fixture": "original"}')
    (source / "config.toml").write_text('model = "fixture-model"\n')
    probes = tmp_path / "runtime-probe.jsonl"
    code = FAKE_CODEX.replace("assert msg['params']['clientInfo']['title'] == 'Trade Compass'", """
        with open(os.environ['RUNTIME_PROBE'],'a') as f:
            f.write(json.dumps({'runtime': os.environ.get('CODEX_HOME'),
                               'originator': os.environ.get('CODEX_INTERNAL_ORIGINATOR_OVERRIDE'),
                               'parent': os.environ.get('CODEX_THREAD_ID'),
                               'sqlite_override': os.environ.get('CODEX_SQLITE_HOME'),
                               'args':sys.argv})+'\\n')
        """)
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + code)
    binary.chmod(0o700)
    monkeypatch.setenv("RUNTIME_PROBE", str(probes))
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-fixture")
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(source / "desktop-db"))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **kw: None)
    config = AppConfig(data_dir=tmp_path / "data-a", memory_dir=tmp_path / "memory-a", data_provider="sample",
                       llm=LLMConfig(provider="codex", cli_path=str(binary), timeout=5, agent_timeout=5))
    first = AgentLoop.from_config(config).run_turn("查询合成日线")
    reopened = AgentLoop.from_config(config)
    cursor = NativeConversation(config.data_dir, first.session_id, [])
    old_id = cursor.record["thread_id"]
    reopened.run_turn("继续解释", session_id=first.session_id)
    assert NativeConversation(config.data_dir, first.session_id, []).record["thread_id"] == old_id
    other = replace(config, data_dir=tmp_path / "data-b", memory_dir=tmp_path / "memory-b")
    AgentLoop.from_config(other).run_turn("独立会话")
    rows = [json.loads(line) for line in probes.read_text().splitlines()]
    assert len(rows) == 3 and rows[0]["runtime"] == rows[1]["runtime"] != rows[2]["runtime"]
    for row in rows:
        root = Path(row["runtime"])
        assert root != source and (root / "auth.json").is_symlink()
        assert (root / "auth.json").resolve() == (source / "auth.json").resolve()
        assert not row["originator"] and not row["parent"] and not row["sqlite_override"]
        assert f'sqlite_home={json.dumps(str(root))}' in row["args"]
    assert (source / "auth.json").read_text() == '{"fixture": "original"}'
    assert os.environ["CODEX_THREAD_ID"] == "parent-fixture"
    assert len(reopened.session_store.load(first.session_id).messages) >= 4


def test_credential_refresh_replacement_and_logout_remain_shared_without_copying(tmp_path):
    source = native_runtime.host_codex_home()
    source.mkdir()
    original = source / "auth.json"
    original.write_text("fixture-before")
    runtime = prepare_native_runtime(tmp_path / "data")
    (runtime / "auth.json").write_text("fixture-refreshed")
    assert original.read_text() == "fixture-refreshed"
    replacement = source / "new-auth.json"
    replacement.write_text("fixture-new-login")
    replacement.replace(original)
    assert (runtime / "auth.json").read_text() == "fixture-new-login"
    original.unlink()
    assert not (runtime / "auth.json").exists()


def test_concurrent_runtime_preparation_keeps_one_credential_reference(tmp_path):
    source = native_runtime.host_codex_home()
    source.mkdir()
    (source / "auth.json").write_text("fixture")
    with ThreadPoolExecutor(max_workers=6) as pool:
        roots = list(pool.map(lambda _: prepare_native_runtime(tmp_path / "data"), range(12)))
    assert len(set(roots)) == 1
    assert (roots[0] / "auth.json").resolve() == source / "auth.json"
    assert (source / "auth.json").read_text() == "fixture"


def test_pre_isolation_cursor_resumes_same_native_thread(tmp_path, monkeypatch):
    legacy_config = {"mcp_servers": {"unrelated": {"enabled": True}}, "sqlite_home": None, "log_dir": None}
    binary = tmp_path / "codex"
    code = FAKE_CODEX.replace("{'mcp_servers':{'unrelated':{'enabled':True}}}",
                             "{'mcp_servers':{'unrelated':{'enabled':True}},"
                             "'sqlite_home':os.environ['CODEX_HOME'],'log_dir':os.environ['CODEX_HOME']+'/log'}")
    binary.write_text(f"#!{sys.executable}\n" + code)
    binary.chmod(0o700)
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="codex", model="", cli_path=str(binary), timeout=5))
    conversation = NativeConversation(config.data_dir, "legacy", [])
    thread_id = str(uuid.uuid4())
    conversation.record = {"status": "ready", "thread_id": thread_id,
                           "history": conversation.history_digest, "context": conversation.context_digest,
                           "profile": fingerprint([str(binary.resolve()), binary.stat().st_mtime_ns,
                                                   "", {}, legacy_config])}
    (conversation.cwd / "fake-thread.json").write_text(json.dumps({"id": thread_id, "tools": []}))
    protocol = tmp_path / "protocol.jsonl"
    monkeypatch.setenv("FAKE_PROTOCOL", str(protocol))
    result = NativeAgentClient(config).run_agent([ChatMessage(role="user", content="继续")],
                                                tools=[], execute_tool=None, conversation=conversation)
    events = [json.loads(line) for line in protocol.read_text().splitlines()]
    assert result.content == "原生回答" and conversation.pending["thread_id"] == thread_id
    assert any(event.get("method") == "thread/resume" for event in events)
    assert not any(event.get("method") == "thread/start" for event in events)


def test_expiry_before_native_request_is_timeout_not_transport_failure(tmp_path):
    binary = tmp_path / "blocked-codex"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    binary.chmod(0o700)

    def expired():
        raise TimeoutError("request budget exhausted")

    with CodexTransport(str(binary), str(tmp_path), expired) as rpc:
        with pytest.raises(TimeoutError, match="request budget exhausted"):
            rpc.send({"id": 1, "method": "initialize", "params": {}})


@pytest.mark.parametrize("archived", [False, True])
def test_legacy_thread_copy_preserves_source_and_is_idempotent(tmp_path, archived):
    source = native_runtime.host_codex_home()
    conversation = NativeConversation(tmp_path / "data", "business-session", [])
    thread_id = str(uuid.uuid4())
    conversation.record = {"thread_id": thread_id, "status": "ready"}
    legacy = source / ("archived_sessions" if archived else "sessions") / f"rollout-2026-10-31-{thread_id}.jsonl"
    legacy.parent.mkdir(parents=True)
    original = json.dumps({"type": "session_meta", "payload": {"id": thread_id, "cwd": str(conversation.cwd)}}) + "\n"
    legacy.write_text(original)
    runtime = prepare_native_runtime(tmp_path / "data", conversation)
    copied = next((runtime / "sessions").rglob("*.jsonl"))
    assert copied.read_text() == original and copied.stat().st_mode & 0o777 == 0o600
    copied.write_text(original + '{"fixture": "continued"}\n')
    prepare_native_runtime(tmp_path / "data", conversation)
    assert "continued" in copied.read_text() and legacy.read_text() == original


def test_legacy_thread_from_another_application_session_is_not_imported(tmp_path):
    source = native_runtime.host_codex_home()
    conversation = NativeConversation(tmp_path / "data", "business-session", [])
    thread_id = str(uuid.uuid4())
    conversation.record = {"thread_id": thread_id}
    legacy = source / "sessions" / f"rollout-2026-10-31-{thread_id}.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"payload": {"id": thread_id, "cwd": str(tmp_path / "unrelated")}}))
    with pytest.raises(AgentUnavailableError, match="归属不匹配"):
        prepare_native_runtime(tmp_path / "data", conversation)
    assert not list((tmp_path / "data/native_agents/codex-runtime/sessions").glob("**/*.jsonl"))


def test_lost_private_rollout_does_not_reimport_stale_host_history(tmp_path):
    source = native_runtime.host_codex_home()
    conversation = NativeConversation(tmp_path / "data", "business-session", [])
    thread_id = str(uuid.uuid4())
    conversation.record = {"thread_id": thread_id, "status": "ready"}
    legacy = source / "sessions" / f"rollout-2026-10-31-{thread_id}.jsonl"
    legacy.parent.mkdir(parents=True)
    original = json.dumps({"payload": {"id": thread_id, "cwd": str(conversation.cwd)}})
    legacy.write_text(original)
    runtime = prepare_native_runtime(tmp_path / "data", conversation)
    copied = next((runtime / "sessions").rglob("*.jsonl"))
    conversation.begin(thread_id, "profile")
    conversation.commit([])
    copied.unlink()
    reopened = NativeConversation(tmp_path / "data", "business-session", [])
    prepare_native_runtime(tmp_path / "data", reopened)
    assert reopened.record["runtime_isolated"] is True
    assert not copied.exists() and legacy.read_text() == original


@pytest.mark.parametrize("store", ["keyring", "auto", "ephemeral"])
def test_non_file_auth_policy_is_reported_without_silent_downgrade(tmp_path, store):
    source = native_runtime.host_codex_home()
    source.mkdir()
    (source / "config.toml").write_text(f'cli_auth_credentials_store = "{store}"\n')
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       llm=LLMConfig(provider="codex", cli_path=sys.executable))
    status = check_connection(config)
    assert status["status"] == "unsupported" and "凭据存储" in status["message"]
    assert not (source / "auth.json").exists()


@pytest.mark.parametrize("portable", [False, True])
def test_archive_roundtrip_keeps_native_history_without_external_auth_references(tmp_path, portable):
    source = native_runtime.host_codex_home()
    source.mkdir()
    (source / "auth.json").write_text("credential-fixture-never-export")
    config = tmp_path / "config.yaml"
    config.write_text("profile: local\n")
    layout = RecoveryLayout(config_path=config, env_path=tmp_path / ".env", mcp_path=tmp_path / "mcp.json",
                            data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", backup_dir=tmp_path / "backups")
    runtime = prepare_native_runtime(layout.data_dir)
    history = runtime / "sessions/rollout.jsonl"
    history.parent.mkdir()
    history.write_text("original native history")
    (layout.data_dir / "agent_sessions.jsonl").write_text("application history")
    temp = runtime / "tmp/worker"
    temp.mkdir(parents=True)
    (temp / "helper").symlink_to(tmp_path / "missing-cli")
    create, restore = (create_portable_export, import_portable_export) if portable else (create_backup, restore_backup)
    saved = create(layout=layout)
    with zipfile.ZipFile(saved.path) as archive:
        names = archive.namelist()
        assert "data/native_agents/codex-runtime/sessions/rollout.jsonl" in names
        assert "data/agent_sessions.jsonl" in names
        assert not any(name.endswith(("auth.json", "config.toml", "tmp/worker/helper")) for name in names)
    history.write_text("new native history")
    preview = restore(saved.path, layout=layout)
    assert preview.recovery_backup is None and history.read_text() == "new native history"
    restored = restore(saved.path, layout=layout, force=True)
    assert history.read_text() == "original native history" and restored.recovery_backup
    assert (runtime / "auth.json").is_symlink()
    assert (source / "auth.json").read_text() == "credential-fixture-never-export"
