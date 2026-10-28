from collections import Counter
from types import SimpleNamespace
import json

from trade_compass_agent.memory.observation_store import extract_concepts
from trade_compass_agent.memory.patterns import _build_daily_summaries


def test_timestamp_fractions_and_decimal_parts_are_not_stock_codes():
    text = '取数18:41:47.623115，价格9.029888；capital=300000.0；000001（代码）与 601928。'
    concepts = extract_concepts(text)
    assert not {"623115", "029888", "300000"}.intersection(concepts)
    assert "000001" in concepts and "601928" in concepts
    assert "000001" in extract_concepts("000001.")


def test_legacy_false_concepts_are_filtered_without_rewriting_observations():
    obs = SimpleNamespace(id="legacy", created_at="2026-10-21T10:41:47+00:00", tool_name="get_bars",
        summary="取数18:41:47.623115，价格9.029888；601928。", raw_preview="", importance=5,
        concepts=["623115", "029888", "601928"])
    daily, frequency = _build_daily_summaries(SimpleNamespace(recent_by_days=lambda _: [obs]), 3)
    assert daily["2026-10-21"][0]["concepts"] == ["601928"]
    assert frequency == Counter({"601928": 1})
    assert obs.concepts == ["623115", "029888", "601928"]


def test_real_code_remains_when_it_also_appears_in_a_decimal():
    from trade_compass_agent.memory.observation_store import validated_concepts
    obs = SimpleNamespace(summary="601928；价格9.601928；跨会话概念", raw_preview="", concepts=["601928", "跨会话概念"])
    assert validated_concepts(obs) == obs.concepts


def test_hotness_evidence_comes_from_the_actual_counted_window():
    from trade_compass_agent.memory.insights import _detect_hotness_spikes
    current = [SimpleNamespace(id=f"oct-{i}", concepts=["龙头"], summary="龙头", raw_preview="") for i in range(3)]

    class Store:
        def recent_by_days(self, days):
            return current

        def search(self, *args, **kwargs):
            raise AssertionError("Unbounded FTS hits cannot support a dated frequency claim")

    insights = _detect_hotness_spikes(Store())
    assert len(insights) == 1
    assert set(insights[0].evidence) == {"oct-0", "oct-1", "oct-2"}
    assert "含近3天" in insights[0].body
    assert "不代表" in insights[0].body


def test_pattern_cannot_reintroduce_a_decimal_fragment_as_an_extra_symbol():
    from trade_compass_agent.memory.patterns import _parse_llm_patterns
    daily = {day: [{"id": day, "concepts": ["601928"]}] for day in ["2026-10-20", "2026-10-21"]}
    base = {"theme": "持仓观察", "evidence_days": list(daily), "concepts": ["601928", "623115"]}
    assert not _parse_llm_patterns(json.dumps([base]), daily)
    assert _parse_llm_patterns(json.dumps([{**base, "concepts": ["601928"]}]), daily)
