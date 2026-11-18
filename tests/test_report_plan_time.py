"""A proposed action has no observation date; its empirical premises still do."""
import json

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('text', [
    '明日两只标的均以持有观察为主。',
    '整体需跟踪大盘企稳后的板块轮动。',
    '暂不改变观察安排，待条件成立后再考虑调整。',
])
@pytest.mark.parametrize('fault', [None, 'missing', 'foreign', 'empirical', 'unknown'])
def test_independently_confirmed_plan_repairs_spurious_present_observation(tmp_path, text, fault):
    result, stages = _publish(tmp_path, text, fault=fault)
    expected = fault is None
    assert (result.verification['status'] == 'complete') is expected
    assert (text in result.artifact['reader_body']) is expected
    assert '002415在2025-01-01的收盘价为22元/股。' in result.artifact['reader_body']
    assert stages == ['writer', 'body', 'fact', 'coverage']
    assert result.artifact['model_calls'] == 4
    assert result.artifact['body_review_model_calls'] == result.artifact['factual_model_calls'] == 1
    if expected:
        resolution = result.artifact['annotation_resolutions'][0]
        assert resolution['previous_statement']['observation_dates'] == ['task_now']
        assert resolution['statement']['observation_dates'] == []
        assert resolution['assessment']['checks']['time'] == 'not_applicable'


@pytest.mark.parametrize('premise', ['bound_value', 'unbound_value', 'qualitative', 'actor', 'distribution', 'explicit_calendar'])
def test_plan_label_and_optimistic_repair_cannot_erase_empirical_obligations(tmp_path, premise):
    result, stages = _publish(tmp_path, '观察安排。', premise=premise)
    assert result.verification['status'] == 'incomplete'
    assert not result.artifact['annotation_resolutions']
    assert '002415在2025-01-01的收盘价为22元/股。' in result.artifact['reader_body']
    assert stages == ['writer', 'body', 'fact', 'coverage']


def _publish(tmp_path, text, *, fault=None, premise=None):
    receipts = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
        'bars': [{'timestamp': '2025-01-01', 'close': 22}]})),
        ('get_fundamentals', json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': '20250101'}))]
    stages = []
    texts = {'unbound_value': '基于公司当前市净率0.8倍，继续持有观察。',
             'qualitative': '今天价格波动明显，之后继续观察。',
             'actor': '主力今天没有出货，之后继续观察。',
             'explicit_calendar': '2025年1月1日观察状态保持不变，之后继续跟踪。',
             'distribution': '以后关注前期成交密集区附近的压力。'}
    text = texts.get(premise, text)
    def assessment(identifier, *, target=False, repaired=False):
        return {'id': identifier, 'comparison': '显式模型替身，逐项核对原文承诺。',
            'verdict': 'unknown' if repaired and fault == 'unknown' else 'entailed',
            'support_basis': 'reasoned' if target else 'direct',
            'commitments': {'participant_activity': target and premise == 'actor', 'causal_relation': False},
            'time_evidence': [],
            'checks': {'identity_scope': 'not_applicable' if repaired else 'entailed',
                'time': 'not_applicable' if target else 'entailed',
                'quantity': 'entailed' if not target or premise in {'bound_value', 'unbound_value'} else 'not_applicable',
                'support': 'unknown' if repaired and fault == 'unknown' else 'entailed'}}
    def writer(system, user):
        payload = json.loads(user)
        stages.append('writer' if 'schema' in payload else 'coverage')
        if 'schema' in payload:
            facts = report_facts(payload)
            price = next(f['id'] for f in facts if f['metric'] == 'close')
            parts = [text]
            if premise == 'bound_value':
                pb = next(f['id'] for f in facts if f['metric'] == 'pb')
                parts = ['基于公司当前市净率', [pb, 'value_with_unit'], '，继续观察。']
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'section': 'market', 'type': 'paragraph',
                 'parts': ['002415在2025-01-01的收盘价为', [price, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'plan', 'section': 'assessment', 'type': 'paragraph', 'parts': parts, 'refs': []}]})
        value = {'reviews': [{'id': row['id'], 'supported': True, 'reason': '冻结的历史价格与原始资料一致。'}
                             for row in payload['inferences']],
                 'coverage': {'complete': True, 'missing': []}}
        if 'annotation_review' in payload:
            resolution = {'id': 'c0' if fault == 'foreign' else 'c1',
                          'empirical_present': fault == 'empirical',
                          'assessment': assessment('c1', target=True, repaired=True)}
            value['annotation_resolutions'] = [] if fault == 'missing' else [resolution]
        return json.dumps(value)
    def checker(system, user):
        payload = json.loads(user)
        stages.append('body' if 'paragraphs' in payload else 'fact')
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [
                {'id': p['id'], 'parts': [{'temporal_scope': 'unspecified', 'participant_scope': 'none', 'distribution_basis': 'estimated_or_measured'
                    if p['id'] == 'plan' and premise == 'distribution' else 'none',
                    'through': p['source_units'][-1]['id'], 'operation_code': 1 if p['id'] == 'plan' else 0,
                    'claim_kind': 'observation' if p['id'] == 'good' or premise in {'unbound_value', 'qualitative'}
                        else 'actor_activity' if premise == 'actor' else 'judgment',
                    'classifications': [], 'observation_dates':
                        (['2025-01-01', 'task_now'] if premise == 'explicit_calendar' else ['task_now'])
                        if p['id'] == 'plan' else []}]}
                for p in payload['paragraphs']]})
        return json.dumps({'assessments': [assessment(s['id'], target=s['id'] == 'c1')
                                          for s in payload['statements']]})
    report = publish_report(task='说明历史收盘价并给出后续观察安排。', draft='', tool_results=receipts,
        run_scope='plan-time-control', run_id='plan-time-control', call=writer, fact_review_call=checker,
        reference_date='2025-01-02', max_calls=2)
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == report.text
    assert saved['receipts'] == report.artifact['receipts']
    assert saved['document'] == report.artifact['document']
    return report, stages
