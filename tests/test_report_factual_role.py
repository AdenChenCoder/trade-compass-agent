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


def body_reply(payload):
    assert set(payload) == {"paragraphs"}
    return {"quantity_issues": [], "paragraphs": [{"id": row["id"], "parts": [{"text": row["text"], "kind": "other",
        "outcome": "none", "extent": "none"}]} for row in payload["paragraphs"]]}


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
        if "paragraphs" in payload:
            return json.dumps(body_reply(payload))
        factual.append(payload)
        assert set(payload) == {"statements", "receipts", "execution_records", "report_context", "request_context"}
        assert payload["request_context"] == {"role": "requirements", "text": "task instruction marker"}
        assert payload["receipts"][0]["result"] == RECEIPTS[0][1]
        assert "untrusted draft marker" not in user
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
    assert result.artifact["model_calls"] == 4 and result.artifact["factual_model_calls"] == 1
    assert result.artifact["body_review_model_calls"] == 1
    assert result.artifact["call_limit"] == 6
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
    assert set(saved["body_review_input"]) == {"paragraphs"}
    assert saved["body_meaning"]["answer"][0]["text"] == factual[0]["statements"][0]["text"]


@pytest.mark.parametrize("meaning", ["plan", "current_outcome"])
def test_rule_context_remains_separate_from_evidence_and_cannot_authorize_execution_claim(tmp_path, meaning):
    task = "仅在价格下降时考虑买入。今天收盘价据说30元；请核对后给出本轮决定。"
    text = "本轮决定继续观察，不执行买入。" if meaning == "plan" else "本轮决定继续观察，且本轮没有买入。"
    observed = []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            refs = [f["id"] for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [{"id": "decision", "type": "paragraph", "section": "plan",
                "parts": [{"type": "text", "text": text}], "refs": refs}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            assert set(payload) == {"paragraphs"}
            return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "decision", "parts": [
                {"text": text, "kind": meaning, "outcome": "none" if meaning == "plan" else "not_performed",
                 "extent": "none" if meaning == "plan" else "operation"}]}]})
        observed.append(payload)
        assert payload["request_context"] == {"role": "requirements", "text": task}
        assert payload["receipts"][0]["result"] == RECEIPTS[0][1]
        assert payload["execution_records"] == []
        assert all(f["value"] != "30" for row in payload["statements"] for f in row["evidence"])
        return json.dumps(assess(payload))
    result = publish_report(task=task, draft="already bought; untrusted draft", tool_results=RECEIPTS,
        run_scope="policy-provenance", call=writer, fact_review_call=checker, max_calls=2)
    if meaning == "plan":
        assert result.verification["status"] == "complete" and text in result.text
        assert len(observed) == 1 and "already bought" not in json.dumps(observed)
    else:
        assert result.verification["status"] == "incomplete" and text not in result.text
        assert not observed  # A later approval cannot establish non-execution.
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text


@pytest.mark.parametrize("receipt_status", [None, "executed", "rejected", "unknown"])
@pytest.mark.parametrize("analysis_supported", [True, False])
def test_only_runtime_receipts_supply_current_outcomes_to_final_reader(tmp_path, receipt_status, analysis_supported):
    prefix = "002415在2025年1月1日和1月2日的收盘价分别为22元/股和23元/股。"
    # The author is wrong even beside a valid receipt: it must not survive in
    # analysis merely because another model would approve the whole paragraph.
    claim = "本轮未执行新的买卖操作。" if receipt_status is None else "本轮已买入300股。"
    rows = list(RECEIPTS)
    if receipt_status == "executed":
        receipt = {"status": "executed", "trade_id": "owned-test", "symbol": "002415", "account": "test",
                   "side": "buy", "quantity": 100, "price": 22, "timestamp": "2025-01-02T10:00:00+08:00"}
    else:
        receipt = {"trade_rejected": True, "error": "可用资金不足"} if receipt_status == "rejected" else {"error": "断开连接"}
    if receipt_status is not None:
        rows.append(("place_paper_trade", json.dumps(receipt)))
    coverage_inputs, factual_inputs = [], []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            refs = [f["id"] for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [{"id": "answer", "section": "market", "type": "paragraph",
                "parts": [{"type": "text", "text": prefix+claim}], "refs": refs}]})
        coverage_inputs.append(payload)
        assert claim not in payload["report"]
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            assert payload == {"paragraphs": [{"id": "answer", "text": prefix+claim}]}
            return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "answer", "parts": [
                {"text": prefix, "kind": "other", "outcome": "none", "extent": "none"},
                {"text": claim, "kind": "current_outcome", "outcome": "not_performed" if receipt_status is None else "performed",
                 "extent": "whole_run" if receipt_status is None else "operation"}]}]})
        factual_inputs.append(payload)
        assert payload["statements"][0]["text"] == prefix
        return json.dumps(assess(payload, "entailed" if analysis_supported else "contradicted"))
    publication = publish_report(task="说明价格与执行结果", draft="", tool_results=rows, current_tool_results=rows,
        run_scope="execution-ownership", run_id="owned", call=writer, fact_review_call=checker, max_calls=2)
    assert claim not in publication.text and claim not in publication.artifact["reader_body"]
    assert len(coverage_inputs) == 1
    assert publication.artifact["body_review_model_calls"] == 1
    assert publication.artifact["factual_model_calls"] == 1
    assert len(factual_inputs) == 1
    assert (prefix in publication.text) is analysis_supported
    if receipt_status is None:
        assert publication.verification["status"] == "incomplete"
        assert publication.artifact["review"]["reviews"][0]["supported"] is False
        assert "本轮操作结果尚未确认。" in publication.text
        assert publication.artifact["execution_projections"]["answer"]["analysis_supported"] is analysis_supported
        if not analysis_supported:
            assert "比较最终陈述与原始资料。" in publication.artifact["review"]["reviews"][0]["reason"]
            assert {f["code"] for f in publication.verification["findings"]} >= {"execution_claim_unconfirmed", "insufficient_support"}
            assert "市场部分尚未完成" in publication.text
    elif receipt_status == "executed":
        assert publication.verification["status"] == ("complete" if analysis_supported else "incomplete")
        assert "买入100股" in publication.text and "买入300股" not in publication.text
    elif receipt_status == "rejected":
        assert publication.verification["status"] == ("complete" if analysis_supported else "incomplete")
        assert "本次未成交：可用资金不足" in publication.text
    else:
        assert publication.verification["execution"] == "unknown"
        assert publication.verification["status"] == "incomplete"
    ref = persist_publication(tmp_path, publication)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == publication.text
    assert saved["execution_projections"]["answer"]["removed"][0]["text"] == claim


@pytest.mark.parametrize("fault", ["omit", "rewrite", "duplicate", "wrong_id", "wrong_kind", "timeout"])
def test_missing_or_unfaithful_body_interpretation_cannot_reach_coverage_or_fact_approval(fault):
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        assert "schema" in payload
        return json.dumps(compose(payload))
    def checker(system, user):
        payload = json.loads(user)
        calls.append(payload)
        assert set(payload) == {"paragraphs"}
        if fault == "timeout":
            raise TimeoutError("body interpretation unavailable")
        result = body_reply(payload)
        row = result["paragraphs"][0]
        if fault == "omit":
            row["parts"][0]["text"] = row["parts"][0]["text"][:-1]
        elif fault == "rewrite":
            row["parts"][0]["text"] += "额外限定"
        elif fault == "duplicate":
            result["paragraphs"].append(row)
        elif fault == "wrong_id":
            row["id"] = "other"
        else:
            row["parts"][0]["kind"] = "approved"
        return json.dumps(result)
    result = publish_report(task="比较收盘价", draft="", tool_results=RECEIPTS, run_scope="invalid-body",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert len(calls) == 1 and result.artifact["factual_model_calls"] == 0
    assert result.verification["status"] == "incomplete"
    assert "22元/股升至23元/股" not in result.text


def test_execution_only_report_uses_receipt_without_empty_prose_or_source_marker():
    claim = "本轮买入300股。"
    receipt = {"status": "executed", "trade_id": "owned-fill", "symbol": "002415", "account": "test",
               "side": "buy", "quantity": 100, "price": 22, "timestamp": "2025-01-02T10:00:00+08:00"}
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            return json.dumps({"schema_version": 1, "items": [{"id": "outcome", "type": "paragraph", "section": "execution",
                "parts": [{"type": "text", "text": claim}], "refs": [f["id"] for f in report_facts(payload)]}]})
        assert "买入100股" in payload["report"] and claim not in payload["report"]
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        assert set(json.loads(user)) == {"paragraphs"}
        return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "outcome", "parts": [{"text": claim, "kind": "current_outcome",
            "outcome": "performed", "extent": "operation"}]}]})
    result = publish_report(task="报告成交结果", draft="", tool_results=[("place_paper_trade", json.dumps(receipt))],
                            run_scope="execution-only", call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == "complete"
    assert result.artifact["factual_model_calls"] == 0 and result.artifact["body_review_model_calls"] == 1
    assert "买入100股" in result.text and "300" not in result.text and "[^source-" not in result.text


def test_cancellation_after_body_interpretation_stops_before_coverage_and_evidence_review():
    stopped = False
    def active():
        if stopped:
            raise TurnInterruptedError("")
    def writer(system, user):
        payload = json.loads(user)
        assert "schema" in payload
        return json.dumps(compose(payload))
    def checker(system, user):
        nonlocal stopped
        stopped = True
        return json.dumps(body_reply(json.loads(user)))
    with pytest.raises(TurnInterruptedError):
        publish_report(task="比较价格", draft="", tool_results=RECEIPTS, run_scope="cancel-body",
                       call=writer, fact_review_call=checker, check_active=active)


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


def test_one_revision_shares_eight_call_limit_and_reviews_actual_final_text_again():
    result, primary, factual = publish(lambda payload, n: assess(payload, "unknown" if n == 1 else "entailed"), max_calls=4)
    assert result.verification["status"] == "complete"
    assert len(primary) == 4 and len(factual) == 2
    assert result.artifact["model_calls"] == result.artifact["call_limit"] == 8
    assert result.artifact["body_review_model_calls"] == 2
    assert result.artifact["selected_attempt"] == 1
    assert len(result.artifact["revision_attempts"]) == 2
    assert factual[-1]["statements"][0]["text"] in result.artifact["reader_body"]


@pytest.mark.parametrize("failure", ["unavailable", "unsupported"])
def test_failed_revision_cannot_erase_first_fact_checked_body(failure):
    def reply(payload, n):
        if n == 2:
            if failure == "unavailable":
                raise TimeoutError("review unavailable")
            return assess(payload, "contradicted")
        return assess(payload)
    result, _, factual = publish(reply, max_calls=4, coverage_missing=True)
    assert len(factual) == 2 and result.artifact["selected_attempt"] == 0
    assert "22元/股升至23元/股" in result.text
    assert result.verification["report"] == "partial"
    assert result.artifact["model_calls"] == 8


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

    factory_configs, queries, checks, body_checks = [], [], [], []
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
            if "paragraphs" in payload:
                body_checks.append(payload)
                if verdict == "unavailable":
                    raise AgentTurnError("local model is not available")
                return ChatCompletion(content=json.dumps(body_reply(payload)))
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
    assert len(checks) == (2 if verdict == "unknown" else 0 if verdict == "unavailable" else 1)
    assert len(body_checks) == (2 if verdict == "unknown" else 1)
    assert detail["status"] == ("completed" if verdict == "entailed" else "degraded")
    assert ("22元/股升至23元/股" in detail["analysis"]) is (verdict == "entailed")
    assert verification["execution"] == "completed"
    artifact = json.loads((config.data_dir/"report_artifacts"/(verification["artifact_id"]+".json")).read_text())
    assert artifact["published_text"] == detail["analysis"]
    assert artifact["factual_model_calls"] == len(checks)
    assert artifact["body_review_model_calls"] == len(body_checks)
    assert artifact["report_context"]["reference_date"] == detail["step_runs"][0]["data"]["session_id"][-10:]
    assert all(row["report_context"] == artifact["report_context"] for row in checks)


@pytest.mark.parametrize("ambiguous", [True, False])
def test_body_quantity_meaning_cannot_be_repaired_by_evidence_approval(tmp_path, ambiguous):
    text = "价格变动比例为4.55。" if ambiguous else "这里的变动比例以百分数计，为4.55。"
    checks = []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market",
                 "parts": [{"type": "text", "text": text}], "refs": ["change"]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        checks.append(payload)
        if "paragraphs" in payload:
            reply = body_reply(payload)
            if ambiguous:
                reply["quantity_issues"] = [{"id": "answer", "text": "4.55", "reason": "正文未明确比例尺度。"}]
            return json.dumps(reply)
        return json.dumps(assess(payload))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="quantity-meaning",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == ("incomplete" if ambiguous else "complete")
    assert (text in result.text) is not ambiguous
    assert len(checks) == (1 if ambiguous else 2)
    assert bool(result.artifact["body_quantity_issues"]) is ambiguous
    assert result.artifact["review"]["reviews"][0]["supported"] is not ambiguous
    if ambiguous:
        assert "quantity_meaning_ambiguous" in {f["code"] for f in result.verification["findings"]}
        assert result.artifact["factual_model_calls"] == 0
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == result.text
    assert saved["body_quantity_issues"] == result.artifact["body_quantity_issues"]


@pytest.mark.parametrize("fault", ["missing", "not_list", "wrong_id", "invented_text", "empty_reason", "extra_field"])
def test_malformed_body_quantity_assessment_is_not_silent_approval(fault):
    def checker(system, user):
        payload = json.loads(user)
        assert set(payload) == {"paragraphs"}
        body = body_reply(payload)
        issue = {"id": "answer", "text": payload["paragraphs"][0]["text"], "reason": "需要明确单位。"}
        body["quantity_issues"] = [issue]
        if fault == "missing":
            del body["quantity_issues"]
        elif fault == "not_list":
            body["quantity_issues"] = None
        elif fault == "wrong_id":
            issue["id"] = "unknown"
        elif fault == "invented_text":
            issue["text"] = "正文没有这句话"
        elif fault == "empty_reason":
            issue["reason"] = ""
        else:
            issue["verdict"] = "approved"
        return json.dumps(body)
    def writer(system, user):
        return json.dumps(compose(json.loads(user)))
    result = publish_report(task="比较收盘价", draft="", tool_results=RECEIPTS, run_scope="invalid-quantity",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == "incomplete"
    assert result.artifact["factual_model_calls"] == 0
    assert "22元/股升至23元/股" not in result.text


@pytest.mark.parametrize("first_text", ["价格变动比例为4.55。", "这里的变动比例以百分数计，为4.55。"])
@pytest.mark.parametrize("corrected", [True, False])
def test_quantity_clarification_revision_is_rechecked_and_bounded(first_text, corrected):
    # The second first_text is legitimate prose that a model may falsely flag.
    # Recovery still rewrites once; another factual mistake cannot be approved.
    revised_text = "价格变动比例为4.55%。" if corrected else "价格变动比例为4.56%。"
    writing_calls = []
    def writer(system, user):
        payload = json.loads(user)
        writing_calls.append(payload)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            text = revised_text if "revision" in payload else first_text
            if "revision" in payload:
                assert payload["revision"]["review"]["reviews"][0]["supported"] is False
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market",
                 "parts": [{"type": "text", "text": text}], "refs": ["change"]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            reply = body_reply(payload)
            if payload["paragraphs"][0]["text"] == first_text:
                reply["quantity_issues"] = [{"id": "answer", "text": "4.55", "reason": "正文未明确比例尺度。"}]
            return json.dumps(reply)
        assert payload["statements"][0]["text"] == revised_text
        return json.dumps(assess(payload, "entailed" if corrected else "contradicted"))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="quantity-revision",
                            call=writer, fact_review_call=checker)
    assert (result.verification["status"] == "complete") is corrected
    assert (revised_text in result.text) is corrected
    assert first_text not in result.text
    assert len(writing_calls) == 4
    assert result.artifact["model_calls"] == 7 and result.artifact["call_limit"] == 8
    assert result.artifact["body_review_model_calls"] == 2 and result.artifact["factual_model_calls"] == 1
    assert result.artifact["selected_attempt"] == 1
    first, revised = result.artifact["revision_attempts"]
    assert first["body_quantity_issues"] and not revised["body_quantity_issues"]
    assert first["verification"]["status"] == "incomplete"


@pytest.mark.parametrize("author_duplicate", [True, False])
@pytest.mark.parametrize("verdict", ["entailed", "contradicted"])
def test_bound_unit_presence_cannot_override_sentence_truth_or_author_quantity(author_duplicate, verdict):
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            parts = [{"type": "text", "text": "价格变动比例为"},
                     {"type": "value", "ref": "change", "field": "value_with_unit"},
                     {"type": "text", "text": "。"}]
            if author_duplicate:
                parts.append({"type": "text", "text": "另一期间的变动比例为4.55。"})
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market", "parts": parts, "refs": []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    factual = []
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            result = body_reply(payload)
            # Exact candidate32 false positive; the same quote may also exist
            # in author prose and must not borrow the other occurrence's unit.
            result["quantity_issues"] = [{"id": "answer", "text": "4.55" if author_duplicate else "4.55%",
                                          "reason": "Missing unit or ratio scale in the reader body"}]
            return json.dumps(result)
        factual.append(payload)
        assert payload["statements"][0]["text"] == "价格变动比例为4.55%。"
        trace, = payload["statements"][0]["calculations"]
        assert trace["display_value_with_unit"] == "4.55%"
        assert trace["result"] == "4.545454545454545454545454545"
        return json.dumps(assess(payload, verdict))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="owned-quantity",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert bool(factual) is not author_duplicate
    assert bool(result.artifact["bound_quantity_checks"]) is not author_duplicate
    assert bool(result.artifact["body_quantity_issues"]) is author_duplicate
    supported = not author_duplicate and verdict == "entailed"
    assert (result.verification["status"] == "complete") is supported
    assert result.artifact["review"]["reviews"][0]["supported"] is supported
    if not author_duplicate:
        assert result.artifact["factual_model_calls"] == 1
    assert "另一期间的变动比例为4.55。" not in result.text


@pytest.mark.parametrize("origin,field,unit", [
    ("author", "value_with_unit", "%"), ("evidence", "subject", "%"),
    ("evidence", "value_with_unit", ""), ("evidence", "value_with_unit", "单位未提供"),
])
def test_unit_presence_requires_runtime_quantity_binding(origin, field, unit):
    from trade_compass_agent.runtime.report_body import runtime_bound_quantity_issue
    segments = [{"origin": origin, "field": field, "quantity_unit": unit, "text": "4.55%"}]
    assert not runtime_bound_quantity_issue({"text": "4.55%"}, segments)


@pytest.mark.parametrize("genuine_gap", [True, False])
@pytest.mark.parametrize("supported", [True, False])
def test_bound_gap_review_keeps_local_factual_and_coverage_roles_independent(genuine_gap, supported):
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            doc = compose(payload)
            doc["items"].append({"id": "gap", "type": "gap", "section": "market",
                                 "text": "缺少营收资料。" if genuine_gap else "缺少收盘价资料。"})
            return json.dumps(doc)
        assert payload["inferences"] == [] and payload["receipts"] == []
        assert len(payload["verified_items"]) == 2
        return json.dumps({"reviews": [], "coverage": {"complete": True, "missing": []},
            "gap_reviews": [{"id": "gap", "status": "required" if genuine_gap else "already_present",
                "refs": [] if genuine_gap else [payload["verified_items"][0]["id"]],
                "reason": "仍无营收资料。" if genuine_gap else "已有收盘价。"}]})
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            return json.dumps(body_reply(payload))
        assert "task" not in payload and "gaps" not in payload
        return json.dumps(assess(payload, "entailed" if supported else "unknown"))
    report = publish_report(task="说明收盘价与资料缺口。", draft="", tool_results=RECEIPTS,
                            run_scope="local-bound-gap", call=writer, fact_review_call=checker, max_calls=2)
    assert (report.verification["status"] == "complete") is (supported and not genuine_gap)
    assert ("22元/股升至23元/股" in report.text) is supported
    assert any(f["code"] == "declared_gap" for f in report.verification["findings"]) is genuine_gap
    assert report.artifact["factual_model_calls"] == report.artifact["body_review_model_calls"] == 1
