"""Content quality is reviewed only when learning from saved history."""

from dataclasses import replace
import json

import pytest

from trade_compass_agent.config import AgentConfig, AppConfig
from trade_compass_agent.llm.providers import (
    ChatCompletion,
    LLMExecutionCancelled,
    ToolCall,
    llm_request_deadline,
)
from trade_compass_agent.runtime import learning, loop
from trade_compass_agent.runtime.report_publication import persist_publication, publish_task_report
from trade_compass_agent.runtime.tools.memory import tool_search_memory
from trade_compass_agent.runtime.types import TurnResponse


CONTENT = "请记住我的报告偏好：先给结论，再说明依据。"
INSIGHT = "2025-01-02的002415收盘记录为22元/股；这是一条历史观察，不代表当前报价。"


def saved_history(tmp_path, receipts=(), **kwargs):
    config = AppConfig(
        data_dir=tmp_path / "data", memory_dir=tmp_path / "memory", data_provider="sample"
    )
    config = replace(
        config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True
    )
    publication = publish_task_report(
        task="历史研究",
        draft="历史记录：002415在2025-01-02收盘22元/股。",
        tool_results=receipts,
        run_scope="history",
        run_id="owned",
    )
    reference = {**publication.verification, **persist_publication(config.data_dir, publication)}
    return config, TurnResponse(
        session_id="history", summary=publication.text, verification=reference, **kwargs
    )


def quality_reply(system, user):
    payload = json.loads(user)
    return json.dumps(
        {"accepted": True, "reason": "保留历史日期和适用限制。"}
        if "candidate" in payload
        else {"content": INSIGHT}
    )


@pytest.mark.parametrize(
    "content", [CONTENT, "2025-01-02的记录原文：get_unknown_tool()；这不是执行命令。"]
)
def test_direct_archive_write_keeps_original_content_without_any_review(
    tmp_path, monkeypatch, content
):
    calls = []

    class Client:
        def stream_complete(self, messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ChatCompletion(
                    content="",
                    tool_calls=[
                        ToolCall(
                            "save",
                            "write_memory",
                            json.dumps({"scope": "preferences", "content": content}),
                        )
                    ],
                )
            return ChatCompletion(content="已保存。")

    monkeypatch.setattr(loop, "create_chat_client", lambda _: Client())
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(
        "trade_compass_agent.llm.providers.create_local_report_fact_check",
        lambda *a, **k: pytest.fail("Direct write cannot create a reviewer"),
    )
    config = AppConfig(
        data_dir=tmp_path / "data",
        memory_dir=tmp_path / "memory",
        data_provider="sample",
        agent=AgentConfig(learning_enabled=False),
    )
    result = loop.AgentLoop.from_config(config).run_turn(
        "保存这条原文：" + content, session_id="direct-save"
    )
    assert not result.degraded and len(calls) == 2
    hits = json.loads(tool_search_memory(config.memory_dir, content))["results"]
    assert hits and hits[0]["content"] == content
    reopened = loop.AgentLoop.from_config(config).session_store.load("direct-save")
    assert reopened.messages[-1].content == result.summary
    assert not (config.data_dir / "learning_reviews").exists()


def test_preexisting_skill_rules_are_preserved(tmp_path):
    from trade_compass_agent.memory.skill_store import SkillStore

    store = SkillStore(tmp_path / "skills")
    dated = "---\nname: preserved-rule\ndescription: 历史案例\n---\n2025-01-02 今日股价22元。"
    assert not store.create("preserved-rule", dated)["ok"]
    normal = "---\nname: preserved-rule\ndescription: 资料复盘\n---\n只在资料可用时对照原始来源，保留适用条件。"
    assert store.create("preserved-rule", normal)["ok"]
    assert not store.create("../escape", normal)["ok"]


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_only_historical_learning_reviews_quality_and_keeps_original_records(
    tmp_path, accepted, count
):
    receipts = [
        (
            "get_bars",
            json.dumps(
                {"symbol": "002415", "bars": [{"timestamp": "2025-01-02", "close": 22 + i}]}
            ),
        )
        for i in range(count)
    ]
    config, response = saved_history(tmp_path, receipts)
    originals = {p: p.read_bytes() for p in (config.data_dir / "report_artifacts").glob("*.json")}
    calls = []

    def model(system, user):
        payload = json.loads(user)
        calls.append(payload)
        assert payload["history"]["report"] == response.summary
        assert payload["history"]["tool_records"] == [list(r) for r in receipts]
        return json.dumps(
            {"accepted": accepted, "reason": "质量判断"}
            if "candidate" in payload
            else {"content": INSIGHT}
        )

    path = learning.curate_turn_insight(
        config=config,
        user_message="历史研究",
        response=response,
        report_run_id="owned",
        report_call=model,
    )
    assert len(calls) == 2 and bool(path) is accepted
    assert bool(json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]) is accepted
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    review = json.loads(next((config.data_dir / "learning_reviews").glob("*.json")).read_text())
    assert review["status"] == ("retained" if accepted else "not_retained")


@pytest.mark.parametrize("fault", ["bad_proposal", "bad_review", "timeout", "archive_failure"])
def test_learning_failure_does_not_save_unapproved_content(tmp_path, monkeypatch, fault):
    config, response = saved_history(tmp_path)
    calls = []

    def model(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if fault == "timeout":
            raise TimeoutError("offline")
        if fault == "bad_proposal" or fault == "bad_review" and "candidate" in payload:
            return "{}"
        return quality_reply(system, user)

    if fault == "archive_failure":
        monkeypatch.setattr(
            learning, "_write_summary", lambda *a: (_ for _ in ()).throw(OSError("unwritable"))
        )
    assert (
        learning.curate_turn_insight(
            config=config,
            user_message="历史研究",
            response=response,
            report_run_id="owned",
            report_call=model,
        )
        is None
    )
    assert json.loads(tool_search_memory(config.memory_dir, "002415"))["results"] == []
    assert response.verification["status"] == "not_reviewed"


@pytest.mark.parametrize("stop_after", [1, 2])
def test_cancelled_history_learning_cannot_write_memory(tmp_path, stop_after):
    import time

    config, response = saved_history(tmp_path)
    calls = []

    def model(system, user):
        calls.append(user)
        return quality_reply(system, user)

    with llm_request_deadline(time.monotonic() + 10, is_cancelled=lambda: len(calls) >= stop_after):
        with pytest.raises(LLMExecutionCancelled):
            learning.curate_turn_insight(
                config=config,
                user_message="历史研究",
                response=response,
                report_run_id="owned",
                report_call=model,
            )
    assert not list((config.memory_dir / "tree" / "insights").glob("*.md"))


def test_quality_storage_failure_does_not_retract_completed_answer(tmp_path, monkeypatch):
    config, _ = saved_history(tmp_path)

    class Client:
        def stream_complete(self, messages, **kwargs):
            try:
                p = json.loads(messages[-1].content)
            except ValueError:
                p = {}
            if "history" in p:
                return ChatCompletion(content=quality_reply("", messages[-1].content))
            return ChatCompletion(content="已经完成的原始答复。")

    monkeypatch.setattr(loop, "create_chat_client", lambda _: Client())
    monkeypatch.setattr(loop, "shared_consolidation_worker", lambda **_: None)
    monkeypatch.setattr(
        learning, "_write_summary", lambda *a: (_ for _ in ()).throw(OSError("unwritable"))
    )
    monkeypatch.setattr(loop.AgentLoop, "_maybe_background_review", lambda *a, **k: None)
    result = loop.AgentLoop.from_config(config).run_turn("完成研究", session_id="quality-failure")
    assert result.summary == "已经完成的原始答复。" and not result.degraded
