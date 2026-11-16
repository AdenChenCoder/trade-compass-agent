"""Publication-boundary controls for semantic annotation reconciliation."""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_publication import publish_report, persist_publication


@pytest.mark.parametrize('scene,accepted', [
    ('opinion', True), ('event_interpretation', True),
    ('actor', False), ('cause', False), ('qualitative_observation', False), ('bound_stale_value', False),
])
@pytest.mark.parametrize('fault', [None, 'foreign_id', 'duplicate', 'missing', 'bad_metadata', 'unowned_reference',
                                   'invalid_initial_metadata', 'time_entailed', 'identity_entailed', 'support_unknown'])
def test_annotation_repair_preserves_supported_prose_and_actual_empirical_obligations(tmp_path, scene, accepted, fault):
    texts = {
        'opinion': '从所给资料看，倾向保持观察。',
        'event_interpretation': '结合所给注册证公告，相关产品进展值得继续跟踪。',
        'actor': '这些统计证明当日非主力出货。',
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
        pure = target and scene == 'opinion'
        return {'id': identifier, 'comparison': '独立核对原文承诺与原始资料。', 'verdict': 'entailed',
            'support_basis': 'reasoned' if target else 'direct', 'time_evidence': [],
            'commitments': {'participant_activity': target and scene == 'actor',
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
        result = {'reviews': [], 'coverage': {'complete': True, 'missing': []}}
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
                {'id': p['id'], 'parts': [{'through': p['source_units'][-1]['id'], 'operation_code': 0,
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
    assert bool(repair_inputs) is (scene not in {'qualitative_observation', 'bound_stale_value'}
                                  and fault != 'invalid_initial_metadata')
    assert len(calls) == result.artifact['model_calls'] == 4
    assert calls[:2] == ['writer', 'body']
    assert {stage: calls.count(stage) for stage in calls} == {'writer': 1, 'body': 1, 'fact': 1, 'coverage': 1}
    assert result.artifact['body_review_model_calls'] == result.artifact['factual_model_calls'] == 1
    assert result.artifact['receipts'][0]['result'] == raw
    if succeeds:
        assert result.artifact['annotation_resolutions'] and result.artifact['review_valid']
    if fault == 'support_unknown' and repair_inputs:
        assert result.artifact['review_valid']
        assert not result.artifact['factual_assessment_errors']
        assert any(f['code'] == 'insufficient_support' for f in result.verification['findings'])
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['document'] == result.artifact['document'] and saved['published_text'] == result.text
    assert saved['receipts'] == result.artifact['receipts']
