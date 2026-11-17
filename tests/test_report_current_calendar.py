"""Equivalent positive calendar metadata must reach the publication boundary."""
import json

import pytest

from report_contract_helpers import report_facts, source_review_rows
from test_report_factual_role import assess, body_reply
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report


@pytest.mark.parametrize('anchor', ['task_now', '2025-01-02'])
@pytest.mark.parametrize('observed', [None, '20250101', '20250102'])
@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('kind', ['observation', 'judgment'])
def test_same_current_observation_keeps_date_obligation(tmp_path, anchor, observed, bound, kind):
    report = _publish(anchor, observed, bound, kind)
    accepted = observed == '20250102'
    assert report.artifact['review_valid']
    assert (report.verification['status'] == 'complete') is accepted
    assert ('0.8倍' in report.artifact['reader_body']) is accepted
    timing = report.artifact['temporal_checks']['c0']
    assert timing['status'] == ('entailed' if accepted else 'unknown')
    assert timing['expected_date'] == '2025-01-02'
    assert timing['evidence'][0]['comparison_date'] == (
        '2025-01-02' if accepted else '2025-01-01' if observed else None)
    reference = persist_publication(tmp_path, report)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == report.text
    assert saved['body_meaning'] == report.artifact['body_meaning']
    assert saved['temporal_checks'] == report.artifact['temporal_checks']
    assert saved['receipts'] == report.artifact['receipts']


def test_historical_and_multiple_dates_keep_their_existing_comparison_scope():
    from trade_compass_agent.runtime.report_time import check_statement_time
    base = {'time_scope': 'other', 'assertion_kind': 'judgment', 'bound_fact_refs': [],
            'time_fact_refs': []}
    assessment = {'checks': {'quantity': 'not_applicable'}, 'time_evidence': []}
    calendar = {'reference_date': '2025-01-02', 'reference_timezone': 'Asia/Shanghai'}
    for anchors in ([], ['2025-01-01'], ['2025-01-01', '2025-01-02']):
        result = check_statement_time({**base, 'observation_dates': anchors}, assessment,
                                      {'evidence': []}, calendar)
        assert result['status'] == 'not_applicable'


@pytest.mark.parametrize('anchor', ['task_now', '2025-01-02'])
@pytest.mark.parametrize('observed', [None, '20250101', '20250102'])
def test_equivalent_current_anchor_allows_same_bounded_witness_repair(anchor, observed):
    report = _publish(anchor, observed, False, 'observation', repair_missing=True)
    assert (report.verification['status'] == 'complete') is (observed == '20250102')
    assert report.artifact['model_calls'] == 4
    assert report.artifact['annotation_candidates'] == {'c0': 'temporal_witness'}
    assert report.artifact['annotation_resolutions'][0]['statement']['observation_dates'] == [anchor]
    assert report.artifact['body_meaning']['pb'][0]['observation_dates'] == [anchor]


def _publish(anchor, observed, bound, kind, *, repair_missing=False):
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            pb = next(f for f in report_facts(payload) if f['metric'] == 'pb')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'pb', 'section': 'market', 'type': 'paragraph',
                 'parts': ['600928今日市净率为', [pb['id'], 'value_with_unit'] if bound else '0.8倍', '。'],
                 'refs': [pb['id']]}]})
        result = {'reviews': source_review_rows(payload),
                  'coverage': {'complete': True, 'missing': []}}
        if 'annotation_review' in payload:
            review = payload['annotation_review']
            pb = next(f for f in review['contexts']['p0']['evidence'] if f['metric'] == 'pb')
            assessment = assess(review)['assessments'][0]
            assessment['time_evidence'] = [pb['id']]
            result['annotation_resolutions'] = [{'id': 'c0', 'empirical_present': True,
                                                  'assessment': assessment}]
        return json.dumps(result)

    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0].update(
                time_scope='reference_date' if anchor == 'task_now' else 'other',
                assertion_kind=kind, observation_dates=[anchor], classifications=[])
            return json.dumps(result)
        result = assess(payload)
        pb = next(f for f in payload['contexts']['p0']['evidence'] if f['metric'] == 'pb')
        result['assessments'][0]['time_evidence'] = [] if bound or repair_missing else [pb['id']]
        # Even an optimistic opinion classification cannot erase a positive
        # empirical date. This is an explicit model double, not model execution.
        if kind == 'judgment':
            result['assessments'][0]['checks']['quantity'] = 'not_applicable'
        return json.dumps(result)

    raw = json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': observed,
                      '_meta': {'retrieved_at': '2025-01-02T11:10:00+08:00'}})
    return publish_report(task='报告所给当日市净率。', draft='',
        tool_results=[('get_fundamentals', raw)], run_scope='current-calendar',
        call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
