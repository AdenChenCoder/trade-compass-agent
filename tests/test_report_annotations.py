"""Publication-boundary controls for semantic annotation reconciliation."""
import json

import pytest

from report_contract_helpers import report_facts, source_review_rows
from trade_compass_agent.runtime.report_publication import publish_report, persist_publication


@pytest.mark.parametrize('scene,accepted', [('plan', True), ('named_plan', True), ('opinion', True), ('observation', False),
    ('quantity', False), ('actor', False), ('category', False), ('explicit_date', False), ('bound', False)])
def test_time_only_annotation_disagreement_preserves_empirical_requirements(scene, accepted):
    from test_report_factual_role import assess
    texts = {'plan': '明日两只标的以持有观察为主。', 'named_plan': '整体后续关注两只标的及市场轮动。',
        'opinion': '后续关注市场轮动和催化进展。',
        'observation': '002415今天盘中存在价格起伏。', 'quantity': '002415当前收盘价为23元/股。',
        'actor': '002415机构今天没有卖出。', 'category': '002415均线呈多头排列。',
        'explicit_date': '002415在2025-01-02出现价格起伏。', 'bound': '002415当前收盘价为'}
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            parts = [texts[scene]]
            if scene == 'bound':
                parts += [[next(f['id'] for f in report_facts(payload) if f['metric'] == 'close'), 'value_with_unit'], '。']
            return json.dumps({'schema_version': 1, 'items': [{'id': 'target', 'type': 'paragraph',
                'section': 'assessment', 'parts': parts, 'refs': []}]})
        result = {'reviews': source_review_rows(payload), 'coverage': {'complete': True, 'missing': []}}
        if scene == 'explicit_date':
            # The explicit source double compares the authored historical day
            # with the supplied prior-day record; no annotation may erase it.
            result['reviews'] = [{'id': row['id'], 'supported': False,
                'reason': '所给资料为2025-01-01，不能确认原文声称的2025-01-02观察。'}
                for row in payload['inferences']]
        if 'annotation_review' in payload:
            calls.append(payload)
            fresh = assess(payload['annotation_review'])['assessments'][0]
            fresh.update(time_evidence=[], support_basis='reasoned')
            fresh['checks'].update(identity_scope='not_applicable', time='entailed', quantity='not_applicable')
            result['annotation_resolutions'] = [{'id': fresh['id'], 'empirical_present': False, 'assessment': fresh}]
        return json.dumps(result)
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'target', 'parts': [{'temporal_scope': 'unspecified', 'participant_scope': 'none',
                'distribution_basis': 'none', 'through': payload['paragraphs'][0]['source_units'][-1]['id'],
                'operation_code': 1 if scene in {'plan', 'named_plan'} else 0,
                'claim_kind': 'actor_activity' if scene == 'actor' else 'observation' if scene == 'observation' else 'judgment',
                'observation_dates': ['2025-01-02' if scene == 'explicit_date' else 'task_now'],
                'classifications': [{'property': 'moving_average_order', 'value': 'bullish_alignment',
                    'scope': 'all_mentioned', 'periods': []}] if scene == 'category' else []}]}]})
        result = assess(payload)
        row = result['assessments'][0]
        row.update(support_basis='reasoned', time_evidence=[])
        row['checks'].update(identity_scope='not_applicable' if accepted else 'entailed',
            quantity='entailed' if scene in {'quantity', 'bound'} else 'not_applicable', time='entailed')
        if scene == 'named_plan':
            row['checks'].update(identity_scope='entailed', time='not_applicable')
        return json.dumps(result)
    result = publish_report(task='按所给资料研究并给出观察安排。', draft='',
        tool_results=[('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
            'bars': [{'timestamp': '2025-01-01', 'close': 22}]}))],
        run_scope='time-only-annotation', call=writer, fact_review_call=checker, max_calls=2,
        reference_date='2025-01-02')
    assert (result.verification['status'] == 'complete') is accepted
    assert (texts[scene] in result.artifact['reader_body']) is accepted
    if accepted:
        assert calls and result.artifact['annotation_candidates'] == {'c0': 'opinion_time'}
        assert not result.artifact['factual_assessment_errors']
        assert result.artifact['factual_review']['assessments'][0]['checks']['time'] == 'not_applicable'
    if scene == 'explicit_date':
        assert result.artifact['body_meaning']['target'][0]['observation_dates'] == ['2025-01-02']
        assert not result.artifact['annotation_resolutions']


@pytest.mark.parametrize('scene,accepted', [
    ('opinion', True), ('event_interpretation', True), ('evidence_limit', True), ('actor_na', False),
    ('actor', False), ('cause', False), ('qualitative_observation', False), ('bound_stale_value', False),
])
@pytest.mark.parametrize('fault', [None, 'foreign_id', 'duplicate', 'missing', 'bad_metadata', 'unowned_reference',
                                   'invalid_initial_metadata', 'time_entailed', 'identity_entailed', 'support_unknown'])
def test_annotation_repair_preserves_supported_prose_and_actual_empirical_obligations(tmp_path, scene, accepted, fault):
    texts = {
        'opinion': '从所给资料看，倾向保持观察。',
        'event_interpretation': '结合所给注册证公告，相关产品进展值得继续跟踪。',
        'actor': '这些统计证明当日非主力出货。',
        'actor_na': '这些统计证明当日非主力出货。',
        'evidence_limit': '该总量指标不能识别特定参与者的出货行为。',
        'cause': '注册证公告直接导致了当日股价上涨。',
        'qualitative_observation': '002415今天盘中存在价格起伏。',
        'bound_stale_value': '600928当前市净率为',
    }
    if scene == 'opinion' and fault == 'support_unknown':
        texts['opinion'] = '从所给资料看，倾向维持持有判断。'
    raw = json.dumps({'symbol': '002415', 'timeframe': '1d', 'bars': [
        {'timestamp': '2025-01-01', 'close': 22, 'high': 23, 'low': 21}]})
    receipts = [('get_bars', raw), ('fetch_url', json.dumps({'symbol': '002415', 'as_of': '2025-01-01',
        'content': '公司取得医疗器械注册证书。', 'url': 'https://example.invalid/certificate'})),
        ('get_fundamentals', json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': '20250101'}))]
    calls, repair_inputs = [], []

    def row(identifier, *, target=False, repaired=False):
        pure = target and scene in {'opinion', 'actor_na', 'evidence_limit'}
        return {'id': identifier, 'comparison': '独立核对原文承诺与原始资料。', 'verdict': 'entailed',
            'support_basis': 'not_applicable' if target and scene in {'actor_na', 'evidence_limit'} and not repaired else 'reasoned' if target else 'direct', 'time_evidence': [],
            'commitments': {'participant_activity': target and (scene in {'actor', 'actor_na'} or scene == 'evidence_limit' and not repaired),
                'causal_relation': target and scene in {'cause', 'event_interpretation'} and not (repaired and accepted)},
            'checks': {'identity_scope': 'not_applicable' if pure else 'entailed',
                'time': 'not_applicable' if target else 'entailed',
                'quantity': 'not_applicable' if target else 'entailed', 'support': 'entailed'}}

    def writer(system, user):
        payload = json.loads(user)
        calls.append('writer' if 'schema' in payload else 'coverage')
        if 'schema' in payload:
            facts = report_facts(payload)
            price = next(f['id'] for f in facts if f['metric'] == 'close')
            parts = [texts[scene]]
            if scene == 'bound_stale_value':
                parts += [[next(f['id'] for f in facts if f['metric'] == 'pb'), 'value_with_unit'], '。']
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'section': 'market', 'type': 'paragraph',
                 'parts': ['002415在2025-01-01的收盘价为', [price, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'target', 'section': 'assessment', 'type': 'paragraph', 'parts': parts, 'refs': []}]})
        result = {'reviews': source_review_rows(payload), 'coverage': {'complete': True, 'missing': []}}
        if 'annotation_review' in payload:
            repair = payload['annotation_review']
            repair_inputs.append(repair)
            assert repair['receipts'] and all(x['id'] == 'c1' for x in repair['statements'])
            resolution = {'id': 'c1', 'empirical_present': False, 'assessment': row('c1', target=True, repaired=True)}
            values = [resolution]
            if fault == 'foreign_id':
                resolution['id'] = 'c0'
            elif fault == 'duplicate':
                values.append(resolution)
            elif fault == 'missing':
                values = []
            elif fault == 'bad_metadata':
                resolution['assessment']['checks'].pop('quantity')
            elif fault == 'unowned_reference':
                resolution['assessment']['time_evidence'] = ['outside-this-run']
            elif fault == 'time_entailed':
                resolution['assessment']['checks']['time'] = 'entailed'
            elif fault == 'identity_entailed':
                resolution['assessment']['checks']['identity_scope'] = 'entailed'
            elif fault == 'support_unknown':
                resolution['assessment']['checks'].update(time='entailed', support='unknown')
            result['annotation_resolutions'] = values
        return json.dumps(result)

    def checker(system, user):
        payload = json.loads(user)
        calls.append('body' if 'paragraphs' in payload else 'fact')
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [
                {'id': p['id'], 'parts': [{'temporal_scope': 'unspecified', 'participant_scope': 'none', 'distribution_basis': 'none', 'through': p['source_units'][-1]['id'], 'operation_code': 0,
                    'claim_kind': 'observation' if p['id'] == 'good' else 'judgment', 'classifications': [],
                    'observation_dates': ['task_now'] if p['id'] == 'target' and scene in {
                        'opinion', 'qualitative_observation', 'bound_stale_value'} else []}]}
                for p in payload['paragraphs']]})
        rows = [row(s['id'], target=s['id'] == 'c1') for s in payload['statements']]
        if fault == 'invalid_initial_metadata':
            rows[1]['checks'] = None
        return json.dumps({'assessments': rows})

    result = publish_report(task='说明所给日期的价格，并给出基于这些资料的研究看法。', draft='', tool_results=receipts,
        run_scope='annotation-control', run_id='annotation-control', call=writer, fact_review_call=checker,
        reference_date='2025-01-02', max_calls=2)
    succeeds = accepted and fault in {None, 'time_entailed', 'identity_entailed'}
    assert ('002415在2025-01-01的收盘价为22元/股。' in result.artifact['reader_body'])
    assert (texts[scene] in result.artifact['reader_body']) is succeeds
    assert (result.verification['status'] == 'complete') is succeeds
    assert bool(repair_inputs) is (scene != 'bound_stale_value'
                                  and fault != 'invalid_initial_metadata')
    assert len(calls) == result.artifact['model_calls'] == 4
    assert calls[:2] == ['writer', 'body']
    assert {stage: calls.count(stage) for stage in calls} == {'writer': 1, 'body': 1, 'fact': 1, 'coverage': 1}
    assert result.artifact['body_review_model_calls'] == result.artifact['factual_model_calls'] == 1
    assert result.artifact['receipts'][0]['result'] == raw
    if succeeds:
        assert result.artifact['annotation_resolutions'] and result.artifact['review_valid']
    if fault == 'support_unknown' and repair_inputs and scene != 'qualitative_observation':
        assert result.artifact['review_valid']
        assert not result.artifact['factual_assessment_errors']
        assert any(f['code'] == 'insufficient_support' for f in result.verification['findings'])
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['document'] == result.artifact['document'] and saved['published_text'] == result.text
    assert saved['receipts'] == result.artifact['receipts']
