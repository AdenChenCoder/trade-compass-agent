from dataclasses import replace
import subprocess
import sys

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatCompletion
from trade_compass_agent.runtime.background_review import ReviewNudgeTracker
from trade_compass_agent.runtime.market_stack import MarketStack


class _ReplyClient:
    name = "mock"
    model = "mock"

    def stream_complete(self, messages, *, tools=None, on_delta=None, is_cancelled=None):
        text = "本轮只核对合成笔记。"
        if on_delta:
            on_delta(text)
        return ChatCompletion(content=text)

    complete = stream_complete


def test_web_review_counts_each_session_across_new_runtime_instances(client, tmp_path, monkeypatch):
    config = replace(AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"),
                     allow_external_llm_memory=True)
    stack = MarketStack.from_config(config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: _ReplyClient())
    started = []
    monkeypatch.setattr("trade_compass_agent.runtime.loop.spawn_background_review", lambda **kw: started.append(kw))

    def turn(sid):
        response = client.post("/api/agent/turn", json={"session_id": sid, "message": "只核对，不保存。"})
        assert response.status_code == 200, response.text

    for _ in range(9):
        turn("first")
        turn("second")
    assert not started
    turn("first")
    assert len(started) == 1 and started[0]["source_session_id"] == "first"
    assert started[0]["review_memory"] and not started[0]["review_skills"]
    turn("first")
    assert len(started) == 1  # A successfully queued review resets only its counter.
    turn("second")
    assert len(started) == 2 and started[1]["source_session_id"] == "second"


def test_review_budget_survives_process_restart_and_does_not_cross_sessions(tmp_path):
    tracker = ReviewNudgeTracker()
    tracker.bind_session(tmp_path, "first")
    for _ in range(9):
        tracker.on_user_turn()
    for _ in range(14):
        tracker.on_tool_iteration()
    code = """
from pathlib import Path
from trade_compass_agent.runtime.background_review import ReviewNudgeTracker
import sys
t = ReviewNudgeTracker()
t.bind_session(Path(sys.argv[1]), 'first')
assert t.should_review() == (False, False)
t.on_user_turn()
t.on_tool_iteration()
assert t.should_review() == (True, True)
t.bind_session(Path(sys.argv[1]), 'second')
assert t.should_review() == (False, False)
"""
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=20)
    assert tracker.should_review() == (True, True)


def test_failed_review_dispatch_preserves_due_checkpoint(client, tmp_path, monkeypatch):
    config = replace(AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory"),
                     allow_external_llm_memory=True)
    stack = MarketStack.from_config(config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: _ReplyClient())
    started = []

    def dispatch(**kw):
        started.append(kw)
        if len(started) == 1:
            raise OSError("request storage unavailable")

    monkeypatch.setattr("trade_compass_agent.runtime.loop.spawn_background_review", dispatch)
    for _ in range(11):
        response = client.post("/api/agent/turn", json={"session_id": "one", "message": "只核对。"})
        assert response.status_code == 200
    assert len(started) == 2
