"""A partial review must resume without repeatedly reviewing unchanged drafts."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from trade_compass_agent.memory.memory_store import MemoryStore
from trade_compass_agent.memory.observation_store import ObservationStore
from trade_compass_agent.memory.reassessment import review_candidates
from trade_compass_agent.memory.rules_store import RulesStore


def defer(payload):
    return json.dumps({"reviews": [{"entry_id": m["entry_id"], "version": m["version"],
        "decision": "defer", "basis": "empirical", "refs": [],
        "reason": "缺少独立实战证据，保留候选。"} for m in payload["candidates"]]})


def partial(store, observations=None, after_first=None):
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append([m["entry_id"] for m in payload["candidates"]])
        if len(calls) == 2:
            raise RuntimeError("model unavailable in the next batch")
        if after_first:
            after_first()
        return defer(payload)

    report = review_candidates(store, call, observations=observations)
    assert not report["ok"] and len(report["reviews"]) == 4
    return set(calls[0])


def drafts(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for i in range(16):
        store.add(f"量价背离后应当上涨的待验证经验第{i}条", source="scheduler")
    return store


def resumed_ids(store, observations=None, *, force=False):
    seen = []

    def call(_system, user):
        payload = json.loads(user)
        seen.extend(m["entry_id"] for m in payload["candidates"])
        return defer(payload)

    report = review_candidates(store, call, observations=observations, force=force)
    assert report["ok"]
    return set(seen)


def test_partial_review_restarts_from_unfinished_batch_and_keeps_history(tmp_path):
    store = drafts(tmp_path)
    completed = partial(store)
    restarted = MemoryStore(tmp_path / "memory")
    assert resumed_ids(restarted).isdisjoint(completed)
    rows = restarted.get_entries_with_meta()
    assert len(rows) == 16 and all(m.status == "candidate" and m.version == 2 for m in rows)
    assert len(restarted.get_entries_with_meta(include_history=True)) == 32
    assert resumed_ids(restarted) == set()


@pytest.mark.parametrize("recover", [True, False])
def test_invalid_json_gets_one_bounded_repair_without_losing_candidates(tmp_path, recover):
    store = MemoryStore(tmp_path / "memory")
    store.add("量价背离后的上涨仍待实战验证", source="scheduler")
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        return defer(payload) if recover and len(calls) == 2 else '{"reviews":[{"decision": ]}'

    result = review_candidates(store, call)
    assert result["ok"] is recover and len(calls) == 2
    assert "JSON" in calls[1]["validation_error"]["error"]
    rows = MemoryStore(tmp_path / "memory").get_entries_with_meta()
    assert len(rows) == 1 and rows[0].status == "candidate"
    assert rows[0].version == (2 if recover else 1)


@pytest.mark.parametrize("malformed", ["truncated_id", "duplicate_id", "wrong_version", "unknown_reference"])
def test_repair_only_requests_invalid_candidates_and_keeps_exact_valid_judgments(tmp_path, malformed):
    store = MemoryStore(tmp_path / "memory")
    for index in range(4):
        store.add(f"待独立验证的市场经验{index}", source="scheduler")
    original = store.get_entries_with_meta()
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        if len(calls) == 2:
            assert [m["entry_id"] for m in payload["candidates"]] == [original[-1].entry_id]
            assert payload["validation_error"]["expected_entry_ids"] == [original[-1].entry_id]
            if malformed == "truncated_id":
                assert not payload["previous_reviews"]
                assert original[-1].entry_id[:-2] not in payload["validation_error"]["error"]
            return defer(payload)
        result = json.loads(defer(payload))
        last = result["reviews"][-1]
        if malformed == "truncated_id":
            last["entry_id"] = last["entry_id"][:-2]
        elif malformed == "duplicate_id":
            result["reviews"].append(dict(last))
        elif malformed == "wrong_version":
            last["version"] += 99
        else:
            last["refs"] = ["invented-reference"]
        return json.dumps(result)

    result = review_candidates(store, call)
    assert result["ok"] and len(calls) == 2 and len(result["reviews"]) == 4
    reopened = MemoryStore(tmp_path / "memory")
    assert all(m.status == "candidate" and m.version == 2 for m in reopened.get_entries_with_meta())
    assert resumed_ids(reopened) == set()


def test_scoped_repair_cannot_reassign_a_retained_judgment_to_another_candidate(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    for index in range(2):
        store.add(f"仍待独立验证的经验{index}", source="scheduler")
    original = store.get_entries_with_meta()
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        result = json.loads(defer(payload))
        if len(calls) == 1:
            result["reviews"][-1]["entry_id"] = "invented"
        else:
            result["reviews"][0]["entry_id"] = original[0].entry_id
        return json.dumps(result)

    result = review_candidates(store, call)
    assert not result["ok"] and len(calls) == 2 and not result["commits"]
    assert all(m.version == 1 and m.status == "candidate" for m in MemoryStore(tmp_path / "memory").get_entries_with_meta())


def test_retained_admission_still_gets_independent_review_of_original_candidate(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    principle = "未由工具取得的报价不得编造。"
    RulesStore(tmp_path / "memory").add(principle, actor="user")
    store.add(principle, source="scheduler")
    store.add("未经验证的行情预测", source="scheduler")
    original = store.get_entries_with_meta()
    calls = []

    def call(_system, user):
        payload = json.loads(user)
        calls.append(payload)
        if "proposals" in payload:
            assert {m["entry_id"] for m in payload["candidates"]} == {m.entry_id for m in original}
            assert payload["candidates"][0]["text"] == principle
            return json.dumps({"reviews": [{"entry_id": original[0].entry_id, "valid": False,
                "reason": "独立复核要求保留候选。"}]})
        if len(calls) == 2:
            return defer(payload)
        return json.dumps({"reviews": [
            {"entry_id": original[0].entry_id, "decision": "admit", "content": principle,
             "basis": "principle", "refs": ["rules"], "reason": "与人工规则一致。"},
            {"entry_id": "invented", "decision": "defer", "reason": "证据不足", "refs": []},
        ]})

    result = review_candidates(store, call)
    assert result["ok"] and len(calls) == 3
    assert not MemoryStore(tmp_path / "memory").list_active()
    assert result["reviews"][0]["decision"] == "defer"


def test_smaller_new_batches_reuse_genuinely_committed_legacy_eight(tmp_path):
    import hashlib
    from trade_compass_agent.memory.reassessment import _context, _review_batch_fingerprint

    store = drafts(tmp_path)
    candidates, _, _, _ = _context(store, None)
    completed = {m.entry_id for m in candidates[:8]}
    for entry in candidates[:8]:
        assert store.record_candidate_review(entry_id=entry.entry_id, expected_version=entry.version,
            decision="defer", reason="旧版八条批次的真实完整判断", evidence=[])["ok"]
    batch, core, references, _ = _context(store, None, candidate_ids=completed)
    key = hashlib.sha256("\n".join(sorted(completed)).encode()).hexdigest()
    store.maintenance_marker("candidate_reviewed_batches", {key: _review_batch_fingerprint(batch, core, references)})
    calls = []

    def call(system, user):
        payload = json.loads(user)
        calls.append([m["entry_id"] for m in payload["candidates"]])
        return defer(payload)

    report = review_candidates(MemoryStore(tmp_path / "memory"), call)
    assert report["ok"]
    assert [len(ids) for ids in calls] == [4, 4]
    assert completed.isdisjoint({entry_id for ids in calls for entry_id in ids})
    assert all(m.version == 2 for m in MemoryStore(tmp_path / "memory").get_entries_with_meta())


def test_earlier_adoption_reopens_later_legacy_batch_with_changed_core(tmp_path):
    import hashlib
    from trade_compass_agent.memory.reassessment import _context, _review_batch_fingerprint

    store = MemoryStore(tmp_path / "memory")
    principle = "未由工具取得的报价不得编造。"
    RulesStore(tmp_path / "memory").add(principle, actor="user")
    store.add(principle, source="scheduler")
    for index in range(15):
        store.add(f"低估值标的次日必涨的待验证经验{index}", source="scheduler")
    candidates, _, _, _ = _context(store, None)
    cached = {m.entry_id for m in candidates[8:]}
    for entry in candidates[8:]:
        assert store.record_candidate_review(entry_id=entry.entry_id, expected_version=entry.version,
            decision="defer", reason="旧批次完成，等待独立证据", evidence=[])["ok"]
    batch, core, refs, _ = _context(store, None, candidate_ids=cached)
    key = hashlib.sha256("\n".join(sorted(cached)).encode()).hexdigest()
    store.maintenance_marker("candidate_reviewed_batches", {key: _review_batch_fingerprint(batch, core, refs)})
    seen = set()

    def call(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews": [{"entry_id": p["entry_id"], "valid": True,
                "reason": "与人工规则直接一致。"} for p in payload["proposals"]]})
        seen.update(m["entry_id"] for m in payload["candidates"])
        return json.dumps({"reviews": [{"entry_id": m["entry_id"],
            "decision": "admit" if m["text"] == principle else "defer",
            "basis": "principle" if m["text"] == principle else "empirical",
            "content": m["text"], "refs": ["rules"] if m["text"] == principle else [],
            "reason": "遵守明确人工规则；无实证的预测继续保留。"} for m in payload["candidates"]]})

    assert review_candidates(store, call)["ok"]
    assert cached <= seen
    assert all(m.version == 3 for m in store.get_entries_with_meta() if m.entry_id in cached)
    assert any(m.text == principle and m.status == "active" for m in store.get_entries_with_meta())


def test_late_adoption_does_not_mark_earlier_old_core_judgments_current(tmp_path):
    import hashlib
    from trade_compass_agent.memory.reassessment import _context, _review_batch_fingerprint

    store = MemoryStore(tmp_path / "memory")
    principle = "未由工具取得的报价不得编造。"
    RulesStore(tmp_path / "memory").add(principle, actor="user")
    for index in range(15):
        store.add(f"低估值标的次日必涨的待验证经验{index}", source="scheduler")
    store.add(principle, source="scheduler")
    candidates, _, _, _ = _context(store, None)
    cached = {m.entry_id for m in candidates[:8]}
    for entry in candidates[:8]:
        assert store.record_candidate_review(entry_id=entry.entry_id, expected_version=entry.version,
            decision="defer", reason="旧批次完整判断", evidence=[])["ok"]
    batch, core, refs, _ = _context(store, None, candidate_ids=cached)
    key = hashlib.sha256("\n".join(sorted(cached)).encode()).hexdigest()
    store.maintenance_marker("candidate_reviewed_batches", {key: _review_batch_fingerprint(batch, core, refs)})

    def call(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews": [{"entry_id": p["entry_id"], "valid": True,
                "reason": "与人工规则一致。"} for p in payload["proposals"]]})
        return json.dumps({"reviews": [{"entry_id": m["entry_id"],
            "decision": "admit" if m["text"] == principle else "defer",
            "basis": "principle" if m["text"] == principle else "empirical",
            "content": m["text"], "refs": ["rules"] if m["text"] == principle else [],
            "reason": "明确人工规则可采纳；无实证预测继续保留。"} for m in payload["candidates"]]})

    assert review_candidates(store, call)["ok"]
    assert not store.maintenance_marker("candidate_reviewed_fingerprint")
    assert cached <= resumed_ids(store)
    assert resumed_ids(store) == set()


def test_admitted_entries_do_not_shift_valid_completed_groups_into_repeated_reviews(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    principles = ["未由工具取得的报价不得编造。", "未确认收盘的快照不得冒充收盘价。"]
    for principle in principles:
        RulesStore(tmp_path / "memory").add(principle, actor="user")
        store.add(principle, source="scheduler")
    for index in range(8):
        store.add(f"低估值标的次日必涨的待验证经验{index}", source="scheduler")
    original = store.get_entries_with_meta()

    def call(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews": [{"entry_id": p["entry_id"], "valid": True,
                "reason": "与明确人工规则一致且保留原义。"} for p in payload["proposals"]]})
        return json.dumps({"reviews": [{"entry_id": m["entry_id"],
            "decision": "admit" if m["text"] in principles else "defer",
            "basis": "principle" if m["text"] in principles else "empirical",
            "content": m["text"], "refs": ["rules"] if m["text"] in principles else [],
            "reason": "明确规则可采纳，无实证预测保留候选。"} for m in payload["candidates"]]})

    assert review_candidates(store, call)["ok"]
    # The first two remaining candidates were reviewed before adoption changed
    # the core; the later groups were actually completed with the new core.
    expected = {entry.entry_id for entry in original[2:4]}
    assert resumed_ids(MemoryStore(tmp_path / "memory")) == expected
    assert all(row.version == 2 for row in store.get_entries_with_meta() if row.entry_id in {entry.entry_id for entry in original[4:]})


@pytest.mark.parametrize("change", ["rules", "pinned", "independent_evidence", "policy", "force"])
def test_material_context_changes_or_force_reopen_completed_batch(tmp_path, monkeypatch, change):
    store = drafts(tmp_path)
    observations = ObservationStore(tmp_path / "observations.db")
    completed = partial(store, observations)
    if change == "rules":
        RulesStore(tmp_path / "memory").add("账户不得使用杠杆", actor="user")
    elif change == "pinned":
        store.add("用户固定的交易原则", source="user_pin")
    elif change == "independent_evidence":
        observations.append("market-case", "get_bars", "量价背离后应当上涨的待验证经验")
    elif change == "policy":
        monkeypatch.setattr("trade_compass_agent.memory.reassessment.REVIEW_PROMPT", "updated review policy")
    assert completed <= resumed_ids(store, observations, force=change == "force")


def test_evidence_arriving_during_review_is_not_marked_as_already_seen(tmp_path):
    store = drafts(tmp_path)
    observations = ObservationStore(tmp_path / "observations.db")
    completed = partial(store, observations, after_first=lambda: observations.append(
        "new-market-case", "get_bars", "量价背离后应当上涨的待验证经验"))
    assert completed <= resumed_ids(store, observations)


@pytest.mark.parametrize("tool", ["load_skill", "get_bars"])
def test_recall_dedup_keeps_independent_market_cases_distinct(tmp_path, monkeypatch, tool):
    store = drafts(tmp_path)
    observations = ObservationStore(tmp_path / "observations.db")
    clock = [datetime.now(timezone.utc) - timedelta(minutes=10)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr("trade_compass_agent.memory.observation_store.datetime", Clock)
    text = "量价背离后应当上涨的待验证经验"
    observations.append("recall-1", tool, text, raw_preview="unchanged data")
    completed = partial(store, observations)
    clock[0] += timedelta(minutes=10)
    observations.append("recall-2", tool, text, raw_preview="unchanged data")
    seen = resumed_ids(store, observations)
    assert seen.isdisjoint(completed) if tool == "load_skill" else completed <= seen
    observations.append("recall-3", tool, text + " changed", raw_preview="changed data")
    assert completed <= resumed_ids(store, observations)


@pytest.mark.parametrize("tool", ["load_skill", "get_bars"])
def test_repeated_recalls_cannot_crowd_other_evidence_out_of_top_six(tmp_path, monkeypatch, tool):
    store = drafts(tmp_path)
    observations = ObservationStore(tmp_path / "observations.db")
    clock = [datetime.now(timezone.utc) - timedelta(hours=4)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr("trade_compass_agent.memory.observation_store.datetime", Clock)
    text = "量价背离后应当上涨的待验证经验"
    observations.append("original", tool, text, raw_preview="unchanged data")
    observations.append("other-source", "load_skill", text + "但存在独立限制条件", raw_preview="other data")
    completed = partial(store, observations)
    for index in range(8):
        clock[0] += timedelta(minutes=10)
        observations.append(f"repeat-{index}", tool, text, raw_preview="unchanged data")
    seen = resumed_ids(store, observations)
    assert seen.isdisjoint(completed) if tool == "load_skill" else completed <= seen
    assert observations.count() == 10


def test_model_can_judge_mixed_snapshot_versions_without_echoing_bookkeeping(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    RulesStore(tmp_path / "memory").add("未由工具取得的报价不得编造。", actor="user")
    store.add("未由工具取得的报价不得编造。", source="scheduler")
    store.add("任意低估值标的次日必涨。", source="scheduler")
    target = next(m for m in store.get_entries_with_meta() if "必涨" in m.text)
    for index in range(2):
        result = store.record_candidate_review(entry_id=target.entry_id, expected_version=target.version,
            decision="defer", reason=f"缺少实证 {index}", evidence=[])
        assert result["ok"]
        target = next(m for m in store.get_entries_with_meta() if m.entry_id == target.entry_id)
    before = {m.entry_id:m for m in store.get_entries_with_meta()}

    def call(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews":[{"entry_id":v["entry_id"], "valid":True,
                "reason":"与给定人工规则一致，未扩大条件。"} for v in payload["proposals"]]})
        return json.dumps({"reviews":[{"entry_id":m["entry_id"],
            "decision":"admit" if "报价" in m["text"] else "defer", "basis":"principle",
            "content":m["text"], "refs":["rules"] if "报价" in m["text"] else [],
            "reason":"对应人工规则；无实证的预测继续保留候选。"} for m in payload["candidates"]]})

    report = review_candidates(store, call)
    assert report["ok"], report
    after = {m.entry_id:m for m in store.get_entries_with_meta()}
    assert all(after[key].version == row.version+1 for key,row in before.items())
    assert next(m for m in after.values() if "报价" in m.text).status == "active"
    assert after[target.entry_id].status == "candidate"


def test_omitted_model_version_does_not_allow_a_stale_snapshot_commit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.add("低估值标的次日必涨。", source="scheduler")
    original = store.get_entries_with_meta()[0]

    def call(system, user):
        payload = json.loads(user)
        if store.get_entries_with_meta()[0].version == original.version:
            assert store.record_candidate_review(entry_id=original.entry_id, expected_version=original.version,
                decision="defer", reason="并发产生的审查结论", evidence=[])["ok"]
        return json.dumps({"reviews":[{"entry_id":m["entry_id"],"decision":"defer",
            "basis":"empirical","refs":[],"reason":"旧快照的判断"} for m in payload["candidates"]]})

    report = review_candidates(store, call)
    assert not report["ok"] and "changed during review" in report["error"]
    current = store.get_entries_with_meta()[0]
    assert current.version == original.version+1 and current.reason == "并发产生的审查结论"


def test_explicitly_wrong_model_version_still_rejects_the_whole_batch(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.add("低估值标的次日必涨。", source="scheduler")
    original = store.get_entries_with_meta()[0]
    calls = []
    def call(system, user):
        calls.append(user)
        return json.dumps({"reviews":[{"entry_id":original.entry_id,"version":999,
            "decision":"defer","basis":"empirical","refs":[],"reason":"版本错误的判断"}]})
    report = review_candidates(store, call)
    assert not report["ok"] and len(calls) == 2
    assert store.get_entries_with_meta()[0].version == original.version


def test_independent_review_cannot_override_the_captured_version(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    RulesStore(tmp_path / "memory").add("未由工具取得的报价不得编造。", actor="user")
    store.add("未由工具取得的报价不得编造。", source="scheduler")
    original = store.get_entries_with_meta()[0]
    def call(system, user):
        payload = json.loads(user)
        if "proposals" in payload:
            return json.dumps({"reviews":[{"entry_id":original.entry_id,"version":999,
                "valid":True,"reason":"内容正确但版本号错误"}]})
        return json.dumps({"reviews":[{"entry_id":original.entry_id,"decision":"admit",
            "basis":"principle","content":original.text,"refs":["rules"],"reason":"与人工规则一致。"}]})
    report = review_candidates(store, call)
    assert not report["ok"] and "matching snapshot" in report["error"]
    assert store.get_entries_with_meta()[0].version == original.version
    assert store.get_entries_with_meta()[0].status == "candidate"
