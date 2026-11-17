"""Independent source veto at the final publication consumer boundary."""
import json

import pytest

from report_contract_helpers import report_facts
from test_report_factual_role import assess, body_reply
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('estimated', [True, False])
def test_independent_review_keeps_unbound_owned_sources_and_their_reference_namespace(estimated):
    receipts = [('get_bars', json.dumps({'symbol': '600928', 'timeframe': '1d',
        'bars': [{'timestamp': '2026-11-12', 'high': 3.84}]}))]
    if estimated:
        receipts.append(('get_chip_distribution', json.dumps({'symbol': '600928', 'date': '2026-11-11',
            'cost_90_high': 3.84, '_meta': {'source': 'eastmoney_cyq_algorithm'},
            'note': '基于K线与换手率的CYQ估算。'})))
    fact_inputs = []
    text = '600928上方3.84元/股为前期成交密集区压力。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            high = next(f['id'] for f in report_facts(payload) if f['metric'] == 'high')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'target', 'type': 'paragraph',
                'section': 'plan', 'parts': ['600928上方', [high, 'value_with_unit'],
                    '为前期成交密集区压力。'], 'refs': []}]})
        first = fact_inputs[-1]
        assert payload['source_contexts'] == first['contexts']
        assert payload['source_fact_references'] == first['source_fact_references']
        assert payload['source_semantics'] == first['source_semantics']
        assert payload['receipts'] == first['receipts']
        assert 'assessments' not in payload
        available = {f['id'] for c in payload['source_contexts'].values() for f in c['evidence']}
        # Method policy references need their actual typed values and scope,
        # not only an address into a separate raw receipt.
        assert all(set(source['fact_refs']) <= available for source in payload['source_semantics'])
        available.update(row[0] for source in payload['source_fact_references']['sources']
                         for observation in source['observations'] for row in observation['rows'])
        assert all(set(source['fact_refs']) <= available for source in payload['source_semantics'])
        return json.dumps({'reviews': [{'id': row['id'], 'supported': estimated,
            'reason': '已有相应筹码估算。' if estimated else '仅有价格高点，没有分布依据。'}
            for row in payload['inferences']], 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps(body_reply(payload))
        fact_inputs.append(payload)
        result = assess(payload)
        result['assessments'][0]['support_basis'] = 'reasoned'
        return json.dumps(result)
    report = publish_report(task='说明所给资料可支持的压力观察位。', draft='', tool_results=receipts,
        run_scope='unbound-estimate-source', call=writer, fact_review_call=checker,
        reference_date='2026-11-12', max_calls=2)
    assert (text in report.artifact['reader_body']) is estimated
    assert (report.verification['status'] == 'complete') is estimated
    assert report.artifact['review_valid'] and not report.artifact['factual_assessment_errors']


@pytest.mark.parametrize('operation', [0, 1])
@pytest.mark.parametrize('fresh_support', ['entailed', 'unknown', 'contradicted'])
def test_repairing_rejected_nonempirical_time_metadata_cannot_clear_the_first_rejection(operation, fresh_support):
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            return json.dumps({'schema_version': 1, 'items': [{'id': 'target', 'type': 'paragraph',
                'section': 'plan', 'parts': ['暂以观察为主。'], 'refs': []}]})
        assert payload['annotation_conflicts']
        row = assess({'statements': [{'id': 'c0'}]})['assessments'][0]
        row.update(support_basis='reasoned', verdict=fresh_support)
        row['checks'].update(identity_scope='not_applicable', time='not_applicable',
                             quantity='not_applicable', support=fresh_support)
        return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []},
            'annotation_resolutions': [{'id': 'c0', 'empirical_present': False, 'assessment': row}]})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'target', 'parts': [{
                'through': payload['paragraphs'][0]['source_units'][-1]['id'], 'operation_code': operation,
                'claim_kind': 'judgment', 'observation_dates': ['task_now'], 'classifications': []}]}]})
        result = assess(payload)
        row = result['assessments'][0]
        row.update(support_basis='reasoned', verdict='unknown')
        row['checks'].update(identity_scope='not_applicable', time='not_applicable',
                             quantity='not_applicable', support='unknown')
        return json.dumps(result)
    report = publish_report(task='说明后续安排。', draft='', tool_results=[], run_scope='negative-metadata',
        call=writer, fact_review_call=checker, reference_date='2026-11-12', max_calls=2)
    assert report.verification['status'] == 'incomplete' and '暂以观察为主。' not in report.artifact['reader_body']
    assert report.artifact['review_valid'] and not report.artifact['factual_assessment_errors']
    assert report.artifact['factual_review']['assessments'][0]['checks']['support'] == (
        'contradicted' if fresh_support == 'contradicted' else 'unknown')
    assert report.artifact['annotation_resolutions'][0]['previous_assessment']['reported_verdict'] == 'unknown'
    assert report.artifact['model_calls'] == 4


@pytest.mark.parametrize('scene,first_supported,second_supported,accepted', [
    ('aggregate', True, True, True), ('actor', True, False, False),
    ('cause', True, False, False), ('actor', False, True, False),
    ('stale', True, True, False),
])
def test_reasoned_empirical_claim_needs_independent_source_support(tmp_path, scene, first_supported, second_supported, accepted):
    receipts = [('compute_volume_ratio', json.dumps({'symbol': '300406', 'as_of': '2026-11-12',
        'volume_ratio': 0.594, 'turnover_pct': 1.18})),
        ('get_fundamentals', json.dumps({'symbol': '600928', 'as_of': '20261111', 'pb': 0.8}))]
    endings = {'aggregate': '，表明相对交易活跃度偏低。', 'actor': '，表明当日非主力出货。',
               'cause': '，证明成交缩量导致了当日股价下跌。', 'stale': '。'}
    audit_inputs, calls = [], []

    def writer(system, user):
        payload = json.loads(user)
        calls.append('writer' if 'schema' in payload else 'coverage')
        if 'schema' in payload:
            facts = report_facts(payload)
            volume = next(f['id'] for f in facts if f['metric'] == 'volume_ratio')
            ref = next(f['id'] for f in facts if f['metric'] == 'pb') if scene == 'stale' else volume
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'type': 'paragraph', 'section': 'market',
                 'parts': ['300406在2026-11-12的量比为', [volume, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'target', 'type': 'paragraph', 'section': 'assessment', 'parts': [
                    '600928当前市净率为' if scene == 'stale' else '300406量比为',
                    [ref, 'value_with_unit'], endings[scene]], 'refs': []}]})
        rows = payload['inferences']
        if rows:
            audit_inputs.append(payload)
            assert {row['id'] for row in rows} == {'c1'}
            assert rows[0]['evidence'] and payload['receipts']
        return json.dumps({'reviews': [{'id': row['id'], 'supported': second_supported,
            'reason': '匿名总量资料不能证明具体参与者或实际因果。' if not second_supported else '所述范围与原始资料相符。'}
            for row in rows], 'coverage': {'complete': True, 'missing': []}})

    def checker(system, user):
        payload = json.loads(user)
        calls.append('body' if 'paragraphs' in payload else 'fact')
        if 'paragraphs' in payload:
            result = body_reply(payload)
            if scene == 'stale':
                result['paragraphs'][1]['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(result)
        result = assess(payload)
        target = result['assessments'][1]
        # Reproduce the actual miss: the mixed statement was called an
        # observation and an approved reasoned interpretation, with both
        # empirical flags false. Correct ownership alone cannot prove it.
        target['support_basis'] = 'reasoned'
        if not first_supported:
            target['checks']['support'] = target['verdict'] = 'unknown'
        return json.dumps(result)

    report = publish_report(task='说明所给资料及其支持的判断。', draft='', tool_results=receipts,
        run_scope='independent-source-review', call=writer, fact_review_call=checker,
        reference_date='2026-11-12', max_calls=2)
    body = report.artifact['reader_body']
    assert '300406在2026-11-12的量比为0.594倍。' in body
    assert (('0.8倍' if scene == 'stale' else endings[scene].strip('，。')) in body) is accepted
    assert (report.verification['status'] == 'complete') is accepted
    assert report.artifact['review_valid'] and not report.artifact['factual_assessment_errors']
    assert bool(audit_inputs) is (first_supported and scene != 'stale')
    assert report.artifact['model_calls'] == len(calls) == 4
    if audit_inputs:
        review = report.artifact['independent_source_reviews']['c1']
        assert review['supported'] is second_supported
        assert report.artifact['factual_review']['assessments'][1]['reported_checks']['support'] == 'entailed'
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == report.text and saved['receipts'] == report.artifact['receipts']


@pytest.mark.parametrize('fault', ['valid_true', 'valid_false', 'missing', 'duplicate', 'unknown_id',
                                   'invalid_boolean', 'unavailable', 'bad_coverage'])
def test_completed_direct_fact_survives_a_separate_source_review_fault(tmp_path, fault):
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'volume_ratio')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'type': 'paragraph', 'section': 'market',
                 'parts': ['300406在2026-11-12量比为', [ref, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'target', 'type': 'paragraph', 'section': 'assessment',
                 'parts': ['相对成交活跃度偏低。'], 'refs': [ref]}]})
        assert [row['id'] for row in payload['inferences']] == ['c1']
        if fault == 'unavailable':
            raise TimeoutError('explicit independent source review timeout')
        row = {'id': 'c1', 'supported': fault != 'valid_false', 'reason': '核对来源范围。'}
        rows = [row]
        if fault == 'missing':
            rows = []
        elif fault == 'duplicate':
            rows.append(row)
        elif fault == 'unknown_id':
            row['id'] = 'foreign'
        elif fault == 'invalid_boolean':
            row['supported'] = 'yes'
        return json.dumps({'reviews': rows, 'coverage': {'complete': True,
            'missing': ['inconsistent coverage metadata'] if fault == 'bad_coverage' else []}})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps(body_reply(payload))
        result = assess(payload)
        result['assessments'][1]['support_basis'] = 'reasoned'
        return json.dumps(result)
    report = publish_report(task='说明量比及相对活跃程度。', draft='', tool_results=[('compute_volume_ratio',
        json.dumps({'symbol': '300406', 'as_of': '2026-11-12', 'volume_ratio': 0.594}))],
        run_scope='independent-source-fault', call=writer, fact_review_call=checker, max_calls=2)
    assert '300406在2026-11-12量比为0.594倍。' in report.artifact['reader_body']
    assert ('相对成交活跃度偏低。' in report.artifact['reader_body']) is (fault == 'valid_true')
    assert (report.verification['status'] == 'complete') is (fault == 'valid_true')
    assert report.artifact['factual_response'] and report.artifact['model_calls'] == 4
    assert not any(f['item_id'] == 'good' for f in report.verification['findings'])
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == report.artifact['reader_body'] and saved['receipts'] == report.artifact['receipts']


@pytest.mark.parametrize('kind,operation,anchors,commitment,support,accepted', [
    ('judgment', 1, [], False, 'entailed', True),
    ('judgment', 1, [], False, 'unknown', False),
    ('observation', 1, [], False, 'entailed', False),
    ('actor_activity', 1, [], False, 'entailed', False),
    ('judgment', 1, ['task_now'], False, 'entailed', False),
    ('judgment', 1, [], True, 'entailed', False),
    ('judgment', 0, [], False, 'entailed', False),
])
def test_nonempirical_choice_does_not_need_measurement_basis_but_preserves_actual_commitments(
        kind, operation, anchors, commitment, support, accepted):
    text = '明日以观察为主。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            return json.dumps({'schema_version': 1, 'items': [{'id': 'plan', 'type': 'paragraph',
                'section': 'plan', 'parts': [text], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'plan', 'parts': [{
                'through': payload['paragraphs'][0]['source_units'][-1]['id'], 'operation_code': operation,
                'claim_kind': kind, 'observation_dates': anchors, 'classifications': []}]}]})
        result = assess(payload)
        row = result['assessments'][0]
        row.update(support_basis='not_applicable')
        row['commitments']['causal_relation'] = commitment
        row['checks'].update(identity_scope='not_applicable', time='not_applicable',
                             quantity='not_applicable', support=support)
        return json.dumps(result)
    report = publish_report(task='给出后续观察安排。', draft='', tool_results=[], run_scope='pure-choice',
        call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
    assert (report.verification['status'] == 'complete') is accepted
    assert (text in report.artifact['reader_body']) is accepted
    assert report.artifact['model_calls'] == 4


@pytest.mark.parametrize('kind', ['observation', 'judgment'])
@pytest.mark.parametrize('day,fresh', [('2025-01-02', True), ('2025-01-01', False)])
@pytest.mark.parametrize('repair', ['matching', 'erase_current', 'empty', 'foreign'])
def test_missing_temporal_link_can_be_repaired_but_current_scope_and_source_date_cannot(tmp_path, kind, day, fresh, repair):
    raw = json.dumps({'symbol': '002415', 'timeframe': '1d', '_meta': {'timestamp': '2025-01-02T15:00:00+08:00'},
        'bars': [{'timestamp': day, 'high': 23, 'low': 22, 'close': 22.5}]})
    callbacks = []
    text = '002415今天盘中存在价格起伏。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            facts = report_facts(payload)
            close = next(f['id'] for f in facts if f['metric'] == 'close')
            refs = [f['id'] for f in facts if f['metric'] in {'high', 'low'}]
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'type': 'paragraph', 'section': 'market',
                 'parts': ['所给记录的收盘价为', [close, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'current', 'type': 'paragraph', 'section': 'assessment', 'parts': [text], 'refs': refs}]})
        callbacks.append(payload)
        context = payload['annotation_review']
        assert context['statements'][0]['requirements']['task_present']
        assert context['statements'][0]['observation_dates'] == ['task_now']
        high = next(f['id'] for f in context['contexts']['p1']['evidence'] if f['metric'] == 'high')
        assessment = assess({'statements': [{'id': 'c1'}]})['assessments'][0]
        assessment['checks']['quantity'] = 'not_applicable'
        assessment['time_evidence'] = [high] if repair == 'matching' else ['foreign'] if repair == 'foreign' else []
        if repair == 'erase_current':
            assessment.update(support_basis='reasoned')
            assessment['checks'].update(identity_scope='not_applicable', time='not_applicable')
        return json.dumps({'reviews': [], 'coverage': {'complete': True, 'missing': []},
            'annotation_resolutions': [{'id': 'c1', 'empirical_present': repair != 'erase_current',
                                        'assessment': assessment}]})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [
                {'id': paragraph['id'], 'parts': [{'through': paragraph['source_units'][-1]['id'],
                    'operation_code': 0, 'claim_kind': kind if paragraph['id'] == 'current' else 'observation',
                    'observation_dates': ['task_now'] if paragraph['id'] == 'current' else [], 'classifications': []}]}
                for paragraph in payload['paragraphs']]})
        result = assess(payload)
        result['assessments'][1]['checks']['quantity'] = 'not_applicable'
        return json.dumps(result)
    report = publish_report(task='说明所给收盘记录和当前价格表现。', draft='',
        tool_results=[('get_bars', raw)], run_scope='witness-repair', run_id='witness-repair',
        call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
    accepted = fresh and repair == 'matching'
    assert (report.verification['status'] == 'complete') is accepted
    assert (text in report.artifact['reader_body']) is accepted
    assert '所给记录的收盘价为22.5元/股。' in report.artifact['reader_body']
    assert callbacks and report.artifact['annotation_candidates'] == {'c1': 'temporal_witness'}
    assert report.artifact['model_calls'] == 4
    if repair == 'matching':
        assert report.artifact['review_valid'] and not report.artifact['factual_assessment_errors']
        assert report.artifact['temporal_checks']['c1']['status'] == ('entailed' if fresh else 'unknown')
    else:
        assert not report.artifact['review_valid']
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['receipts'][0]['result'] == raw and saved['published_text'] == report.text
    assert all(f['observed_at'] == day for f in saved['facts'] if f['kind'] != 'identity')
