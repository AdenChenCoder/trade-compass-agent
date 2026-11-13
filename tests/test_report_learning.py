"""Actual archive/search boundary with explicit composition and review doubles."""
from dataclasses import replace
import json
import time

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import LLMExecutionCancelled, llm_request_deadline
from trade_compass_agent.runtime.learning import curate_turn_insight
from trade_compass_agent.runtime.report_publication import publish_report, persist_publication
from trade_compass_agent.runtime.tools.memory import tool_search_memory
from trade_compass_agent.runtime.types import TurnResponse


RECEIPTS = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2026-11-10", "close": 22}, {"timestamp": "2026-11-11", "close": 23}]}))]


def reply(system, user):
    payload = json.loads(user)
    if "schema" in payload:
        facts = [f for f in report_facts(payload) if f["metric"] == "close"]
        return json.dumps({"schema_version": 1, "items": [
            *[{"id": f"price-{i}", "type": "fact", "section": "market", "ref": f["id"]}
              for i, f in enumerate(facts)],
            {"id": "change", "type": "calculation", "section": "market",
             "operation": "change_pct", "refs": [f["id"] for f in facts]},
        ]})
    return json.dumps({"reviews": [{"id": i["id"], "supported": True, "reason": ""}
                                  for i in payload["inferences"]],
                       "coverage": {"complete": True, "missing": []}})


def source(tmp_path, **kwargs):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    config = replace(config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True)
    publication = publish_report(task="比较两日收盘价", draft="", tool_results=RECEIPTS,
        call=reply, run_scope="source", run_id="owned-run", reader_prose=False)
    reference = {**publication.verification, **persist_publication(config.data_dir, publication)}
    return config, TurnResponse(session_id="source", turn_id="turn-1", summary="收盘价由22元升至23元。",
                                verification=reference, **kwargs)


def learn(config, response, call=reply, run_id="owned-run"):
    return curate_turn_insight(config=config, user_message="比较两日收盘价", response=response,
                               report_run_id=run_id, report_call=call)


def test_wording_changes_do_not_change_saved_facts_after_reopen(tmp_path, monkeypatch):
    config, response = source(tmp_path)
    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: pytest.fail("Report learning must use the runtime's read-only client"))
    bodies = ["收盘价由22元升至23元，涨幅4.55%。", "22元，后23元。涨了4.55%。",
              "涨幅4.55%，两日收盘价分别是22元、23元。"]
    paths, saved = [], []
    for body in bodies:
        path = learn(config, replace(response, summary=body))
        assert path
        paths.append(path)
        # Search opens the persisted index again, rather than inspecting a mock.
        hits = json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]
        saved.append(next(hit["content"] for hit in hits if hit["path"] == path))
    assert len(set(saved)) == 1 and len(set(paths)) == 3
    assert "4.55%" in saved[0] and "22元/股" in saved[0] and "23元/股" in saved[0]
    assert "2026-11-10" in saved[0] and "2026-11-11" in saved[0]
    artifacts = [json.loads(p.read_text()) for p in (config.data_dir / "report_artifacts").glob("*.json")]
    curated = [a for a in artifacts if "learning_source" in a]
    assert len(curated) == 3
    assert all(a["learning_source"]["verification"] == response.verification for a in curated)
    assert all(a["model_calls"] == 2 and a["published_text"] == saved[0] for a in curated)
    assert not (config.memory_dir / "KNOWLEDGE.md").exists()


@pytest.mark.parametrize("fault", ["missing", "tampered", "owner", "partial", "absent_reference", "no_owner"])
def test_untrusted_source_cannot_fall_back_to_prose(tmp_path, fault):
    config, response = source(tmp_path)
    path = config.data_dir / "report_artifacts" / (response.verification["artifact_id"] + ".json")
    run_id = "owned-run"
    if fault == "missing":
        path.unlink()
    elif fault == "tampered":
        path.write_text(path.read_text().replace('"22"', '"99"'))
    elif fault == "owner":
        run_id = "another-run"
    elif fault == "partial":
        response = replace(response, verification={**response.verification, "status": "partial"})
    elif fault == "absent_reference":
        response = replace(response, verification={"status": "complete"})
    else:
        run_id = ""
    assert learn(config, response, lambda *_: pytest.fail("Untrusted evidence must stop before inference"), run_id) is None
    assert not list(config.memory_dir.rglob("*.md"))


@pytest.mark.parametrize("fault", ["unknown_ref", "wrong_unit", "unsupported", "gap", "unavailable"])
def test_bad_curated_memory_is_not_indexed_even_with_valid_source(tmp_path, fault):
    config, response = source(tmp_path)
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if fault == "unavailable":
            raise TimeoutError("local review unavailable")
        if "schema" in payload:
            doc = json.loads(reply(system, user))
            if fault == "unknown_ref":
                doc["items"][0]["ref"] = "not-owned"
            elif fault == "wrong_unit":
                doc["items"] = [{"id": "plan", "type": "condition", "section": "plan",
                    "ref": report_facts(payload)[0]["id"], "comparison": "lt", "threshold": "5",
                    "unit": "%", "action": "review"}]
            elif fault == "unsupported":
                doc["items"].append({"id": "judgment", "type": "inference", "section": "assessment",
                    "text": "已执行买入。", "refs": [doc["items"][0]["ref"]]})
            elif fault == "gap":
                doc["items"] = [{"id": "gap", "type": "gap", "section": "gaps", "text": "没有可保留的研究经验。"}]
            return json.dumps(doc)
        review = json.loads(reply(system, user))
        if fault == "unsupported":
            review["reviews"] = [{"id": i["id"], "supported": False, "reason": "没有执行回执"}
                                 for i in payload["inferences"]]
        return json.dumps(review)
    assert learn(config, response, call) is None
    assert len(calls) <= 4
    assert not list(config.memory_dir.rglob("*.md"))
    assert json.loads(tool_search_memory(config.memory_dir, "002415"))["results"] == []
    attempts = [json.loads(p.read_text()) for p in (config.data_dir / "report_artifacts").glob("*.json")]
    assert any(a.get("learning_source") and a["verification"]["status"] != "complete" for a in attempts)


def test_cancelled_review_cannot_write_memory(tmp_path):
    config, response = source(tmp_path)
    stopped = False
    def call(system, user):
        nonlocal stopped
        answer = reply(system, user)
        stopped = "inferences" in json.loads(user)
        return answer
    with llm_request_deadline(time.monotonic() + 20, is_cancelled=lambda: stopped):
        with pytest.raises(LLMExecutionCancelled):
            learn(config, response, call)
    assert not list(config.memory_dir.rglob("*.md"))


def test_scheduled_turn_passes_owned_evidence_to_real_archive_boundary(tmp_path, monkeypatch):
    from trade_compass_agent.llm.providers import ChatCompletion
    from trade_compass_agent.runtime.loop import AgentLoop
    from report_contract_helpers import report_reply

    config, _ = source(tmp_path)
    class Client:
        def stream_complete(self, messages, **kwargs):
            return report_reply(messages, metrics={"close"}) or ChatCompletion(content="已比较两个观察日。")
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client())
    monkeypatch.setattr("trade_compass_agent.runtime.learning.create_llm_provider",
                        lambda **_: pytest.fail("Scheduled turn fell back to prose-only learning"))
    monkeypatch.setattr(AgentLoop, "_maybe_background_review", lambda *a, **k: None)
    agent = AgentLoop.from_config(config, memory_actor="scheduler")
    agent.initial_evidence = RECEIPTS
    agent.report_run_id = "scheduled-run"
    response = agent.run_turn("比较两个观察日", session_id="scheduler-learning-test")
    assert response.verification["status"] == "complete"
    hits = json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]
    assert len(hits) == 1 and hits[0]["scope"] == "insights"
    assert "22元/股" in hits[0]["content"] and "23元/股" in hits[0]["content"]
