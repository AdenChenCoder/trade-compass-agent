"""Consumer checks for freely written, evidence-bound paragraphs.

Model doubles are explicit: these test the publication boundary, not semantic
model accuracy (covered separately by real-model acceptance).
"""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_publication import publish_report, persist_publication

RECEIPTS = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d",
    "bars": [{"timestamp": "2026-11-10", "close": 10}, {"timestamp": "2026-11-11", "close": 11}]}))]


def text(value):
    return {"type": "text", "text": value}


def value(ref, field="value_with_unit"):
    return {"type": "value", "ref": ref, "field": field}


def document(payload, reverse=False):
    facts = [f for f in report_facts(payload) if f["metric"] == "close"]
    a, b = [f["id"] for f in facts]
    parts = ([text("收盘价从"), value(a), text("升至"), value(b), text("，变动为"), value("change"), text("。")]
             if reverse else
             [value(a, "subject"), text("在"), value(a, "date"), text("至"), value(b, "date"),
              text("的收盘价由"), value(a), text("升至"), value(b), text("，变动"), value("change"),
              text("。这里只比较这两个观察日，不能据此推断后续走势。")])
    items = [
        {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct", "refs": [a, b]},
        {"id": "answer", "type": "paragraph", "section": "assessment", "parts": parts, "refs": []},
    ]
    return {"schema_version": 1, "items": list(reversed(items)) if reverse else items}


def publish(*, reverse=False, mutate=None, verdict=True, unavailable=False, receipts=RECEIPTS, run_id="", reason="价格资料不能支持该结论"):
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            doc = document(payload, reverse)
            if mutate:
                mutate(doc)
            return json.dumps(doc, ensure_ascii=False)
        if unavailable:
            raise TimeoutError("review unavailable")
        return json.dumps({"reviews": [{"id": p["id"], "supported": verdict,
                           "reason": "" if verdict else reason} for p in payload["inferences"]],
                           "coverage": {"complete": True, "missing": []}})
    return publish_report(task="比较两日收盘价", draft="", tool_results=receipts,
                          run_scope="narrative", run_id=run_id, call=call), calls


def test_wording_variants_preserve_reopened_downstream_evidence(tmp_path):
    """Explicit review doubles isolate transport invariance, not model accuracy."""
    from trade_compass_agent.ops.job_definition import StepOutput
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_publication import upstream_evidence

    evidence_versions, fact_versions, bodies = [], [], []
    for reverse in (False, True):
        report, _ = publish(reverse=reverse, run_id="owned-report")
        reference = {**report.verification, **persist_publication(tmp_path, report)}
        upstream = {"analysis": StepOutput(message=report.text, data={
            "analysis": report.text, "verification": reference})}
        # The next step reopens the persisted artifact via its normal ownership
        # checks; it must not derive numbers or direction from the prose style.
        receipts, findings = upstream_evidence(tmp_path, upstream, "owned-report")
        assert not findings and receipts == RECEIPTS
        evidence_versions.append(receipts)
        facts = EvidenceCatalog.build(receipts, run_scope="next-step").facts
        fact_versions.append(list(facts.values()))
        bodies.append(report.text)
    assert bodies[0] != bodies[1]
    assert evidence_versions[0] == evidence_versions[1]
    assert fact_versions[0] == fact_versions[1]
    assert [(fact.value, fact.unit, fact.observed_at) for fact in fact_versions[0]
            if fact.metric == "close"] == [
        ("10", "元/股", "2026-11-10"), ("11", "元/股", "2026-11-11")]


@pytest.mark.parametrize("reverse", [False, True])
def test_final_natural_paragraph_is_reviewed_and_persisted_without_rewriting(tmp_path, reverse):
    report, calls = publish(reverse=reverse)
    assert report.verification["status"] == "complete"
    assert len(calls) == 2
    assert "10元/股升至11元/股" in report.text
    assert "10.00%" in report.text
    assert not report.text.startswith("#")
    assert "研判：" not in report.text and "| 指标 |" not in report.text
    assert calls[-1]["report"] == report.artifact["reader_body"]
    assert calls[-1]["inferences"][0]["text"] in report.text
    segments = calls[-1]["inferences"][0]["segments"]
    assert "".join(s["text"] for s in segments) == calls[-1]["inferences"][0]["text"]
    assert {s["origin"] for s in segments} == {"author", "evidence"}
    assert all(s.get("ref") for s in segments if s["origin"] == "evidence")
    assert report.artifact["published_text"] == report.text
    assert report.artifact["review_input"] == calls[-1]
    saved = persist_publication(tmp_path, report)
    artifact = json.loads((tmp_path / "report_artifacts" / (saved["artifact_id"] + ".json")).read_text())
    assert artifact["published_text"] == report.text
    assert all("e0" != part.get("ref") for item in artifact["document"]["items"] if item["type"] == "paragraph"
               for part in item["parts"])
    assert "[^source-1]:" in report.text


@pytest.mark.parametrize("fault", ["unknown", "missing_date", "calculation_field", "cycle"])
def test_invalid_evidence_cannot_be_approved_by_semantic_reviewer(fault):
    receipts = RECEIPTS
    if fault == "missing_date":
        receipts = [("get_bars", json.dumps({"symbol": "002415", "bars": [{"close": 10}, {"close": 11}]}))]
    def mutate(doc):
        paragraph = doc["items"][1]
        if fault == "unknown":
            paragraph["parts"].insert(0, value("invented"))
        elif fault == "calculation_field":
            paragraph["parts"].insert(0, value("change", "date"))
        elif fault == "cycle":
            paragraph["parts"].insert(0, value("answer"))
    report, calls = publish(mutate=mutate, receipts=receipts)
    assert report.verification["status"] == "incomplete"
    assert any(f["code"] == "invalid_evidence" for f in report.verification["findings"])
    assert not calls[-1].get("inferences")
    assert "升至" not in report.text
    assert len(calls) <= 4


@pytest.mark.parametrize("unavailable", [False, True])
def test_unsupported_or_unreviewed_paragraph_is_not_published(unavailable):
    def mutate(doc):
        doc["items"][1]["parts"].append(text("所以业绩增长必然持续。"))
    report, calls = publish(mutate=mutate, verdict=False, unavailable=unavailable)
    assert "业绩增长必然持续" in calls[-1]["report"]
    assert "业绩增长必然持续" not in report.text
    assert "10.00%" in report.text  # Valid calculation retained as partial evidence.
    assert report.verification["report"] == "partial"
    assert report.verification["execution"] == "completed"


def test_same_binding_can_be_used_in_distinct_prose_without_text_matching():
    for prose in ["这段期间价格走高，但未来方向仍不确定。", "不能外推下一日表现；已观察到的变化向上。"]:
        def mutate(doc):
            doc["items"][1]["parts"].append(text(prose))
        report, calls = publish(mutate=mutate)
        assert report.verification["status"] == "complete"
        assert prose in report.text and prose in calls[-1]["inferences"][0]["text"]


def test_binding_a_wrong_scope_calculation_never_reaches_review():
    receipts = [*RECEIPTS, ("get_bars", json.dumps({"symbol": "000001", "timeframe": "1d",
                "bars": [{"timestamp": "2026-11-11", "close": 11}]}))]
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market",
                 "operation": "change_pct", "refs": [facts[0]["id"], facts[-1]["id"]]},
                {"id": "answer", "type": "paragraph", "section": "overview", "refs": [],
                 "parts": [text("价格变化"), value("change"), text("。")]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task="比较002415价格", draft="", tool_results=receipts, run_scope="scope", call=call)
    assert result.verification["status"] == "incomplete"
    assert "10.00%" not in result.text
    assert calls[-1]["inferences"] == []
    assert any("不同标的" in f["message"] for f in result.verification["findings"])

def test_unresolved_definition_remains_visible_beside_a_valid_paragraph():
    def mutate(doc):
        doc["items"].append({"id": "unresolved", "type": "fact", "section": "market", "ref": "missing"})
    report, _ = publish(mutate=mutate)
    assert "10元/股升至11元/股" in report.text
    assert report.verification["report"] == "partial"
    assert "市场部分尚未完成，相关结论暂无法确认。" in report.text
    assert any(row["message"] == "引用不属于本轮可核验资料" for row in report.verification["findings"])

def test_review_batch_view_preserves_full_instrument_rows_and_explicit_omissions():
    from dataclasses import replace
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_transport import review_receipts
    raw = {"timeframe": "1d", "count": 3, "errors": {"000003": "unavailable"}, "results": {
        symbol: {"bars_count": 60, "bars_truncated": True, "returned_count": 2,
                 "_meta": {"source": "frozen", "adjusted": False},
                 "bars": [{"timestamp": "2025-01-01", "close": 10},
                          {"timestamp": "2025-01-02", "close": 11}]}
        for symbol in ["002415", "000001"]}}
    catalog = EvidenceCatalog.build([("batch_get_bars", json.dumps(raw))], run_scope="batch-view")
    original = json.loads(json.dumps(catalog.receipts))
    fact = next(f for f in catalog.facts.values() if f.subject == "002415" and f.metric == "close")
    view = review_receipts(catalog, [fact])[0]
    projected = json.loads(view["result"])
    assert projected["results"] == {"002415": raw["results"]["002415"]}
    assert projected["count"] == 3 and projected["errors"] == raw["errors"]
    assert view["selection"] == {"kind": "complete_instrument_subtrees",
                                 "included": ["002415"], "omitted": ["000001"]}
    assert view["original_sha256"] == original[0]["sha256"]
    assert catalog.receipts == original
    # No root-level or multi-instrument evidence may be silently narrowed.
    assert review_receipts(catalog, [replace(fact, pointer="/count")]) == original
    both = [f for f in catalog.facts.values() if f.metric == "close"]
    assert review_receipts(catalog, both) == original

@pytest.mark.parametrize("parts", [None, 7, {}, "invalid"])
def test_malformed_paragraph_preserves_verified_siblings(parts):
    def mutate(doc):
        doc["items"][1]["parts"] = parts
    report, calls = publish(mutate=mutate)
    assert report.verification["report"] == "partial"
    assert "10.00%" in report.text
    assert any(f["code"] == "invalid_structure" for f in report.verification["findings"])
    assert len(calls) <= 4

def test_internal_memory_evidence_keeps_structured_presentation():
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            assert "paragraph_rendering_example" not in payload
            assert "Paragraph" not in payload["schema"]["$defs"]
            fact = next(f for f in report_facts(payload) if f["metric"] == "close")
            return json.dumps({"schema_version": 1, "items": [
                {"id": "fact", "type": "fact", "section": "market", "ref": fact["id"]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task="保留记忆依据", draft="", tool_results=RECEIPTS,
                            run_scope="memory", call=call, reader_prose=False)
    assert result.verification["status"] == "complete"
    assert result.text == calls[-1]["report"]
    assert result.text.startswith("## 市场")
    assert "收盘价 10元/股" in result.text
    assert "[^source-" not in result.text and "| 指标 |" not in result.text

def test_source_notes_cannot_substitute_for_answering_the_task_in_the_body():
    seen = []
    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            doc = document(payload)
            doc["items"][1]["parts"] = [text("价格上升。")]
            doc["items"][1]["refs"] = ["change"]
            return json.dumps(doc)
        seen.append(payload)
        assert "10.00%" not in payload["report"]
        assert "reader_body" not in payload  # One authoritative body, not two competing views.
        assert "[^source-1]:" not in payload["report"]
        assert payload["inferences"][0]["calculations"][0]["unit"] == "%"
        return json.dumps({"reviews": [{"id": "answer", "supported": True, "reason": ""}],
                           "coverage": {"complete": False, "missing": ["正文未回答收盘价变动比例"]}})
    result = publish_report(task="比较收盘价并给出变动比例", draft="", tool_results=RECEIPTS,
                            run_scope="body-coverage", call=call)
    assert result.verification["report"] == "partial"
    assert "正文未回答收盘价变动比例" in result.artifact["reader_body"]
    assert "价格上升" in result.text
    assert seen


def test_reader_composition_omits_redundant_order_index_without_dropping_evidence():
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_transport import ReportTransport
    receipts = [*RECEIPTS, ("get_bars", json.dumps({"symbol": "000001", "timeframe": "1d",
        "bars": [{"timestamp": "2026-11-10", "close": 10}, {"timestamp": None, "close": 11}]}))]
    _, calls = publish(receipts=receipts)
    catalog = EvidenceCatalog.build(receipts, run_scope="narrative")
    full = ReportTransport(catalog).table
    assert "fact_order" in full and "fact_order" not in calls[0]
    for key in ("fact_columns", "facts", "fact_rendering_example", "paragraph_rendering_example"):
        assert calls[0][key] == full[key]
    # The complete catalog still restores the original interleaving and null dates.
    assert len(full["fact_order"]) == len(catalog.facts)
    assert any(f.observed_at is None for f in catalog.facts.values())


@pytest.mark.parametrize("raw_field", ["value", "unit"])
def test_numeric_display_is_atomic_and_cannot_omit_its_unit(raw_field):
    result, calls = publish()
    fields = [s for s in calls[-1]["inferences"][0]["segments"] if s["origin"] == "evidence"]
    assert [s["text"] for s in fields if s["field"] == "value_with_unit"] == ["10元/股", "11元/股", "10.00%"]
    assert "10元/股升至11元/股" in result.artifact["reader_body"]
    def omit_unit(doc):
        next(p for p in doc["items"][1]["parts"] if p.get("ref") == "change")["field"] = raw_field
    invalid, _ = publish(mutate=omit_unit)
    assert invalid.verification["report"] == "partial"
    assert not any(r["id"] == "answer" and r["supported"] for r in invalid.artifact["review"]["reviews"])


def revision_case(*, gap=False, unavailable=False, cancel=False, max_calls=4):
    from trade_compass_agent.runtime.exceptions import TurnInterruptedError
    calls, cancelled = [], [False]
    def active():
        if cancelled[0]:
            raise TurnInterruptedError()
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            doc = document(payload)
            if "revision" not in payload:
                doc["items"][1]["parts"] = [text("价格上升。")]
                doc["items"][1]["refs"] = ["change"]
                if gap:
                    doc["items"].append({"id": "revenue", "type": "gap", "section": "gaps", "text": "缺少营收资料"})
            else:
                assert payload["revision"]["review"]["coverage"]["repairable"]
                assert payload["facts"] == calls[0]["facts"]
                assert payload["execution_records"] == calls[0]["execution_records"] == []
                # Deliberately omit the gap: the runtime, not the writer, retains it.
            return json.dumps(doc)
        revised = len(calls) == 4
        if revised and unavailable:
            raise TimeoutError("second review unavailable")
        if cancel:
            cancelled[0] = True
        response = {"reviews": [{"id": "answer", "supported": True, "reason": ""}],
                    "coverage": {"complete": revised, "missing": [] if revised else ["正文未写日期、收盘价及比例"],
                                 "repairable": not revised}}
        if gap:
            response["gap_reviews"] = [{"id": "revenue", "status": "outside_task" if revised else "required",
                                        "refs": [], "reason": "试图删除缺口" if revised else ""}]
        return json.dumps(response)
    result = publish_report(task="比较两日收盘价及比例", draft="", tool_results=iter(RECEIPTS),
                            run_scope="revision", call=call, check_active=active, max_calls=max_calls)
    return result, calls


def test_semantic_feedback_repairs_omission_then_reviews_the_final_body():
    result, calls = revision_case()
    assert len(calls) == result.artifact["model_calls"] == result.artifact["call_limit"] == 4
    assert result.verification["status"] == "complete"
    assert "10元/股升至11元/股" in result.artifact["reader_body"]
    assert "10.00%" in result.artifact["reader_body"]
    assert calls[-1]["report"] == result.artifact["reader_body"]
    assert result.artifact["selected_attempt"] == 1
    assert len(result.artifact["revision_attempts"]) == 2
    assert result.artifact["revision_attempts"][0]["verification"]["status"] == "incomplete"


def test_revision_cannot_delete_or_dismiss_a_preserved_real_gap():
    result, calls = revision_case(gap=True)
    assert len(calls) == 4
    assert result.verification["report"] == "partial" and result.verification["data"] == "insufficient"
    assert "缺少营收资料" in result.artifact["reader_body"]
    assert "10.00%" in result.artifact["reader_body"]
    assert any(i["id"] == "revenue" for i in result.artifact["document"]["items"])


def test_unavailable_revision_retains_the_first_reviewed_evidence():
    result, calls = revision_case(unavailable=True)
    assert len(calls) == 4 and result.verification["report"] == "partial"
    assert result.artifact["selected_attempt"] == 0
    assert result.text == result.artifact["revision_attempts"][0]["published_text"]
    assert "价格上升。" in result.artifact["reader_body"]
    assert result.artifact["facts"] and result.artifact["calculations"]


@pytest.mark.parametrize("budget", [2, 3])
def test_revision_requires_budget_for_both_writing_and_review(budget):
    result, calls = revision_case(max_calls=budget)
    assert len(calls) == 2 and result.verification["report"] == "partial"
    assert "revision_attempts" not in result.artifact


def test_cancellation_prevents_semantic_revision():
    from trade_compass_agent.runtime.exceptions import TurnInterruptedError
    with pytest.raises(TurnInterruptedError):
        revision_case(cancel=True)


@pytest.mark.parametrize("invalid_gap_decision", [False, True])
def test_real_data_gap_does_not_retry_or_discard_an_independently_valid_body(invalid_gap_decision):
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "schema" in payload:
            doc = document(payload)
            doc["items"].append({"id": "revenue", "type": "gap", "section": "gaps", "text": "缺少营收资料"})
            return json.dumps(doc)
        return json.dumps({"reviews": [{"id": "answer", "supported": True, "reason": ""}],
            "coverage": {"complete": False, "missing": ["缺少营收"], "repairable": False},
            "gap_reviews": [{"id": "revenue", "status": "already_present" if invalid_gap_decision else "required",
                             "refs": [], "reason": ""}]})
    result = publish_report(task="比较价格和营收", draft="", tool_results=RECEIPTS, run_scope="gap", call=call)
    assert len(calls) == 2 and result.verification["report"] == "partial"
    assert "10元/股升至11元/股" in result.artifact["reader_body"]
    assert "缺少营收资料" in result.artifact["reader_body"]
    assert result.artifact["review_valid"]
    assert any(a.get("gap_review_error_type") == "invalid_gap_review" for a in result.artifact["attempts"]) is invalid_gap_decision


@pytest.mark.parametrize("missing", [False, True])
def test_reader_incomplete_notice_does_not_repeat_internal_review_or_false_claims(missing):
    reason = "segments中author新增的‘该公司已经获批注册证’没有receipts支持，schema字段不可作为依据。"
    def mutate(doc):
        doc["items"].append({**doc["items"][1], "id": "second"})
        if missing:
            doc["items"].append({"id": "gap", "type": "gap", "section": "gaps", "text": "缺少营收资料，无法比较营收变化。"})
    report, _ = publish(verdict=False, reason=reason, mutate=mutate)
    assert reason not in report.text
    assert "注册证" not in report.text and "receipts" not in report.text
    assert report.text.count("研判部分尚未完成，相关结论暂无法确认。") == 1
    assert ("缺少营收资料" in report.text) is missing
    assert report.verification["report"] == "partial"
    assert report.verification["execution"] == "completed"
    assert report.verification["data"] == ("insufficient" if missing else "available")
    assert any(row["message"] == reason for row in report.verification["findings"])
    assert report.artifact["review"]["reviews"][0]["reason"] == reason


@pytest.mark.parametrize("observation", [False, True])
def test_all_rejected_prose_retains_cited_readable_evidence(observation):
    def mutate(doc):
        if observation:
            doc["items"].append({"id": "price", "type": "fact", "section": "market",
                                 "ref": doc["items"][0]["refs"][0]})
    report, calls = publish(verdict=False, mutate=mutate)
    body, sources = report.text.split("[^source-1]:", 1)
    assert "get_bars" not in body and "周期：1d" not in body
    assert r"get\_bars" in sources and "周期：1d" in sources
    assert "10.00%" in body and "10元/股（2026-11-10）" in body
    assert "11元/股（2026-11-11）" in body
    assert "[^source-1]" in body
    assert "研判部分尚未完成" in body
    assert report.verification["report"] == "partial"
    assert report.verification["data"] == "available"
    assert report.artifact["reader_body"] == body.strip()
    assert calls[-1]["receipts"] == report.artifact["receipts"]
