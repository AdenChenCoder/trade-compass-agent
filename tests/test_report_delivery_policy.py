"""Delivery is independent of admission to reusable knowledge."""
import json
from dataclasses import replace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.runtime.learning import curate_turn_insight
from trade_compass_agent.runtime.report_contract import combine_verifications, incomplete
from trade_compass_agent.runtime.report_publication import persist_publication, publish_task_report
from trade_compass_agent.runtime.tools.memory import tool_search_memory
from trade_compass_agent.runtime.types import TurnResponse


@pytest.mark.parametrize('text', [
    '昨日收盘22元/股，今日23元/股；明日观察能否站稳。',
    '依据昨日估值，当前仍倾向于持有；参与者构成无法确定。',
    '当前报价999元/股，非主力出货。',
])
def test_delivery_preserves_answer_and_raw_receipts_without_certifying_content(tmp_path, text):
    raw = '{"symbol":"002415","price":22,"timestamp":"2025-01-01"}'
    result = publish_task_report(task='说明走势', draft=text, run_scope='delivery', run_id='run',
        tool_results=[('sina_realtime_quote', raw), ('web_search', 'Original plain-text result')])
    assert result.text == text
    assert result.verification['status'] == 'not_reviewed'
    assert result.verification['review_status'] == 'not_requested'
    assert not incomplete(result.verification)
    assert result.artifact['model_calls'] == 0 and not result.artifact['review_valid']
    reference = persist_publication(tmp_path, result)
    saved = json.loads((tmp_path/'report_artifacts'/(reference['artifact_id']+'.json')).read_text())
    assert saved['published_text'] == text
    assert [r['result'] for r in saved['receipts']] == [raw, 'Original plain-text result']
    # A restarted workflow can deliver it without upgrading its review status.
    combined = combine_verifications({'report': {'verification': saved['verification']}})
    assert combined['status'] == 'not_reviewed' and not incomplete(combined)


def test_delivery_preserves_real_uncertain_execution(tmp_path):
    result = publish_task_report(task='报告结果', draft='操作结果需要核对。', run_scope='unknown',
        tool_results=[('place_paper_trade', '{"error":"connection lost"}')])
    assert result.text.startswith('操作结果需要核对。')
    assert result.verification['status'] == 'incomplete'
    assert result.verification['execution'] == 'unknown'
    combined = combine_verifications({'report': {'verification': result.verification}})
    assert incomplete(combined) and combined['review_status'] == 'not_requested'


@pytest.mark.parametrize('supported', [False, True])
def test_unreviewed_answer_requires_new_strict_review_before_searchable_memory(tmp_path, supported):
    from test_report_learning import RECEIPTS, reply

    config = AppConfig(data_dir=tmp_path/'data', memory_dir=tmp_path/'memory')
    config = replace(config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True)
    result = publish_task_report(task='比较收盘价', draft='随意写出的结论不构成事实依据。',
        tool_results=RECEIPTS, run_scope='ordinary', run_id='owned-run')
    reference = {**result.verification, **persist_publication(config.data_dir, result)}
    response = TurnResponse(session_id='source', summary=result.text, verification=reference)
    calls = []

    def review(system, user):
        payload = json.loads(user)
        calls.append(payload)
        if not supported:
            return 'invalid review response'
        return reply(system, user)

    path = curate_turn_insight(config=config, user_message='比较收盘价', response=response,
        report_run_id='owned-run', report_call=review)
    assert calls
    assert bool(path) is supported
    memories = json.loads(tool_search_memory(config.memory_dir, '002415'))['results']
    assert bool(memories) is supported
    assert all(result.text not in row.get('content', '') for row in memories)
    assert result.verification['status'] == 'not_reviewed'
