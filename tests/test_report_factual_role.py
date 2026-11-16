"""Explicit model doubles for the independent local factual publication boundary."""
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.config import AppConfig, LLMConfig, invalidate_config_cache, load_app_config
from trade_compass_agent.llm import providers
from trade_compass_agent.runtime.exceptions import AgentTurnError, TurnInterruptedError
from trade_compass_agent.runtime.report_publication import persist_publication, publish_report

RECEIPTS = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]}))]


def compose(payload):
    facts = [f for f in report_facts(payload) if f["metric"] == "close"]
    return {"schema_version": 1, "items": [{"id": "answer", "type": "paragraph", "section": "market",
        "parts": [{"type": "text", "text": "002415收盘价由"},
                  {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                  {"type": "text", "text": "升至"},
                  {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                  {"type": "text", "text": "。"}], "refs": []}]}


def assess(payload, verdict="entailed"):
    return {"assessments": [{"id": row["id"], "comparison": "比较最终陈述与原始资料。", "verdict": verdict,
                            "support_basis": "direct",
                            "commitments": {"participant_activity": False, "causal_relation": False},
                            "time_evidence": [],
                            "checks": {key: verdict for key in ('identity_scope', 'time', 'quantity', 'support')}}
                            for row in payload["statements"]]}


METADATA_FAULTS = ('bad_check', 'missing_check', 'bad_verdict', 'bad_comparison', 'bad_time_refs', 'extra_field')


def break_assessment_metadata(row, fault):
    if fault == 'bad_check':
        row['checks']['support'] = 'reasoned'
    elif fault == 'missing_check':
        row['checks'].pop('quantity')
    elif fault == 'bad_verdict':
        row['verdict'] = 'not_applicable'
    elif fault == 'bad_comparison':
        row['comparison'] = None
    elif fault == 'bad_time_refs':
        row['time_evidence'] = [True]
    elif fault == 'extra_field':
        row['unrequested'] = 'not evidence'
    else:
        raise AssertionError('unknown fixture fault')


@pytest.mark.parametrize('assertion_kind', ['actor_activity', 'causal_relation'])
@pytest.mark.parametrize('basis,supported,unavailable', [
    ('direct', True, False), ('reasoned', False, False), ('calculated', False, False),
    ('unknown', False, False), ('not_applicable', False, False),
    (None, False, True), ('not-a-basis', False, True), ([], False, True),
    ('missing_commitments', False, True), ('invalid_commitments', False, True),
])
def test_empirical_commitment_cannot_publish_on_analysis_basis(tmp_path, assertion_kind, basis, supported, unavailable):
    claim = ('在所给统计覆盖范围内，002415在2025-01-02的机构卖出额为0元。'
             if assertion_kind == 'actor_activity' else
             '所给公司行动记录证实，002415在2025-01-02的历史复权价差由分红形成。')
    plan = '建议继续观察；若之后收盘价超过24元/股，再重新评估持仓。'
    receipts = [*RECEIPTS, ('fetch_url', json.dumps({'url': 'https://example.invalid/source',
        'content': claim}))]
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            document = compose(payload)
            refs = [f['id'] for f in report_facts(payload)]
            document['items'].extend([
                {'id': 'claim', 'type': 'paragraph', 'section': 'assessment', 'parts': [claim], 'refs': refs},
                {'id': 'plan', 'type': 'paragraph', 'section': 'plan', 'parts': [plan], 'refs': refs},
            ])
            return json.dumps(document)
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            for row in result['paragraphs']:
                part = row['parts'][0]
                if row['id'] == 'claim':
                    part['assertion_kind'] = assertion_kind
                elif row['id'] == 'plan':
                    part.update(assertion_kind='judgment', kind='plan')
                original = next(p for p in payload['paragraphs'] if p['id'] == row['id'])
                row['parts'] = [{'through': original['source_units'][-1]['id'],
                    'operation_code': 1 if row['id'] == 'plan' else 0,
                    'claim_kind': part['assertion_kind'], 'as_of_task_now': False}]
            return json.dumps(result)
        result = assess(payload)
        for row, statement in zip(result['assessments'], payload['statements'], strict=True):
            row['checks']['time'] = 'not_applicable'
            if statement['context_id'] == 'p1':
                if basis is None:
                    row.pop('support_basis')
                elif basis in ('missing_commitments', 'invalid_commitments'):
                    if basis == 'missing_commitments':
                        row.pop('commitments')
                    else:
                        row['commitments']['participant_activity'] = 'false'
                else:
                    row['support_basis'] = basis
            elif statement['context_id'] == 'p2':
                row['support_basis'] = 'reasoned'
        return json.dumps(result)
    result = publish_report(task='报告资料并提出后续观察条件。', draft='', tool_results=receipts,
        run_scope='support-contract', call=writer, fact_review_call=checker, max_calls=2)
    assert len(calls) == 2 and result.artifact['model_calls'] == 4
    assert (result.verification['status'] == 'complete') is supported
    assert (claim in result.artifact['reader_body']) is supported
    assert '22元/股升至23元/股' in result.artifact['reader_body'] and plan in result.artifact['reader_body']
    assert result.artifact['review_valid'] is not unavailable
    target = next(row for row in result.artifact['factual_review']['assessments'] if row['id'] == 'c1')
    assert target['reported_checks']['support'] == 'entailed'
    assert target['checks']['support'] == ('entailed' if supported else 'unknown')
    assert (target.get('validation_status') == 'unavailable') is unavailable
    if not supported and not unavailable:
        assert not result.artifact['factual_assessment_errors']  # Completed rejection, not missing review.
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['support_basis_checks'] == result.artifact['support_basis_checks']
    assert [r['result'] for r in saved['receipts']] == [raw for _, raw in receipts]


def test_support_feedback_repairs_body_once_and_survives_saved_publication(tmp_path):
    original = '所给价量变化证明，当日没有机构卖出002415。'
    corrected = '仅凭所给价量资料，无法确认002415当日卖方身份。'
    revisions = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            document = compose(payload)
            if 'revision' in payload:
                revisions.append(payload['revision'])
            text = corrected if revisions else original
            document['items'].append({'id': 'claim', 'type': 'paragraph', 'section': 'assessment',
                'parts': [text], 'refs': [f['id'] for f in report_facts(payload)]})
            return json.dumps(document)
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][1]['parts'][0]['assertion_kind'] = 'judgment' if revisions else 'actor_activity'
            return json.dumps(result)
        result = assess(payload)
        for row in result['assessments']:
            row['checks']['time'] = 'not_applicable'
        result['assessments'][1]['support_basis'] = 'reasoned'
        return json.dumps(result)
    result = publish_report(task='说明两日收盘价和价量资料的解释范围。', draft='', tool_results=RECEIPTS,
        run_scope='support-revision', call=writer, fact_review_call=checker, max_calls=4)
    assert len(revisions) == 1 and result.artifact['model_calls'] == 8
    feedback = revisions[0]['factual_feedback'][1]
    assert feedback['text'] == original and feedback['completed']
    assert feedback['checks']['support'] == 'unknown'
    assert feedback['support_basis_check']['reason'] == 'inference_is_not_actor_or_cause_evidence'
    assert result.verification['status'] == 'complete' and result.artifact['selected_attempt'] == 1
    assert original not in result.artifact['reader_body'] and corrected in result.artifact['reader_body']
    assert '22元/股升至23元/股' in result.artifact['reader_body']
    assert result.artifact['support_basis_checks']['c1']['status'] == 'compatible'
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['support_basis_checks'] == result.artifact['support_basis_checks']
    assert saved['revision_input']['factual_feedback'][1] == feedback


@pytest.mark.parametrize('zone,day,stamp,supported,comparison_day', [
    ('UTC', '2025-01-01', '2025-01-01T16:00:00+00:00', True, '2025-01-01'),
    ('UTC', '2025-01-02', '2025-01-01T16:00:00+00:00', False, '2025-01-01'),
    ('America/Los_Angeles', '2025-01-01', '2025-01-02T03:00:00+00:00', True, '2025-01-01'),
    ('America/Los_Angeles', '2025-01-02', '2025-01-02T03:00:00+00:00', False, '2025-01-01'),
    ('Asia/Shanghai', '2025-01-02', '2025-01-01T16:00:00+00:00', True, '2025-01-02'),
    ('UTC', '2025-01-02', '2025-01-02', True, '2025-01-02'),
    ('America/Los_Angeles', '2025-01-02', '2025-01-02', True, '2025-01-02'),
    ('UTC', '2025-01-02', '20250102', True, '2025-01-02'),
    ('UTC', '2025-01-03', '20250102', False, '2025-01-02'),
])
def test_current_observation_compares_in_task_calendar_without_changing_display(tmp_path, zone, day, stamp, supported, comparison_day):
    raw = json.dumps({'symbol': '002415', 'price': 22, 'timestamp': stamp})
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'price')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
                'section': 'overview', 'parts': [{'type': 'text', 'text': '002415当前报价为'},
                    {'type': 'value', 'ref': ref, 'field': 'value_with_unit'},
                    {'type': 'text', 'text': '。'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(result)
        return json.dumps(assess(payload))
    result = publish_report(task='按任务时区说明当日报价。', draft='', tool_results=[('sina_realtime_quote', raw)],
        run_scope='calendar-boundary', call=writer, fact_review_call=checker, max_calls=2,
        reference_date=day, reference_timezone=zone)
    assert result.artifact['review_valid']
    assert (result.verification['status'] == 'complete') is supported
    assert ('22元/股' in result.artifact['reader_body']) is supported
    timing = result.artifact['temporal_checks']['c0']
    assert timing['comparison_timezone'] == zone
    assert timing['evidence'][0]['observed_date'] == '2025-01-02'
    assert timing['evidence'][0]['comparison_date'] == comparison_day
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['receipts'][0]['result'] == raw


@pytest.mark.parametrize('legacy_hint', [None, {'entity': ['reader-only-phantom']}, {'evidence': ['reader-only-phantom']}])
def test_original_reader_context_resolves_ellipsis_without_copied_hints(legacy_hint):
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            refs = [f['id'] for f in report_facts(payload) if f['metric'] == 'close']
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'frame', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '以下002415收盘价均以元/股计。'}], 'refs': []},
                {'id': 'answer', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '先22，后23。'}], 'refs': refs}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0]['assertion_kind'] = 'discourse'
            part = result['paragraphs'][1]['parts'][0]
            if legacy_hint is None:
                part.pop('referents')
            else:
                part['referents'] = legacy_hint
            return json.dumps(result)
        assert all(set(context) == {'evidence', 'calculations'} for context in payload['contexts'].values())
        target = next(row for row in payload['statements'] if row['text'] == '先22，后23。')
        assert [row['text'] for row in payload['statements']] == ['以下002415收盘价均以元/股计。', '先22，后23。']
        assert all('referents' not in row for row in payload['statements'])
        assert 'reader-only-phantom' not in json.dumps(payload)
        assert payload['contexts'][target['context_id']]['evidence']
        return json.dumps(assess(payload))
    result = publish_report(task='报告两个观察日的收盘价。', draft='', tool_results=RECEIPTS,
        run_scope='reader-referents', call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification['status'] == 'complete'
    assert '先22，后23。' in result.artifact['reader_body']


@pytest.mark.parametrize('zone', ['Asia/Shanghai', 'UTC'])
@pytest.mark.parametrize('kind', ['observation', 'judgment'])
@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('scope,reference,as_of,supported', [
    ('reference_date', '2025-01-02', '20250101', False),
    ('reference_date', '2025-01-02', '20250102', True),
    ('reference_date', None, '20250102', False),
    ('reference_date', '2025-01-02', None, False),
    ('other', '2025-01-02', '20250101', True),
])
def test_owned_date_controls_final_publication_even_when_model_approves(tmp_path, zone, kind, bound, scope, reference, as_of, supported):
    raw = json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': as_of,
                      '_meta': {'timestamp': '2025-01-02T15:00:00+08:00'}})
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            pb = next(f for f in report_facts(payload) if f['metric'] == 'pb')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '600928当前市净率为' if scope == 'reference_date' else '按2025年1月1日数据，600928市净率为'},
                    {'type': 'value', 'ref': pb['id'], 'field': 'value_with_unit'} if bound else {'type': 'text', 'text': '0.8倍'},
                    {'type': 'text', 'text': '。'}], 'refs': [pb['id']]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0]['time_scope'] = scope
            result['paragraphs'][0]['parts'][0]['assertion_kind'] = kind
            return json.dumps(result)
        result = assess(payload)
        pb = next(f for f in payload['contexts']['p0']['evidence'] if f['metric'] == 'pb')
        # For a bound value, omit the reviewer link deliberately: the owned
        # rendering binding must still be checked. Literal prose uses its link.
        result['assessments'][0]['time_evidence'] = [] if bound else [pb['id']]
        if kind == 'judgment':
            result['assessments'][0]['checks']['quantity'] = 'not_applicable'
        return json.dumps(result)
    result = publish_report(task='说明所给市净率。', draft='', tool_results=[('get_fundamentals', raw)],
        run_scope='owned-date', call=writer, fact_review_call=checker, max_calls=2,
        reference_date=reference, reference_timezone=zone)
    assert result.artifact['review_valid']
    assert (result.verification['status'] == 'complete') is supported
    assert ('0.8倍' in result.artifact['reader_body']) is supported
    row = result.artifact['factual_review']['assessments'][0]
    assert row['reported_verdict'] == 'entailed' and row['reported_checks']['time'] == 'entailed'
    assert row['verdict'] == ('entailed' if supported else 'unknown')
    assert result.artifact['temporal_checks']['c0']['status'] == (
        'not_applicable' if scope == 'other' else 'entailed' if supported else 'unknown')
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['receipts'][0]['result'] == raw
    assert saved['verification']['status'] == ('complete' if supported else 'incomplete')


@pytest.mark.parametrize('support', ['entailed', 'unknown'])
def test_present_research_opinion_is_not_a_new_measurement_but_still_needs_support(support):
    text = '从所给观察资料看，倾向保持谨慎判断。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [{'type': 'text', 'text': text}],
                'refs': [f['id'] for f in report_facts(payload) if f['metric'] == 'close']}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            part = result['paragraphs'][0]['parts'][0]
            part.update(assertion_kind='judgment', time_scope='reference_date')
            return json.dumps(result)
        result = assess(payload)
        result['assessments'][0]['checks'].update(quantity='not_applicable', time='not_applicable', support=support)
        return json.dumps(result)
    result = publish_report(task='给出基于所给资料的研究看法。', draft='', tool_results=RECEIPTS,
        run_scope='present-opinion', call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-03')
    assert result.artifact['review_valid']
    assert (result.verification['status'] == 'complete') is (support == 'entailed')
    assert (text in result.artifact['reader_body']) is (support == 'entailed')
    assert result.artifact['temporal_checks']['c0']['reason'] == 'non_observational_judgment'


@pytest.mark.parametrize('fault', ['missing', 'wrong_type', 'duplicate', 'unknown_ref'])
def test_temporal_links_must_belong_to_the_assertion_context(fault):
    def reply(payload, _):
        result = assess(payload)
        row = result['assessments'][0]
        ref = payload['contexts']['p0']['evidence'][0]['id']
        if fault == 'missing':
            del row['time_evidence']
        elif fault == 'wrong_type':
            row['time_evidence'] = ref
        elif fault == 'duplicate':
            row['time_evidence'] = [ref, ref]
        else:
            row['time_evidence'] = ['unowned-evidence']
        return result
    result, _, _ = publish(reply)
    assert not result.artifact['review_valid']
    assert result.verification['status'] == 'incomplete'
    assert '22元/股升至23元/股' not in result.text


@pytest.mark.parametrize('other_day,supported', [('2025-01-02', True), ('2025-01-01', False)])
@pytest.mark.parametrize('other_scope', ['same', 'entity', 'account', 'timeframe', 'adjustment', 'unknown_ref'])
def test_owned_provenance_is_independent_of_semantic_scope_and_date(tmp_path, other_day, supported, other_scope):
    first = {'symbol': '002415', 'account': 'short_stock', 'timeframe': '1d', 'adjustment': 'unadjusted',
             'bars': [{'timestamp': '2025-01-02', 'close': 22}]}
    second = {'symbol': '002415', 'account': 'short_stock', 'timeframe': '1d', 'adjustment': 'unadjusted',
              'bars': [{'timestamp': other_day, 'high': 24}]}
    if other_scope == 'entity':
        second['symbol'] = '000001'
    elif other_scope == 'account':
        second['account'] = 'long_stock'
    elif other_scope == 'timeframe':
        second['timeframe'] = '1w'
    elif other_scope == 'adjustment':
        second['adjustment'] = 'adjusted'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            facts = report_facts(payload)
            close = next(f['id'] for f in facts if f['metric'] == 'close')
            high = next(f['id'] for f in facts if f['metric'] == 'high')
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'answer', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '002415今日收盘价为'},
                    {'type': 'value', 'ref': close, 'field': 'value_with_unit'}], 'refs': []},
                {'id': 'history', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '另一条已注明日期的观察记录：'},
                    {'type': 'value', 'ref': high, 'field': 'date'},
                    {'type': 'value', 'ref': high, 'field': 'value_with_unit'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(result)
        result = assess(payload)
        high = next(f['id'] for f in payload['contexts']['p1']['evidence'] if f['metric'] == 'high')
        result['assessments'][0]['time_evidence'] = ['not-a-run-fact'] if other_scope == 'unknown_ref' else [high]
        return json.dumps(result)
    result = publish_report(task='核对当日观察并保留另一条注明日期的记录。', draft='',
        tool_results=[('get_bars', json.dumps(first)), ('get_bars', json.dumps(second))],
        run_scope='cross-paragraph-date', call=writer, fact_review_call=checker,
        max_calls=4 if other_scope == 'unknown_ref' else 2,
        reference_date='2025-01-02')
    assert result.artifact['review_valid'] is (other_scope != 'unknown_ref')
    assert (result.verification['status'] == 'complete') is (supported and other_scope != 'unknown_ref')
    if other_scope != 'unknown_ref':
        timing = result.artifact['temporal_checks']['c0']
        assert timing['status'] == ('entailed' if supported else 'unknown')
        assert {row['observed_date'] for row in timing['evidence']} == {'2025-01-02', other_day}
    else:
        assert any(row['code'] == 'review_unavailable' for row in result.verification['findings'])
        assert result.artifact['factual_assessment_errors']['c0']['code'] == 'temporal_reference_not_in_run_facts'
        assert not result.artifact['review_feedback_valid']
        assert not result.artifact.get('revision_attempts') and result.artifact['model_calls'] == 4
        assert '24元/股' in result.artifact['reader_body'], 'valid other paragraph must survive an annotation error'
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text


@pytest.mark.parametrize('observed', ['2025-01-02', '2025-01-01'])
@pytest.mark.parametrize('witness', [True, False])
def test_qualitative_current_observation_needs_a_completed_time_witness(tmp_path, observed, witness):
    text = '002415今天盘中存在价格起伏。'
    raw = json.dumps({'symbol': '002415', 'timeframe': '1d', 'bars': [
        {'timestamp': observed, 'high': 23, 'low': 22, 'close': 22.5}]})
    writing = []
    def writer(system, user):
        payload = json.loads(user)
        writing.append(payload)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        assert 'revision' not in payload, 'missing review metadata alone cannot retry prose'
        return json.dumps({'schema_version': 1, 'items': [{'id': 'price', 'type': 'paragraph',
            'section': 'overview', 'parts': [{'type': 'text', 'text': text}],
            'refs': [f['id'] for f in report_facts(payload) if f['metric'] in {'high', 'low'}]}]})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            reply = body_reply(payload)
            reply['paragraphs'][0]['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(reply)
        reply = assess(payload)
        row = reply['assessments'][0]
        row['checks']['quantity'] = 'not_applicable'
        if witness:
            row['time_evidence'] = [f['id'] for f in payload['contexts']['p0']['evidence'] if f['metric'] == 'high']
        return json.dumps(reply)
    result = publish_report(task='说明本轮观察到的价格表现。', draft='', tool_results=[('get_bars', raw)],
        run_scope='qualitative-time', call=writer, fact_review_call=checker,
        reference_date='2025-01-02', max_calls=2 if witness else 4)
    supported = witness and observed == '2025-01-02'
    assert (result.verification['status'] == 'complete') is supported
    assert (text in result.artifact['reader_body']) is supported
    assert result.artifact['review_valid'] is witness
    assert len(writing) == 2 and result.artifact['model_calls'] == 4
    assert not result.artifact.get('revision_attempts')
    if witness:
        assert not result.artifact['factual_assessment_errors']
        assert result.artifact['review_feedback_valid'] is not supported
        assert result.artifact['temporal_checks']['c0']['status'] == ('entailed' if supported else 'unknown')
    else:
        assert result.artifact['factual_assessment_errors']['c0']['code'] == 'temporal_witness_missing'
        assert not result.artifact['review_feedback_valid']
        assert result.artifact['factual_review']['assessments'][0]['validation_status'] == 'unavailable'
        assert any(f['code'] == 'review_unavailable' for f in result.verification['findings'])
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(ref['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['receipts'][0]['result'] == raw


@pytest.mark.parametrize('observed', ['2025-01-01', None])
@pytest.mark.parametrize('uses_feedback', [True, False])
def test_revision_receives_exact_assertion_and_runtime_dates_and_must_act_on_them(tmp_path, observed, uses_feedback):
    raw = json.dumps({'symbol': '002415', 'pb': 0.8, 'as_of': observed})
    writing, reviews = [], []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        writing.append(payload)
        fact = next(f for f in report_facts(payload) if f['metric'] == 'pb')
        corrected = 'revision' in payload and uses_feedback
        if 'revision' in payload:
            feedback = payload['revision']['factual_feedback']
            assert len(feedback) == 1
            row = feedback[0]
            assert row['paragraph_id'] == 'valuation' and row['completed'] is True
            assert row['text'] == '002415当前市净率为0.8倍。'
            assert row['checks']['time'] == 'unknown' and row['verdict'] == 'unknown'
            assert row['temporal_check']['expected_date'] == '2025-01-02'
            assert row['temporal_check']['evidence'][0]['comparison_date'] == observed
            assert row['temporal_check']['evidence'][0]['observed_date'] == observed
        parts = [{'type': 'text', 'text': '002415所给资料的市净率为' if corrected else '002415当前市净率为'},
                 {'type': 'value', 'ref': fact['id'], 'field': 'value_with_unit'}]
        if corrected and observed:
            parts += [{'type': 'text', 'text': '，资料日期为'}, {'type': 'value', 'ref': fact['id'], 'field': 'date'},
                      {'type': 'text', 'text': '，不能据此确认当前值。'}]
        else:
            parts += [{'type': 'text', 'text': '，有效日期未提供，无法确认当前值。' if corrected else '。'}]
        return json.dumps({'schema_version': 1, 'items': [{'id': 'valuation', 'type': 'paragraph',
            'section': 'overview', 'parts': parts, 'refs': []}]})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            reply = body_reply(payload)
            reply['paragraphs'][0]['parts'][0]['time_scope'] = 'other' if len(writing) == 2 and uses_feedback else 'reference_date'
            return json.dumps(reply)
        reviews.append(payload)
        return json.dumps(assess(payload))  # Even optimistic model dates cannot override owned time checks.
    result = publish_report(task='说明所给估值并保留时点限制。', draft='', tool_results=[('get_fundamentals', raw)],
        run_scope='structured-time-repair', call=writer, fact_review_call=checker,
        reference_date='2025-01-02', reference_timezone='Asia/Shanghai')
    assert len(writing) == len(reviews) == 2 and result.artifact['model_calls'] == 8
    assert (result.verification['status'] == 'complete') is uses_feedback
    assert ('0.8倍' in result.artifact['reader_body']) is uses_feedback
    assert result.artifact['receipts'][0]['result'] == raw
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(ref['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text
    assert saved['revision_attempts'][1]['revision_input']['factual_feedback'] == writing[1]['revision']['factual_feedback']


@pytest.mark.parametrize('annotation_fault', ('invalid_reference', *METADATA_FAULTS))
def test_partial_review_can_repair_a_verified_error_once_without_resampling_annotation_failures(tmp_path, annotation_fault):
    counts = {'compose': 0, 'body': 0, 'fact': 0}
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            counts['compose'] += 1
            corrected = 'revision' in payload
            if corrected:
                feedback = {row['assertion_id']: row for row in payload['revision']['factual_feedback']}
                assert feedback['c0']['completed'] is False
                assert feedback['c1']['completed'] is True
                assert feedback['c1']['checks']['support'] == 'unknown'
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
            parts = [{'type': 'text', 'text': '002415收盘价为'},
                     {'type': 'value', 'ref': ref, 'field': 'value_with_unit'}]
            if corrected:
                parts = [{'type': 'text', 'text': '002415在'}, {'type': 'value', 'ref': ref, 'field': 'date'},
                         {'type': 'text', 'text': '的收盘价为'}, *parts[1:]]
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'price', 'section': 'overview', 'type': 'paragraph', 'parts': parts, 'refs': []},
                {'id': 'meaning', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '这些统计不足以确认交易主体。' if corrected else '这些统计证明非主力出货。'}],
                 'refs': [ref]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            counts['body'] += 1
            return json.dumps(body_reply(payload))
        counts['fact'] += 1
        result = assess(payload)
        if counts['fact'] == 1:
            if annotation_fault == 'invalid_reference':
                result['assessments'][0]['time_evidence'] = ['not-a-run-fact']
            else:
                break_assessment_metadata(result['assessments'][0], annotation_fault)
            result['assessments'][1]['checks']['support'] = 'unknown'
        return json.dumps(result)
    result = publish_report(task='报告观察价格并说明能否据此判断交易主体。', draft='', tool_results=RECEIPTS,
        run_scope='partial-review-repair', call=writer, fact_review_call=checker, max_calls=4)
    assert result.artifact['review_valid'] and result.verification['status'] == 'complete'
    assert counts == {'compose': 2, 'body': 2, 'fact': 2} and result.artifact['model_calls'] == 8
    assert '不足以确认交易主体' in result.artifact['reader_body']
    first, final = result.artifact['revision_attempts']
    assert not first['review_valid'] and first['review_feedback_valid']
    assert set(first['factual_assessment_errors']) == {'c0'}
    assert first['factual_review']['assessments'][1]['verdict'] == 'unknown'
    assert 'validation_status' not in first['factual_review']['assessments'][1]
    assert final['review_valid'] and not final['factual_assessment_errors']
    assert result.artifact['selected_attempt'] == 1 and result.artifact['revision_changed_body']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['receipts'] == result.artifact['receipts']


@pytest.mark.parametrize('annotation_error', ['missing_witness', 'invalid_reference'])
@pytest.mark.parametrize('retains_plan', [True, False])
def test_incomplete_revision_keeps_verified_analysis_without_publishing_unreviewed_claims(
    tmp_path, annotation_error, retains_plan,
):
    counts = {'writer': 0, 'body': 0, 'fact': 0}
    plan_text = '后续继续观察，不能仅据这些价格认定趋势会延续。'
    pending_text = '002415当前两次收盘观察的价格走高。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        counts['writer'] += 1
        corrected = 'revision' in payload
        ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
        price_parts = ([{'type': 'text', 'text': '所给观察日'}, {'type': 'value', 'ref': ref, 'field': 'date'},
            {'type': 'text', 'text': '002415收盘价为'}, {'type': 'value', 'ref': ref, 'field': 'value_with_unit'}]
            if corrected else [{'type': 'text', 'text': '002415收盘价为999元/股。'}])
        items = [{'id': 'price', 'section': 'positions', 'type': 'paragraph', 'parts': price_parts, 'refs': [ref]}]
        if corrected:
            items.append({'id': 'pending', 'section': 'overview', 'type': 'paragraph',
                'parts': [{'type': 'text', 'text': pending_text}], 'refs': [f['id'] for f in report_facts(payload) if f['metric'] == 'close']})
        if not corrected or retains_plan:
            items.append({'id': 'plan', 'section': 'plan', 'type': 'paragraph',
                'parts': [{'type': 'text', 'text': plan_text}], 'refs': [ref]})
        return json.dumps({'schema_version': 1, 'items': items})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            counts['body'] += 1
            reply = body_reply(payload)
            for row in reply['paragraphs']:
                if row['id'] == 'pending':
                    row['parts'][0].update(assertion_kind='comparison', time_scope='reference_date')
                elif row['id'] == 'plan':
                    row['parts'][0].update(assertion_kind='judgment', kind='plan')
            return json.dumps(reply)
        counts['fact'] += 1
        reply = assess(payload)
        if counts['fact'] == 1:
            reply['assessments'][0]['checks']['quantity'] = 'contradicted'
        else:
            pending = next(row['id'] for row in payload['statements'] if row['text'] == pending_text)
            assessment = next(row for row in reply['assessments'] if row['id'] == pending)
            assessment['time_evidence'] = [] if annotation_error == 'missing_witness' else ['not-owned']
        return json.dumps(reply)
    result = publish_report(task='报告观察价格与后续观察态度。', draft='', tool_results=RECEIPTS,
        run_scope='incomplete-correction', call=writer, fact_review_call=checker, max_calls=4,
        reference_date='2025-01-02')
    assert counts == {'writer': 2, 'body': 2, 'fact': 2}
    assert result.artifact['model_calls'] == result.artifact['call_limit'] == 8
    assert result.artifact['selected_attempt'] == int(retains_plan)
    assert result.verification['status'] == 'incomplete' and result.verification['report'] == 'partial'
    assert plan_text in result.text and '999元/股' not in result.text and pending_text not in result.text
    assert ('22元/股' in result.artifact['reader_body']) is retains_plan
    revised = result.artifact['revision_attempts'][1]
    assert not revised['review_valid'] and len(revised['factual_assessment_errors']) == 1
    assert next(row for row in revised['review']['reviews'] if row['id'] == 'price')['supported']
    assert next(row for row in revised['review']['reviews'] if row['id'] == 'pending')['supported'] is False
    if retains_plan:
        assert not result.artifact['review_valid'] and result.artifact['factual_assessment_errors']
        assert any(row['code'] == 'review_unavailable' for row in result.verification['findings'])
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['verification']['status'] == 'incomplete'
    assert saved['receipts'] == result.artifact['receipts'] and len(saved['revision_attempts']) == 2


def test_newer_fact_in_another_paragraph_cannot_refresh_an_old_bound_pb():
    receipts = [('get_fundamentals', json.dumps({'symbol': '600928', 'pb': 0.4572, 'as_of': '20261111'})),
                ('get_fundamentals', json.dumps({'symbol': '600928', 'pe_ttm': 12, 'as_of': '20261112'}))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            facts = report_facts(payload)
            return json.dumps({'schema_version': 1, 'items': [
                {'id': metric, 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '600928当前' + metric + '为'},
                    {'type': 'value', 'ref': next(f['id'] for f in facts if f['metric'] == metric),
                     'field': 'value_with_unit'}], 'refs': []} for metric in ('pb', 'pe_ttm')]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            for row in result['paragraphs']:
                row['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(result)
        result = assess(payload)
        fresh = next(f['id'] for f in payload['contexts']['p1']['evidence'] if f['metric'] == 'pe_ttm')
        result['assessments'][0]['time_evidence'] = [fresh]
        return json.dumps(result)
    result = publish_report(task='说明估值。', draft='', tool_results=receipts, run_scope='no-date-laundering',
        call=writer, fact_review_call=checker, max_calls=2, reference_date='2026-11-12')
    assert result.artifact['review_valid'] and result.verification['status'] == 'incomplete'
    assert '0.4572倍' not in result.artifact['reader_body'] and '12倍' in result.artifact['reader_body']
    timing = result.artifact['temporal_checks']['c0']
    assert timing['reason'] == 'observation_not_at_asserted_date'
    assert {row['observed_date'] for row in timing['evidence']} == {'2026-11-11', '2026-11-12'}


def test_current_bound_change_keeps_the_original_interval_and_checks_its_end_date():
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            refs = [f['id'] for f in report_facts(payload) if f['metric'] == 'close']
            return json.dumps({'schema_version': 1, 'items': [
                {'id': 'change', 'section': 'overview', 'type': 'calculation', 'operation': 'change_pct', 'refs': refs},
                {'id': 'answer', 'section': 'overview', 'type': 'paragraph', 'parts': [
                    {'type': 'text', 'text': '今天较上一观察日上涨'},
                    {'type': 'value', 'ref': 'change', 'field': 'value_with_unit'},
                    {'type': 'text', 'text': '。'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            result = body_reply(payload)
            result['paragraphs'][0]['parts'][0]['time_scope'] = 'reference_date'
            return json.dumps(result)
        result = assess(payload)
        result['assessments'][0]['time_evidence'] = [f['id'] for f in payload['contexts']['p0']['evidence']]
        return json.dumps(result)
    result = publish_report(task='给出今天相较上一观察日的收盘变动。', draft='', tool_results=RECEIPTS,
        run_scope='interval-date', call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
    assert result.verification['status'] == 'complete' and '4.55%' in result.artifact['reader_body']
    timing = result.artifact['temporal_checks']['c0']
    assert timing['status'] == 'entailed'
    assert [row['observed_date'] for row in timing['evidence']] == ['2025-01-02']
    assert result.artifact['calculations']['change']['refs'] == [f['id'] for f in result.artifact['facts'] if f['metric'] == 'close']


@pytest.mark.parametrize('as_of,supported', [('20250101', False), ('20250102', True)])
def test_execution_projection_preserves_the_following_bound_observation_date(as_of, supported):
    prefix = '本轮已经买入002415共100股。'
    receipts = [('get_fundamentals', json.dumps({'symbol': '600928', 'pb': 0.8, 'as_of': as_of})),
                ('place_paper_trade', json.dumps({'status': 'executed', 'trade_id': 'frozen-date-test',
                    'symbol': '002415', 'account': 'test', 'side': 'buy', 'quantity': 100, 'price': 22,
                    'timestamp': '2025-01-02T10:00:00+08:00'}))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            pb = next(f for f in report_facts(payload) if f['metric'] == 'pb')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [{'type': 'text', 'text': prefix + '600928当前市净率为'},
                    {'type': 'value', 'ref': pb['id'], 'field': 'value_with_unit'},
                    {'type': 'text', 'text': '。'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            text = payload['paragraphs'][0]['text']
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'answer', 'parts': [
                {'text': prefix, 'kind': 'current_outcome', 'assertion_kind': 'actor_activity',
                 'time_scope': 'other', 'referents': {}, 'outcome': 'performed', 'extent': 'operation'},
                {'text': text[len(prefix):], 'kind': 'other', 'assertion_kind': 'observation',
                 'time_scope': 'reference_date', 'referents': {}, 'outcome': 'none', 'extent': 'none'}]}]})
        return json.dumps(assess(payload))
    result = publish_report(task='说明本轮结果和市净率。', draft='', tool_results=receipts,
        current_tool_results=receipts, run_scope='projection-date', call=writer,
        fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
    assert result.artifact['review_valid']
    assert (result.verification['status'] == 'complete') is supported
    assert len(result.artifact['execution_records']) == 1
    assert next(row['supported'] for row in result.artifact['review']['reviews'] if row['id'] == 'answer') is supported
    assert result.artifact['temporal_checks']['c0']['status'] == ('entailed' if supported else 'unknown')
    assert result.artifact['factual_review_input']['statements'][0]['bound_fact_refs']
    assert prefix not in result.artifact['reader_body']


@pytest.mark.parametrize('dimension', ['identity_scope', 'time', 'quantity', 'support'])
@pytest.mark.parametrize('verdict', ['entailed', 'unknown', 'contradicted', 'not_applicable'])
def test_each_review_dimension_controls_persisted_consumer_result(tmp_path, dimension, verdict):
    def reply(payload, _):
        result = assess(payload)
        result['assessments'][0]['checks'][dimension] = verdict
        return result
    result, _, _ = publish(reply)
    supported = verdict in {'entailed', 'not_applicable'}
    assert result.artifact['review_valid']
    assert (result.verification['status'] == 'complete') is supported
    assert ('22元/股升至23元/股' in result.text) is supported
    row = result.artifact['factual_review']['assessments'][0]
    assert row['reported_verdict'] == 'entailed'
    assert row['verdict'] == (verdict if not supported else 'entailed')
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text
    assert saved['verification']['status'] == ('complete' if supported else 'incomplete')
    assert not any(key in result.text for key in ('identity_scope', 'checks', 'reported_verdict'))


@pytest.mark.parametrize('fault', ['missing_checks', 'missing_dimension', 'unknown_dimension', 'invalid_status', 'bool_status'])
def test_incomplete_dimension_review_is_unavailable_not_a_successful_fact_check(fault):
    def reply(payload, _):
        result = assess(payload)
        row = result['assessments'][0]
        if fault == 'missing_checks':
            del row['checks']
        elif fault == 'missing_dimension':
            del row['checks']['time']
        elif fault == 'unknown_dimension':
            row['checks']['other'] = 'entailed'
        else:
            row['checks']['time'] = True if fault == 'bool_status' else 'approved'
        return result
    result, _, _ = publish(reply)
    assert not result.artifact['review_valid']
    assert result.verification['status'] == 'incomplete'
    assert '22元/股升至23元/股' not in result.text
    assert any(f['code'] == 'review_unavailable' for f in result.verification['findings'])


@pytest.mark.parametrize('as_of,expected,utc_date', [
    ('20261111', '2026-11-11', '2026-11-11'), ('20261112', '2026-11-12', '2026-11-12'),
    ('2026-11-12', '2026-11-12', '2026-11-12'),
    ('2026-11-12T00:00:00+08:00', '2026-11-12', '2026-11-11'), (None, None, None)])
def test_review_observation_date_comes_from_source_not_retrieval(as_of, expected, utc_date):
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog
    raw = json.dumps({'_meta': {'timestamp': '2026-11-12T15:10:51'},
                      'symbol': '600928', 'pb': 0.4572, 'as_of': as_of})
    catalog = EvidenceCatalog.build([('get_fundamentals', raw)], run_scope='dates')
    fact = next(f for f in catalog.facts.values() if f.metric == 'pb')
    assert fact.observation_date('Asia/Shanghai') == expected
    assert fact.observation_date('UTC') == utc_date
    assert catalog.receipts[0]['result'] == raw


@pytest.mark.parametrize('fault', ['none', 'missing', 'duplicate', 'unknown_id'])
@pytest.mark.parametrize('legitimate', [True, False])
def test_each_assertion_must_be_reviewed_before_the_complete_paragraph_is_published(tmp_path, fault, legitimate):
    prefix = '300406的量比为0.594倍，换手率为1.18%。'
    conclusion = '这些统计不足以判断交易主体是否卖出。' if legitimate else '这些统计证明非主力出货。'
    receipts = [('compute_volume_ratio', json.dumps({'symbol': '300406', 'volume_ratio': 0.594, 'turnover_pct': 1.18}))]
    seen = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            return json.dumps({'schema_version': 1, 'items': [{'id': 'c0', 'type': 'paragraph', 'section': 'market',
                'parts': [{'type': 'text', 'text': prefix + conclusion}], 'refs': [f['id'] for f in report_facts(payload)]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': 'c0', 'parts': [
                {'text': text, 'kind': 'other', 'assertion_kind': category, 'time_scope': 'other', 'referents': {}, 'outcome': 'none', 'extent': 'none'}
                for text, category in [(prefix, 'observation'), (conclusion, 'judgment' if legitimate else 'actor_activity')]]}]})
        seen.append(payload)
        assert [row['text'] for row in payload['statements']] == [prefix, conclusion]
        assert [row.get('requirements', {}) for row in payload['statements']] == [
            {}, {} if legitimate else {'participant_activity': True}]
        assert all(not {'assertion_kind', 'meaning_kind', 'time_scope'}.intersection(row)
                   for row in payload['statements'])
        assert {row['context_id'] for row in payload['statements']} == {'p0'}
        assert 'text' not in payload['contexts']['p0']
        assert set(payload['contexts']['p0']) == {'evidence', 'calculations'}
        assert len(payload['contexts']['p0']['evidence']) == 3  # identity, ratio, turnover
        reply = assess(payload)
        reply['assessments'][1]['verdict'] = 'entailed' if legitimate else 'unknown'
        if fault == 'missing':
            reply['assessments'].pop()
        elif fault == 'duplicate':
            reply['assessments'][1] = reply['assessments'][0]
        elif fault == 'unknown_id':
            reply['assessments'][1]['id'] = 'unrequested-claim'
        return json.dumps(reply)
    result = publish_report(task='说明成交活跃度及资料局限。', draft='', tool_results=receipts,
        run_scope='assertion-coverage', call=writer, fact_review_call=checker, max_calls=2)
    supported = legitimate and fault == 'none'
    assert len(seen) == 1
    assert (result.verification['status'] == 'complete') is supported
    assert (conclusion in result.artifact['reader_body']) is supported
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text and saved['factual_review_input'] == seen[0]
    assert saved['verification']['status'] == ('complete' if supported else 'incomplete')


@pytest.mark.parametrize('value,display', [(0.42287699999999995, '0.42%'), (-0.42287699999999995, '-0.42%'),
                                         (0.004, '0.004%'), (-0.004, '-0.004%'), (0, '0%')])
def test_percent_reader_precision_preserves_source_values_and_real_nonzero_changes(tmp_path, value, display):
    receipts = [('get_market_pulse', json.dumps({'_meta': {'data_as_of': None,
        'retrieved_at': '2026-11-12T15:00:00+08:00', 'timezone': 'Asia/Shanghai'},
        'sectors': [{'name': '金融行业', 'change_pct': value}]}))]
    expected_raw = str(value)
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            fact = next(f for f in report_facts(payload) if f['metric'] == 'change_pct')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'percentage', 'section': 'market', 'type': 'paragraph',
                'parts': [{'type': 'text', 'text': '金融行业的涨跌幅为'},
                          {'type': 'value', 'ref': fact['id'], 'field': 'value_with_unit'}, {'type': 'text', 'text': '。'}], 'refs': []}]})
        assert display in payload['report']
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            return json.dumps(body_reply(payload))
        evidence = next(f for row in payload['contexts'].values() for f in row['evidence'] if f['metric'] == 'change_pct')
        assert evidence['display_value_with_unit'] == display
        assert evidence['value'] == expected_raw
        assert evidence['observed_at'] is None
        assert evidence['observed_date'] is None
        return json.dumps(assess(payload))
    result = publish_report(task='说明所给金融行业涨跌幅。', draft='', tool_results=receipts,
        run_scope='percentage-precision', call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification['status'] == 'complete' and display in result.text
    assert '2026-11-12' not in result.artifact['reader_body']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['receipts'][0]['result'] == receipts[0][1]
    assert next(f['value'] for f in saved['facts'] if f['metric'] == 'change_pct') == expected_raw


def body_reply(payload):
    assert set(payload) == {"paragraphs", "report_context"}
    return {"quantity_issues": [], "paragraphs": [{"id": row["id"], "parts": [{"text": row["text"], "kind": "other", "assertion_kind": "observation", "time_scope": "other", "referents": {},
        "outcome": "none", "extent": "none"}]} for row in payload["paragraphs"]]}


@pytest.mark.parametrize('change', [
    {'claim_kind': 'availability'}, {'operation_code': 'other'},
    {'as_of_task_now': 'reference_date'}, {'kind': 'other'},
    {'operation_code': True}, {'operation_code': 11},
])
def test_mixed_body_wire_axes_cannot_publish_a_valid_price(change):
    def checker(system, user):
        payload = json.loads(user)
        assert 'paragraphs' in payload  # Invalid BODY must never reach FACT.
        row = payload['paragraphs'][0]
        part = {'through': row['source_units'][-1]['id'], 'operation_code': 0,
                'claim_kind': 'observation', 'as_of_task_now': False, **change}
        return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': row['id'], 'parts': [part]}]})
    def writer(system, user):
        payload = json.loads(user)
        assert 'schema' in payload
        return json.dumps(compose(payload))
    result = publish_report(task='说明所给价格。', draft='', tool_results=RECEIPTS,
        run_scope='invalid-body-wire', call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification['status'] == 'incomplete' and not result.artifact['review_valid']
    assert result.artifact['model_calls'] == 2 and result.artifact['factual_model_calls'] == 0
    assert result.artifact['attempts'][-1]['review_error_type'] == 'BodyMeaningError'
    assert '22元/股升至23元/股' not in result.artifact['reader_body']


@pytest.mark.parametrize('prefix', ['针对收盘价记录的核对，', '针对持仓逻辑是否仍成立的问题，', '并非没有观察资料，', '若只看已提供的记录；', '说明👩‍💻（e\u0301），'])
@pytest.mark.parametrize('wire_format', ['legacy', 'compact_axes'])
def test_source_unit_selection_preserves_all_reader_text_without_model_copying(tmp_path, prefix, wire_format):
    expected = prefix + '002415在2025-01-01的收盘价为22元/股。'
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [{'type': 'text', 'text': prefix + '002415在'},
                    {'type': 'value', 'ref': ref, 'field': 'date'}, {'type': 'text', 'text': '的收盘价为'},
                    {'type': 'value', 'ref': ref, 'field': 'value_with_unit'}, {'type': 'text', 'text': '。'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            row = payload['paragraphs'][0]
            assert row['text'] == expected
            part = body_reply(payload)['paragraphs'][0]['parts'][0]
            part.pop('text')
            part.pop('referents')
            part['through'] = row['source_units'][-1]['id']
            if wire_format == 'compact_axes':
                part = {'through': part['through'], 'operation_code': 0,
                    'claim_kind': 'observation', 'as_of_task_now': False}
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': row['id'], 'parts': [part]}]})
        assert payload['statements'][0]['text'] == expected
        return json.dumps(assess(payload))
    result = publish_report(task='报告所给第一日收盘记录。', draft='', tool_results=RECEIPTS,
        run_scope='addressed-reader-text', call=writer, fact_review_call=checker, max_calls=2)
    assert result.artifact['review_valid'] and result.verification['status'] == 'complete'
    assert result.artifact['reader_body'].startswith(expected)
    assert result.artifact['body_meaning']['answer'][0]['text'] == expected
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text


@pytest.mark.parametrize('fault', ['unknown', 'repeat', 'reverse', 'early_end', 'mixed'])
def test_invalid_source_unit_selection_cannot_reach_fact_approval(fault):
    factual = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [{'type': 'text', 'text': '针对收盘记录，002415收盘价为'},
                    {'type': 'value', 'ref': ref, 'field': 'value_with_unit'}, {'type': 'text', 'text': '。'}], 'refs': []}]})
        raise AssertionError('invalid body selection must not reach coverage')
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' not in payload:
            factual.append(payload)
            raise AssertionError('invalid selection must not reach fact review')
        row = payload['paragraphs'][0]
        part = body_reply(payload)['paragraphs'][0]['parts'][0]
        part.pop('text')
        part['through'] = row['source_units'][-1]['id']
        parts = [part]
        if fault == 'unknown':
            part['through'] = 'not-a-source-unit'
        elif fault == 'early_end':
            part['through'] = row['source_units'][0]['id']
        elif fault in {'repeat', 'reverse'}:
            parts.append({**part, 'through': part['through'] if fault == 'repeat' else row['source_units'][0]['id']})
        else:
            parts.append({**{key: value for key, value in part.items() if key != 'through'}, 'text': row['text']})
        return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': row['id'], 'parts': parts}]})
    result = publish_report(task='报告观察。', draft='', tool_results=RECEIPTS,
        run_scope='bad-source-selection', call=writer, fact_review_call=checker, max_calls=2)
    assert not result.artifact['review_valid'] and result.verification['status'] == 'incomplete'
    assert not factual and result.artifact['factual_model_calls'] == 0
    assert '22元/股' not in result.artifact['reader_body']
    assert any(row.get('stage') == 'body_meaning' for row in result.artifact['attempts'])


@pytest.mark.parametrize('day,analysis_supported', [('2025-01-02', True), ('2025-01-01', False)])
def test_selected_source_units_preserve_bound_dates_after_execution_projection(day, analysis_supported):
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' in payload:
            ref = next(f['id'] for f in report_facts(payload) if f['metric'] == 'close')
            return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'section': 'overview',
                'type': 'paragraph', 'parts': [{'type': 'text', 'text': '已买入100股。002415当前收盘价为'},
                    {'type': 'value', 'ref': ref, 'field': 'value_with_unit'}, {'type': 'text', 'text': '。'}], 'refs': []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            row = payload['paragraphs'][0]
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': row['id'], 'parts': [
                {'through': row['source_units'][0]['id'], 'operation_code': 3,
                 'claim_kind': 'actor_activity', 'as_of_task_now': False},
                {'through': row['source_units'][-1]['id'], 'operation_code': 0,
                 'claim_kind': 'observation', 'as_of_task_now': True}]}]})
        assert '已买入' not in payload['statements'][0]['text']
        return json.dumps(assess(payload))
    result = publish_report(task='说明价格与执行状态。', draft='',
        tool_results=[('get_bars', json.dumps({'symbol': '002415', 'timeframe': '1d',
                      'bars': [{'timestamp': day, 'close': 22}]}))], current_tool_results=[],
        run_scope='selected-projection', call=writer, fact_review_call=checker, max_calls=2, reference_date='2025-01-02')
    assert result.artifact['review_valid'] and result.verification['status'] == 'incomplete'
    assert ('22元/股' in result.artifact['reader_body']) is analysis_supported
    assert result.artifact['execution_projections']['answer']['analysis_supported'] is analysis_supported
    assert '已买入100股' not in result.artifact['reader_body']


def publish(fact_reply, *, max_calls=2, coverage_missing=False, check_active=lambda: None, reference_date=None):
    primary, factual = [], []
    def writer(system, user):
        payload = json.loads(user)
        primary.append(payload)
        if "schema" in payload:
            doc = compose(payload)
            if "revision" in payload:
                doc["items"][0]["parts"][0]["text"] = "002415在所给观察日的收盘价由"
            return json.dumps(doc)
        assert payload["inferences"] == [] and payload["receipts"] == []
        assert "22元/股升至23元/股" in payload["report"]
        missing = coverage_missing and len(primary) == 2
        return json.dumps({"reviews": [], "coverage": {"complete": not missing,
            "missing": ["正文仍需补充任务答案"] if missing else [], "repairable": missing}})
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            return json.dumps(body_reply(payload))
        factual.append(payload)
        assert set(payload) == {"statements", "contexts", "receipts", "execution_records", "report_context", "request_context", "source_semantics"}
        assert payload['source_semantics'] == []  # These bar receipts contain no typed estimate method.
        assert payload["request_context"] == {"role": "requirements", "text": "task instruction marker"}
        assert payload["receipts"][0]["result"] == RECEIPTS[0][1]
        assert "untrusted draft marker" not in user
        result = fact_reply(payload, len(factual))
        return result if isinstance(result, str) else json.dumps(result)
    result = publish_report(task="task instruction marker", draft="untrusted draft marker", tool_results=RECEIPTS,
        run_scope="factual-role", run_id="owned", call=writer, fact_review_call=checker,
        max_calls=max_calls, check_active=check_active, reference_date=reference_date)
    return result, primary, factual


@pytest.mark.parametrize("verdict", ["entailed", "contradicted", "unknown"])
def test_only_supported_final_body_reaches_reader_and_reopened_artifact(tmp_path, verdict):
    result, primary, factual = publish(lambda payload, _: assess(payload, verdict))
    assert len(primary) == 2 and len(factual) == 1
    assert result.artifact["model_calls"] == 4 and result.artifact["factual_model_calls"] == 1
    assert result.artifact["body_review_model_calls"] == 1
    assert result.artifact["call_limit"] == 6
    assert (result.verification["status"] == "complete") is (verdict == "entailed")
    assert ("22元/股升至23元/股" in result.text) is (verdict == "entailed")
    assert result.verification["execution"] == "completed"
    assert result.verification["data"] == "available"
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == result.text
    assert saved["coverage_review_input"] == primary[-1]
    assert saved["factual_review_input"] == factual[0]
    assert saved["factual_review"]["assessments"][0]["verdict"] == verdict
    assert set(saved["body_review_input"]) == {"paragraphs", "report_context"}
    assert saved["body_meaning"]["answer"][0]["text"] == factual[0]["statements"][0]["text"]


@pytest.mark.parametrize("meaning", ["plan", "current_outcome"])
def test_rule_context_remains_separate_from_evidence_and_cannot_authorize_execution_claim(tmp_path, meaning):
    task = "仅在价格下降时考虑买入。今天收盘价据说30元；请核对后给出本轮决定。"
    text = "本轮决定继续观察，不执行买入。" if meaning == "plan" else "本轮决定继续观察，且本轮没有买入。"
    observed = []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            refs = [f["id"] for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [{"id": "decision", "type": "paragraph", "section": "plan",
                "parts": [{"type": "text", "text": text}], "refs": refs}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            assert set(payload) == {"paragraphs", "report_context"}
            return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "decision", "parts": [
                {"text": text, "kind": meaning, "time_scope": "other", "referents": {}, "assertion_kind": "judgment" if meaning == "plan" else "actor_activity", "outcome": "none" if meaning == "plan" else "not_performed",
                 "extent": "none" if meaning == "plan" else "operation"}]}]})
        observed.append(payload)
        assert payload["request_context"] == {"role": "requirements", "text": task}
        assert payload["receipts"][0]["result"] == RECEIPTS[0][1]
        assert payload["execution_records"] == []
        assert [row['requirements'] for row in payload['statements']] == [{'proposal': True}]
        assert all(not {'assertion_kind', 'meaning_kind', 'time_scope'}.intersection(row)
                   for row in payload['statements'])
        assert all(f["value"] != "30" for row in payload["contexts"].values() for f in row["evidence"])
        return json.dumps(assess(payload))
    result = publish_report(task=task, draft="already bought; untrusted draft", tool_results=RECEIPTS,
        run_scope="policy-provenance", call=writer, fact_review_call=checker, max_calls=2)
    if meaning == "plan":
        assert result.verification["status"] == "complete" and text in result.text
        assert len(observed) == 1 and "already bought" not in json.dumps(observed)
    else:
        assert result.verification["status"] == "incomplete" and text not in result.text
        assert not observed  # A later approval cannot establish non-execution.
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == result.text


@pytest.mark.parametrize("receipt_status", [None, "executed", "rejected", "unknown"])
@pytest.mark.parametrize("analysis_supported", [True, False])
def test_only_runtime_receipts_supply_current_outcomes_to_final_reader(tmp_path, receipt_status, analysis_supported):
    prefix = "002415在2025年1月1日和1月2日的收盘价分别为22元/股和23元/股。"
    # The author is wrong even beside a valid receipt: it must not survive in
    # analysis merely because another model would approve the whole paragraph.
    claim = "本轮未执行新的买卖操作。" if receipt_status is None else "本轮已买入300股。"
    rows = list(RECEIPTS)
    if receipt_status == "executed":
        receipt = {"status": "executed", "trade_id": "owned-test", "symbol": "002415", "account": "test",
                   "side": "buy", "quantity": 100, "price": 22, "timestamp": "2025-01-02T10:00:00+08:00"}
    else:
        receipt = {"trade_rejected": True, "error": "可用资金不足"} if receipt_status == "rejected" else {"error": "断开连接"}
    if receipt_status is not None:
        rows.append(("place_paper_trade", json.dumps(receipt)))
    coverage_inputs, factual_inputs = [], []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            refs = [f["id"] for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [{"id": "answer", "section": "market", "type": "paragraph",
                "parts": [{"type": "text", "text": prefix+claim}], "refs": refs}]})
        coverage_inputs.append(payload)
        assert claim not in payload["report"]
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            assert [{key: row[key] for key in ("id", "text")} for row in payload["paragraphs"]] == [{"id": "answer", "text": prefix+claim}]
            return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "answer", "parts": [
                {"text": prefix, "kind": "other", "assertion_kind": "observation", "time_scope": "other", "referents": {}, "outcome": "none", "extent": "none"},
                {"text": claim, "kind": "current_outcome", "assertion_kind": "actor_activity", "time_scope": "other", "referents": {}, "outcome": "not_performed" if receipt_status is None else "performed",
                 "extent": "whole_run" if receipt_status is None else "operation"}]}]})
        factual_inputs.append(payload)
        assert payload["statements"][0]["text"] == prefix
        return json.dumps(assess(payload, "entailed" if analysis_supported else "contradicted"))
    publication = publish_report(task="说明价格与执行结果", draft="", tool_results=rows, current_tool_results=rows,
        run_scope="execution-ownership", run_id="owned", call=writer, fact_review_call=checker, max_calls=2)
    assert claim not in publication.text and claim not in publication.artifact["reader_body"]
    assert len(coverage_inputs) == 1
    assert publication.artifact["body_review_model_calls"] == 1
    assert publication.artifact["factual_model_calls"] == 1
    assert len(factual_inputs) == 1
    assert (prefix in publication.text) is analysis_supported
    if receipt_status is None:
        assert publication.verification["status"] == "incomplete"
        assert publication.artifact["review"]["reviews"][0]["supported"] is False
        assert "本轮操作结果尚未确认。" in publication.text
        assert publication.artifact["execution_projections"]["answer"]["analysis_supported"] is analysis_supported
        if not analysis_supported:
            assert "比较最终陈述与原始资料。" in publication.artifact["review"]["reviews"][0]["reason"]
            assert {f["code"] for f in publication.verification["findings"]} >= {"execution_claim_unconfirmed", "insufficient_support"}
            assert "市场部分尚未完成" in publication.text
    elif receipt_status == "executed":
        assert publication.verification["status"] == ("complete" if analysis_supported else "incomplete")
        assert "买入100股" in publication.text and "买入300股" not in publication.text
    elif receipt_status == "rejected":
        assert publication.verification["status"] == ("complete" if analysis_supported else "incomplete")
        assert "本次未成交：可用资金不足" in publication.text
    else:
        assert publication.verification["execution"] == "unknown"
        assert publication.verification["status"] == "incomplete"
    ref = persist_publication(tmp_path, publication)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == publication.text
    assert saved["execution_projections"]["answer"]["removed"][0]["text"] == claim


@pytest.mark.parametrize("fault", ["omit", "rewrite", "duplicate", "wrong_id", "wrong_kind", "missing_assertion_kind", "unknown_assertion_kind", "missing_time_scope", "unknown_time_scope", "timeout"])
def test_missing_or_unfaithful_body_interpretation_cannot_reach_coverage_or_fact_approval(fault):
    calls = []
    def writer(system, user):
        payload = json.loads(user)
        assert "schema" in payload
        return json.dumps(compose(payload))
    def checker(system, user):
        payload = json.loads(user)
        calls.append(payload)
        assert set(payload) == {"paragraphs", "report_context"}
        if fault == "timeout":
            raise TimeoutError("body interpretation unavailable")
        result = body_reply(payload)
        row = result["paragraphs"][0]
        if fault == "omit":
            row["parts"][0]["text"] = row["parts"][0]["text"][:-1]
        elif fault == "rewrite":
            row["parts"][0]["text"] += "额外限定"
        elif fault == "duplicate":
            result["paragraphs"].append(row)
        elif fault == "wrong_id":
            row["id"] = "other"
        elif fault == "missing_assertion_kind":
            row["parts"][0].pop("assertion_kind")
        elif fault == "unknown_assertion_kind":
            row["parts"][0]["assertion_kind"] = "approved_technical_analysis"
        elif fault == "missing_time_scope":
            row['parts'][0].pop('time_scope')
        elif fault == "unknown_time_scope":
            row['parts'][0]['time_scope'] = 'latest_available'
        else:
            row["parts"][0]["kind"] = "approved"
        return json.dumps(result)
    result = publish_report(task="比较收盘价", draft="", tool_results=RECEIPTS, run_scope="invalid-body",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert len(calls) == 1 and result.artifact["factual_model_calls"] == 0
    assert result.verification["status"] == "incomplete"
    assert "22元/股升至23元/股" not in result.text


def test_execution_only_report_uses_receipt_without_empty_prose_or_source_marker():
    claim = "本轮买入300股。"
    receipt = {"status": "executed", "trade_id": "owned-fill", "symbol": "002415", "account": "test",
               "side": "buy", "quantity": 100, "price": 22, "timestamp": "2025-01-02T10:00:00+08:00"}
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            return json.dumps({"schema_version": 1, "items": [{"id": "outcome", "type": "paragraph", "section": "execution",
                "parts": [{"type": "text", "text": claim}], "refs": [f["id"] for f in report_facts(payload)]}]})
        assert "买入100股" in payload["report"] and claim not in payload["report"]
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        assert set(json.loads(user)) == {"paragraphs", "report_context"}
        return json.dumps({"quantity_issues": [], "paragraphs": [{"id": "outcome", "parts": [{"text": claim, "kind": "current_outcome", "assertion_kind": "actor_activity", "time_scope": "other", "referents": {},
            "outcome": "performed", "extent": "operation"}]}]})
    result = publish_report(task="报告成交结果", draft="", tool_results=[("place_paper_trade", json.dumps(receipt))],
                            run_scope="execution-only", call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == "complete"
    assert result.artifact["factual_model_calls"] == 0 and result.artifact["body_review_model_calls"] == 1
    assert "买入100股" in result.text and "300" not in result.text and "[^source-" not in result.text


def test_cancellation_after_body_interpretation_stops_before_coverage_and_evidence_review():
    stopped = False
    def active():
        if stopped:
            raise TurnInterruptedError("")
    def writer(system, user):
        payload = json.loads(user)
        assert "schema" in payload
        return json.dumps(compose(payload))
    def checker(system, user):
        nonlocal stopped
        stopped = True
        return json.dumps(body_reply(json.loads(user)))
    with pytest.raises(TurnInterruptedError):
        publish_report(task="比较价格", draft="", tool_results=RECEIPTS, run_scope="cancel-body",
                       call=writer, fact_review_call=checker, check_active=active)


@pytest.mark.parametrize("reference_date", [None, "2025-01-02", "2025-01-03"])
def test_runtime_date_reaches_writer_reviewer_revision_and_reopened_record(tmp_path, reference_date):
    result, primary, factual = publish(lambda payload, n: assess(payload, "unknown" if n == 1 else "entailed"),
                                      max_calls=4, reference_date=reference_date)
    context = {"reference_date": reference_date, "reference_timezone": "Asia/Shanghai", "display_timezone": "Asia/Shanghai"}
    assert len(primary) == 4 and len(factual) == 2
    assert all(row["report_context"] == context for row in [*primary, *factual])
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["report_context"] == context
    assert all(row["factual_review_input"]["report_context"] == context for row in saved["revision_attempts"])


@pytest.mark.parametrize("reference_date", ["20250102", "2025-02-30", "today", True])
def test_invalid_runtime_date_cannot_be_guessed_by_model(reference_date):
    with pytest.raises(ValueError):
        publish(lambda *_: pytest.fail("invalid date reached model"), reference_date=reference_date)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong_id", "wrong_type", "extra", "blank", "timeout"])
def test_malformed_or_unavailable_local_review_never_falls_back_to_writer_approval(fault):
    def reply(payload, _):
        if fault == "timeout":
            raise TimeoutError("local model unavailable")
        result = assess(payload)
        row = result["assessments"][0]
        if fault == "missing":
            result["assessments"] = []
        elif fault == "duplicate":
            result["assessments"].append(row)
        elif fault == "wrong_id":
            row["id"] = "other"
        elif fault == "wrong_type":
            row["verdict"] = True
        elif fault == "extra":
            row["approved"] = True
        else:
            row["comparison"] = " "
        return result
    result, _, factual = publish(reply)
    assert len(factual) == 1 and not result.artifact["review_valid"]
    assert result.verification["report"] == "partial"
    assert "22元/股升至23元/股" not in result.text
    assert any(f["code"] == "review_unavailable" for f in result.verification["findings"])


def test_one_revision_shares_eight_call_limit_and_reviews_actual_final_text_again():
    result, primary, factual = publish(lambda payload, n: assess(payload, "unknown" if n == 1 else "entailed"), max_calls=4)
    assert result.verification["status"] == "complete"
    assert len(primary) == 4 and len(factual) == 2
    assert result.artifact["model_calls"] == result.artifact["call_limit"] == 8
    assert result.artifact["body_review_model_calls"] == 2
    assert result.artifact["selected_attempt"] == 1
    assert len(result.artifact["revision_attempts"]) == 2
    assert factual[-1]["statements"][0]["text"] in result.artifact["reader_body"]


@pytest.mark.parametrize("failure", ["unavailable", "unsupported"])
def test_failed_revision_cannot_erase_first_fact_checked_body(failure):
    def reply(payload, n):
        if n == 2:
            if failure == "unavailable":
                raise TimeoutError("review unavailable")
            return assess(payload, "contradicted")
        return assess(payload)
    result, _, factual = publish(reply, max_calls=4, coverage_missing=True)
    assert len(factual) == 2 and result.artifact["selected_attempt"] == 0
    assert "22元/股升至23元/股" in result.text
    assert result.verification["report"] == "partial"
    assert result.artifact["model_calls"] == 8


def test_cancellation_after_fact_call_prevents_publication_or_revision():
    cancelled = False
    def active():
        if cancelled:
            raise TurnInterruptedError("")
    def reply(payload, _):
        nonlocal cancelled
        cancelled = True
        return assess(payload)
    with pytest.raises(TurnInterruptedError):
        publish(reply, max_calls=4, check_active=active)


def test_local_reviewer_routes_locally_without_tools_retries_or_deadline_reset(tmp_path, monkeypatch):
    configs, invocations = [], []
    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory", llm=LLMConfig(
        provider="openai", model="writer", api_key_env="WRITER_KEY", local_report_review_model="local-fact-model"))
    def client(review_config):
        configs.append(review_config)
        def stream(messages, **kwargs):
            invocations.append(kwargs)
            assert not providers._maintenance_mode.get()
            assert providers._precise_response.get()
            assert providers._response_output_limit.get() == 16384
            assert providers._remaining_request_time() <= 10
            assert kwargs["tools"] is None and kwargs["is_cancelled"] is cancelled
            return SimpleNamespace(content='{"assessments":[]}', tool_calls=[])
        return SimpleNamespace(stream_complete=stream)
    monkeypatch.setattr(providers, "create_chat_client", client)
    def cancelled():
        return False
    callback = providers.create_local_report_fact_check(config, is_cancelled=cancelled)
    assert not configs
    with providers.llm_request_deadline(time.monotonic()+10):
        assert callback("review", "data") == '{"assessments":[]}'
        callback("review again", "data")
    assert len(configs) == 1 and len(invocations) == 2
    assert not providers._precise_response.get()
    actual = configs[0]
    assert actual.llm.provider == "lmstudio" and actual.llm.model == "local-fact-model"
    assert actual.llm.api_key_env == "" and actual.llm.max_retries == 0
    assert actual.data_dir == config.data_dir and actual.memory_dir == config.memory_dir
    assert config.llm.provider == "openai"  # No mutation or implicit model switch.
    assert providers.LMSTUDIO_BASE_URL == "http://localhost:1234/v1"
    assert providers.create_local_report_fact_check(replace(config, llm=replace(config.llm, local_report_review_model=""))) is None


def test_local_reviewer_cannot_request_a_business_tool(tmp_path, monkeypatch):
    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory",
                       llm=LLMConfig(local_report_review_model="local-fact-model"))
    monkeypatch.setattr(providers, "create_chat_client", lambda _: SimpleNamespace(stream_complete=lambda *a, **k:
        SimpleNamespace(content="", tool_calls=[object()])))
    with pytest.raises(AgentTurnError, match="不能执行工具"):
        providers.create_local_report_fact_check(config)("review", "data")


def test_review_model_configuration_survives_reload_without_changing_main_model(tmp_path, monkeypatch):
    path = tmp_path/"config.yaml"
    monkeypatch.setenv("TRADE_COMPASS_CONFIG", str(path))
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(tmp_path/"home"))
    path.write_text('llm:\n  provider: lmstudio\n  model: writer\n  local_report_review_model: local-reviewer\n')
    try:
        for _ in range(2):
            invalidate_config_cache()
            config = load_app_config()
            assert config.llm.model == "writer" and config.llm.local_report_review_model == "local-reviewer"
    finally:
        invalidate_config_cache()


@pytest.mark.parametrize("verdict", ["entailed", "unknown", "unavailable"])
def test_scheduled_api_uses_configured_factual_role_and_never_replays_business_tools(tmp_path, monkeypatch, verdict):
    from fastapi.testclient import TestClient
    from trade_compass_agent.config import AgentConfig
    from trade_compass_agent.llm.providers import ChatCompletion, ToolCall
    from trade_compass_agent.ops.prompt_jobs import PromptJobExecutor, PromptJobStore
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.web.app import create_app

    factory_configs, queries, checks, body_checks = [], [], [], []
    class Writer:
        name = model = "mock-writer"
        def stream_complete(self, messages, **kwargs):
            try:
                payload = json.loads(messages[-1].content)
            except (ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict) and "schema" in payload:
                return ChatCompletion(content=json.dumps(compose(payload)))
            if isinstance(payload, dict) and "inferences" in payload:
                assert payload["inferences"] == []
                return ChatCompletion(content='{"reviews":[],"coverage":{"complete":true,"missing":[]}}')
            if not any(m.role == "tool" for m in messages):
                return ChatCompletion(content="", tool_calls=[ToolCall("bars", "get_bars", '{"symbol":"002415"}')])
            return ChatCompletion(content="已读取收盘资料。")
        complete = stream_complete

    class FactChecker:
        def stream_complete(self, messages, **kwargs):
            assert kwargs["tools"] is None
            payload = json.loads(messages[-1].content)
            if "paragraphs" in payload:
                body_checks.append(payload)
                if verdict == "unavailable":
                    raise AgentTurnError("local model is not available")
                return ChatCompletion(content=json.dumps(body_reply(payload)))
            checks.append(payload)
            if verdict == "unavailable":
                raise AgentTurnError("local model is not available")
            return ChatCompletion(content=json.dumps(assess(payload, verdict)))

    def make_checker(config):
        factory_configs.append(config)
        assert config.llm.provider == "lmstudio" and config.llm.model == "local-reviewer"
        return FactChecker()
    def query(_registry, name, _arguments):
        queries.append(name)
        assert name == "get_bars"
        return RECEIPTS[0][1]

    config = AppConfig(data_dir=tmp_path/"data", memory_dir=tmp_path/"memory", data_provider="sample",
        agent=AgentConfig(learning_enabled=False), llm=LLMConfig(local_report_review_model="local-reviewer"))
    monkeypatch.setattr("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Writer())
    monkeypatch.setattr(providers, "create_chat_client", make_checker)
    monkeypatch.setattr("trade_compass_agent.runtime.tools.registry.ToolRegistry.execute", query)
    monkeypatch.setattr("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", lambda *_: None)
    jobs = PromptJobStore(config.data_dir/"scheduler.db")
    job = jobs.create(name="价格比较", prompt="比较两个观察日的收盘价", schedule="daily 12:00", delivery_channels=())
    runs = SqliteRunStore(config.data_dir/"scheduler.db")
    PromptJobExecutor(config, runs).execute(job)
    run = runs.recent_runs(job_id=f"custom:{job.id}")[0]
    monkeypatch.setattr("trade_compass_agent.web.api.load_app_config", lambda: config)
    detail = TestClient(create_app()).get(f"/api/jobs/runs/{run.id}").json()
    verification = detail["step_runs"][0]["data"]["verification"]
    assert queries == ["get_bars"] and len(factory_configs) == 1
    assert len(checks) == (2 if verdict == "unknown" else 0 if verdict == "unavailable" else 1)
    assert len(body_checks) == (2 if verdict == "unknown" else 1)
    assert detail["status"] == ("completed" if verdict == "entailed" else "degraded")
    assert ("22元/股升至23元/股" in detail["analysis"]) is (verdict == "entailed")
    assert verification["execution"] == "completed"
    artifact = json.loads((config.data_dir/"report_artifacts"/(verification["artifact_id"]+".json")).read_text())
    assert artifact["published_text"] == detail["analysis"]
    assert artifact["factual_model_calls"] == len(checks)
    assert artifact["body_review_model_calls"] == len(body_checks)
    assert artifact["report_context"]["reference_date"] == detail["step_runs"][0]["data"]["session_id"][-10:]
    assert all(row["report_context"] == artifact["report_context"] for row in checks)


@pytest.mark.parametrize("ambiguous", [True, False])
def test_body_quantity_meaning_cannot_be_repaired_by_evidence_approval(tmp_path, ambiguous):
    text = "价格变动比例为4.55。" if ambiguous else "这里的变动比例以百分数计，为4.55。"
    checks = []
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market",
                 "parts": [{"type": "text", "text": text}], "refs": ["change"]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        checks.append(payload)
        if "paragraphs" in payload:
            reply = body_reply(payload)
            if ambiguous:
                reply["quantity_issues"] = [{"id": "answer", "text": "4.55", "reason": "正文未明确比例尺度。"}]
            return json.dumps(reply)
        return json.dumps(assess(payload))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="quantity-meaning",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == ("incomplete" if ambiguous else "complete")
    assert (text in result.text) is not ambiguous
    assert len(checks) == (1 if ambiguous else 2)
    assert bool(result.artifact["body_quantity_issues"]) is ambiguous
    assert result.artifact["review"]["reviews"][0]["supported"] is not ambiguous
    if ambiguous:
        assert "quantity_meaning_ambiguous" in {f["code"] for f in result.verification["findings"]}
        assert result.artifact["factual_model_calls"] == 0
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/"report_artifacts"/(ref["artifact_id"]+".json")).read_text())
    assert saved["published_text"] == result.text
    assert saved["body_quantity_issues"] == result.artifact["body_quantity_issues"]


@pytest.mark.parametrize("fault", ["missing", "not_list", "wrong_id", "invented_text", "empty_reason", "extra_field"])
def test_malformed_body_quantity_assessment_is_not_silent_approval(fault):
    def checker(system, user):
        payload = json.loads(user)
        assert set(payload) == {"paragraphs", "report_context"}
        body = body_reply(payload)
        issue = {"id": "answer", "text": payload["paragraphs"][0]["text"], "reason": "需要明确单位。"}
        body["quantity_issues"] = [issue]
        if fault == "missing":
            del body["quantity_issues"]
        elif fault == "not_list":
            body["quantity_issues"] = None
        elif fault == "wrong_id":
            issue["id"] = "unknown"
        elif fault == "invented_text":
            issue["text"] = "正文没有这句话"
        elif fault == "empty_reason":
            issue["reason"] = ""
        else:
            issue["verdict"] = "approved"
        return json.dumps(body)
    def writer(system, user):
        return json.dumps(compose(json.loads(user)))
    result = publish_report(task="比较收盘价", draft="", tool_results=RECEIPTS, run_scope="invalid-quantity",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert result.verification["status"] == "incomplete"
    assert result.artifact["factual_model_calls"] == 0
    assert "22元/股升至23元/股" not in result.text


@pytest.mark.parametrize("first_text", ["价格变动比例为4.55。", "这里的变动比例以百分数计，为4.55。"])
@pytest.mark.parametrize("corrected", [True, False])
def test_quantity_clarification_revision_is_rechecked_and_bounded(first_text, corrected):
    # The second first_text is legitimate prose that a model may falsely flag.
    # Recovery still rewrites once; another factual mistake cannot be approved.
    revised_text = "价格变动比例为4.55%。" if corrected else "价格变动比例为4.56%。"
    writing_calls = []
    def writer(system, user):
        payload = json.loads(user)
        writing_calls.append(payload)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            text = revised_text if "revision" in payload else first_text
            if "revision" in payload:
                assert payload["revision"]["review"]["reviews"][0]["supported"] is False
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market",
                 "parts": [{"type": "text", "text": text}], "refs": ["change"]}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            reply = body_reply(payload)
            if payload["paragraphs"][0]["text"] == first_text:
                reply["quantity_issues"] = [{"id": "answer", "text": "4.55", "reason": "正文未明确比例尺度。"}]
            return json.dumps(reply)
        assert payload["statements"][0]["text"] == revised_text
        return json.dumps(assess(payload, "entailed" if corrected else "contradicted"))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="quantity-revision",
                            call=writer, fact_review_call=checker)
    assert (result.verification["status"] == "complete") is corrected
    assert (revised_text in result.text) is corrected
    assert first_text not in result.text
    assert len(writing_calls) == 4
    assert result.artifact["model_calls"] == 7 and result.artifact["call_limit"] == 8
    assert result.artifact["body_review_model_calls"] == 2 and result.artifact["factual_model_calls"] == 1
    assert result.artifact["selected_attempt"] == 1
    first, revised = result.artifact["revision_attempts"]
    assert first["body_quantity_issues"] and not revised["body_quantity_issues"]
    assert first["verification"]["status"] == "incomplete"


@pytest.mark.parametrize("author_duplicate", [True, False])
@pytest.mark.parametrize("verdict", ["entailed", "contradicted"])
def test_bound_unit_presence_cannot_override_sentence_truth_or_author_quantity(author_duplicate, verdict):
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            facts = [f for f in report_facts(payload) if f["metric"] == "close"]
            parts = [{"type": "text", "text": "价格变动比例为"},
                     {"type": "value", "ref": "change", "field": "value_with_unit"},
                     {"type": "text", "text": "。"}]
            if author_duplicate:
                parts.append({"type": "text", "text": "另一期间的变动比例为4.55。"})
            return json.dumps({"schema_version": 1, "items": [
                {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
                 "refs": [f["id"] for f in facts]},
                {"id": "answer", "type": "paragraph", "section": "market", "parts": parts, "refs": []}]})
        return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
    factual = []
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            result = body_reply(payload)
            # Exact candidate32 false positive; the same quote may also exist
            # in author prose and must not borrow the other occurrence's unit.
            result["quantity_issues"] = [{"id": "answer", "text": "4.55" if author_duplicate else "4.55%",
                                          "reason": "Missing unit or ratio scale in the reader body"}]
            return json.dumps(result)
        factual.append(payload)
        assert payload["statements"][0]["text"] == "价格变动比例为4.55%。"
        trace, = payload["contexts"][payload["statements"][0]["context_id"]]["calculations"]
        assert trace["display_value_with_unit"] == "4.55%"
        assert trace["result"] == "4.545454545454545454545454545"
        return json.dumps(assess(payload, verdict))
    result = publish_report(task="给出变动比例", draft="", tool_results=RECEIPTS, run_scope="owned-quantity",
                            call=writer, fact_review_call=checker, max_calls=2)
    assert bool(factual) is not author_duplicate
    assert bool(result.artifact["bound_quantity_checks"]) is not author_duplicate
    assert bool(result.artifact["body_quantity_issues"]) is author_duplicate
    supported = not author_duplicate and verdict == "entailed"
    assert (result.verification["status"] == "complete") is supported
    assert result.artifact["review"]["reviews"][0]["supported"] is supported
    if not author_duplicate:
        assert result.artifact["factual_model_calls"] == 1
    assert "另一期间的变动比例为4.55。" not in result.text


@pytest.mark.parametrize("origin,field,unit", [
    ("author", "value_with_unit", "%"), ("evidence", "subject", "%"),
    ("evidence", "value_with_unit", ""), ("evidence", "value_with_unit", "单位未提供"),
])
def test_unit_presence_requires_runtime_quantity_binding(origin, field, unit):
    from trade_compass_agent.runtime.report_body import runtime_bound_quantity_issue
    segments = [{"origin": origin, "field": field, "quantity_unit": unit, "text": "4.55%"}]
    assert not runtime_bound_quantity_issue({"text": "4.55%"}, segments)


@pytest.mark.parametrize("genuine_gap", [True, False])
@pytest.mark.parametrize("supported", [True, False])
def test_bound_gap_review_keeps_local_factual_and_coverage_roles_independent(genuine_gap, supported):
    def writer(system, user):
        payload = json.loads(user)
        if "schema" in payload:
            doc = compose(payload)
            doc["items"].append({"id": "gap", "type": "gap", "section": "market",
                                 "text": "缺少营收资料。" if genuine_gap else "缺少收盘价资料。"})
            return json.dumps(doc)
        assert payload["inferences"] == [] and payload["receipts"] == []
        assert len(payload["verified_items"]) == 2
        return json.dumps({"reviews": [], "coverage": {"complete": True, "missing": []},
            "gap_reviews": [{"id": "gap", "status": "required" if genuine_gap else "already_present",
                "refs": [] if genuine_gap else [payload["verified_items"][0]["id"]],
                "reason": "仍无营收资料。" if genuine_gap else "已有收盘价。"}]})
    def checker(system, user):
        payload = json.loads(user)
        if "paragraphs" in payload:
            return json.dumps(body_reply(payload))
        assert "task" not in payload and "gaps" not in payload
        return json.dumps(assess(payload, "entailed" if supported else "unknown"))
    report = publish_report(task="说明收盘价与资料缺口。", draft="", tool_results=RECEIPTS,
                            run_scope="local-bound-gap", call=writer, fact_review_call=checker, max_calls=2)
    assert (report.verification["status"] == "complete") is (supported and not genuine_gap)
    assert ("22元/股升至23元/股" in report.text) is supported
    assert any(f["code"] == "declared_gap" for f in report.verification["findings"]) is genuine_gap
    assert report.artifact["factual_model_calls"] == report.artifact["body_review_model_calls"] == 1


@pytest.mark.parametrize('fault', METADATA_FAULTS)
def test_bad_assessment_row_retains_other_verified_prose_without_annotation_retry(tmp_path, fault):
    counts = {'compose': 0, 'body': 0, 'fact': 0}
    good = '另外，002415在2025-01-02的收盘价为23元/股。'
    raw_answers = []
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        counts['compose'] += 1
        assert 'revision' not in payload, 'a metadata-only failure cannot resample the same body'
        document = compose(payload)
        fact = next(f for f in report_facts(payload) if f['metric'] == 'close' and f['observed_at']=='2025-01-02')
        document['items'].append({'id': 'good', 'type': 'paragraph', 'section': 'market',
            'parts': ['另外，', [fact['id'], 'subject'], '在', [fact['id'], 'date'],
                      '的收盘价为', [fact['id'], 'value_with_unit'], '。'], 'refs': []})
        return json.dumps(document)
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            counts['body'] += 1
            return json.dumps(body_reply(payload))
        counts['fact'] += 1
        response = assess(payload)
        break_assessment_metadata(response['assessments'][0], fault)
        raw_answers.append(json.dumps(response))
        return raw_answers[-1]
    result = publish_report(task='报告两日收盘记录。', draft='', tool_results=RECEIPTS,
        run_scope='per-row-availability', call=writer, fact_review_call=checker, max_calls=4)
    assert result.verification['status'] == 'incomplete' and not result.artifact['review_valid']
    assert counts == {'compose': 1, 'body': 1, 'fact': 1}
    assert good in result.artifact['reader_body']
    assert '22元/股升至23元/股' not in result.artifact['reader_body']
    assert set(result.artifact['factual_assessment_errors']) == {'c0'}
    rows = {row['id']: row for row in result.artifact['factual_review']['assessments']}
    assert rows['c0']['validation_status'] == 'unavailable' and rows['c0']['verdict'] != 'entailed'
    assert rows['c1']['verdict'] == 'entailed' and 'validation_status' not in rows['c1']
    assert not result.artifact['review_feedback_valid'] and not result.artifact.get('revision_attempts')
    assert result.artifact['factual_response'] == raw_answers[0]
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['reader_body'] == result.artifact['reader_body']
    assert saved['factual_response'] == raw_answers[0] and saved['receipts'] == result.artifact['receipts']


@pytest.mark.parametrize('scene', ['dated_valuation', 'ma_alignment', 'correct_valuation'])
def test_owned_date_or_indicator_conflict_can_fund_real_revision_despite_bad_annotation(tmp_path, scene):
    counts = {'compose': 0, 'body': 0, 'fact': 0}
    needs_change = scene != 'correct_valuation'
    day = '2025-01-01' if needs_change else '2025-01-02'
    receipts = [('compute_ma', json.dumps({'symbol': '002415', 'timeframe': '1d',
        '_meta': {'as_of': '2025-01-02'}, 'ma': {'MA5': 22, 'MA10': 23, 'MA20': 20, 'MA60': 18},
        'trend': 'mixed'}))] if scene == 'ma_alignment' else [
            ('get_fundamentals', json.dumps({'symbol': '002415', 'as_of': day, 'pb': 0.8}))]
    def writer(system, user):
        payload = json.loads(user)
        if 'schema' not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        counts['compose'] += 1
        changed = 'revision' in payload
        if changed:
            assert needs_change
            feedback = payload['revision']['factual_feedback'][0]
            assert feedback['completed'] is False and feedback['runtime_findings']
        facts = report_facts(payload)
        if scene == 'ma_alignment':
            parts = ['002415在2025-01-02的均线呈' + ('混合排列。' if changed else '多头排列。')]
        else:
            fact = next(f for f in facts if f['metric'] == 'pb')
            parts = ['002415在', [fact['id'], 'date'], '的市净率为', [fact['id'], 'value_with_unit'], '。'] if changed else [
                '002415当前的市净率为', [fact['id'], 'value_with_unit'], '。']
        return json.dumps({'schema_version': 1, 'items': [{'id': 'answer', 'type': 'paragraph',
            'section': 'assessment', 'parts': parts, 'refs': [f['id'] for f in facts]}]})
    def checker(system, user):
        payload = json.loads(user)
        if 'paragraphs' in payload:
            counts['body'] += 1
            p = payload['paragraphs'][0]
            changed = counts['body'] > 1
            return json.dumps({'quantity_issues': [], 'paragraphs': [{'id': p['id'], 'parts': [{
                'through': p['source_units'][-1]['id'], 'operation_code': 0, 'claim_kind': 'observation',
                'observation_dates': ['2025-01-02'] if scene == 'ma_alignment' else [day] if changed else ['task_now'],
                'classifications': [{'property': 'moving_average_order',
                    'value': 'mixed' if changed else 'bullish_alignment', 'operator': 'eq',
                    'scope': 'all_mentioned', 'periods': []}] if scene == 'ma_alignment' else []}]}]})
        counts['fact'] += 1
        response = assess(payload)
        owned = next(f for c in payload['contexts'].values() for f in c['evidence']
                     if f['kind'] in {'ratio', 'moving_average'})
        response['assessments'][0]['time_evidence'] = [owned['id']]
        if counts['fact'] == 1:
            break_assessment_metadata(response['assessments'][0], 'bad_check')
        return json.dumps(response)
    result = publish_report(task='说明所给日期的均线排列。' if scene == 'ma_alignment' else '说明所给市净率和数据日期。',
        draft='', tool_results=receipts, run_scope='owned-conflict-'+scene, call=writer,
        fact_review_call=checker, max_calls=4, reference_date='2025-01-02')
    assert counts == dict.fromkeys(counts, 2 if needs_change else 1)
    assert (result.verification['status'] == 'complete') is needs_change
    if needs_change:
        first, final = result.artifact['revision_attempts']
        assert first['review_feedback_valid'] and not first['review_valid']
        row = first['factual_review']['assessments'][0]
        assert row['validation_status'] == 'unavailable' and row['runtime_findings']
        assert final['review_valid'] and not final['factual_assessment_errors']
        assert result.artifact['revision_changed_body'] and result.artifact['selected_attempt'] == 1
        assert ('混合排列' if scene == 'ma_alignment' else day) in result.artifact['reader_body']
    else:
        assert not result.artifact['review_feedback_valid'] and not result.artifact.get('revision_attempts')
    ref = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(ref['artifact_id']+'.json')).read_text())
    assert [(r['tool'], r['result']) for r in saved['receipts']] == receipts
    assert saved['reader_body'] == result.artifact['reader_body']
