"""Explicit model doubles for the independent local factual publication boundary."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.config import AppConfig, LLMConfig, invalidate_config_cache, load_app_config
from trade_compass_agent.llm import providers
from trade_compass_agent.runtime.exceptions import AgentTurnError, TurnInterruptedError
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report

RECEIPTS = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]}))]


def compose(payload):
    facts = [f for f in report_facts(payload) if f["metric"] == "close"]
    return {"schema_version": 1, "items": [{"id": "answer", "type": "paragraph", "section": "market",
        "parts": [{"type": "text", "text": "002415收盘价由"},
                  {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                  {"type": "text", "text": "升至"},
                  {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                  {"type": "text", "text": "。"}], "refs": []}]}


def assess(payload, verdict="entailed"):
    return {"assessments": [{"id": row["id"], "comparison": "比较最终陈述与原始资料。", "verdict": verdict}
                            for row in payload["statements"]]}


def publish(fact_reply, *, max_calls=2, coverage_missing=False, check_active=lambda: None, reference_date=None):
    primary, factual = [], []
    def writer(system, user):
        payload = json.loads(user)
        primary.append(payload)
        if "schema" in payload:
            return json.dumps(compose(payload))
        assert payload["inferences"] == [] and payload["receipts"] == []
        assert "22元/股升至23元/股" in payload["report"]
        missing = coverage_missing and len(primary) == 2
        return json.dumps({"reviews": [], "coverage": {"complete": not missing,
            "missing": ["正文仍需补充任务答案"] if missing else [], "repairable": missing}})
    def checker(system, user):
        payload = json.loads(user)
        factual.append(payload)
        assert set(payload) == {"statements", "receipts", "execution_records", "report_context"}
        assert payload["receipts"][0]["result"] == RECEIPTS[0][1]
        assert "task instruction marker" not in user and "untrusted draft marker" not in user
        result = fact_reply(payload, len(factual))
        return result if isinstance(result, str) else json.dumps(result)
    result = publish_report(task="task instruction marker", draft="untrusted draft marker", tool_results=RECEIPTS,
        run_scope="factual-role", run_id="owned", call=writer, fact_review_call=checker,
        max_calls=max_calls, check_active=check_active, reference_date=reference_date)
    return result, primary, factual


@pytest.mark.parametrize("verdict", ["entailed", "contradicted", "unknown"])
def test_only_supported_final_body_reaches_reader_and_reopened_artifact(tmp_path, verdict):
    result, primary, factual = publish(lambda payload, _: assess(payload, verdict))
    assert len(primary) == 2 and len(factual) == 1
    assert result.artifact["model_calls"] == 3 and result.artifact["factual_model_calls"] == 1
    assert result.artifact["call_limit"] == 4
    assert (result.verification["status"] == "complete") is (verdict == "entailed")
    assert ("22元/股升至23元/股" in result.text) is (verdict == "entailed")
    assert result.verification["execution"] == "completed"
    assert result.verification["data"] == "available"
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == result.text
    assert saved["coverage_review_input"] == primary[-1]
    assert saved["factual_review_input"] == factual[0]
    assert saved["factual_review"]["assessments"][0]["verdict"] == verdict


@pytest.mark.parametrize("reference_date", [None, "2025-01-02", "2025-01-03"])
def test_runtime_date_reaches_writer_reviewer_revision_and_reopened_record(tmp_path, reference_date):
    result, primary, factual = publish(lambda payload, n: assess(payload, "unknown" if n == 1 else "entailed"),
                                      max_calls=4, reference_date=reference_date)
    context = {"reference_date": reference_date, "reference_timezone": "Asia/Shanghai", "display_timezone": "Asia/Shanghai"}
    assert len(primary) == 4 and len(factual) == 2
    assert all(row["report_context"] == context for row in [*primary, *factual])
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["report_context"] == context
    assert all(row["factual_review_input"]["report_context"] == context for row in saved["revision_attempts"])


@pytest.mark.parametrize("reference_date", ["20250102", "2025-02-30", "today", True])
def test_invalid_runtime_date_cannot_be_guessed_by_model(reference_date):
    with pytest.raises(ValueError):
        publish(lambda *_: pytest.fail("invalid date reached model"), reference_date=reference_date)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong_id", "wrong_type", "extra", "blank", "timeout"])
def test_malformed_or_unavailable_local_review_never_falls_back_to_writer_approval(fault):
    def reply(payload, _):
        if fault == "timeout":
            raise TimeoutError("local model unavailable")
        result = assess(payload)
        row = result["assessments"][0]
        if fault == "missing":
            result["assessments"] = []
        elif fault == "duplicate":
            result["assessments"].append(row)
        elif fault == "wrong_id":
            row["id"] = "other"
        elif fault == "wrong_type":
            row["verdict"] = True
        elif fault == "extra":
            row["approved"] = True
        else:
            row["comparison"] = " "
        return result
    result, _, factual = publish(reply)
    assert len(factual) == 1 and not result.artifact["review_valid"]
    assert result.verification["report"] == "partial"
    assert "22元/股升至23元/股" not in result.text
    assert any(f["code"] == "review_unavailable" for f in result.verification["findings"])


def test_one_revision_shares_six_call_limit_and_reviews_actual_final_text_again():
    result, primary, factual = publish(lambda payload, n: assess(payload, "unknown" if n == 1 else "entailed"), max_calls=4)
    assert result.verification["status"] == "complete"
    assert len(primary) == 4 and len(factual) == 2
    assert result.artifact["model_calls"] == result.artifact["call_limit"] == 6
    assert result.artifact["selected_attempt"] == 1
    assert len(result.artifact["revision_attempts"]) == 2
    assert factual[-1]["statements"][0]["text"] in result.artifact["reader_body"]


def test_failed_revision_cannot_erase_first_fact_checked_body():
    def reply(payload, n):
        if n == 2:
            raise TimeoutError("review unavailable")
        return assess(payload)
    result, _, factual = publish(reply, max_calls=4, coverage_missing=True)
    assert len(factual) == 2 and result.artifact["selected_attempt"] == 0
    assert "22元/股升至23元/股" in result.text
    assert result.verification["report"] == "partial"
    assert result.artifact["model_calls"] == 6


def test_cancellation_after_fact_call_prevents_publication_or_revision():
    cancelled = False
    def active():
        if cancelled:
            raise TurnInterruptedError("")
    def reply(payload, _):
        nonlocal cancelled
        cancelled = True
        return assess(payload)
    with pytest.raises(TurnInterruptedError):
        publish(reply, max_calls=4, check_active=active)


def test_local_reviewer_routes_locally_without_tools_retries_or_deadline_reset(tmp_path, monkeypatch):
    configs, invocations = [], []
    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory", llm=LLMConfig(
        provider="openai", model="writer", api_key_env="WRITER_KEY", local_report_review_model="local-fact-model"))
    def client(review_config):
        configs.append(review_config)
        def stream(messages, **kwargs):
            invocations.append(kwargs)
            assert providers._maintenance_mode.get()
            assert providers._remaining_request_time() <= 10
            assert kwargs["tools"] is None and kwargs["is_cancelled"] is cancelled
            return SimpleNamespace(content='{"assessments":[]}', tool_calls=[])
        return SimpleNamespace(stream_complete=stream)
    monkeypatch.setattr(providers, "create_chat_client", client)
    def cancelled():
        return False
    callback = providers.create_local_report_fact_check(config, is_cancelled=cancelled)
    assert not configs
    with providers.llm_request_deadline(time.monotonic()+10):
        assert callback("review", "data") == '{"assessments":[]}'
        callback("review again", "data")
    assert len(configs) == 1 and len(invocations) == 2
    actual = configs[0]
    assert actual.llm.provider == "lmstudio" and actual.llm.model == "local-fact-model"
    assert actual.llm.api_key_env == "" and actual.llm.max_retries == 0
    assert actual.data_dir == config.data_dir and actual.memory_dir == config.memory_dir
    assert config.llm.provider == "openai"  # No mutation or implicit model switch.
    assert providers.LMSTUDIO_BASE_URL == "http://localhost:1234/v1"
    assert providers.create_local_report_fact_check(replace(config, llm=replace(config.llm, local_report_review_model=""))) is None


def test_local_reviewer_cannot_request_a_business_tool(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory",
                       llm=LLMConfig(local_report_review_model="local-fact-model"))
    monkeypatch.setattr(providers, "create_chat_client", lambda _: SimpleNamespace(stream_complete=lambda *a, **k:
        SimpleNamespace(content="", tool_calls=[object()])))
    with pytest.raises(AgentTurnError, match="不能执行工具"):
        providers.create_local_report_fact_check(config)("review", "data")


def test_review_model_configuration_survives_reload_without_changing_main_model(tmp_path, monkeypatch):
    path = tmp_path/"config.yaml"
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path/"home"))
    path.write_text('llm:\n  provider: lmstudio\n  model: writer\n  local_report_review_model: local-reviewer\n')
    try:
        for _ in range(2):
            invalidate_config_cache()
            config = load_app_config()
            assert config.llm.model == "writer" and config.llm.local_report_review_model == "local-reviewer"
    finally:
        invalidate_config_cache()


@pytest.mark.parametrize("verdict", ["entailed", "unknown", "unavailable"])
def test_scheduled_api_uses_configured_factual_role_and_never_replays_business_tools(tmp_path, monkeypatch, verdict):
    from fastapi.testclient import TestClient
    from trade_compass_agent.config import AgentConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.ops.prompt_jobs import PromptJobExecutor, PromptJobStore
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.web.app import create_app

    factory_configs, queries, checks = [], [], []
    class Writer:
        name = model = "mock-writer"
        def stream_complete(self, messages, **kwargs):
            try:
                payload = json.loads(messages[-1].content)
            except (ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict) and "schema" in payload:
                return ChatCompletion(content=json.dumps(compose(payload)))
            if isinstance(payload, dict) and "inferences" in payload:
                assert payload["inferences"] == []
                return ChatCompletion(content='{"reviews":[],"coverage":{"complete":true,"missing":[]}}')
            if not any(m.role == "tool" for m in messages):
                return ChatCompletion(content="", tool_calls=[ToolCall("bars", "get_bars", '{"symbol":"002415"}')])
            return ChatCompletion(content="已读取收盘资料。")
        complete = stream_complete

    class FactChecker:
        def stream_complete(self, messages, **kwargs):
            assert kwargs["tools"] is None
            payload = json.loads(messages[-1].content)
            checks.append(payload)
            if verdict == "unavailable":
                raise AgentTurnError("local model is not available")
            return ChatCompletion(content=json.dumps(assess(payload, verdict)))

    def make_checker(config):
        factory_configs.append(config)
        assert config.llm.provider == "lmstudio" and config.llm.model == "local-reviewer"
        return FactChecker()
    def query(_registry, name, _arguments):
        queries.append(name)
        assert name == "get_bars"
        return RECEIPTS[0][1]

    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory", data_provider="sample",
        agent=AgentConfig(learning_enabled=False), llm=LLMConfig(local_report_review_model="local-reviewer"))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Writer())
    monkeypatch.setattr(providers, "create_chat_client", make_checker)
    monkeypatch.setattr("trade_compass_agent.runtime.tools.registry.ToolRegistry.execute", query)
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)
    jobs = PromptJobStore(config.data_dir/"scheduler.db")
    job = jobs.create(name="价格比较", prompt="比较两个观察日的收盘价", schedule="daily 12:00", delivery_channels=())
    runs = SqliteRunStore(config.data_dir/"scheduler.db")
    PromptJobExecutor(config, runs).execute(job)
    run = runs.recent_runs(job_id=f"custom:{job.id}")[0]
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    detail = TestClient(create_app()).get(f"/api/jobs/runs/{run.id}").json()
    verification = detail["step_runs"][0]["data"]["verification"]
    assert queries == ["get_bars"] and len(factory_configs) == 1
    assert len(checks) == (2 if verdict == "unknown" else 1)
    assert detail["status"] == ("completed" if verdict == "entailed" else "degraded")
    assert ("22元/股升至23元/股" in detail["analysis"]) is (verdict == "entailed")
    assert verification["execution"] == "completed"
    artifact = json.loads((config.data_dir/"report_artifacts"/(verification["artifact_id"]+".json")).read_text())
    assert artifact["published_text"] == detail["analysis"]
    assert artifact["factual_model_calls"] == len(checks)
    assert artifact["report_context"]["reference_date"] == detail["step_runs"][0]["data"]["session_id"][-10:]
    assert all(row["report_context"] == artifact["report_context"] for row in checks)
