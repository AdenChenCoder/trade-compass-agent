"""A factual review's basis label cannot waive an unsupported reader predicate."""
import json

import pytest

from report_contract_helpers import source_review_scope, report_facts
from test_report_factual_role import assess, body_reply
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('basis', ['direct', 'reasoned'])
@pytest.mark.parametrize('ending,supported', [
    ('，表明相对交易活跃度偏低。', True),
    ('，表明当日非主力出货。', False),
    ('，证明成交缩量导致了当日股价下跌。', False),
])
def test_same_empirical_claim_keeps_source_obligations_when_basis_label_changes(tmp_path, basis, ending, supported):
    raw = json.dumps({'symbol': '300406', 'as_of': '2026-11-12', 'volume_ratio': 0.594})
    stages, source_inputs = [], []
    def writer(system, user):
        payload = json.loads(user)
        stages.append('writer' if 'schema' in payload else 'coverage')
        if 'schema' in payload:
            volume = next(f['id'] for f in report_facts(payload) if f['metric'] == 'volume_ratio')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'good', 'section': 'market', 'type': 'paragraph',
                 'parts': ['300406在2026-11-12的量比为', [volume, 'value_with_unit'], '。'], 'refs': []},
                {'id': 'target', 'section': 'assessment', 'type': 'paragraph',
                 'parts': ['300406量比为', [volume, 'value_with_unit'], ending], 'refs': []}]})
        source_inputs.append(payload)
        return json.dumps({'reviews': [{**source_review_scope(payload), 'id': r['id'], 'supported': supported if r['id'] == 'c1' else True,
            'reason': '匿名量比不能证明参与者行为或实际因果。' if r['id'] == 'c1' and not supported
                else '原始量比与所述范围一致。'} for r in payload['inferences']],
            'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        payload = json.loads(user)
        stages.append('body' if 'paragraphs' in payload else 'fact')
        if 'paragraphs' in payload:
            return json.dumps(body_reply(payload))
        value = assess(payload)
        # The supplied first verdict is deliberately optimistic for the whole
        # mixed assertion, as in actual118. Only its self-reported basis changes.
        value['assessments'][1]['support_basis'] = basis
        return json.dumps(value)
    report = publish_report(task='说明量比和这些资料可以支持的分析。', draft='',
        tool_results=[('compute_volume_ratio', raw)], run_scope='source-label-control',
        call=writer, fact_review_call=checker, reference_date='2026-11-12', max_calls=2)
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == report.text and saved['receipts'] == report.artifact['receipts']
    assert (report.verification['status'] == 'complete') is supported
    assert (ending.strip('，。') in report.artifact['reader_body']) is supported
    assert '300406在2026-11-12的量比为0.594倍。' in report.artifact['reader_body']
    assert report.artifact['review_valid'] and not report.artifact['factual_assessment_errors']
    assert stages == ['writer', 'body', 'fact', 'coverage']
    assert report.artifact['model_calls'] == 4
    assert report.artifact['receipts'][0]['result'] == raw
    method, = source_inputs[0]['source_semantics']
    assert method['source_method'] == 'relative_total_trading_activity'
    assert method['entity'] == '300406' and method['observed_at'] == '2026-11-12'
    facts = {f['id']: f for context in source_inputs[0]['source_contexts'].values()
             for f in context['evidence']}
    fact, = (facts[ref] for ref in method['fact_refs'])
    assert fact['source'] == 'compute_volume_ratio' and fact['metric'] == 'volume_ratio'
    assert fact['value'] == '0.594' and fact['observed_at'] == '2026-11-12'
    if not supported:
        assert any(any(row['id'] == 'c1' for row in payload['inferences']) for payload in source_inputs)
