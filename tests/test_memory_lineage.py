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
