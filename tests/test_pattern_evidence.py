import json
from collections import Counter
from types import SimpleNamespace

import pytest

from trade_compass_agent.memory.patterns import _discover_via_llm, _parse_llm_patterns


def test_verified_identity_uses_tool_rows_even_when_later_preview_is_truncated():
    from trade_compass_agent.memory.patterns import _observation_identities

    raw = '{"positions":[{"symbol":"601928","name":"凤凰传媒","price_is_fresh":true}],"account_summaries":['
    observation = SimpleNamespace(id="quote", tool_name="analyze_portfolio", raw_preview=raw,
                                  created_at="2026-10-20T20:00:00+00:00")
    assert _observation_identities(observation)["601928"]["name"] == "凤凰传媒"
    observation.tool_name = "search_memory"
    assert _observation_identities(observation) == {}
    observation.tool_name = "analyze_portfolio"
    observation.raw_preview = raw.replace('true', 'false')
    assert _observation_identities(observation) == {}
    observation.raw_preview = raw[:30]
    assert _observation_identities(observation) == {}
    observation.tool_name = "sina_realtime_quote"
    observation.raw_preview = '{"quotes":[{"symbol":"601928","name":"凤凰传媒"}]}'
    assert _observation_identities(observation)["601928"]["name"] == "凤凰传媒"


@pytest.mark.parametrize("reverse", [False, True])
def test_discovery_receives_verified_identity_and_separates_attention_from_effect(reverse):
    captured = []
    daily = {"2026-10-19": [{"id": "old", "summary": "601928新华传媒检索", "tool": "search_memory",
                             "concepts": ["601928", "新华传媒"], "identities": {"601928": {
                                 "name": "旧名称", "observation_id": "old-quote", "tool": "sina_realtime_quote",
                                 "created_at": "2026-10-19T20:00:00+00:00"}}}],
             "2026-10-20": [{"id": "actual", "summary": "持仓601928", "tool": "analyze_portfolio",
                             "concepts": ["601928"], "identities": {"601928": {
                                 "name": "凤凰传媒", "observation_id": "actual", "tool": "analyze_portfolio",
                                 "created_at": "2026-10-20T20:00:00+00:00"}}}]}
    if reverse:
        daily = dict(reversed(list(daily.items())))
    _discover_via_llm(daily, Counter({"601928": 2}), [],
                      lambda system, user: captured.append(user) or "[]", 3)
    assert '"name": "凤凰传媒"' in captured[0]
    verified = json.loads(captured[0].split("未核对的名称只写代码）\n", 1)[1].split("\n\n## 任务", 1)[0])
    assert verified["601928"]["observation_id"] == "actual"
    assert "检索频率不能证明" in captured[0]
    assert "未核对的名称只写代码" in captured[0]


@pytest.mark.parametrize("days", [["2026-10-19", "2099-01-01"],
                                 ["2026-10-19", "2026-10-19"], ["2026-10-19"]])
def test_pattern_cannot_invent_or_duplicate_days(days):
    daily = {"2026-10-19": [{"id": "one", "concepts": ["601928"]}],
             "2026-10-20": [{"id": "two", "concepts": ["601928"]}]}
    raw = json.dumps([{"theme": "关注", "concepts": ["601928"], "evidence_days": days}])
    assert _parse_llm_patterns(raw, daily) == []


def test_claimed_day_must_have_matching_observation():
    daily = {"2026-10-19": [{"id": "one", "concepts": ["601928"]}],
             "2026-10-20": [{"id": "other", "concepts": ["000001"]}]}
    raw = json.dumps([{"theme": "关注", "concepts": ["601928"],
                       "evidence_days": ["2026-10-19", "2026-10-20"]}])
    assert _parse_llm_patterns(raw, daily) == []


def test_one_shanghai_day_across_utc_midnight_is_not_two_evidence_days():
    from trade_compass_agent.memory.patterns import _build_daily_summaries

    observations = [SimpleNamespace(id=str(i), tool_name="get_bars", raw_preview="", summary="601928研究",
        concepts=["601928"], importance=5, created_at=stamp) for i, stamp in enumerate([
            "2026-10-20T16:30:00+00:00", "2026-10-21T01:00:00+00:00"])]
    store = SimpleNamespace(recent_by_days=lambda _: observations)
    daily, _ = _build_daily_summaries(store, 3)
    assert list(daily) == ["2026-10-21"]
    assert len(daily["2026-10-21"]) == 2
    raw = json.dumps([{"theme":"研究", "concepts":["601928"],
                       "evidence_days":["2026-10-20","2026-10-21"]}])
    assert _parse_llm_patterns(raw, daily) == []


def test_daily_grouping_uses_scheduler_timezone_and_handles_legacy_naive_time():
    from trade_compass_agent.memory.patterns import _build_daily_summaries

    observations = [SimpleNamespace(id=str(i), tool_name="get_bars", raw_preview="", summary="观察",
        concepts=["601928"], importance=5, created_at=stamp) for i, stamp in enumerate([
            "2026-10-20T23:00:00+00:00", "2026-10-21T09:00:00"])]
    store = SimpleNamespace(recent_by_days=lambda _: observations)
    local, _ = _build_daily_summaries(store, 3)
    assert list(local) == ["2026-10-21"]
    utc, _ = _build_daily_summaries(store, 3, timezone_name="UTC")
    assert set(utc) == {"2026-10-20", "2026-10-21"}


@pytest.mark.parametrize("keep_attention", [False, True])
def test_independent_review_rejects_recalled_market_numbers_but_keeps_research_attention(keep_attention):
    daily = {day: [{"id": identity, "tool": "load_skill", "summary": "涨停家数25是流程示例",
                   "raw_preview": "示例并非当日行情", "concepts": ["涨停池"]}]
             for day, identity in [("2026-10-19", "one"), ("2026-10-20", "two")]}
    calls = []

    def llm(system, user):
        calls.append((system, user))
        if len(calls) == 1:
            return json.dumps([{"theme": "涨停池关注", "description": "持续研究涨停池" if keep_attention else "涨停家数连续两日维持25家",
                "concepts": ["涨停池"], "significance": "研究关注" if keep_attention else "市场情绪高位",
                "evidence_days": list(daily)}])
        payload = json.loads(user)
        assert payload["observations"]["one"]["tool"] == "load_skill"
        assert payload["observations"]["one"]["day"] == "2026-10-19"
        assert "示例并非当日行情" in payload["observations"]["two"]["raw_preview"]
        return json.dumps({"reviews": [{"pattern_id": payload["patterns"][0]["id"], "valid": keep_attention,
                                        "reason": "只能证明研究关注", "evidence": ["one", "two"]}]})

    patterns = _discover_via_llm(daily, Counter({"涨停池": 2}), [], llm, 3)
    assert len(calls) == 2
    assert len(patterns) == int(keep_attention)
    if patterns:
        assert patterns[0].evidence == ["one", "two"]


@pytest.mark.parametrize("refs", [["one", "invented"], ["one"], ["one", "one"]])
def test_pattern_review_cannot_invent_evidence_or_lose_a_claimed_day(refs):
    daily = {"2026-10-19": [{"id": "one", "tool": "search_memory", "summary": "关注涨停池", "concepts": ["涨停池"]}],
             "2026-10-20": [{"id": "two", "tool": "search_memory", "summary": "关注涨停池", "concepts": ["涨停池"]}]}
    calls = []

    def llm(system, user):
        calls.append(user)
        if len(calls) == 1:
            return json.dumps([{"theme": "研究关注", "concepts": ["涨停池"], "evidence_days": list(daily)}])
        pid = json.loads(user)["patterns"][0]["id"]
        return json.dumps({"reviews": [{"pattern_id": pid, "valid": True, "reason": "研究关注", "evidence": refs}]})

    assert _discover_via_llm(daily, Counter({"涨停池": 2}), [], llm, 3) == []


def test_pattern_evidence_limit_keeps_every_claimed_day():
    daily = {day: [{"id": f"{day}-{i}", "concepts": ["涨停池"]} for i in range(60)]
             for day in ["2026-10-19", "2026-10-20", "2026-10-21"]}
    patterns = _parse_llm_patterns(json.dumps([{"theme": "研究关注", "concepts": ["涨停池"], "evidence_days": list(daily)}]), daily)
    assert {identity[:10] for identity in patterns[0].evidence} == set(daily)


def test_fallback_describes_research_records_instead_of_market_co_movement():
    from trade_compass_agent.memory.patterns import _discover_deterministic

    daily = {day: [{"id": day, "concepts": ["涨停池", "突破"]}]
             for day in ["2026-10-19", "2026-10-20"]}
    patterns = _discover_deterministic(daily, Counter({"涨停池": 2, "突破": 2}))
    assert "观察或检索记录" in patterns[0].description and "不能据此判断行情共振" in patterns[0].description


@pytest.mark.parametrize("matching", [True, False])
def test_review_can_share_seen_citations_only_for_matching_concepts_and_dates(matching):
    from trade_compass_agent.memory.patterns import TradingPattern, _verify_patterns

    patterns = [TradingPattern("attention", "研究关注", "关注涨停池", ["涨停池"], 2, 2, .5, "仅研究关注",
                              "2026-10-19", "2026-10-20", evidence=["one", "two"]),
                TradingPattern("other", "其它关注", "其它研究", ["涨停池" if matching else "均线"], 2, 2, .5, "仅研究关注",
                              "2026-10-19", "2026-10-20", evidence=["other-one", "other-two"])]
    daily = {day: [{"id": oid, "concepts": concepts} for oid, concepts in rows]
             for day, rows in [("2026-10-19", [("one", ["涨停池"]), ("other-one", patterns[1].concepts)]),
                               ("2026-10-20", [("two", ["涨停池"]), ("other-two", patterns[1].concepts)])]}
    response = json.dumps({"reviews": [{"pattern_id": "attention", "valid": True, "reason": "所给研究观察有依据",
                                        "evidence": ["other-one", "other-two"]},
                                       {"pattern_id": "other", "valid": False, "reason": "无依据", "evidence": []}]})
    accepted = _verify_patterns(patterns, daily, lambda *_: response)
    assert len(accepted) == int(matching)


@pytest.mark.parametrize("tools", [("search_memory", "search_memory"), ("search_stock_news", "emit_signal"),
                                  ("get_market_pulse", "search_memory")])
def test_every_claimed_market_day_needs_a_direct_observation_even_if_judge_agrees(tools):
    from trade_compass_agent.memory.patterns import TradingPattern, _verify_patterns

    pattern = TradingPattern("stable-id", "市场涨停扩容", "涨停家数连续维持25至32只", ["涨停池"],
                             2, 2, .8, "市场风险偏好已改善", "2026-10-19", "2026-10-20", evidence=["one", "two"])
    daily = {day: [{"id": oid, "tool": tool, "concepts": ["涨停池"]}]
             for (day, oid), tool in zip([("2026-10-19", "one"), ("2026-10-20", "two")], tools)}
    raw = json.dumps({"reviews": [{"pattern_id": "stable-id", "valid": True, "reason": "同意", "evidence": ["one", "two"]}]})
    result = _verify_patterns([pattern], daily, lambda *_: raw)[0]
    assert result.id == "stable-id" and result.evidence == ["one", "two"]
    assert result.theme.startswith("研究关注")
    assert "25" not in result.description and "32" not in result.description
    assert "不能据此确认当日行情" in result.description


def test_direct_market_observations_on_every_day_preserve_verified_market_pattern():
    from trade_compass_agent.memory.patterns import TradingPattern, _verify_patterns

    pattern = TradingPattern("market", "涨停池变化", "涨停池从25只到32只", ["涨停池"],
                             2, 2, .8, "持续观察", "2026-10-19", "2026-10-20", evidence=["one", "two"])
    daily = {day: [{"id": oid, "tool": "get_market_pulse", "concepts": ["涨停池"],
                    "raw_preview": json.dumps({"date": day, "limit_up": {"count": count}})}]
             for day, oid, count in [("2026-10-19", "one", 25), ("2026-10-20", "two", 32)]}
    raw = json.dumps({"reviews": [{"pattern_id": "market", "valid": True, "reason": "逐日读数匹配",
                                   "evidence": ["one", "two"]}]})
    result = _verify_patterns([pattern], daily, lambda *_: raw)[0]
    assert result.theme == pattern.theme and result.description == pattern.description
