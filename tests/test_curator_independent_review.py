import json

import pytest

from trade_compass_agent.memory.contradiction import scan_active_conflicts, apply_conflict_reports
from trade_compass_agent.memory.memory_store import MemoryStore


def setup_case(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.add("海外指数仅作风险警示；内需驱动且趋势未坏时不机械减仓。", source="curator")
    proposal = [{"verdict": "SUPERSEDE", "entry_prefix": "海外指数仅作风险警示", "conflicts_with": "海外指数仅作风险警示",
                 "reason": "未重复行情须来自工具的全局前置条件", "refined": "补上所有工具步骤后执行风险警示，内需驱动且趋势未坏时不机械减仓。"}]
    return store, entry, proposal


def test_missing_global_rule_repetition_does_not_mutate_core_memory(tmp_path):
    store, entry, proposal = setup_case(tmp_path)
    calls = []

    def judge(system, prompt):
        calls.append((system, prompt))
        if len(calls) == 1:
            return json.dumps(proposal)
        data = json.loads(prompt)
        assert "全局前置条件" in data["proposals"][0]["reason"]
        return json.dumps({"reviews": [{"entry_id": entry["entry_id"], "valid": False,
            "reason": "全局数据约束自动适用，原文未否定它；缺少复述不是实质冲突。"}]})

    reports = scan_active_conflicts(store.list_active(), "行情必须来自工具", "", judge)
    assert not reports and not apply_conflict_reports(reports, store)
    assert store.list_active()[0].version == entry["version"]
    assert len(calls) == 2 and "未重复" in calls[0][1]


@pytest.mark.parametrize("response", ["{}", '{"reviews":[]}', '{"reviews":[{"entry_id":"wrong","valid":true,"reason":"ok"}]}', "timeout"])
def test_unverified_core_changes_are_not_applied(tmp_path, response):
    store, entry, proposal = setup_case(tmp_path)
    count = 0

    def judge(system, prompt):
        nonlocal count
        count += 1
        if count == 1:
            return json.dumps(proposal)
        if response == "timeout":
            raise TimeoutError("independent review timed out")
        return response

    with pytest.raises((ValueError, TimeoutError)):
        scan_active_conflicts(store.list_active(), "行情必须来自工具", "", judge)
    assert store.list_active()[0].version == entry["version"]


def test_independently_verified_actual_contradiction_can_still_be_repaired(tmp_path):
    store = MemoryStore(tmp_path)
    entry = store.add("100股持仓止盈可卖出33股；未破趋势时持有。", source="curator")
    count = 0

    def judge(system, prompt):
        nonlocal count
        count += 1
        if count == 1:
            return json.dumps([{"verdict": "SUPERSEDE", "entry_prefix": "100股持仓止盈", "conflicts_with": "100股持仓止盈",
                "refined": "100股持仓不能卖出33股；未破趋势时持有。", "reason": "33股不满足最小手数且已有不可执行数量。"}])
        return json.dumps({"reviews": [{"entry_id": entry["entry_id"], "valid": True,
            "reason": "确有33股不可执行的矛盾，修订保留未破趋势时持有的例外。"}]})

    reports = scan_active_conflicts(store.list_active(), "卖出须满足最小手数100股", "", judge)
    assert len(apply_conflict_reports(reports, store)) == 1
    assert store.list_active()[0].text == "100股持仓不能卖出33股；未破趋势时持有。"
