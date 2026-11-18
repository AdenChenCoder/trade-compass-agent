"""CYQ estimates retain their producer meaning through writing and history."""
import hashlib
import json

import pytest

from report_contract_helpers import report_facts, source_review_rows
from trade_compass_agent.runtime.report_contract import Calculation, EvidenceCatalog, calculate
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report
from trade_compass_agent.runtime.tools import stock_structure


def chip_receipt(monkeypatch, *, day='2025-01-02'):
    monkeypatch.setattr(stock_structure, '_fetch_cyq_from_eastmoney', lambda symbol: None)
    monkeypatch.setattr(stock_structure, '_compute_cyq', lambda symbol: {
        'date': day, 'avg_cost': 22, 'cost_90_low': 20, 'cost_90_high': 23,
        'cost_70_low': 20.5, 'cost_70_high': 21.5, 'concentration': '集中'})
    return ('get_chip_distribution', stock_structure.tool_get_chip_distribution(symbol='002415'))


def test_real_producer_cost_fields_reach_writer_and_saved_reader_without_becoming_account_cost(monkeypatch, tmp_path):
    receipt = chip_receipt(monkeypatch)
    portfolio = ('analyze_portfolio', json.dumps({'positions': [
        {'symbol': '002415', 'account': 'test', 'avg_cost': 22}]}))
    expected = '002415在2025-01-02的90%筹码成本估算上沿为23元/股，筹码平均成本估算为22元/股；账户持仓成本价为22元/股。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            assert payload['inferences'][0]['text'] == expected
            return json.dumps({'reviews': [{'id': 'answer', 'supported': True, 'reason': 'explicit double'}],
                               'coverage': {'complete': True, 'missing': []}})
        facts = report_facts(payload)
        upper = next(f for f in facts if f['metric'] == 'cost_90_high')
        mean = next(f for f in facts if f['metric'] == 'avg_cost' and f['kind'] == 'chip_cost')
        owner = next(f for f in facts if f['metric'] == 'avg_cost' and f['kind'] == 'cost')
        assert upper['observed_at'] == '2025-01-02' and mean['label'] == '筹码平均成本估算'
        assert owner['label'] == '持仓成本价' and owner['account'] == 'test'
        assert len({upper['id'], mean['id'], owner['id']}) == 3
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': [[upper['id'], 'subject'], '在', [upper['id'], 'date'],
                '的', [upper['id'], 'label'], '为', [upper['id'], 'value_with_unit'], '，',
                [mean['id'], 'label'], '为', [mean['id'], 'value_with_unit'], '；账户',
                [owner['id'], 'label'], '为', [owner['id'], 'value_with_unit'], '。'], 'refs': []}]})
    result = publish_report(task='说明筹码估算与账户持仓成本。', draft='', tool_results=[receipt, portfolio],
        run_scope='chip-reader', call=writer, max_calls=2)
    assert result.verification['status'] == 'complete' and expected in result.artifact['reader_body']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['receipts'][0]['result'] == receipt[1]
    facts = [f for f in saved['facts'] if f['kind'] == 'chip_cost']
    assert len(facts) == 5 and all(f['observed_at'] == '2025-01-02' for f in facts)
    assert all(json.loads(f['scope']) == {'estimate': 'chip_cost_distribution', 'method': 'eastmoney_cyq_algorithm'} for f in facts)
    for fact in facts:
        assert fact['id'] == hashlib.sha256((fact['receipt_id']+fact['pointer']).encode()).hexdigest()[:24]


@pytest.mark.parametrize('values,count', [({}, 0), ({'avg_cost': 22}, 1),
    ({'avg_cost': 22, 'cost_90_low': 20, 'cost_90_high': 23, 'cost_70_low': 20.5, 'cost_70_high': 21.5}, 5)])
def test_chip_cost_availability_is_not_filled_from_other_fields(values, count):
    raw = json.dumps({'symbol': '002415', 'date': '2025-01-02', **values})
    evidence = EvidenceCatalog.build([('get_chip_distribution', raw)], run_scope='availability')
    assert len([f for f in evidence.facts.values() if f.kind == 'chip_cost']) == count
    assert evidence.receipts[0]['result'] == raw


@pytest.mark.parametrize('value,available', [(None, False), (False, False), (0, False), (-1, False),
    ('NaN', False), (23, True), ('23.00', True)])
def test_invalid_chip_costs_stay_unavailable_without_changing_valid_precision(value, available):
    raw = json.dumps({'symbol': '002415', 'cost_90_high': value, '_meta': {'timestamp': '2026-11-12T15:10:00'}})
    evidence = EvidenceCatalog.build([('get_chip_distribution', raw)], run_scope='invalid-cost')
    costs = [f for f in evidence.facts.values() if f.kind == 'chip_cost']
    assert bool(costs) is available
    if available:
        assert costs[0].value == str(value) and costs[0].observed_at is None


def test_unavailable_and_unknown_producers_cannot_create_chip_costs():
    raw = json.dumps({'symbol': '002415', 'cost_90_high': 23, 'unavailable': True})
    for tool in ('get_chip_distribution', 'get_bars', 'fetch_url'):
        evidence = EvidenceCatalog.build([(tool, raw)], run_scope=tool)
        assert not any(f.kind == 'chip_cost' for f in evidence.facts.values())
        assert evidence.receipts[0]['result'] == raw


def test_cost_estimate_range_can_be_calculated_but_not_reported_as_a_stock_return(monkeypatch):
    receipt = chip_receipt(monkeypatch)
    previous = ('get_chip_distribution', json.dumps({'symbol': '002415', 'date': '2025-01-01',
        'avg_cost': 21, '_meta': {'source': 'eastmoney_cyq_algorithm'}}))
    evidence = EvidenceCatalog.build([receipt, previous], run_scope='estimate-calculations')
    facts = list(evidence.facts.values())
    bounds = [next(f.id for f in facts if f.metric == metric) for metric in ('cost_90_low', 'cost_90_high')]
    _, trace = calculate(Calculation(id='range', type='calculation', section='assessment',
        operation='difference', refs=bounds), evidence)
    assert trace['result'] == '3' and trace['unit'] == '元/股'
    means = sorted((f for f in facts if f.metric == 'avg_cost'), key=lambda f: f.observed_at)
    with pytest.raises(ValueError, match='价格变动比例'):
        calculate(Calculation(id='return', type='calculation', section='assessment',
            operation='change_pct', refs=[f.id for f in means]), evidence)


@pytest.mark.parametrize('day,complete', [('2025-01-02', True), ('2025-01-01', False)])
def test_current_estimate_uses_its_source_date_despite_an_optimistic_model(monkeypatch, day, complete):
    receipt = chip_receipt(monkeypatch, day=day)
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return json.dumps({'reviews': source_review_rows(payload), 'coverage': {'complete': True, 'missing': []}})
        upper = next(f for f in report_facts(payload) if f['metric'] == 'cost_90_high')
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': [[upper['id'], 'subject'], '当前', [upper['id'], 'label'],
                '为', [upper['id'], 'value_with_unit'], '。'], 'refs': []}]})
    def assessor(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{'temporal_scope': 'unspecified', 'participant_scope': 'none', 'distribution_basis': 'none',
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'observation',
                'observation_dates': ['task_now'], 'classifications': []}]} for p in payload['paragraphs']]})
        return json.dumps({'assessments': [{'id': s['id'], 'commitments': {
            'participant_activity': False, 'causal_relation': False}, 'time_evidence': s['bound_fact_refs'],
            'comparison': 'explicit optimistic double', 'support_basis': 'direct',
            'checks': {k: 'entailed' for k in ('identity_scope', 'time', 'quantity', 'support')},
            'verdict': 'entailed'} for s in payload['statements']]})
    result = publish_report(task='说明当前筹码成本估算。', draft='', tool_results=[receipt],
        run_scope='current-estimate', call=writer, fact_review_call=assessor, max_calls=2,
        reference_date='2025-01-02')
    assert (result.verification['status'] == 'complete') is complete
    check = next(iter(result.artifact['temporal_checks'].values()))
    assert check['status'] == ('entailed' if complete else 'unknown')
    assert result.artifact['receipts'][0]['result'] == receipt[1]
