"""A bound reader date is distinct from a claim about the task's present."""
import json

import pytest

from report_contract_helpers import report_facts, source_review_rows
from test_report_factual_role import assess
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('current', [False, True])
def test_literal_bound_display_date_does_not_refresh_a_different_task_calendar(tmp_path, current):
    body_inputs = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            price = next(f for f in report_facts(payload) if f['metric'] == 'price')
            parts = (['002415当前报价为', [price['id'], 'value_with_unit'], '。'] if current else
                     ['002415在', [price['id'], 'date'], '的记录报价为', [price['id'], 'value_with_unit'], '。'])
            return json.dumps({'schema_version': 1, 'items': [{'id': 'quote', 'section': 'market',
                'type': 'paragraph', 'parts': parts, 'refs': []}]})
        return json.dumps({'reviews': source_review_rows(payload), 'coverage': {'complete': True, 'missing': []}})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            body_inputs.extend(payload['paragraphs'])
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'distribution_basis': 'none',
                'operation_code': 0, 'claim_kind': 'observation', 'classifications': [],
                'observation_dates': ['task_now' if current else '2025-01-02']}]}
                for p in payload['paragraphs']]})
        return json.dumps(assess(payload))
    receipt = json.dumps({'symbol': '002415', 'price': 22, 'timestamp': '2025-01-02T02:30:00+00:00'})
    report = publish_report(task='仅说明所给记录的价格及其记录日期。', draft='',
        tool_results=[('sina_realtime_quote', receipt)], run_scope='reader-calendar-control',
        call=writer, fact_review_call=checker, reference_date='2025-01-02',
        reference_timezone='America/New_York', max_calls=2)
    saved = persist_publication(tmp_path, report)
    artifact = json.loads((tmp_path/'report_artifacts'/(saved['artifact_id']+'.json')).read_text())
    assert artifact['published_text'] == report.text
    assert artifact['receipts'] == report.artifact['receipts']
    assert report.artifact['review_valid']
    assert (report.verification['status'] == 'complete') is (not current)
    if not current:
        # The runtime owns this rendered date (Asia/Shanghai), not the model.
        assert body_inputs[0]['text'] == '002415在2025-01-02 10:30:00（北京时间）的记录报价为22元/股。'
        assert body_inputs[0]['text'] in report.artifact['reader_body']
    else:
        assert report.artifact['temporal_checks']['c0']['status'] == 'unknown'
