"""Historical learning checks quality without gating delivery or direct writes."""

from dataclasses import replace
import json

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.runtime.learning import curate_turn_insight
from trade_compass_agent.runtime.report_publication import persist_publication, publish_task_report
from trade_compass_agent.runtime.tools.memory import tool_search_memory
from trade_compass_agent.runtime.types import TurnResponse

RECEIPTS = [
    (
        "get_bars",
        json.dumps(
            {
                "symbol": "002415",
                "timeframe": "1d",
                "bars": [
                    {"timestamp": "2026-11-10", "close": 22},
                    {"timestamp": "2026-11-11", "close": 23},
                ],
            }
        ),
    )
]
INSIGHT = "002415在2026-11-10收盘22元/股，2026-11-11收盘23元/股；这是历史观察，不能外推当前价格。"


def reply(system, user):
    payload = json.loads(user)
    return json.dumps(
        {"accepted": True, "reason": "忠于历史资料并保留限制。"}
        if "candidate" in payload
        else {"content": INSIGHT}
    )


def source(tmp_path, **kwargs):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    config = replace(
        config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True
    )
    publication = publish_task_report(
        task="比较两日收盘价",
        draft="历史收盘价由22元升至23元。",
        tool_results=RECEIPTS,
        run_scope="source",
        run_id="owned-run",
    )
    reference = {**publication.verification, **persist_publication(config.data_dir, publication)}
    return config, TurnResponse(
        session_id="source",
        turn_id="turn-1",
        summary=publication.text,
        verification=reference,
        **kwargs,
    )


def learn(config, response, call=reply, run_id="owned-run"):
    return curate_turn_insight(
        config=config,
        user_message="比较两日收盘价",
        response=response,
        report_run_id=run_id,
        report_call=call,
    )


def test_quality_learning_preserves_sources_and_is_searchable_after_reopen(tmp_path):
    config, response = source(tmp_path)
    original = {p: p.read_bytes() for p in (config.data_dir / "report_artifacts").glob("*.json")}
    path = learn(config, response)
    assert path
    for _ in range(2):
        hits = json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]
        assert len(hits) == 1 and hits[0]["content"].strip().endswith(INSIGHT)
    assert all(p.read_bytes() == content for p, content in original.items())
    assert not (config.memory_dir / "KNOWLEDGE.md").exists()
    review = json.loads(next((config.data_dir / "learning_reviews").glob("*.json")).read_text())
    assert (
        review["status"] == "retained"
        and review["source_artifact"] == response.verification["artifact_id"]
    )


@pytest.mark.parametrize("fault", ["missing", "tampered", "owner", "absent_reference", "no_owner"])
def test_untrusted_source_cannot_fall_back_to_prose(tmp_path, fault):
    config, response = source(tmp_path)
    path = config.data_dir / "report_artifacts" / (response.verification["artifact_id"] + ".json")
    run_id = "owned-run"
    if fault == "missing":
        path.unlink()
    elif fault == "tampered":
        path.write_text(path.read_text() + " ")
    elif fault == "owner":
        run_id = "other-run"
    elif fault == "absent_reference":
        response = replace(response, verification={})
    else:
        run_id = ""
    assert (
        learn(
            config, response, lambda *_: pytest.fail("Untrusted history cannot be learned"), run_id
        )
        is None
    )
    assert not list(config.memory_dir.rglob("*.md"))


@pytest.mark.parametrize("degraded", [False, True])
def test_historical_quality_review_does_not_require_a_successful_market_report(tmp_path, degraded):
    config, response = source(tmp_path, degraded=degraded)
    calls = []

    def model(system, user):
        calls.append(json.loads(user))
        return reply(system, user)

    assert learn(config, response, model)
    assert len(calls) == 2
    assert calls[0]["history"]["tool_records"] == [list(r) for r in RECEIPTS]


def test_scheduled_turn_learns_only_after_the_answer_is_saved(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion
    from trade_compass_agent.runtime.loop import AgentLoop

    config, _ = source(tmp_path)

    class Client:
        def stream_complete(self, messages, **kwargs):
            try:
                payload = json.loads(messages[-1].content)
            except (ValueError, TypeError):
                payload = {}
            if "history" in payload:
                store = AgentLoop.from_config(config).session_store.load("scheduler-learning-test")
                assert store.messages[-1].content == "已比较两个观察日。"
                return ChatCompletion(content=reply("", messages[-1].content))
            return ChatCompletion(content="已比较两个观察日。")

    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client())
    monkeypatch.setattr(
        "trade_compass_agent.runtime.loop.shared_consolidation_worker", lambda **_: None
    )
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **k: None)
    monkeypatch.setattr(
        "trade_compass_agent.llm.providers.create_local_report_fact_check",
        lambda *a, **k: pytest.fail(
            "Historical quality learning does not create a factual reviewer"
        ),
    )
    agent = AgentLoop.from_config(config, memory_actor="scheduler")
    agent.initial_evidence = RECEIPTS
    agent.report_run_id = "scheduled-run"
    response = agent.run_turn("比较两个观察日", session_id="scheduler-learning-test")
    assert response.verification["status"] == "not_reviewed"
    hits = json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]
    assert len(hits) == 1 and hits[0]["content"].strip().endswith(INSIGHT)
