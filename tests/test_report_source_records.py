"""Receipt selection preserves actual data, not just selected numeric rows."""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_contract import Calculation, EvidenceCatalog, calculate
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report
from trade_compass_agent.runtime.report_transport import review_receipts, source_record_context, composition_source_records


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
    assert all(f.kind in {'identity', 'quotation'} for f in catalog.facts.values())
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


@pytest.mark.parametrize('alerts,body', [
    ([], '本轮规则扫描没有返回退出预警。'),
    (['条件A'], '本轮规则扫描返回了退出预警。'),
    (['条件A', '条件B'], '本轮规则扫描返回了退出预警。'),
])
def test_same_run_scan_record_can_anchor_unbound_prose_and_survive_reopening(tmp_path, alerts, body):
    receipts = [('builtin.check_exit_signals', json.dumps({'message': '规则扫描结果', 'data': {'alerts': alerts}}))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []}})
        # Source ownership does not depend on selecting a numerical binding.
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': [body], 'refs': []}]})
    def assessor(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'observation',
                'observation_dates': ['task_now'], 'classifications': []}]} for p in payload['paragraphs']]})
        quote = next(f for ctx in payload['contexts'].values() for f in ctx['evidence'] if f['metric'] == 'read_result')
        assert quote['kind'] == 'quotation' and quote['value'] == receipts[0][1]
        assert quote['observed_at'] is None and quote['observed_date'] is None
        return json.dumps({'assessments': [{'id': s['id'], 'commitments': {
            'participant_activity': False, 'causal_relation': False}, 'time_evidence': [quote['id']],
            'comparison': 'explicit transport double: this run scan result, not market-risk absence',
            'support_basis': 'direct', 'checks': dict.fromkeys(('identity_scope', 'time', 'quantity', 'support'), 'entailed'),
            'verdict': 'entailed'} for s in payload['statements']]})
    result = publish_report(task='报告本轮规则扫描返回结果。', draft='', tool_results=receipts,
        run_scope='run-owned-scan', call=writer, fact_review_call=assessor, max_calls=2, reference_date='2025-01-02')
    assert result.verification['status'] == 'complete'
    assert body in result.artifact['reader_body'] and not result.artifact.get('factual_assessment_errors')
    assert next(iter(result.artifact['temporal_checks'].values()))['reason'] == 'read_result_snapshot'
    assert not result.artifact['reviewed_source_refs']
    assert 'read_result' not in result.text and 'alerts' not in result.text
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert [(r['tool'], r['result']) for r in saved['receipts']] == receipts


def test_scan_record_is_an_exact_quotation_not_a_number_or_invented_observation_date():
    rows = [('builtin.check_exit_signals', json.dumps({'data': data}))
        for data in ({}, {'alerts': []})]
    catalog = EvidenceCatalog.build(rows, run_scope='record-not-measurement')
    facts = list(catalog.facts.values())
    assert len(facts) == 2
    assert [f.value for f in facts] == [row[1] for row in rows]
    assert all(f.kind == 'quotation' and f.observed_at is None and f.pointer == '' for f in facts)
    with pytest.raises(ValueError, match='必须为数值'):
        calculate(Calculation(type='calculation', id='invalid', section='assessment',
            operation='difference', refs=[f.id for f in facts]), catalog)
    assert 'alerts' not in json.loads(facts[0].value)['data']


def test_source_only_context_preserves_user_ids_and_never_reintroduces_batch_quotations():
    rows = [*source_receipts(), ('batch_get_bars', json.dumps({'results': {
        '000001': {'symbol': '000001', 'content': 'omitted-instrument-record',
                   'bars': [{'timestamp': '2025-01-02', 'close': 23}]}}}))]
    catalog = EvidenceCatalog.build(rows, run_scope='source-context-scope')
    contexts = {'source_records': {'evidence': [], 'calculations': []}}
    actual = source_record_context(catalog, contexts, catalog.receipts, 'Asia/Shanghai')
    assert actual['source_records'] == contexts['source_records']
    assert set(actual) == {'source_records', '_source_records'}
    assert {f['source'] for f in actual['_source_records']['evidence']} == {'get_bars', 'get_events', 'builtin.check_exit_signals'}
    assert contexts == {'source_records': {'evidence': [], 'calculations': []}}


def test_run_read_snapshot_cannot_waive_an_adjacent_market_observation_date():
    from trade_compass_agent.runtime.report_time import check_statement_time
    context = {'evidence': [
        {'id': 'scan', 'kind': 'quotation', 'source': 'builtin.check_exit_signals', 'observed_at': None,
         'observed_date': None},
        {'id': 'price', 'kind': 'price', 'source': 'get_bars', 'observed_at': '2025-01-01',
         'observed_date': '2025-01-01'}]}
    statement = {'time_scope': 'reference_date', 'assertion_kind': 'observation',
        'bound_fact_refs': [], 'time_fact_refs': ['price'], 'observation_dates': ['task_now']}
    assessment = {'checks': {'quantity': 'entailed'}, 'time_evidence': ['scan']}
    result = check_statement_time(statement, assessment, context,
        {'reference_date': '2025-01-02', 'reference_timezone': 'Asia/Shanghai'})
    assert result['status'] == 'unknown' and result['reason'] == 'observation_not_at_asserted_date'
    assert [row['ref'] for row in result['evidence']] == ['price']


def test_writing_metadata_view_preserves_limits_positions_empty_states_and_all_values():
    from copy import deepcopy
    receipts = [
        ('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d', '_meta': {
            'source': 'frozen-real', 'warnings': ['仅覆盖所给历史日期'], 'volume_unit': 'shares'},
            'vendor_count': 7, 'bars': [
                {'timestamp': '2025-01-01', 'close': 22, 'volume': 100},
                {'timestamp': '2025-01-02', 'close': 23, 'volume': 120}]})),
        ('compute_ma', json.dumps({'symbol': '002415', 'ma': {'MA5': 22, 'MA10': 23}})),
        ('builtin.check_exit_signals', json.dumps({'data': {'alerts': []}})),
        ('get_fundamentals', json.dumps({'symbol': '002415', 'pb': 0.8, 'error': 'unavailable'})),
    ]
    catalog = EvidenceCatalog.build(receipts, run_scope='lossless-metadata')
    before = deepcopy(catalog.receipts)
    views = composition_source_records(catalog)
    assert views[0]['data']['bars'] == [{'timestamp': '2025-01-01'}, {'timestamp': '2025-01-02'}]
    assert views[0]['data']['vendor_count'] == 7
    assert views[0]['data']['_meta'] == json.loads(receipts[0][1])['_meta']
    assert views[0]['selection']['omitted_numeric_values'] == 4
    assert [v['data'] for v in views[1:]] == [json.loads(raw) for _, raw in receipts[1:]]
    # Independently reconstruct every source value from its original canonical
    # pointer and type. No selector can remove an unindexed field or list item.
    for view, receipt in zip(views, catalog.receipts, strict=True):
        restored = deepcopy(view['data'])
        original = json.loads(receipt['result'])
        for fact in catalog.facts.values():
            if fact.receipt_id != receipt['id'] or not fact.pointer:
                continue
            keys = [s.replace('~1', '/').replace('~0', '~') for s in fact.pointer.split('/')[1:]]
            old, new = original, restored
            for key in keys[:-1]:
                old = old[int(key)] if isinstance(old, list) else old[key]
                new = new[int(key)] if isinstance(new, list) else new[key]
            if isinstance(new, dict) and keys[-1] not in new:
                value = old[keys[-1]]
                assert type(value) in (int, float) and str(value) == fact.value
                new[keys[-1]] = value
        assert restored == original
    assert catalog.receipts == before


@pytest.mark.parametrize('body', [
    '300406当前日线RSI处于中性区间。',
    '就当前日线RSI而言，300406既不过热，也不过冷。',
])
@pytest.mark.parametrize('symbol,stamp,value,accepted', [
    ('300406', '2026-11-12', 57.22, True),
    ('600928', '2026-11-12', 57.22, False),
    ('300406', '2026-11-11', 57.22, False),
    ('300406', '2026-11-12', 99, False),
])
def test_unselected_indicator_remains_addressable_without_changing_body_or_original_bindings(
        tmp_path, body, symbol, stamp, value, accepted):
    receipts = [
        ('compute_ma', json.dumps({'symbol': '300406', 'timestamp': '2026-11-12', 'ma': {'MA5': 12.782}})),
        ('compute_rsi', json.dumps({'symbol': symbol, 'timestamp': stamp, 'current_rsi': value})),
    ]
    seen, selected = [], []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []}})
        ma = next(f for f in report_facts(payload) if f['metric'] == 'MA5')
        selected.append(ma['id'])
        return json.dumps({'schema_version': 1, 'items': [{'id': 'assessment', 'type': 'paragraph',
            'section': 'assessment', 'parts': [body], 'refs': [ma['id']]}]})
    def assessor(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'judgment',
                'observation_dates': ['task_now'], 'classifications': []}]} for p in payload['paragraphs']]})
        seen.append(payload)
        facts = [f for ctx in payload['contexts'].values() for f in ctx['evidence']]
        rsi = next((f for f in facts if f['metric'] == 'current_rsi'), None)
        # A real assessor must decide identity and support independently. An
        # optimistic date verdict is deliberately checked by the runtime.
        checks = dict.fromkeys(('identity_scope', 'time', 'quantity', 'support'), 'entailed')
        if rsi is None:
            checks['time'] = 'unknown'
        elif rsi['entity'] != '300406':
            checks['identity_scope'] = 'unknown'
        elif not 30 < float(rsi['value']) < 70:
            checks['support'] = 'contradicted'
        verdict = 'entailed' if set(checks.values()) == {'entailed'} else 'unknown'
        return json.dumps({'assessments': [{'id': s['id'], 'commitments': {
            'participant_activity': False, 'causal_relation': False},
            'time_evidence': [rsi['id']] if rsi else [], 'comparison': 'explicit source-identity/value double',
            'support_basis': 'direct', 'checks': checks, 'verdict': verdict} for s in payload['statements']]})
    result = publish_report(task='判断300406当前日线RSI是否处于中性区间。', draft='', tool_results=receipts,
        run_scope='unselected-numeric', call=writer, fact_review_call=assessor, max_calls=2,
        reference_date='2026-11-12')
    assert len(seen) == 1 and result.artifact['model_calls'] == 4
    assert [(r['tool'], r['result']) for r in seen[0]['receipts']] == receipts
    assert (result.verification['status'] == 'complete') is accepted
    original = result.artifact['document']['items'][0]
    assert original['parts'] == [{'type': 'text', 'text': body}]
    assert len(original['refs']) == 1  # Original writer bindings stay immutable.
    rsi = next(f for ctx in seen[0]['contexts'].values() for f in ctx['evidence'] if f['metric'] == 'current_rsi')
    assert rsi['observed_at'] == stamp and rsi['value'] == str(value)
    assert not result.artifact.get('factual_assessment_errors')
    if accepted:
        assert result.artifact['reader_body'] == body + '[^source-1]'
        assert 'MA5' in result.text and '57.22' in result.text
        assert result.artifact['reviewed_source_refs'] == {'assessment': [rsi['id']]}
    else:
        assert body not in result.artifact['reader_body']
        assert not result.artifact['reviewed_source_refs']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['reviewed_source_refs'] == result.artifact['reviewed_source_refs']
    assert [(r['tool'], r['result']) for r in saved['receipts']] == receipts
