"""User-visible memory history preserves identities, outcomes and honest provenance."""
from dataclasses import asdict
import json

import pytest

from trade_compass_agent.memory.memory_store import EntryMeta, MemoryStore
from trade_compass_agent.memory.semantic_merge import merge_similar_entries
from trade_compass_agent.web import api


def read(client, monkeypatch, path):
    monkeypatch.setattr(api, '_memory_store_for_api', lambda: (MemoryStore(path), None))
    response = client.get('/api/memory/memory')
    assert response.status_code == 200
    return response.json()


def test_existing_duplicate_is_readable_without_migration_or_rewriting(client, monkeypatch, tmp_path):
    # Already-migrated ledgers contain only duplicate_of, not a successor field.
    winner = EntryMeta(entry_id='kept', text='仅使用有效行情下单，行情过期时重新查询。', source='curator')
    duplicate = EntryMeta(entry_id='duplicate', text=winner.text, source='curator',
                          status='archived', reason='duplicate_of:kept', needs_review=True)
    ledger = tmp_path/'.memory_meta.json'
    ledger.write_text(json.dumps({'schema_version': 3, 'revision': 9,
                                 'memory': [asdict(winner), asdict(duplicate)], 'user': [], 'history': []}))
    before = ledger.read_bytes()
    payload = read(client, monkeypatch, tmp_path)
    row = next(r for r in payload['entries'] if r['entry_id'] == 'duplicate')
    assert row['change_kind'] == 'deduplicated' and row['review_method'] == ''
    assert row['successors'] == [{'entry_id': 'kept', 'version': 1, 'text': winner.text, 'status': 'active'}]
    assert row['lineage_status'] == 'complete' and not row['needs_review']
    assert payload['chars_used'] == len(winner.text) and payload['char_limit'] == 3000
    assert not MemoryStore(tmp_path).capacity()['maintenance_needed']
    assert ledger.read_bytes() == before


def test_ai_merge_and_later_revisions_keep_exact_result_and_current_result(client, monkeypatch, tmp_path):
    store = MemoryStore(tmp_path)
    a = store.add('涨停家数超过三十家可作为市场情绪偏强的参考指标', source='curator')
    store.add('涨停超过三十家可视为市场情绪偏强的信号', source='curator')
    calls = []
    def model(system, user):
        calls.append(system)
        if '审查记忆修订' in system:
            return '{"valid":true,"reason":"保留阈值和参考属性"}'
        return '{"content":"涨停超过三十家是情绪偏强的参考信号","reason":"合并相同阈值"}'
    assert merge_similar_entries(store, model, force=True) == 1 and len(calls) == 2
    merged = store.list_active()[0]
    store.replace(merged.text, '涨停超过三十家可参考市场情绪，需结合成交量', entry_id=merged.entry_id)
    payload = read(client, monkeypatch, tmp_path)
    original = next(r for r in payload['entries'] if r['entry_id'] == a['entry_id'])
    assert original['change_kind'] == 'merged' and original['review_method'] == 'ai'
    assert [(r['entry_id'], r['version']) for r in original['successors']] == [(merged.entry_id, 1), (merged.entry_id, 2)]
    assert original['successors'][0]['text'] == merged.text
    assert original['successors'][1]['text'].endswith('需结合成交量')
    assert original['successors'][1]['status'] == 'active' and original['lineage_status'] == 'complete'
    middle = next(r for r in payload['entries'] if r['entry_id'] == merged.entry_id and r['version'] == 1)
    assert middle['review_method'] == ''  # A curator source does not establish AI review.


@pytest.mark.parametrize('case,expected', [('missing', 'unavailable'), ('ambiguous', 'ambiguous'), ('cycle', 'cycle')])
def test_missing_ambiguous_and_cyclic_history_never_invents_a_result(client, monkeypatch, tmp_path, case, expected):
    old = EntryMeta(entry_id='old', text='旧判断', status='archived', successor_id='new')
    rows = [old]
    if case == 'ambiguous':
        rows += [EntryMeta(entry_id='new', version=v, text=f'第{v}版', status='archived') for v in (1, 2)]
    if case == 'cycle':
        rows += [EntryMeta(entry_id='new', text='下一条', status='archived', successor_id='old')]
    (tmp_path/'.memory_meta.json').write_text(json.dumps({'schema_version': 3, 'revision': 1,
        'memory': [asdict(r) for r in rows], 'user': []}))
    row = read(client, monkeypatch, tmp_path)['entries'][0]
    assert row['lineage_status'] == expected
    assert len(row['successors']) == (1 if case == 'cycle' else 0)


def test_legacy_same_id_revision_resolves_by_version_without_ai_claim(client, monkeypatch, tmp_path):
    store = MemoryStore(tmp_path)
    created = store.add('旧判断', source='curator')
    store.replace('旧判断', '新判断')
    # Strip the additive metadata to emulate a previous version of the application.
    path = tmp_path/'.memory_meta.json'
    data = json.loads(path.read_text())
    for row in data['history']:
        for field in ('successor_version', 'change_kind', 'review_method'):
            row.pop(field, None)
    path.write_text(json.dumps(data))
    row = next(r for r in read(client, monkeypatch, tmp_path)['entries'] if r['version'] == 1)
    assert row['successors'][0]['entry_id'] == created['entry_id']
    assert row['successors'][0]['version'] == 2 and row['review_method'] == ''


def test_legacy_migration_records_duplicate_successor_and_clears_review(tmp_path):
    text = '长期有效的交易原则'
    rows = [asdict(EntryMeta(text=text, source='curator', needs_review=True)) for _ in range(2)]
    (tmp_path/'.memory_meta.json').write_text(json.dumps({'memory': rows, 'user': []}))
    (tmp_path/'KNOWLEDGE.md').write_text(text)
    store = MemoryStore(tmp_path)
    winner = store.list_active()[0]
    old = next(r for r in store.get_entries_with_meta() if r.status == 'archived')
    assert old.successor_id == winner.entry_id and old.successor_version == winner.version
    assert old.change_kind == 'deduplicated' and not old.needs_review


def feedback_history(tmp_path, origin):
    text = '600183 放量突破后趋势延续需结合成交量确认'
    if origin == 'duplicate':
        rows = [asdict(EntryMeta(text=text, source='curator', confidence=.85,
                                source_obs_ids=['observation-1'])) for _ in range(2)]
        (tmp_path/'.memory_meta.json').write_text(json.dumps({'memory': rows, 'user': []}))
        (tmp_path/'KNOWLEDGE.md').write_text(text)
        store = MemoryStore(tmp_path)
        original = next(r for r in store.get_entries_with_meta() if r.status == 'archived')
        return store, original.entry_id, store.list_active()[0]
    store = MemoryStore(tmp_path)
    a = store.add('600183 放量突破后趋势延续需结合成交量确认', source='curator',
                  meta_extra={'source_obs_ids': ['observation-1']})
    store.add('600183 放量突破后趋势延续需要成交量确认', source='curator')
    def model(system, user):
        if '审查记忆修订' in system:
            return '{"valid":true,"reason":"保留成交量条件"}'
        return json.dumps({'content': text, 'reason': '保留标的与成交量条件'})
    assert merge_similar_entries(store, model, force=True) == 1
    return store, a['entry_id'], store.list_active()[0]


@pytest.mark.parametrize('origin', ['merge', 'duplicate'])
def test_real_outcome_feedback_retry_archive_and_revival_keep_lineage(client, monkeypatch, tmp_path, origin):
    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.ops.outcome_feedback import apply_outcome_feedback
    from trade_compass_agent.ops.reflection import PendingReflection

    store, old_id, kept = feedback_history(tmp_path, origin)
    config = AppConfig(memory_dir=tmp_path, data_dir=tmp_path/'data')
    pending = PendingReflection(job_id='close', run_id='r1', run_date='2026-09-15',
                                predictions={'source_obs_ids': ['observation-1']})
    actuals = {'positions': [{'symbol': '600183', 'predicted_pnl_pct': 10,
                              'actual_pnl_pct': -5, 'delta_pnl_pct': -15}]}
    result = apply_outcome_feedback(pending, actuals, '成交量依据失效', store, config)
    assert len(result) == 1 and result[0]['entry_id'] == kept.entry_id
    assert result[0]['confidence'] == pytest.approx(.55)
    first_snapshot = read(client, monkeypatch, tmp_path)
    original = next(r for r in first_snapshot['entries'] if r['entry_id'] == old_id)
    assert original['lineage_status'] == 'complete'
    assert [r['version'] for r in original['successors']] == [1, 2]
    assert original['successors'][-1]['status'] == 'active'
    v1 = next(r for r in MemoryStore(tmp_path).get_entries_with_meta(include_history=True)
              if r.entry_id == kept.entry_id and r.version == 1)
    assert v1.confidence == .85 and not v1.adjustments and v1.disproof_count == 0
    assert v1.successor_version == 2 and v1.review_method == ''
    apply_outcome_feedback(pending, actuals, '重复交付', store, config)
    assert read(client, monkeypatch, tmp_path) == first_snapshot
    pending.run_id = 'r2'
    apply_outcome_feedback(pending, actuals, '再次证伪', store, config)
    archived = read(client, monkeypatch, tmp_path)
    original = next(r for r in archived['entries'] if r['entry_id'] == old_id)
    assert [r['version'] for r in original['successors']] == [1, 2, 3]
    assert original['successors'][-1]['status'] == 'archived'
    assert archived['chars_used'] == 0
    revived = store.add(kept.text, source='curator', meta_extra={'source_obs_ids': ['observation-1']})
    assert revived['entry_id'] == kept.entry_id and revived['version'] == 4
    restored = read(client, monkeypatch, tmp_path)
    original = next(r for r in restored['entries'] if r['entry_id'] == old_id)
    assert [r['version'] for r in original['successors']] == [1, 2, 3, 4]
    assert original['lineage_status'] == 'complete' and original['successors'][-1]['status'] == 'active'
    assert all(r['text'] == kept.text for r in original['successors'])
    assert restored['chars_used'] == len(kept.text)


@pytest.mark.parametrize('operation', ['candidate', 'explicit_archive', 'low_confidence_archive'])
def test_all_status_version_changes_preserve_merged_history(client, monkeypatch, tmp_path, operation):
    store, old_id, kept = feedback_history(tmp_path, 'merge')
    if operation == 'explicit_archive':
        store.archive_entry(entry_id=kept.entry_id, reason='条件已失效')
        versions, status = [1, 2], 'archived'
    else:
        store.adjust_confidence(entry_hash=kept.content_hash, delta=-.6, reason='独立证据不足', run_id='r1')
        versions, status = [1, 2], 'candidate'
        if operation == 'low_confidence_archive':
            assert store.archive_stale() == [kept.text]
            versions, status = [1, 2, 3], 'archived'
            assert store.archive_stale() == []
    payload = read(client, monkeypatch, tmp_path)
    original = next(r for r in payload['entries'] if r['entry_id'] == old_id)
    assert original['lineage_status'] == 'complete'
    assert [r['version'] for r in original['successors']] == versions
    assert original['successors'][-1]['status'] == status and payload['chars_used'] == 0


def test_feedback_failure_keeps_old_version_and_retry_commits_once(client, monkeypatch, tmp_path):
    store, old_id, kept = feedback_history(tmp_path, 'merge')
    before = (tmp_path/'.memory_meta.json').read_bytes()
    write = store._atomic_write
    def fail_ledger(path, text):
        if path == store._meta_file:
            raise OSError('injected failure before ledger commit')
        write(path, text)
    monkeypatch.setattr(store, '_atomic_write', fail_ledger)
    with pytest.raises(OSError):
        store.adjust_confidence(entry_hash=kept.content_hash, delta=.01, reason='核实', run_id='r1')
    assert (tmp_path/'.memory_meta.json').read_bytes() == before
    store = MemoryStore(tmp_path)
    for _ in range(2):
        assert store.adjust_confidence(entry_hash=kept.content_hash, delta=.01, reason='核实', run_id='r1')['ok']
    payload = read(client, monkeypatch, tmp_path)
    original = next(r for r in payload['entries'] if r['entry_id'] == old_id)
    assert [r['version'] for r in original['successors']] == [1, 2]
    assert original['lineage_status'] == 'complete'
    versions = [r for r in MemoryStore(tmp_path).get_entries_with_meta(include_history=True) if r.entry_id == kept.entry_id]
    assert len(versions) == 2 and sum(len(r.adjustments) for r in versions) == 1


def test_feedback_selected_identity_still_checks_content_and_pin(tmp_path):
    store = MemoryStore(tmp_path)
    old = store.add('原有条件必须经过验证', source='curator')
    original_hash = store.list_active()[0].content_hash
    store.replace('原有条件', '修订后的不同条件须有证据', entry_id=old['entry_id'])
    before = (tmp_path/'.memory_meta.json').read_bytes()
    result = store.adjust_confidence(entry_id=old['entry_id'], entry_hash=original_hash,
                                     delta=-.1, reason='针对旧结论的迟到反馈')
    assert result['disposition'] == 'version_conflict'
    pin = store.add('用户固定规则不得修改', source='user_pin')
    assert not store.adjust_confidence(entry_id=pin['entry_id'], delta=-.1, reason='无权修改')['ok']
    # Only the explicit pin addition changed the ledger after the rejected feedback.
    assert json.loads(before)['history'] == json.loads((tmp_path/'.memory_meta.json').read_text())['history']
    assert next(r for r in store.list_active() if r.entry_id == old['entry_id']).confidence == .85


def test_concurrent_feedback_preserves_each_version_and_retry_identity(client, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store, old_id, kept = feedback_history(tmp_path, 'merge')
    stores = [MemoryStore(tmp_path), MemoryStore(tmp_path)]
    def update(index):
        return stores[index].adjust_confidence(entry_id=kept.entry_id, entry_hash=kept.content_hash,
                                              delta=.01, reason='核实', run_id=f'r{index}')
    with ThreadPoolExecutor(2) as pool:
        assert all(r['ok'] for r in pool.map(update, range(2)))
    assert update(0)['changed'] is False
    payload = read(client, monkeypatch, tmp_path)
    original = next(r for r in payload['entries'] if r['entry_id'] == old_id)
    assert original['lineage_status'] == 'complete'
    assert [r['version'] for r in original['successors']] == [1, 2, 3]
    versions = sorted((r for r in store.get_entries_with_meta(include_history=True) if r.entry_id == kept.entry_id),
                      key=lambda r: r.version)
    assert [r.confidence for r in versions] == pytest.approx([.85, .86, .87])
    assert [len(r.adjustments) for r in versions] == [0, 1, 2]
