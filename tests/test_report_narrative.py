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


def value(ref, field="value"):
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


def publish(*, reverse=False, mutate=None, verdict=True, unavailable=False, receipts=RECEIPTS):
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
                           "reason": "" if verdict else "价格资料不能支持该结论"} for p in payload["inferences"]],
                           "coverage": {"complete": True, "missing": []}})
    return publish_report(task="比较两日收盘价", draft="", tool_results=receipts,
                          run_scope="narrative", call=call), calls


@pytest.mark.parametrize("reverse", [False, True])
def test_final_natural_paragraph_is_reviewed_and_persisted_without_rewriting(tmp_path, reverse):
    report, calls = publish(reverse=reverse)
    assert report.verification["status"] == "complete"
    assert len(calls) == 2
    assert "10元/股升至11元/股" in report.text
    assert "10.00%" in report.text
    assert not report.text.startswith("#")
    assert "研判：" not in report.text and "| 指标 |" not in report.text
    assert calls[-1]["report"] == report.text
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
    assert "引用不属于本轮可核验资料" in report.text

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
        assert "10.00%" in payload["report"]
        assert "10.00%" not in payload["reader_body"]
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
