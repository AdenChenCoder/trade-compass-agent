"""Consumer regressions with explicit body/assessment doubles, no model calls."""
import json
import pytest

from report_contract_helpers import report_facts
from test_report_factual_role import assess
from trade_compass_agent.runtime.indicator_values import moving_average_alignment
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.report_publication import publish_report, persist_publication


def publication(*, values, prose='均线呈多头排列。', operator='eq', periods=(),
                anchors=('task_now',), reference='2025-01-02', observed='2025-01-02',
                extra_receipts=(), bad_witness=False, commitments=None, basis='direct', wire_change=None,
                category_only=False, select_values=None):
    receipt = {'symbol': '600928', 'timeframe': '1d', '_meta': {'as_of': observed},
               'ma': values, 'trend': moving_average_alignment(values)}
    receipts = [('compute_ma', json.dumps(receipt)), *extra_receipts]
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        selected = [f for f in report_facts(payload) if f['kind'] == ('classification' if category_only else 'moving_average') and f['subject'] == '600928']
        if select_values is not None:
            selected = [f for f in selected if float(f['value']) == select_values[f['metric']]]
        parts = ['600928的均线数据为：']
        for f in selected:
            parts.extend([f['label']+'为', [f['id'], 'value_with_unit'], '；'])
        parts.append(prose)
        other = [f['id'] for f in report_facts(payload) if f['subject'] == '002415'] if bad_witness else []
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'market', 'parts': parts, 'refs': other}]})
    def checker(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if 'paragraphs' in payload:
            assert set(payload) == {'paragraphs', 'report_context'}
            assert payload['report_context']['reference_date'] == reference
            part = {
                'through': payload['paragraphs'][0]['source_units'][-1]['id'], 'operation_code': 0,
                'claim_kind': 'observation', 'observation_dates': list(anchors), 'classifications': [{
                    'property': 'moving_average_order', 'value': 'bullish_alignment', 'operator': operator,
                    'scope': 'explicit_subset' if periods else 'all_mentioned', 'periods': list(periods)}]}
            part.update(wire_change or {})
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'answer', 'parts': [part]}]})
        assert all(not {'time_scope', 'assertion_kind', 'meaning_kind'}.intersection(row)
                   for row in payload['statements'])
        answer = assess(payload)
        answer['assessments'][0]['support_basis'] = basis
        if commitments is not None:
            answer['assessments'][0]['commitments'] = commitments
        if bad_witness:
            answer['assessments'][0]['time_evidence'] = [f['id'] for context in payload['contexts'].values()
                for f in context['evidence'] if f['subject'] == '002415']
        return json.dumps(answer)
    return publish_report(task='报告均线现状。', draft='', tool_results=receipts, run_scope='typed-indicator',
        call=writer, fact_review_call=checker, max_calls=2, reference_date=reference), receipts, calls


MIXED = {'MA5': 3.812, 'MA10': 3.815, 'MA20': 3.652, 'MA60': 3.578}
BULLISH = {'MA5': 3.9, 'MA10': 3.8, 'MA20': 3.7, 'MA60': 3.6}


@pytest.mark.parametrize('prose', ['均线呈多头排列。', '按所列均线观察，短周期至长周期构成多头排列。'])
@pytest.mark.parametrize('values,supported', [(MIXED, False), (BULLISH, True)])
def test_typed_category_checks_actual_operands_despite_blanket_model_approval(tmp_path, prose, values, supported):
    result, receipts, calls = publication(values=values, prose=prose)
    assert (result.verification['status'] == 'complete') is supported
    assert (prose in result.artifact['reader_body']) is supported
    assert len(calls) == 2 and result.artifact['model_calls'] == 4
    assert result.artifact['review_valid'] and not result.artifact['factual_assessment_errors']
    check = result.artifact['classification_checks']['c0']
    assert check['claims'][0]['actual'] == moving_average_alignment(values)
    assert check['status'] == ('entailed' if supported else 'unknown')
    assessment = result.artifact['factual_review']['assessments'][0]
    assert assessment['reported_checks']['support'] == 'entailed'
    assert assessment['checks']['support'] == ('entailed' if supported else 'unknown')
    category = next(f for f in result.artifact['facts'] if f['metric'] == 'ma_alignment')
    assert category['value'] == moving_average_alignment(values)
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(ref['artifact_id']+'.json')).read_text())
    assert saved['classification_checks'] == result.artifact['classification_checks']
    assert saved['body_meaning']['answer'][0]['observation_dates'] == ['task_now']
    assert [r['result'] for r in saved['receipts']] == [raw for _, raw in receipts]


@pytest.mark.parametrize('values,supported', [(MIXED, True), (BULLISH, False)])
def test_category_negation_keeps_its_predicate(values, supported):
    result, _, _ = publication(values=values, prose='所列均线尚未构成多头排列。', operator='ne')
    assert (result.verification['status'] == 'complete') is supported
    assert result.artifact['classification_checks']['c0']['claims'][0]['claim']['operator'] == 'ne'


def test_explicit_subset_does_not_invent_whole_system_alignment():
    result, _, _ = publication(values=MIXED, prose='MA20与MA60构成多头排列。', periods=(20, 60))
    assert result.verification['status'] == 'complete'
    check = result.artifact['classification_checks']['c0']['claims'][0]
    assert check['actual'] == 'bullish_alignment' and len(check['evidence']) == 2


@pytest.mark.parametrize('values,bullish', [(MIXED, False), (BULLISH, True)])
@pytest.mark.parametrize('redundant', [[20], [20, 60]])
@pytest.mark.parametrize('operator', ['eq', 'ne'])
def test_whole_system_scope_is_not_narrowed_by_redundant_period_metadata(tmp_path, values, bullish, redundant, operator):
    prose = '均线呈多头排列。' if operator == 'eq' else '均线尚未构成多头排列。'
    result, receipts, calls = publication(values=values, prose=prose, wire_change={'classifications': [{
        'property': 'moving_average_order', 'value': 'bullish_alignment', 'operator': operator,
        'scope': 'all_mentioned', 'periods': redundant}]})
    supported = bullish if operator == 'eq' else not bullish
    assert result.artifact['review_valid'] and not result.artifact['factual_assessment_errors']
    assert (result.verification['status'] == 'complete') is supported
    assert (prose in result.artifact['reader_body']) is supported
    check = result.artifact['classification_checks']['c0']['claims'][0]
    assert check['claim']['periods'] == [] and len(check['evidence']) == 4
    assert check['actual'] == ('bullish_alignment' if bullish else 'mixed')
    original = json.loads(result.artifact['body_response'])['paragraphs'][0]['parts'][0]['classifications'][0]
    assert original['periods'] == redundant and len(calls) == 2
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['body_response'] == result.artifact['body_response']
    assert [r['result'] for r in saved['receipts']] == [raw for _, raw in receipts]


@pytest.mark.parametrize('values,periods', [({}, ()), ({'MA5': 3.8}, ()), (MIXED, (10, 120))])
def test_unavailable_indicator_operands_do_not_manufacture_an_approved_category(values, periods):
    result, _, _ = publication(values=values, periods=periods, anchors=())
    assert result.verification['status'] == 'incomplete'
    assert result.artifact['classification_checks']['c0']['status'] == 'unknown'
    assert not result.artifact['factual_assessment_errors']


def test_reviewer_cannot_use_another_security_to_override_bound_indicator_operands():
    extra = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
        '_meta': {'as_of': '2025-01-02'}, 'ma': BULLISH, 'trend': 'bullish_alignment'}))]
    result, _, _ = publication(values=MIXED, extra_receipts=extra, bad_witness=True)
    assert result.verification['status'] == 'incomplete'
    assert result.artifact['classification_checks']['c0']['claims'][0]['actual'] == 'mixed'
    assert result.artifact['factual_review']['assessments'][0]['time_evidence']


@pytest.mark.parametrize('anchors,reference,observed,supported', [
    (('2025-01-02',), None, '2025-01-02', True),
    (('2025-01-01',), '2025-01-02', '2025-01-01', True),
    (('task_now',), '2025-01-02', '2025-01-01', False),
    (('task_now',), None, '2025-01-01', False),
    (('unresolved',), None, '2025-01-01', False),
])
def test_concrete_calendar_anchor_and_task_present_commitment_survive_the_wire(anchors, reference, observed, supported):
    result, _, _ = publication(values=BULLISH, anchors=anchors, reference=reference, observed=observed)
    assert (result.verification['status'] == 'complete') is supported
    assert result.artifact['factual_review_input']['statements'][0]['observation_dates'] == list(anchors)


def test_categorical_fact_keeps_owned_source_and_normal_display_value():
    catalog = EvidenceCatalog.build([('compute_ma', json.dumps({'symbol': '600928',
        '_meta': {'as_of': '20250102'}, 'ma': MIXED, 'trend': 'mixed'}))], run_scope='category')
    fact = next(f for f in catalog.facts.values() if f.kind == 'classification')
    assert fact.pointer == '/trend' and fact.metric == 'ma_alignment'
    assert fact.observation_date('Asia/Shanghai') == '2025-01-02'
    assert fact.display_value_with_unit == '混合排列'


@pytest.mark.parametrize('commitments', [
    {'participant_activity': True, 'causal_relation': False},
    {'participant_activity': False, 'causal_relation': True},
])
def test_independent_empirical_commitment_cannot_borrow_a_weak_body_category(commitments):
    result, _, _ = publication(values=BULLISH, prose='均线呈多头排列，证明主力已经买入。',
        commitments=commitments, basis='reasoned')
    assert result.verification['status'] == 'incomplete'
    assert result.artifact['body_meaning']['answer'][0]['assertion_kind'] == 'observation'
    assert not result.artifact['factual_assessment_errors']
    assert result.artifact['support_basis_checks']['c0']['reason'] == 'inference_is_not_actor_or_cause_evidence'
    assert result.artifact['factual_review']['assessments'][0]['reported_verdict'] == 'entailed'


def test_evidence_limitation_with_the_same_indicator_data_remains_publishable():
    prose = '均线呈多头排列，但这不能识别参与买入的主体。'
    result, _, _ = publication(values=BULLISH, prose=prose, basis='reasoned')
    assert result.verification['status'] == 'complete' and prose in result.artifact['reader_body']


@pytest.mark.parametrize('change', [
    {'observation_dates': '2025-01-02'}, {'observation_dates': ['2025-02-30']},
    {'observation_dates': ['task_now', 'task_now']}, {'classifications': 'bullish_alignment'},
    {'as_of_task_now': False},
    {'classifications': [{'property': 'moving_average_order', 'value': [], 'scope': 'all_mentioned', 'periods': []}]},
])
def test_malformed_value_extraction_is_unavailable_and_never_a_correct_factual_rejection(change):
    result, _, calls = publication(values=BULLISH, wire_change=change)
    assert result.verification['status'] == 'incomplete' and not result.artifact['review_valid']
    assert result.artifact['factual_model_calls'] == 0 and len(calls) == 1
    assert any(f['code'] == 'review_unavailable' for f in result.verification['findings'])


def test_direct_categorical_binding_does_not_require_repeating_its_numeric_operands():
    result, _, _ = publication(values=BULLISH, category_only=True)
    assert result.verification['status'] == 'complete'
    assert '多头排列' in result.artifact['reader_body']
    assert result.artifact['classification_checks']['c0']['claims'][0]['actual'] == 'bullish_alignment'


def test_repeated_observation_preserves_canonical_deduplication_and_classification():
    newer = {'symbol': '600928', 'timeframe': '1d', '_meta': {'as_of': '2025-01-02'},
             'ma': {**BULLISH, 'MA5': 4.0}, 'trend': 'bullish_alignment'}
    result, _, _ = publication(values=BULLISH, extra_receipts=[('compute_ma', json.dumps(newer))], select_values=newer['ma'])
    assert result.verification['status'] == 'complete'
    assert result.artifact['classification_checks']['c0']['claims'][0]['actual'] == 'bullish_alignment'


def test_conflicting_values_for_one_observation_remain_unknown():
    newer = {'symbol': '600928', 'timeframe': '1d', '_meta': {'as_of': '2025-01-02'},
             'ma': {**BULLISH, 'MA5': 4.0}, 'trend': 'bullish_alignment'}
    result, _, _ = publication(values=BULLISH, extra_receipts=[('compute_ma', json.dumps(newer))])
    assert result.verification['status'] == 'incomplete'
    assert result.artifact['classification_checks']['c0']['claims'][0]['reason'] == 'indicator_values_conflict'
