from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time
import threading

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import LLMExecutionCancelled, llm_request_deadline
from trade_compass_agent.runtime.learning import compact_memory_summary, curate_turn_insight
from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory
from trade_compass_agent.runtime.types import TurnResponse


def _config(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    return replace(config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True)


def _learn(config, kind):
    if kind == "insights":
        return curate_turn_insight(
            config=config, user_message="记住研究偏好",
            response=TurnResponse(session_id="test", summary="风险偏好需要持续复核。", sections=[]),
        )
    return compact_memory_summary(config)


@pytest.mark.parametrize("kind", ["insights", "compaction"])
def test_learning_archive_is_searchable_beside_existing_hits_after_reopen(tmp_path, monkeypatch, kind):
    config = _config(tmp_path)
    old = json.loads(tool_write_memory(config.memory_dir, "notes", "风险偏好：旧的研究备注。"))
    summary = "风险偏好：紫杉方案只讨论用户明确授权的研究范围。"
    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: SimpleNamespace(name="test", complete=lambda _: SimpleNamespace(text=summary)))

    path = Path(_learn(config, kind))

    # This public tool opens the index again. An old FTS hit must not hide the
    # newly generated archive by preventing the filesystem fallback.
    for _ in range(2):
        hits = json.loads(tool_search_memory(config.memory_dir, "风险偏好"))["results"]
        assert {old["path"], str(path)} <= {hit["path"] for hit in hits}
        assert any(hit["scope"] == kind and hit["content"] == summary for hit in hits)
    assert not (config.memory_dir / "KNOWLEDGE.md").exists()


@pytest.mark.parametrize("kind", ["insights", "compaction"])
@pytest.mark.parametrize("termination", ["cancel", "deadline"])
def test_late_learning_response_cannot_publish_archive(tmp_path, monkeypatch, kind, termination):
    config = _config(tmp_path)
    tool_write_memory(config.memory_dir, "notes", "风险偏好：保留已有的研究备注。")
    before = set(config.memory_dir.rglob("*.md"))
    stopped = False
    clock = [time.monotonic()]
    monkeypatch.setattr("trade_compass_agent.llm.providers.time.monotonic", lambda: clock[0])

    def complete(_):
        nonlocal stopped
        stopped = True
        clock[0] += 2
        return SimpleNamespace(text="风险偏好：不应在任务终止后写入的新内容。")

    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: SimpleNamespace(name="test", complete=complete))
    cancelled = (lambda: stopped) if termination == "cancel" else None
    with llm_request_deadline(clock[0] + 1, is_cancelled=cancelled):
        with pytest.raises(LLMExecutionCancelled if termination == "cancel" else TimeoutError):
            _learn(config, kind)
    assert set(config.memory_dir.rglob("*.md")) == before
    assert not any("不应" in hit["content"] for hit in
                   json.loads(tool_search_memory(config.memory_dir, "风险偏好"))["results"])


def test_compaction_network_failure_can_archive_existing_content(tmp_path, monkeypatch):
    config = _config(tmp_path)
    tool_write_memory(config.memory_dir, "notes", "风险偏好：保留已有的研究备注。")

    def complete(_):
        raise TimeoutError("provider connection timed out")

    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: SimpleNamespace(name="test", complete=complete))
    with llm_request_deadline(time.monotonic() + 30):
        path = compact_memory_summary(config)
    assert str(path) in {hit["path"] for hit in
                         json.loads(tool_search_memory(config.memory_dir, "风险偏好"))["results"]}


@pytest.mark.parametrize("backend", ["api", "native"])
def test_stop_during_learning_preserves_one_published_answer(client, tmp_path, monkeypatch, backend):
    from trade_compass_agent.llm.native import NativeAgentClient
    from trade_compass_agent.llm.providers import ChatCompletion, check_execution_active
    from trade_compass_agent.runtime.market_stack import MarketStack

    config = _config(tmp_path)
    if backend == "native":
        config = replace(config, llm=replace(config.llm, provider="codex"))
    stack = MarketStack.from_config(config)
    monkeypatch.setattr("trade_compass_agent.web.agent_api._get_cached_stack", lambda: (config, stack))
    summary = "已完成本轮研究，保留这个用户可见结论。"

    class Reply:
        def stream_complete(self, *a, on_delta=None, **kw):
            if on_delta:
                on_delta(summary)
            return ChatCompletion(content=summary)

    class NativeReply(NativeAgentClient):
        def __init__(self):
            pass

        def run_agent(self, *a, on_delta=None, **kw):
            return Reply().stream_complete(on_delta=on_delta)

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client",
                        lambda _: NativeReply() if backend == "native" else Reply())
    preparing = threading.Event()
    cleanup = threading.Event()

    def curate(_):
        preparing.set()
        while not cleanup.wait(.01):
            check_execution_active()
        raise RuntimeError("test cleanup")

    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: SimpleNamespace(name="test", complete=curate))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.spawn_background_review",
                        lambda **kw: pytest.fail("Stopped turn must not start another review"))
    responses = []
    errors = []

    def turn():
        try:
            responses.append(client.post("/api/agent/turn", json={"session_id": "learning-stop", "message": "检查。"}))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=turn, daemon=True)
    worker.start()
    try:
        assert preparing.wait(5)
        stopped = client.post("/api/agent/control", json={"session_id": "learning-stop", "action": "interrupt"})
        assert stopped.status_code == 200 and stopped.json()["ok"]
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert responses[0].status_code == 200
        assert responses[0].json()["interrupted"] is True
        assert responses[0].json()["summary"] == summary
        visible = client.get("/api/agent/sessions/learning-stop/messages").json()
        answers = [m["content"] for m in visible["messages"] if m["role"] == "assistant"]
        assert answers == [summary]
        assert not list((config.memory_dir / "tree" / "insights").glob("*.md"))
    finally:
        cleanup.set()
        worker.join(5)
