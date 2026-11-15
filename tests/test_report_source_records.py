"""Receipt selection preserves actual data, not just selected numeric rows."""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report
from trade_compass_agent.runtime.report_transport import review_receipts


def source_receipts(alerts=()):
    return [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
        'bars': [{'timestamp': '2025-01-02', 'close': 23}]})),
        ('get_events', json.dumps({'symbol': '002415', 'events': [
            {'date': '2025-01-02', 'title': '取得产品许可公告'}]})),
        ('builtin.check_exit_signals', json.dumps({'message': '规则扫描结果', 'data': {'alerts': list(alerts)}}))]


@pytest.mark.parametrize('alerts', [[], ['条件A'], ['条件A', '条件B']])
def test_unselected_announcement_and_zero_one_many_read_results_reach_review(alerts):
    catalog = EvidenceCatalog.build(source_receipts(alerts), run_scope='read-results')
    selected = [f for f in catalog.facts.values() if f.metric == 'close']
    assert len(selected) == 1
    actual = review_receipts(catalog, selected)
    assert actual == catalog.receipts
    assert json.loads(actual[-1]['result'])['data']['alerts'] == alerts


@pytest.mark.parametrize('draft_tool', ['agent_draft', 'dispatch_specialists'])
@pytest.mark.parametrize('bad', [
    {'is_sample': True}, {'_meta': {'source': 'synthetic'}}, {'has_real_fundamentals': False},
    {'error': 'source failed'},
])
def test_excluded_source_values_are_not_reintroduced_through_the_raw_view(bad, draft_tool):
    raw = json.dumps({'symbol': '000001', 'pb': 23, **bad})
    draft = json.dumps({'title': 'model-created title', 'content': 'model-created price 23'})
    receipts = [source_receipts()[0], ('get_fundamentals', raw), (draft_tool, draft)]
    catalog = EvidenceCatalog.build(receipts, run_scope='excluded')
    assert review_receipts(catalog, []) == catalog.receipts[:1]
    assert catalog.receipts[1]['result'] == raw and catalog.receipts[2]['result'] == draft


@pytest.mark.parametrize('extra', [{'ok': False}, {'available': False}, {'unavailable': True},
    {'error': 'failed'}, {'is_sample': True}])
def test_a_failed_or_simulated_empty_read_is_not_promoted_to_a_successful_read(extra):
    raw = json.dumps({'data': {'alerts': []}, **extra})
    catalog = EvidenceCatalog.build([('builtin.check_exit_signals', raw)], run_scope='read-failure')
    assert review_receipts(catalog, []) == []
    assert catalog.receipts[0]['result'] == raw


@pytest.mark.parametrize('tool,data', [
    ('builtin.check_exit_signals', {'data': {}}),
    ('get_events', {'symbol': '002415', 'events': []}),
    ('get_bars', {'symbol': '002415', 'bars': []}),
])
def test_empty_or_missing_fields_remain_exact_original_read_results(tool, data):
    raw = json.dumps(data)
    catalog = EvidenceCatalog.build([(tool, raw)], run_scope='no-fabricated-records')
    assert all(f.kind == 'identity' for f in catalog.facts.values())
    assert review_receipts(catalog, []) == catalog.receipts
    assert review_receipts(catalog, [])[0]['result'] == raw


def test_deduplicated_numeric_rows_do_not_discard_source_qualifications():
    rows = [source_receipts()[0], ('get_bars', json.dumps({
        'symbol': '002415', 'timeframe': '1d', 'bars': [{'timestamp': '2025-01-02', 'close': 23}],
        'note': '仅覆盖所给观察日，不包含其他观察日。'}))]
    catalog = EvidenceCatalog.build(rows, run_scope='same-fact-new-qualification')
    baseline = EvidenceCatalog.build(rows[:1], run_scope='baseline')
    assert len(catalog.facts) == len(baseline.facts)
    assert {f.receipt_id for f in catalog.facts.values()} == {catalog.receipts[0]['id']}
    assert review_receipts(catalog, []) == catalog.receipts


def test_batch_view_still_excludes_unselected_instruments_and_keeps_original_metadata():
    raw = json.dumps({'_meta': {'volume_unit': 'shares'}, 'results': {
        symbol: {'symbol': symbol, 'bars': [{'timestamp': '2025-01-02', 'close': 23}]}
        for symbol in ('002415', '000001')}})
    catalog = EvidenceCatalog.build([('batch_get_bars', raw)], run_scope='batch')
    selected = [f for f in catalog.facts.values() if f.metric == 'close' and f.entity == '002415']
    view = review_receipts(catalog, selected)
    assert len(view) == 1 and view[0]['selection']['omitted'] == ['000001']
    assert list(json.loads(view[0]['result'])['results']) == ['002415']
    assert json.loads(view[0]['result'])['_meta'] == {'volume_unit': 'shares'}
    assert catalog.receipts[0]['result'] == raw
    assert review_receipts(catalog, []) == []


def test_only_numeric_binding_is_not_a_filter_on_review_sources_or_saved_reader(tmp_path):
    receipts = source_receipts()
    expected = '002415在2025-01-02的收盘价为23元/股。公告提到取得产品许可，本轮规则扫描未发现退出预警。'
    seen = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []}})
        close = next(f for f in report_facts(payload) if f['metric'] == 'close')
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': [[close['id'], 'subject'], '在', [close['id'], 'date'],
                '的收盘价为', [close['id'], 'value_with_unit'], '。公告提到取得产品许可，本轮规则扫描未发现退出预警。'],
            'refs': []}]})
    def assessor(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'observation',
                'observation_dates': ['2025-01-02'], 'classifications': []}]} for p in payload['paragraphs']]})
        seen.append(payload)
        assert [(r['tool'], r['result']) for r in payload['receipts']] == receipts
        assert ''.join(s['text'] for s in payload['statements']) == expected
        return json.dumps({'assessments': [{'id': s['id'], 'commitments': {
            'participant_activity': False, 'causal_relation': False}, 'time_evidence': s['bound_fact_refs'],
            'comparison': 'explicit transport double', 'support_basis': 'direct',
            'checks': dict.fromkeys(('identity_scope', 'time', 'quantity', 'support'), 'entailed'),
            'verdict': 'entailed'} for s in payload['statements']]})
    result = publish_report(task='报告收盘、公告和退出预警情况。', draft='', tool_results=receipts,
        run_scope='receipt-consumer', call=writer, fact_review_call=assessor, max_calls=2)
    assert result.verification['status'] == 'complete' and len(seen) == 1
    assert expected in result.artifact['reader_body']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert [(r['tool'], r['result']) for r in saved['receipts']] == receipts


def test_an_unselected_newer_receipt_cannot_replace_the_bound_observation_date():
    receipts = [('get_fundamentals', json.dumps({'symbol': '600928', 'as_of': day, 'pb': 0.8}))
        for day in ('2025-01-01', '2025-01-02')]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []}})
        old = next(f for f in report_facts(payload) if f['metric'] == 'pb' and f['observed_at'] == '2025-01-01')
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': [[old['id'], 'subject'], '当前市净率为', [old['id'], 'value_with_unit'], '。'], 'refs': []}]})
    def assessor(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'observation',
                'observation_dates': ['task_now'], 'classifications': []}]} for p in payload['paragraphs']]})
        assert len(payload['receipts']) == 2
        return json.dumps({'assessments': [{'id': s['id'], 'commitments': {
            'participant_activity': False, 'causal_relation': False}, 'time_evidence': [],
            'comparison': 'explicit optimistic double', 'support_basis': 'direct',
            'checks': dict.fromkeys(('identity_scope', 'time', 'quantity', 'support'), 'entailed'),
            'verdict': 'entailed'} for s in payload['statements']]})
    result = publish_report(task='报告当前市净率。', draft='', tool_results=receipts,
        run_scope='bound-date', call=writer, fact_review_call=assessor, max_calls=2, reference_date='2025-01-02')
    assert result.verification['status'] != 'complete'
    assert next(iter(result.artifact['temporal_checks'].values()))['status'] == 'unknown'
    assert not result.artifact.get('factual_assessment_errors')
