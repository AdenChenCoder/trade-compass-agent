from __future__ import annotations

import json
import sqlite3
import sys
from collections import deque

import pytest

from trade_compass_agent.config import AppConfig, LLMConfig
from trade_compass_agent.llm.native import NativeAgentClient
from trade_compass_agent.llm.native_session import NativeConversation
from trade_compass_agent.llm.providers import ChatMessage


def total(input_tokens, output_tokens, cached_tokens=0):
    return {"inputTokens": input_tokens, "outputTokens": output_tokens,
            "cachedInputTokens": cached_tokens, "totalTokens": input_tokens + output_tokens}


@pytest.fixture
def usage_runner(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory",
                       data_provider="sample", llm=LLMConfig(provider="codex", cli_path=sys.executable))
    state = {"number": 0, "snapshots": [], "last": None}
    history = []

    class Transport:
        def __init__(self, binary, cwd, check):
            self.events = deque()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, *args, **kwargs):
            pass

        def usage(self, turn_id, value, thread_id="thread-test"):
            return {"method": "thread/tokenUsage/updated", "params": {
                "threadId": thread_id, "turnId": turn_id, "tokenUsage": {"total": value}}}

        def call(self, method, params):
            if method in ("initialize", "config/read"):
                return {}
            if method == "thread/resume" and state["last"] is not None:
                self.events.append(self.usage(f"turn-{state['number']}", state["last"]))
            if method in ("thread/start", "thread/resume"):
                return {"thread": {"id": "thread-test"}}
            assert method == "turn/start"
            state["number"] += 1
            turn_id = f"turn-{state['number']}"
            for snapshot in state["snapshots"]:
                self.events.append(self.usage(turn_id, snapshot))
                state["last"] = snapshot
            self.events.append(self.usage(turn_id, total(9000, 1000), "unrelated-thread"))
            self.events.extend([
                {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "OK"}}},
                {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            ])
            return {"turn": {"id": turn_id}}

        def receive(self):
            return self.events.popleft()

    monkeypatch.setattr("trade_compass_agent.llm.codex_transport.CodexTransport", Transport)

    def run(snapshots):
        state["snapshots"] = snapshots
        conversation = NativeConversation(config.data_dir, "usage-session", history)
        user = ChatMessage(role="user", content="测试")
        response = NativeAgentClient(config).run_agent(
            [*history, user], tools=[], execute_tool=None, conversation=conversation)
        history.extend([user, ChatMessage(role="assistant", content=response.content)])
        conversation.commit(history)
        with sqlite3.connect(config.data_dir / "llm_usage.db") as db:
            rows = db.execute("SELECT prompt_tokens, completion_tokens, cached_tokens, total_tokens "
                              "FROM llm_usage_calls ORDER BY started_at").fetchall()
        return conversation, rows

    return run


def test_resumed_usage_counts_only_current_turn_and_deduplicates_updates(usage_runner):
    _, rows = usage_runner([total(80, 20, 10)])
    assert rows == [(80, 20, 10, 100)]
    cursor, rows = usage_runner([total(160, 40, 30), total(240, 60, 50), total(240, 60, 50)])
    assert rows == [(80, 20, 10, 100), (160, 40, 40, 200)]
    assert sum(row[3] for row in rows) == 300
    assert json.loads(cursor.path.read_text())["usage_total"] == total(240, 60, 50)


def test_legacy_cursor_is_unknown_once_then_establishes_a_reliable_baseline(usage_runner):
    cursor, _ = usage_runner([total(80, 20)])
    record = json.loads(cursor.path.read_text())
    del record["usage_total"]
    cursor.path.write_text(json.dumps(record))
    _, rows = usage_runner([total(160, 40)])
    assert rows[-1] == (None, None, None, None)
    _, rows = usage_runner([total(240, 60)])
    assert rows[-1] == (80, 20, 0, 100)


def test_resume_replayed_usage_does_not_replace_missing_current_usage(usage_runner):
    usage_runner([total(80, 20)])
    cursor, rows = usage_runner([])
    assert rows[-1] == (None, None, None, None)
    assert "usage_total" not in json.loads(cursor.path.read_text())


@pytest.mark.parametrize("snapshot", [total(8, 2), {"totalTokens": 100}, total(True, 20), None])
def test_invalid_or_decreasing_cumulative_usage_is_unknown(usage_runner, snapshot):
    usage_runner([total(80, 20)])
    _, rows = usage_runner([snapshot])
    assert rows[-1] == (None, None, None, None)


def test_usage_baseline_is_not_published_before_transcript_commit_or_after_abandon(tmp_path):
    conversation = NativeConversation(tmp_path, "session", [])
    conversation.begin("thread", "profile")
    conversation.set_usage_total(total(80, 20))
    assert "usage_total" not in json.loads(conversation.path.read_text())
    conversation.abandon()
    conversation.commit([])
    record = json.loads(conversation.path.read_text())
    assert record["status"] == "pending" and "usage_total" not in record
