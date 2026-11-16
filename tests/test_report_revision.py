"""Consumer regressions for bounded edits; every model result is an explicit double."""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_body import reader_source_units
from trade_compass_agent.runtime.report_contract import EvidenceCatalog, ReportDocument, paragraph_text
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report
from trade_compass_agent.runtime.report_transport import ReportTransport


RECEIPTS = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
    'bars': [{'timestamp': '2025-01-02', 'close': 23}]}))]


@pytest.mark.parametrize('repair_status', ['valid', 'unsupported', 'unavailable'])
def test_publisher_preserves_correct_observation_when_neighbor_is_repaired(tmp_path, repair_status):
    writes, bodies, reviews = [], [], []
    price = '002415在2025-01-02的收盘价为23元/股。'
    original_error = '所给价格证明机构没有卖出。'
    replacement = ('所给价格足以证明机构没有卖出。' if repair_status == 'unsupported' else
                   '所给价格资料不足以识别交易主体。')

    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            writes.append(payload)
            fact = next(f for f in report_facts(payload) if f['metric'] == 'close')
            if 'revision' in payload:
                plan = payload['revision'].get('assertion_edits')
                if plan:
                    assert len(plan['edits']) == 1
                    assert plan['edits'][0]['text'] == original_error
                    return json.dumps({'schema_version': 1, 'edits': [{
                        'id': plan['edits'][0]['id'], 'parts': [replacement], 'refs': []}], 'gaps': []})
                # The old whole-report revision keeps both in one failure domain.
                return json.dumps({'schema_version': 1, 'items': [{
                    'id': 'answer', 'type': 'paragraph', 'section': 'market', 'parts': [
                        '002415在2025-01-02的收盘价为', [fact['id'], 'value_with_unit'],
                        '。' + replacement], 'refs': []}]})
            return json.dumps({'schema_version': 1, 'items': [{
                'id': 'answer', 'type': 'paragraph', 'section': 'market',
                'parts': ['002415在2025-01-02的收盘价为', [fact['id'], 'value_with_unit'],
                          '。' + original_error], 'refs': []}]})
        complete = price in payload['report']
        return json.dumps({'reviews': [], 'coverage': {'complete': complete,
            'missing': [] if complete else ['缺少日期及收盘价。'], 'repairable': not complete}})

    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            bodies.append(payload)
            rows = []
            for paragraph in payload['paragraphs']:
                units = reader_source_units(paragraph['text'])
                first = len(bodies) == 1
                observation = paragraph['text'].startswith(price)
                parts = [{'through': units[0 if first else -1]['id'], 'operation_code': 0,
                    'claim_kind': 'observation' if observation else 'judgment',
                    'observation_dates': ['2025-01-02'] if observation else [], 'classifications': []}]
                if observation and len(units) > 1:
                    parts[0]['through'] = units[0]['id']
                    parts.append({'through': units[-1]['id'], 'operation_code': 0,
                        'claim_kind': 'actor_activity' if first or repair_status == 'unsupported' else 'judgment',
                        'observation_dates': [], 'classifications': []})
                elif repair_status == 'unsupported' and not observation:
                    parts[0]['claim_kind'] = 'actor_activity'
                rows.append({'id': paragraph['id'], 'parts': parts})
            return json.dumps({'quantity_issues': [], 'paragraphs': rows})
        reviews.append(payload)
        assessments = []
        for statement in payload['statements']:
            observation = bool(statement['bound_fact_refs'])
            supported = observation or (len(reviews) == 2 and repair_status != 'unsupported')
            row = {'id': statement['id'], 'verdict': 'entailed' if supported else 'unknown',
                'comparison': '与本轮价格资料核对。', 'support_basis': 'direct' if observation else 'reasoned',
                'commitments': {'participant_activity': not observation and not supported, 'causal_relation': False},
                'time_evidence': [], 'checks': {axis: 'entailed' if supported else 'unknown'
                    for axis in ['identity_scope', 'time', 'quantity', 'support']}}
            if len(reviews) == 2 and repair_status == 'unavailable' and not observation:
                row['time_evidence'] = ['foreign-observation']
            assessments.append(row)
        return json.dumps({'assessments': assessments})

    report = publish_report(task='报告历史收盘价，并说明能否识别交易主体。', draft='',
        tool_results=RECEIPTS, run_scope='bounded-assertion-repair', call=writer,
        fact_review_call=checker, max_calls=4)
    assert price in report.text
    assert (report.verification['status'] == 'complete') is (repair_status == 'valid')
    assert (replacement in report.text) is (repair_status == 'valid')
    assert original_error not in report.text
    assert len(writes) == len(bodies) == len(reviews) == 2
    assert report.artifact['model_calls'] == report.artifact['call_limit'] == 8
    if writes[1]['revision'].get('assertion_edits'):
        assert len(bodies[1]['paragraphs']) == 2
        assert bodies[1]['paragraphs'][0]['text'] == price
        assert bodies[1]['paragraphs'][1]['text'] == replacement
    bindings = [part for item in report.artifact['document']['items'] if item['type'] == 'paragraph'
                for part in item['parts'] if part['type'] == 'value']
    assert len(bindings) == 1 and bindings[0]['field'] == 'value_with_unit'
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == report.text
    assert saved['receipts'] == report.artifact['receipts']


def _fixture():
    from trade_compass_agent.runtime.report_revision import assertion_edit_plan
    catalog = EvidenceCatalog.build(RECEIPTS, run_scope='edit-fixture')
    fact = next(f for f in catalog.facts.values() if f.metric == 'close')
    document = ReportDocument.model_validate({'schema_version': 1, 'items': [{
        'id': 'answer', 'type': 'paragraph', 'section': 'market', 'parts': [
            {'type': 'text', 'text': '原始观察：'}, {'type': 'value', 'ref': fact.id, 'field': 'value_with_unit'},
            {'type': 'text', 'text': '。错误结论。'}], 'refs': []}]})
    texts = ['原始观察：23元/股。', '错误结论。']
    plan = assertion_edit_plan(document, catalog, [{'id': f'c{i}', 'text': text} for i, text in enumerate(texts)],
                               {'answer': ['c0', 'c1']}, {'c1'})
    transport = ReportTransport(catalog)
    revision = {'previous_document': transport.encode_document(document.model_dump()), 'assertion_edits': plan}
    return catalog, transport, revision, document


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'foreign', 'unchanged', 'extra', 'bad_binding',
                                  'smuggled_paragraph', 'reused_gap_identity'])
def test_edit_contract_rejects_unowned_or_unchanged_replacements(fault):
    from trade_compass_agent.runtime.report_revision import apply_assertion_edits
    catalog, transport, revision, _ = _fixture()
    row = {'id': 'c1', 'parts': ['修正后的限制。'], 'refs': []}
    value = {'schema_version': 1, 'edits': [row], 'gaps': []}
    if fault == 'missing':
        value['edits'] = []
    elif fault == 'duplicate':
        value['edits'].append(dict(row))
    elif fault == 'foreign':
        row['id'] = 'not-requested'
    elif fault == 'unchanged':
        row['parts'] = ['错误结论。']
    elif fault == 'extra':
        value['unowned_text'] = '改写相邻观察'
    elif fault == 'bad_binding':
        row['parts'] = [['foreign', 'value_with_unit']]
    elif fault == 'smuggled_paragraph':
        value['gaps'] = [{'id': 'added', 'type': 'paragraph', 'section': 'market',
                          'parts': ['越过允许范围的新段落。'], 'refs': []}]
    else:
        value['gaps'] = [{'id': 'answer', 'type': 'gap', 'section': 'gaps', 'text': '覆盖原段落。'}]
    with pytest.raises((ValueError, KeyError)):
        apply_assertion_edits(value, revision, transport, catalog)


def test_empty_edit_removes_only_failed_assertion_and_keeps_atomic_binding():
    from trade_compass_agent.runtime.report_revision import apply_assertion_edits
    catalog, transport, revision, original = _fixture()
    result = apply_assertion_edits({'schema_version': 1, 'edits': [
        {'id': 'c1', 'parts': [], 'refs': []}], 'gaps': []}, revision, transport, catalog)
    document = ReportDocument.model_validate(result)
    assert len(document.items) == 1
    assert paragraph_text(document.items[0], document, catalog) == '原始观察：23元/股。'
    assert document.items[0].parts[1] == original.items[0].parts[1]


def test_edit_plan_declines_to_split_an_atomic_runtime_value():
    from trade_compass_agent.runtime.report_revision import assertion_edit_plan
    catalog, _, _, document = _fixture()
    assert assertion_edit_plan(document, catalog, [
        {'id': 'c0', 'text': '原始观察：2'}, {'id': 'c1', 'text': '3元/股。错误结论。'}],
        {'answer': ['c0', 'c1']}, {'c1'}) is None
