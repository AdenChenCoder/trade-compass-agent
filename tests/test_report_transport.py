import json
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.compression.budget import estimate_tokens
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.report_publication import publish_report
from trade_compass_agent.runtime.report_transport import ReportTransport


def market_receipts():
    return [("get_bars", json.dumps({"symbol": symbol, "timeframe": "1d", "adjustment": "unadjusted",
        "bars": [{"timestamp": (date(2026, 10, 1) + timedelta(days=i)).isoformat(),
                  "open": 10+i/10, "high": 11+i/10, "low": 9+i/10, "close": 10.5+i/10,
                  "volume": 1000+i} for i in range(40)]})) for symbol in ["000001", "000002", "000003", "000004", "000005", "000006", "000007", "000008"]]


def test_full_large_catalog_fits_without_losing_values_or_scopes():
    catalog = EvidenceCatalog.build(market_receipts(), run_scope="large")
    original = [asdict(f) for f in catalog.facts.values()]
    transport = ReportTransport(catalog)
    decoded = report_facts(transport.table)
    columns = catalog.model_table()["fact_columns"]
    assert len(decoded) > 1600
    for actual, expected in zip(decoded, original, strict=True):
        actual["id"] = transport.references[actual["id"]]
        assert actual == {key: expected[key] for key in columns}
    assert estimate_tokens(json.dumps(transport.table, ensure_ascii=False)) < 70000
    assert estimate_tokens(json.dumps(catalog.model_table(), ensure_ascii=False)) > 98000
    assert [asdict(f) for f in catalog.facts.values()] == original


def test_short_references_never_escape_to_artifacts_or_calculation_provenance():
    receipts = market_receipts()
    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["subject"] == "000008" and f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [
                {"id": "delta", "type": "calculation", "section": "market", "operation": "change_pct", "refs": [facts[-2]["id"], facts[-1]["id"]]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task="比较最后一个标的最后两日收盘价", draft="", tool_results=receipts, run_scope="large", call=call)
    assert result.verification["status"] == "complete"
    canonical = EvidenceCatalog.build(receipts, run_scope="large").facts
    assert all(ref in canonical for ref in result.artifact["document"]["items"][0]["refs"])
    assert len(result.artifact["facts"]) == len(canonical)
    assert set(result.artifact["composition_input"]["fact_aliases"].values()) == set(canonical)
    assert result.artifact["composition_input"]["task"] == "比较最后一个标的最后两日收盘价"
    assert "000008" in result.text and "0.70%" in result.text


@pytest.mark.parametrize("invalid", [
    {"id": "bad", "type": "fact", "section": "invented", "ref": "unknown"},
    {"type": "fact", "section": "market", "ref": "unknown"},
    42,
])
def test_malformed_sibling_cannot_erase_valid_observations(invalid):
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":false,"missing":["尚有未核验项"]}}'
        fact = next(f for f in report_facts(payload) if f["metric"] == "close")
        if len(calls) == 1:
            return json.dumps({"schema_version": 1, "items": [
                {"id": "valid", "type": "fact", "section": "market", "ref": fact["id"]}, invalid]})
        assert payload["structure_errors"]
        return "invalid JSON"
    result = publish_report(task="列出报价与另一项内容", draft="", tool_results=market_receipts()[:1], run_scope="partial", call=call)
    assert result.verification["report"] == "partial"
    assert result.verification["status"] == "incomplete"
    assert result.verification["execution"] == "completed"
    assert "10.5" in result.text
    assert "尚未完成核验" in result.text
    assert len(calls) == 4
    assert result.artifact["attempts"][0]["response"]
    assert result.artifact["attempts"][0]["structure_errors"]


@pytest.mark.parametrize("partial_repair", [False, True])
def test_repair_uses_same_aliases_and_cannot_replace_verified_fact(partial_repair):
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        facts = [f for f in report_facts(payload) if f["metric"] == "close"]
        if len(calls) == 1:
            return json.dumps({"schema_version": 1, "items": [
                {"id": "kept", "type": "fact", "section": "market", "ref": facts[0]["id"]},
                {"id": "bad", "type": "fact", "section": "market", "ref": "unknown"}]})
        assert payload["previous_document"]["items"][0]["ref"] == facts[0]["id"]
        assert payload["repair_evidence"] == [{"item_id": "bad", "facts": [], "refs_not_in_fact_catalog": ["unknown"]}]
        items = [
            {"id": "kept", "type": "fact", "section": "market", "ref": facts[-1]["id"]},
            {"id": "bad", "type": "fact", "section": "market", "ref": facts[1]["id"]}]
        return json.dumps({"schema_version": 1, "items": items[1:] if partial_repair else items})
    result = publish_report(task="列出首两日收盘价", draft="", tool_results=market_receipts()[:1], run_scope="repair", call=call)
    assert result.verification["status"] == "complete"
    assert "10.5" in result.text and "10.6" in result.text and "14.4" not in result.text


def test_repair_expands_exact_sources_without_promoting_report_calculations():
    from trade_compass_agent.runtime.report_contract import Finding, ReportDocument
    catalog = EvidenceCatalog.build(market_receipts()[:1], run_scope="repair")
    transport = ReportTransport(catalog)
    fact = next(f for f in catalog.facts.values() if f.metric == "close")
    document = ReportDocument.model_validate({"schema_version": 1, "items": [{
        "id": "bad", "type": "calculation", "section": "market", "operation": "share_pct", "refs": [fact.id, "report_sum"]}]})
    context = transport.repair_evidence(document, [Finding("report", "invalid_evidence", "invalid", "bad")])[0]
    assert context["refs_not_in_fact_catalog"] == ["report_sum"]
    expected = {k: getattr(fact, k) for k in catalog.model_table()["fact_columns"]}
    expected["id"] = transport.aliases[fact.id]
    assert context["facts"] == [expected]


@pytest.mark.parametrize("fault", ["shadow", "duplicate", "envelope"])
def test_ambiguous_identity_or_envelope_is_still_rejected(fault):
    def call(system, user):
        payload = json.loads(user)
        fact = next(f for f in report_facts(payload) if f["metric"] == "close")
        item = {"id": fact["id"] if fault == "shadow" else "same", "type": "fact", "section": "market", "ref": fact["id"]}
        value = {"schema_version": 2 if fault == "envelope" else 1, "items": [item, item] if fault == "duplicate" else [item]}
        return json.dumps(value)
    result = publish_report(task="报价", draft="", tool_results=market_receipts()[:1], run_scope="reject", call=call)
    assert result.verification["report"] == "unavailable"
    assert result.verification["execution"] == "completed"
    assert len(result.artifact["attempts"]) == 3
