"""Positive empirical obligations survive broad reviewer categories and labels."""
import json

import pytest

from report_contract_helpers import report_facts
from test_report_factual_role import assess
from trade_compass_agent.runtime.report_body import BodyMeaningError, validate_body_meaning
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('basis', ['direct', 'reasoned'])
@pytest.mark.parametrize('flag_owner', ['body_scope', 'body_kind', 'fact'])
@pytest.mark.parametrize('witness', ['activity', 'classified', 'foreign', 'title', 'industry_flow'])
def test_participant_exclusion_needs_owned_group_source_despite_first_approval(tmp_path, basis, witness, flag_owner):
    receipts = [('compute_volume_ratio', json.dumps({'symbol': '300406', 'as_of': '2026-11-12', 'volume_ratio': 0.594}))]
    if witness == 'industry_flow':
        receipts.append(('get_fund_flow', json.dumps({'industry_flow': [{'name': '金融行业', 'net_inflow_yi': 15.38}]})))
    elif witness != 'activity':
        receipts.append(('fetch_url', json.dumps({'symbol': '600928' if witness == 'foreign' else '300406', 'title': '分类统计',
            'as_of': '2026-11-12', 'url': 'https://example.invalid/classified',
            'content': '2026-11-12的分类成交统计：该证券全部卖出成交均属于非主力类别，主力卖出额为0元。'})))
    def writer(system, user):
        p = json.loads(user)
        if 'schema' in p:
            method, = p['source_semantics']
            assert method['source_method'] == 'relative_total_trading_activity'
            assert 'trader_group_composition_or_absence' in method['not_established']
            ref = next(f['id'] for f in report_facts(p) if f['metric'] == 'volume_ratio')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'base', 'section': 'market', 'type': 'paragraph',
                 'parts': ['300406在2026-11-12的量比为', [ref, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'actor', 'section': 'assessment', 'type': 'paragraph',
                 'parts': ['300406在2026-11-12的量比为', [ref, 'value_with_unit'], '，当日卖方均属于非主力类别。'], 'refs': []}]})
        refs = [f['id'] for c in p.get('source_contexts', {}).values() for f in c['evidence']
                if f['metric'] == {'activity': 'volume_ratio', 'industry_flow': 'net_inflow_yi', 'title': 'title'}.get(witness, 'content')]
        # Deliberately optimistic SOURCE and FACT. Anonymous numeric evidence or
        # another instrument cannot meet the runtime source-family requirement.
        return json.dumps({'reviews': [{'id': r['id'], 'supported': True, 'reason': 'Explicit optimistic double.',
            **({'evidence_refs': refs} if r.get('source_requirements') else {})} for r in p['inferences']],
            'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        p = json.loads(user)
        if 'paragraphs' in p:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': r['id'], 'parts': [{
                'through': r['source_units'][-1]['id'], 'distribution_basis': 'none',
                'temporal_scope': 'record_date', 'participant_scope': 'excluded' if r['id'] == 'actor' and flag_owner == 'body_scope' else 'none',
                'operation_code': 0, 'claim_kind': 'actor_activity' if r['id'] == 'actor' and flag_owner == 'body_kind' else 'judgment',
                'observation_dates': ['2026-11-12'], 'classifications': []}]} for r in p['paragraphs']]})
        value = assess(p)
        for r in value['assessments']:
            r['support_basis'] = basis
            if r['id'] == 'c1' and flag_owner == 'fact':
                r['commitments']['participant_activity'] = True
        return json.dumps(value)
    report = publish_report(task='说明量比及参与者统计所能支持的结论。', draft='', tool_results=receipts,
        run_scope='empirical-scope-controls', call=writer, fact_review_call=checker,
        max_calls=2, reference_date='2026-11-12')
    supported = witness == 'classified' and basis == 'direct'
    assert (report.verification['status'] == 'complete') is supported
    assert ('当日卖方均属于非主力类别' in report.text) is supported
    assert '量比为0.594倍' in report.text
    assert report.artifact['review_valid']
    saved = persist_publication(tmp_path, report)
    artifact = json.loads((tmp_path/'report_artifacts'/(saved['artifact_id']+'.json')).read_text())
    assert artifact['published_text'] == report.text
    assert artifact['receipts'] == report.artifact['receipts']


@pytest.mark.parametrize('missing', ['temporal_scope', 'participant_scope'])
def test_fresh_body_cannot_omit_independent_empirical_axis(missing):
    part = {'through': 'u0', 'operation_code': 0, 'claim_kind': 'observation',
            'distribution_basis': 'none', 'temporal_scope': 'task_present', 'participant_scope': 'none',
            'observation_dates': ['2025-01-02'], 'classifications': []}
    part.pop(missing)
    with pytest.raises(BodyMeaningError, match='empirical_scope_declaration_missing'):
        validate_body_meaning({'quantity_issues': [], 'paragraphs': [{'id': 'p', 'parts': [part]}]},
                              {'p': '当前报价为22元/股。'}, require_source_declarations=True)


def test_record_date_declaration_requires_a_record_anchor():
    part = {'through': 'u0', 'operation_code': 0, 'claim_kind': 'observation',
            'distribution_basis': 'none', 'temporal_scope': 'record_date', 'participant_scope': 'none',
            'observation_dates': [], 'classifications': []}
    with pytest.raises(BodyMeaningError, match='record_date_anchor_missing'):
        validate_body_meaning({'quantity_issues': [], 'paragraphs': [{'id': 'p', 'parts': [part]}]},
                              {'p': '记录报价为22元/股。'}, require_source_declarations=True)


def test_author_stated_utc_record_is_not_relabelled_as_display_calendar():
    def writer(system, user):
        p = json.loads(user)
        if 'schema' in p:
            ref = next(f['id'] for f in report_facts(p) if f['metric'] == 'price')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'q', 'section': 'market',
                'type': 'paragraph', 'parts': ['002415在UTC日历2025-01-01的记录报价为', [ref, 'value_with_unit'], '。'], 'refs': []}]})
        return json.dumps({'reviews': [{'id': r['id'], 'supported': True, 'reason': 'Explicit UTC observation matches.'}
            for r in p['inferences']], 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        p = json.loads(user)
        if 'paragraphs' in p:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': r['id'], 'parts': [{
                'through': r['source_units'][-1]['id'], 'distribution_basis': 'none',
                'temporal_scope': 'record_date', 'participant_scope': 'none', 'operation_code': 0,
                'claim_kind': 'observation', 'observation_dates': ['2025-01-01'], 'classifications': []}]}
                for r in p['paragraphs']]})
        return json.dumps(assess(p))
    report = publish_report(task='报告UTC日历记录。', draft='',
        tool_results=[('sina_realtime_quote', json.dumps({'symbol': '002415', 'price': 22,
                                                       'timestamp': '2025-01-01T20:00:00+00:00'}))],
        run_scope='explicit-utc-record', call=writer, fact_review_call=checker, max_calls=2,
        reference_date='2025-01-01', reference_timezone='UTC')
    assert report.verification['status'] == 'complete'
    assert 'UTC日历2025-01-01' in report.text


@pytest.mark.parametrize('latest', ['2025-01-02', '2025-01-01'])
def test_comparison_retains_declared_prior_date_and_needs_a_current_witness(latest):
    receipts = [('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d', 'bars': [
        {'timestamp': '2025-01-01', 'close': 22}, {'timestamp': latest, 'close': 23}]}))]
    def writer(system, user):
        p = json.loads(user)
        if 'schema' in p:
            refs = [f['id'] for f in report_facts(p) if f['metric'] == 'close']
            return json.dumps({'schema_version': 1, 'items': [{'id': 'comparison', 'section': 'market',
                'type': 'paragraph', 'parts': ['002415较2025-01-01收盘价', [refs[0], 'value_with_unit'],
                    '，今天收盘价为', [refs[1], 'value_with_unit'], '，价格走高。'], 'refs': []}]})
        return json.dumps({'reviews': [{'id': r['id'], 'supported': True, 'reason': 'Explicit optimistic source double.'}
            for r in p['inferences']], 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        p = json.loads(user)
        if 'paragraphs' in p:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': r['id'], 'parts': [{
                'through': r['source_units'][-1]['id'], 'distribution_basis': 'none',
                'temporal_scope': 'record_and_present', 'participant_scope': 'none', 'operation_code': 0,
                'claim_kind': 'comparison', 'observation_dates': ['2025-01-01', 'task_now'], 'classifications': []}]}
                for r in p['paragraphs']]})
        return json.dumps(assess(p))
    report = publish_report(task='比较昨日与今日收盘价。', draft='', tool_results=receipts,
        run_scope='comparison-calendars', call=writer, fact_review_call=checker,
        max_calls=2, reference_date='2025-01-02')
    assert (report.verification['status'] == 'complete') is (latest == '2025-01-02')
    assert report.artifact['review_valid']


@pytest.mark.parametrize('empirical', [False, True])
def test_current_research_horizon_is_separate_from_empirical_current_time(empirical):
    receipts = [('get_fundamentals', json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': '2025-01-01'}))]
    text = '当前估值偏低。' if empirical else '目前我仍倾向于继续持有。'
    def writer(system, user):
        p = json.loads(user)
        if 'schema' in p:
            ref = next(f['id'] for f in report_facts(p) if f['metric'] == 'pb')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'opinion', 'section': 'assessment',
                'type': 'paragraph', 'parts': [text], 'refs': [ref]}]})
        return json.dumps({'reviews': [{'id': r['id'], 'supported': True, 'reason': 'Explicit independent double.'}
            for r in p['inferences']], 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        p = json.loads(user)
        if 'paragraphs' in p:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': r['id'], 'parts': [{
                'through': r['source_units'][-1]['id'], 'distribution_basis': 'none',
                'temporal_scope': 'task_present' if empirical else 'no_observation',
                'participant_scope': 'none', 'operation_code': 0, 'claim_kind': 'judgment',
                'observation_dates': [], 'classifications': []}]} for r in p['paragraphs']]})
        value = assess(p)
        ref = next(f['id'] for c in p['contexts'].values() for f in c['evidence'] if f['metric'] == 'pb')
        value['assessments'][0].update(support_basis='reasoned', time_evidence=[ref])
        value['assessments'][0]['checks']['quantity'] = 'not_applicable'
        value['assessments'][0]['checks']['time'] = 'entailed' if empirical else 'not_applicable'
        return json.dumps(value)
    report = publish_report(task='给出研究判断。', draft='', tool_results=receipts,
        run_scope='research-and-observation-time', call=writer, fact_review_call=checker,
        max_calls=2, reference_date='2025-01-02')
    assert (report.verification['status'] == 'complete') is (not empirical)
    assert (text in report.text) is (not empirical)


def test_saved_legacy_body_stays_readable_but_is_not_a_fresh_declaration():
    value = {'quantity_issues': [], 'paragraphs': [{'id': 'p', 'parts': [{
        'through': 'u0', 'operation_code': 0, 'claim_kind': 'observation', 'as_of_task_now': False}]}]}
    parts, issues = validate_body_meaning(value, {'p': '历史记录。'})
    assert parts['p'][0]['text'] == '历史记录。' and not issues
    with pytest.raises(BodyMeaningError, match='source_property_declaration_missing'):
        validate_body_meaning(value, {'p': '历史记录。'}, require_source_declarations=True)


@pytest.mark.parametrize('group_claim', [False, True])
def test_observed_account_position_is_not_market_participant_classification(group_claim):
    receipts = [('analyze_portfolio', json.dumps({'account': 'paper', 'positions': [
        {'symbol': '002415', 'quantity': 100}]}))]
    def writer(system, user):
        p = json.loads(user)
        if 'schema' in p:
            ref = next(f['id'] for f in report_facts(p) if f['metric'] == 'quantity')
            parts = ['本账户持有002415共', [ref, 'value_with_unit'], '。']
            if group_claim:
                parts += ['这说明该股没有主力卖出。']
            return json.dumps({'schema_version': 1, 'items': [{'id': 'position', 'type': 'paragraph',
                'section': 'positions', 'parts': parts, 'refs': []}]})
        refs = [f['id'] for c in p['source_contexts'].values() for f in c['evidence'] if f['metric'] == 'quantity']
        return json.dumps({'reviews': [{'id': r['id'], 'supported': True, 'reason': 'Explicit optimistic double.',
            'evidence_refs': refs} for r in p['inferences']], 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        p = json.loads(user)
        if 'paragraphs' in p:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': r['id'], 'parts': [{
                'through': r['source_units'][-1]['id'], 'distribution_basis': 'none',
                'temporal_scope': 'unspecified', 'participant_scope': 'excluded' if group_claim else 'none',
                'operation_code': 0, 'claim_kind': 'actor_activity', 'observation_dates': [], 'classifications': []}]}
                for r in p['paragraphs']]})
        value = assess(p)
        value['assessments'][0]['commitments']['participant_activity'] = True
        return json.dumps(value)
    report = publish_report(task='说明所给账户记录。', draft='', tool_results=receipts,
        run_scope='own-account-actor-record', call=writer, fact_review_call=checker, max_calls=2)
    assert (report.verification['status'] == 'complete') is (not group_claim)
    assert report.artifact['review_valid']


@pytest.mark.parametrize('scope,accepted', [('recorded_actor', True), ('attributed', False), ('excluded', False)])
def test_issuer_announcement_record_cannot_prove_trader_group(scope, accepted):
    from dataclasses import asdict
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_source_properties import check_source_properties
    catalog = EvidenceCatalog.build([('get_events', json.dumps({'symbol': '300406',
        'title': '关于取得医疗器械注册证书的公告', 'date': '2025-01-02'}))], run_scope='issuer-domain')
    ref = next(f.id for f in catalog.facts.values() if f.metric == 'title')
    parts, _ = validate_body_meaning({'quantity_issues': [], 'paragraphs': [{'id': 'p', 'parts': [{
        'through': 'u0', 'operation_code': 0, 'claim_kind': 'actor_activity', 'distribution_basis': 'none',
        'temporal_scope': 'record_date', 'participant_scope': scope,
        'observation_dates': ['2025-01-02'], 'classifications': []}]}]}, {'p': '公司公告事项。'}, require_source_declarations=True)
    statement = {'bound_fact_refs': [], 'classifications': parts['p'][0]['classifications'], 'assertion_kind': 'actor_activity'}
    result = check_source_properties(statement, {'evidence': [asdict(f) for f in catalog.facts.values()]},
        catalog, {'evidence_refs': [ref]})
    assert (result['status'] == 'entailed') is accepted


@pytest.mark.parametrize('basis,agreed,accepted', [('estimated', True, True), ('not_measured', True, True),
    ('measured', True, False), ('estimated', False, False), ('paragraph', False, True)])
def test_anaphoric_method_source_needs_independent_owned_reference_agreement(basis, agreed, accepted):
    from dataclasses import asdict
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    from trade_compass_agent.runtime.report_source_properties import check_source_properties
    catalog = EvidenceCatalog.build([
        ('get_bars', json.dumps({'symbol': '300406', 'bars': [{'timestamp': '2025-01-02', 'close': 22}]})),
        ('get_chip_distribution', json.dumps({'symbol': '600928', 'as_of': '2025-01-02',
             'cost_90_high': 3.84}))], run_scope='mixed-method-record')
    ref = next(f.id for f in catalog.facts.values() if f.kind == 'chip_cost')
    statement = {'bound_fact_refs': [], 'classifications': [{'property': 'distribution_basis', 'value': 'estimated' if basis == 'paragraph' else basis}]}
    if basis == 'paragraph':
        statement['paragraph_bound_refs'] = [ref]
    result = check_source_properties(statement, {'evidence': [asdict(f) for f in catalog.facts.values()]},
        catalog, {'evidence_refs': [ref]}, {'time_evidence': [ref] if agreed else []})
    assert (result['status'] == 'entailed') is accepted
