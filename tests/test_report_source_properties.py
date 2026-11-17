"""Publication must retain source requirements despite optimistic review doubles."""
import json

import pytest

from report_contract_helpers import report_facts
from test_report_factual_role import assess
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('basis', ['direct', 'reasoned'])
@pytest.mark.parametrize('scene,requirement,accepted', [
    ('estimate', 'estimated_or_measured', True), ('estimate', 'estimated', True),
    ('missing', 'estimated_or_measured', False), ('other_estimate', 'estimated_or_measured', False),
    ('estimate', 'measured', False), ('quoted', 'estimated_or_measured', True),
    ('quoted', 'measured', True), ('quoted', 'estimated', True),
    ('other_quoted', 'estimated_or_measured', False), ('title', 'estimated_or_measured', False),
    ('read_result', 'estimated_or_measured', False), ('price_ref', 'estimated_or_measured', False),
    ('quote_not_selected', 'estimated_or_measured', False), ('foreign_ref', 'estimated_or_measured', False),
    ('source_veto', 'estimated_or_measured', False),
])
def test_source_property_is_checked_at_published_and_persisted_consumer(tmp_path, basis, scene, requirement, accepted):
    receipts = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
        'bars': [{'timestamp': '2025-01-02', 'high': 23, 'close': 22.8}]}))]
    if scene in {'estimate', 'other_estimate', 'source_veto'}:
        receipts.append(('get_chip_distribution', json.dumps({
            'symbol': '600928' if scene == 'other_estimate' else '002415', 'date': '2025-01-02',
            'avg_cost': 22, 'cost_90_high': 23, '_meta': {'source': 'eastmoney_cyq_algorithm'}})))
    if scene in {'quoted', 'other_quoted', 'quote_not_selected', 'title'}:
        symbol = '600928' if scene == 'other_quoted' else '002415'
        content = (f'{symbol}在2025-01-02的逐价成交量实测分布显示，23元附近为历史成交密集区。'
                   if requirement == 'measured' else
                   f'筹码算法根据日线及换手率估算，{symbol}在2025-01-02的90%成本区间上沿为23元/股，可作密集区观察位。')
        receipts.append(('fetch_url', json.dumps({'symbol': '600928' if scene == 'other_quoted' else '002415',
            'as_of': '2025-01-02', 'url': 'https://example.invalid/distribution',
            'title' if scene == 'title' else 'content': content})))
    if scene == 'read_result':
        receipts.append(('builtin.check_exit_signals', json.dumps({'message': '成功', 'data': {'alerts': []}})))
    source_inputs, calls = [], []
    def writer(system, user):
        payload = json.loads(user)
        calls.append('writer' if 'schema' in payload else 'source')
        if 'schema' in payload:
            high = next(f['id'] for f in report_facts(payload) if f['metric'] == 'high')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'type': 'paragraph', 'section': 'market',
                 'parts': ['002415在2025-01-02的日线最高价为', [high, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'target', 'type': 'paragraph', 'section': 'plan',
                 'parts': ['002415上方', [high, 'value_with_unit'], '为前期成交密集区压力，后续观察突破。'], 'refs': []}]})
        source_inputs.append(payload)
        assert [row['id'] for row in payload['inferences']] == ['c0', 'c1']
        target = payload['inferences'][1]
        assert target['source_requirements'] == [
            {'property': 'distribution_basis', 'value': requirement}]
        refs = []
        if scene in {'quoted', 'other_quoted', 'title', 'read_result'}:
            refs = [f['id'] for context in payload['source_contexts'].values()
                for f in context['evidence'] if f['kind'] == 'quotation']
        elif scene == 'price_ref':
            refs = [f['id'] for f in target['evidence'] if f['metric'] == 'high']
        elif scene == 'foreign_ref':
            refs = ['not-owned']
        return json.dumps({'reviews': [{'id': 'c0', 'supported': True, 'reason': '显式价格与来源一致。'},
            {'id': 'c1', 'supported': scene != 'source_veto',
            'reason': '故意乐观的审查替身不能豁免来源种类与对象要求。', 'evidence_refs': refs}],
            'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        payload = json.loads(user)
        calls.append('body' if 'paragraphs' in payload else 'fact')
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [
                {'id': row['id'], 'parts': [{'distribution_basis': requirement if row['id'] == 'target' else 'none', 'through': row['source_units'][-1]['id'], 'operation_code': 1 if row['id'] == 'target' else 0,
                    'claim_kind': 'judgment' if row['id'] == 'target' else 'observation',
                    'observation_dates': ['2025-01-02'], 'classifications': []}]}
                for row in payload['paragraphs']]})
        result = assess(payload)
        result['assessments'][1]['support_basis'] = basis
        return json.dumps(result)
    report = publish_report(task='说明所给记录及后续观察位。', draft='', tool_results=receipts,
        run_scope='source-property', call=writer, fact_review_call=checker,
        reference_date='2025-01-02', max_calls=2)
    assert calls == ['writer', 'body', 'fact', 'source'] and len(source_inputs) == 1
    assert report.artifact['review_valid'] is (scene != 'foreign_ref')
    assert (report.verification['status'] == 'complete') is accepted
    assert '日线最高价为23元/股' in report.artifact['reader_body']
    assert ('前期成交密集区压力' in report.artifact['reader_body']) is accepted
    check = report.artifact['source_property_checks']['c1']
    assert check['status'] == ('entailed' if accepted or scene == 'source_veto' else
                               'unavailable' if scene == 'foreign_ref' else 'unknown')
    assert bool(report.artifact['factual_assessment_errors']) is (scene == 'foreign_ref')
    ref = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(ref['artifact_id']+'.json')).read_text())
    assert saved['source_property_checks'] == report.artifact['source_property_checks']
    assert [r['result'] for r in saved['receipts']] == [raw for _, raw in receipts]


@pytest.mark.parametrize('value', ['estimated_or_measured', 'estimated', 'measured'])
def test_distribution_requirement_does_not_take_moving_average_fields(value):
    from trade_compass_agent.runtime.report_body import BodyMeaningError, _observation_commitments
    good = {'observation_dates': [], 'classifications': [{'property': 'distribution_basis', 'value': value}]}
    assert _observation_commitments(good) == good
    for wrong in ({'value': 'unknown'}, {'scope': 'all_mentioned'}, {'operator': 'eq'}, {'periods': []}):
        with pytest.raises(BodyMeaningError):
            _observation_commitments({**good, 'classifications': [{**good['classifications'][0], **wrong}]})


@pytest.mark.parametrize('answer', ['missing', None, True, 0, [], '', 'none|estimated_or_measured'])
def test_missing_or_invalid_source_answer_is_unavailable_not_implicitly_none(answer):
    from test_report_classifications import BULLISH, publication
    result, _, calls = publication(values=BULLISH, omit_source=answer == 'missing',
        wire_change={} if answer == 'missing' else {'distribution_basis': answer})
    assert result.verification['status'] == 'incomplete' and not result.artifact['review_valid']
    assert result.artifact['factual_model_calls'] == 0 and len(calls) == 1
    assert '多头排列' not in result.artifact['reader_body']
    assert any(f['code'] == 'review_unavailable' for f in result.verification['findings'])


def test_old_saved_body_wire_remains_decodable_but_cannot_approve_new_publication():
    from trade_compass_agent.runtime.report_body import BodyMeaningError, validate_body_meaning
    old = {'quantity_issues': [], 'paragraphs': [{'id': 'saved', 'parts': [
        {'text': '旧报告仍可读取。', 'kind': 'other', 'assertion_kind': 'discourse',
         'time_scope': 'other', 'outcome': 'none', 'extent': 'none'}]}]}
    paragraphs = {'saved': '旧报告仍可读取。'}
    meaning, issues = validate_body_meaning(old, paragraphs)
    assert meaning['saved'][0]['text'] == paragraphs['saved'] and not issues
    with pytest.raises(BodyMeaningError, match='source_property_declaration_missing'):
        validate_body_meaning(old, paragraphs, require_source_declarations=True)


def test_conflicting_source_answers_cannot_fall_back_to_one_convenient_shape():
    from test_report_classifications import BULLISH, publication
    result, _, _ = publication(values=BULLISH, wire_change={'classifications': [
        {'property': 'distribution_basis', 'value': 'estimated_or_measured'}]})
    assert result.verification['status'] == 'incomplete' and not result.artifact['review_valid']
    assert result.artifact['factual_model_calls'] == 0
