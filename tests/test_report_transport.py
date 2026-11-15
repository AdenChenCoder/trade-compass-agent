import json
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.compression.budget import estimate_tokens
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.report_publication import publish_report
from trade_compass_agent.runtime.report_transport import ReportTransport, parse_report_items


def test_category_display_is_available_before_writing_and_final_body_keeps_raw_evidence(tmp_path):
    receipts = [('compute_ma', json.dumps({'symbol': '600928', 'timeframe': '1d',
        '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 3.812, 'MA10': 3.815, 'MA20': 3.652},
        'trend': 'mixed'}))]
    expected = '600928在2025-01-02的均线为混合排列。'
    seen = []
    def writer(system, user):
        payload = json.loads(user)
        seen.append(payload)
        if 'schema' not in payload:
            assert payload['inferences'][0]['text'] == expected
            return '{"reviews":[{"id":"answer","supported":true,"reason":"explicit double"}],"coverage":{"complete":true,"missing":[]}}'
        group = next(g for g in payload['facts'] if g['kind'] == 'classification')
        ref, raw, stamp = group['rows'][0]
        assert raw == 'mixed' and stamp == '2025-01-02'
        assert group['display_values'] == {ref: '混合排列'}
        assert payload['categorical_rendering_example']['rendered'] == '600928的均线排列为混合排列。'
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'market', 'parts': [[ref, 'subject'], '在', [ref, 'date'],
                '的均线为', [ref, 'value_with_unit'], '。'], 'refs': []}]})
    result = publish_report(task='说明所给日期的均线排列。', draft='', tool_results=receipts,
        run_scope='category-display', call=writer, max_calls=2)
    assert len(seen) == 2 and result.verification['status'] == 'complete'
    assert expected in result.artifact['reader_body']
    assert next(f['value'] for f in result.artifact['facts'] if f['kind'] == 'classification') == 'mixed'
    from trade_compass_agent.runtime.report_publication import persist_publication
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['receipts'][0]['result'] == receipts[0][1]


def test_internal_evidence_format_does_not_receive_reader_only_category_examples():
    receipts = [('compute_ma', json.dumps({'symbol': '600928', 'timeframe': '1d',
        '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 3.812, 'MA10': 3.815}, 'trend': 'mixed'}))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        assert 'categorical_rendering_example' not in payload
        assert all('display_values' not in group for group in payload['facts'])
        ref = next(f['id'] for f in report_facts(payload) if f['kind'] == 'classification')
        return json.dumps({'schema_version': 1, 'items': [
            {'id': 'state', 'type': 'fact', 'section': 'market', 'ref': ref}]})
    result = publish_report(task='保留资料中的均线状态。', draft='', tool_results=receipts,
        run_scope='internal-category', call=writer, reader_prose=False, max_calls=2)
    assert result.verification['status'] == 'complete'


def market_receipts():
    return [("get_bars", json.dumps({"symbol": symbol, "timeframe": "1d", "adjustment": "unadjusted",
        "bars": [{"timestamp": (date(2026, 10, 1) + timedelta(days=i)).isoformat(),
                  "open": 10+i/10, "high": 11+i/10, "low": 9+i/10, "close": 10.5+i/10,
                  "volume": 1000+i} for i in range(40)]})) for symbol in ["000001", "000002", "000003", "000004", "000005", "000006", "000007", "000008"]]


@pytest.mark.parametrize('compact', [False, True])
def test_compact_and_legacy_writer_parts_have_the_same_reviewed_persisted_body(tmp_path, compact):
    receipts = [('get_bars', json.dumps({'symbol': '600928', 'bars': [
        {'timestamp': '2025-01-02', 'close': 3.79}]}))]
    expected = ' 原文“🌱”：600928在2025-01-02的收盘价为3.79元/股。\n'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            assert payload['inferences'][0]['text'] == expected
            return '{"reviews":[{"id":"answer","supported":true,"reason":"explicit double"}],"coverage":{"complete":true,"missing":[]}}'
        fact = next(f for f in report_facts(payload) if f['metric'] == 'close')
        ref = fact['id']
        parts = [' 原文“🌱”：', [ref, 'subject'], '在', [ref, 'date'], '的收盘价为',
                 [ref, 'value_with_unit'], '。\n'] if compact else [
            {'type': 'text', 'text': ' 原文“🌱”：'}, {'type': 'value', 'ref': ref, 'field': 'subject'},
            {'type': 'text', 'text': '在'}, {'type': 'value', 'ref': ref, 'field': 'date'},
            {'type': 'text', 'text': '的收盘价为'}, {'type': 'value', 'ref': ref, 'field': 'value_with_unit'},
            {'type': 'text', 'text': '。\n'}]
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'positions', 'parts': parts, 'refs': []}]}, ensure_ascii=False)
    result = publish_report(task='说明所给收盘价和日期。', draft='', tool_results=receipts,
        run_scope='compact-parts', call=writer, max_calls=2)
    assert result.verification['status'] == 'complete'
    assert result.artifact['review_input']['inferences'][0]['text'] == expected
    assert all(isinstance(part, dict) for part in result.artifact['document']['items'][0]['parts'])
    from trade_compass_agent.runtime.report_publication import persist_publication
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['receipts'][0]['result'] == receipts[0][1]
    assert saved['review_input']['inferences'][0]['text'] == expected
    transport = ReportTransport(EvidenceCatalog.build(receipts, run_scope='compact-parts'))
    assert transport.decode_document(transport.encode_document(saved['document'])) == saved['document']


@pytest.mark.parametrize('part', [['e0'], ['e0', 'date', 'extra'], [0, 'date'],
    ['e0', None], ['e0', 'value'], {'type': 'text', 'text': '', 'extra': True}])
def test_compact_wire_cannot_launder_malformed_parts_into_valid_content(part):
    transport = ReportTransport(EvidenceCatalog.build([], run_scope='wire'))
    value = transport.decode_document({'schema_version': 1, 'items': [
        {'id': 'bad', 'type': 'paragraph', 'section': 'overview', 'parts': ['原文。', part], 'refs': []}]})
    document, findings, errors = parse_report_items(value)
    assert not document.items and findings[0].code == 'invalid_structure' and errors


def test_compact_wire_never_interprets_reference_looking_prose():
    transport = ReportTransport(EvidenceCatalog.build([], run_scope='wire'))
    prose = '保留原文 ["e0","date"] 与 {"type":"text","text":"。"}。'
    value = transport.decode_document({'schema_version': 1, 'items': [
        {'id': 'answer', 'type': 'paragraph', 'section': 'overview', 'parts': [prose], 'refs': []}]})
    assert value['items'][0]['parts'] == [{'type': 'text', 'text': prose}]


@pytest.mark.parametrize('position', [0, 1, 3])
def test_zero_character_prose_does_not_erase_a_readable_holding_after_reopen(tmp_path, position):
    receipts = [('get_bars', json.dumps({'symbol': '600928', 'bars': [
        {'timestamp': '2025-01-02', 'close': 3.79}]}))]
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
            parts = [{'type': 'text', 'text': ' 600928收盘价为'},
                     {'type': 'value', 'ref': ref, 'field': 'value_with_unit'},
                     {'type': 'text', 'text': '。\n'}]
            parts.insert(position, {'type': 'text', 'text': ''})
            return json.dumps({'schema_version': 1, 'items': [{'id': 'holding', 'section': 'positions',
                'type': 'paragraph', 'parts': parts, 'refs': []}]})
        assert payload['inferences'][0]['text'] == ' 600928收盘价为3.79元/股。\n'
        return '{"reviews":[{"id":"holding","supported":true,"reason":""}],"coverage":{"complete":true,"missing":[]}}'
    result = publish_report(task='说明600928所给收盘价。', draft='', tool_results=receipts,
        run_scope='zero-character', call=writer, max_calls=2)
    assert result.verification['status'] == 'complete' and len(calls) == 2
    assert '600928收盘价为3.79元/股。' in result.text
    from trade_compass_agent.runtime.report_publication import persist_publication
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text
    assert saved['review_input']['inferences'][0]['text'] == ' 600928收盘价为3.79元/股。\n'
    assert saved['receipts'][0]['result'] == receipts[0][1]
    assert {'type': 'text', 'text': ''} in json.loads(saved['attempts'][0]['response'])['items'][0]['parts']


@pytest.mark.parametrize('parts', [
    [{'type': 'text', 'text': ''}],
    [{'type': 'text', 'text': '', 'unrecognized': True}, {'type': 'text', 'text': '有内容。'}],
    [{'type': 'text', 'text': None}, {'type': 'text', 'text': '有内容。'}],
    [{'type': 'value', 'ref': '', 'field': ''}, {'type': 'text', 'text': '有内容。'}],
])
def test_zero_character_canonicalization_cannot_accept_an_empty_or_malformed_paragraph(parts):
    document, findings, errors = parse_report_items({'schema_version': 1, 'items': [
        {'id': 'bad', 'type': 'paragraph', 'section': 'overview', 'parts': parts, 'refs': []}]})
    assert not document.items and findings[0].code == 'invalid_structure' and errors


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


@pytest.mark.parametrize('stamp,expected', [
    ('2026-11-10T00:00:00', '2026-11-10 00:00:00（北京时间）'),
    ('2026-11-09T16:00:00+00:00', '2026-11-10 00:00:00（北京时间）'),
    (None, None),
])
def test_writer_date_example_is_the_actual_reader_binding_without_rewriting_source(stamp, expected):
    raw = json.dumps({'symbol': '300406', 'events': [{'title': '取得注册证书公告', 'timestamp': stamp}]})
    seen = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [{'id': row['id'], 'supported': True, 'reason': ''}
                for row in payload['inferences']], 'coverage': {'complete': True, 'missing': []}})
        example = payload.get('date_rendering_example')
        seen.append(example)
        if expected is None:
            assert example is None
            return '{"schema_version":1,"items":[{"id":"gap","type":"gap","section":"overview","text":"公告时点未提供。"}]}'
        fact = next(f for f in report_facts(payload) if f['id'] == example['part'][0])
        assert fact['observed_at'] == example['observed_at']
        assert example['observed_at'] == '2026-11-09T16:00:00+00:00'
        assert example['rendered'] == expected
        return json.dumps({'schema_version': 1, 'items': [{'id': 'date', 'type': 'paragraph', 'section': 'overview',
            'parts': [{'type': 'text', 'text': '所给公告记录时点为'}, example['part']], 'refs': []}]})
    result = publish_report(task='说明所给公告记录时点。', draft='', tool_results=[('get_events', raw)],
        run_scope='writer-calendar', call=writer, reader_prose=True)
    assert seen
    assert result.artifact['receipts'][0]['result'] == raw
    assert (result.verification['status'] == 'complete') is (expected is not None)
    if expected:
        assert expected in result.artifact['reader_body']
        assert result.artifact['composition_input']['fact_aliases'][seen[0]['part'][0]] in {
            fact['id'] for fact in result.artifact['facts']}
    assert 'date_rendering_example' not in result.text


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


def test_composer_retains_non_numeric_tool_context_without_a_second_fact_namespace():
    raw = {'symbol': '002415', 'timeframe': '1d', '_meta': {'as_of': '2025-01-02'},
           'ma': {'MA5': 22, 'MA10': 23, 'MA20': 21}, 'trend': 'mixed',
           'interpretation': '来源自己的限制，不是新增数值事实。'}
    receipts = [('compute_ma', json.dumps(raw, ensure_ascii=False))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [{'id': row['id'], 'supported': True, 'reason': ''}
                for row in payload['inferences']], 'coverage': {'complete': True, 'missing': []}})
        source = payload['source_records'][0]
        assert source['tool'] == 'compute_ma' and source['data'] == raw
        assert source['data']['trend'] == 'mixed'
        assert source['receipt_id'] not in {f['id'] for f in report_facts(payload)}
        assert not any(f['metric'] == 'interpretation' for f in report_facts(payload))
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'overview', 'parts': [{'type': 'text', 'text': '所给均线结构为交错排列。'}],
            'refs': [f['id'] for f in report_facts(payload) if f['metric'].startswith('MA')]}]})
    result = publish_report(task='说明所给均线状态。', draft='', tool_results=receipts,
        run_scope='source-context', call=writer)
    assert result.verification['status'] == 'complete'
    assert '所给均线结构为交错排列。' in result.artifact['reader_body']
    assert result.artifact['receipts'][0]['result'] == receipts[0][1]
    assert not any(key in result.text for key in ('source_records', 'source-context', 'receipt_id'))


def test_full_large_composition_includes_original_tool_context_with_response_room():
    receipts = market_receipts()
    seen = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [{'id': row['id'], 'supported': True, 'reason': ''}
                for row in payload['inferences']], 'coverage': {'complete': True, 'missing': []}})
        assert estimate_tokens(user) < 131072 - 16384
        assert [row['data'] for row in payload['source_records']] == [json.loads(raw) for _, raw in receipts]
        seen.append(payload)
        fact = next(f for f in report_facts(payload) if f['subject'] == '000008' and f['metric'] == 'close')
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'overview', 'parts': [{'type': 'text', 'text': '000008所给首日收盘价为'},
                {'type': 'value', 'ref': fact['id'], 'field': 'value_with_unit'}, {'type': 'text', 'text': '。'}], 'refs': []}]})
    result = publish_report(task='说明000008所给首日收盘价。', draft='', tool_results=receipts,
        run_scope='large-source-context', call=writer)
    assert seen and result.verification['status'] == 'complete'
    assert '000008所给首日收盘价为10.5元/股。' in result.artifact['reader_body']


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
        assert all(d["properties"]["id"]["enum"] == ["bad"] for d in payload["schema"]["$defs"].values()
                   if "id" in d["properties"])
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
    # Review the exact reader presentation, not a separate audit rendering.
    assert reviews[0] == result.artifact["reader_body"]
    assert "**002415**" in result.text
    assert ("收盘价 34.83元/股" if observed else "代码 002415") in result.text
    assert (observed if observed else "时点未提供") in result.text
    assert "2026-11-12" not in result.text
    assert "2026-11-12" not in examples[0]["rendered"]
    assert (observed if observed else "时点未提供") in examples[0]["rendered"]
    assert "fact_rendering_example" not in result.text
    assert "fact_rendering_example" not in ReportTransport(EvidenceCatalog.build([], run_scope="empty")).table
