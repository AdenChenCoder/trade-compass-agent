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


def test_fact_groups_bind_literal_identity_without_reordering_equal_values():
    receipts = [("get_bars", json.dumps({"symbol": symbol, "account": account,
        "bars": [{"timestamp": "2025-01-29", "close": 50},
                 {"timestamp": "2025-01-30", "close": 51}]}))
        for symbol, account in [("000005", "a"), ("002415", "a"), ("002415", "b"), ("000005", "a")]]
    catalog = EvidenceCatalog.build(receipts, run_scope="identity")
    transport = ReportTransport(catalog)
    assert "subject" not in transport.table["fact_columns"]
    assert "fact_dictionaries" not in transport.table
    assert transport.table["fact_columns"] == ["id", "value", "observed_at"]
    # Series use literal identities, metrics and units. The explicit order table
    # preserves receipt/fact order even when series share equal numeric values.
    groups = transport.table["facts"]
    assert all(isinstance(group["metric"], str) and isinstance(group["unit"], str) for group in groups)
    assert {(group["subject"], group["account"]) for group in groups} == {
        (fact.subject, fact.account) for fact in catalog.facts.values()}
    assert transport.table["fact_order"] == [transport.aliases[fact.id] for fact in catalog.facts.values()]
    decoded = report_facts(transport.table)
    columns = catalog.model_table()["fact_columns"]
    for actual, fact in zip(decoded, catalog.facts.values(), strict=True):
        actual["id"] = transport.references[actual["id"]]
        assert actual == {column: getattr(fact, column) for column in columns}


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
    {"id": [], "type": "fact", "section": "market", "ref": "unknown"},
    {"id": {}, "type": "fact", "section": "market", "ref": "unknown"},
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


@pytest.mark.parametrize("repair_mode", ["full", "partial", "duplicate_fixed", "invalid_fixed", "unrelated_duplicates"])
def test_repair_uses_same_aliases_and_cannot_replace_verified_fact(repair_mode):
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
        assert payload["schema"]["properties"]["items"]["maxItems"] == 1
        assert all(d["properties"]["id"]["enum"] == ["bad"] for d in payload["schema"]["$defs"].values())
        items = [
            {"id": "kept", "type": "fact", "section": "market", "ref": facts[-1]["id"]},
            {"id": "bad", "type": "fact", "section": "market", "ref": facts[1]["id"]}]
        if repair_mode == "partial":
            items = items[1:]
        elif repair_mode == "duplicate_fixed":
            items.append({**items[0], "ref": facts[2]["id"]})
        elif repair_mode == "invalid_fixed":
            items[0] = {"id": "kept", "type": "invented"}
        elif repair_mode == "unrelated_duplicates":
            items.extend([{**items[0], "id": "unrequested"}, {**items[0], "id": "unrequested", "ref": facts[2]["id"]}])
        return json.dumps({"schema_version": 1, "items": items})
    result = publish_report(task="列出首两日收盘价", draft="", tool_results=market_receipts()[:1], run_scope="repair", call=call)
    assert result.verification["status"] == "complete"
    assert "10.5" in result.text and "10.6" in result.text and "14.4" not in result.text


def test_repair_cannot_clear_an_unresolved_item_by_omitting_it():
    calls = []
    def call(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "inferences" in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        fact = next(f for f in report_facts(payload) if f["metric"] == "close")
        if len(calls) == 1:
            items = [{"id": "valid", "type": "fact", "section": "market", "ref": fact["id"]},
                     {"id": "pending", "type": "fact", "section": "market", "ref": "unknown"}]
        else:
            items = [{"id": "new", "type": "fact", "section": "market", "ref": fact["id"]}]
        return json.dumps({"schema_version": 1, "items": items})
    result = publish_report(task="报告", draft="", tool_results=market_receipts()[:1], run_scope="pending", call=call)
    assert result.verification["report"] == "partial"
    assert any(f["item_id"] == "pending" and f["code"] == "invalid_evidence" for f in result.verification["findings"])
    assert len(calls) == 4 and "10.5" in result.text


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


@pytest.mark.parametrize("observed", [None, "2026-11-10"])
def test_fact_rendering_example_matches_consumer_dates_and_missing_time(observed):
    bar = {"close": 34.83, **({"timestamp": observed} if observed else {})}
    receipts = [("get_bars", json.dumps({"symbol": "002415", "bars": [bar],
                 "_meta": {"timestamp": "2026-11-12T12:00:00"}}))]
    examples = []
    reviews = []

    def call(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            example = payload["fact_rendering_example"]
            examples.append(example)
            return json.dumps({"schema_version": 1, "items": [
                {"id": "price", "type": "fact", "section": "market", "ref": example["ref"]}]})
        reviews.append(payload["report"])
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'

    result = publish_report(task="报告事实及其可用时点", draft="", tool_results=receipts, run_scope="dates", call=call)
    # The model still sees the full audit example. Public layout can differ,
    # but must retain the selected identity/value and the same observation time.
    assert examples[0]["rendered"] in reviews[0]
    assert "**002415**" in result.text
    assert ("收盘价 34.83元/股" if observed else "代码 002415") in result.text
    assert (observed if observed else "时点未提供") in result.text
    assert "2026-11-12" not in result.text
    assert "2026-11-12" not in examples[0]["rendered"]
    assert (observed if observed else "时点未提供") in examples[0]["rendered"]
    assert "fact_rendering_example" not in result.text
    assert "fact_rendering_example" not in ReportTransport(EvidenceCatalog.build([], run_scope="empty")).table
