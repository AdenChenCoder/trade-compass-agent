"""Reports bind source fields; wording is not a validation input."""
import json
from dataclasses import replace

import pytest

from trade_compass_agent.runtime.report_contract import (
    Calculation, EvidenceCatalog, ReportDocument, calculate, validate_document,
)


def catalog(*receipts, scope="run-a"):
    return EvidenceCatalog.build([(name, json.dumps(value)) for name, value in receipts], run_scope=scope)


def ref(evidence, metric, *, value=None, subject=None):
    return next(f.id for f in evidence.facts.values() if f.metric == metric
                and (value is None or f.value == str(value)) and (subject is None or subject in f.subject))


def prices():
    return catalog(("get_bars", {"symbol": "002415", "name": "海康威视", "timeframe": "1d", "bars": [
        {"timestamp": "2026-11-09", "close": 6.60}, {"timestamp": "2026-11-10", "close": 7.46}]}))


def test_runtime_owns_identity_signed_values_and_arithmetic():
    evidence = prices()
    item = Calculation(id="c", section="market", type="calculation", operation="change_pct",
                       refs=[ref(evidence, "close", value=6.6), ref(evidence, "close", value=7.46)])
    text, trace = calculate(item, evidence)
    assert "13.03%" in text and "海康威视" in text
    assert trace["result"].startswith("13.0303")
    # There is no field in which a model can supply a different answer/name.
    with pytest.raises(ValueError):
        Calculation.model_validate({**item.model_dump(), "result": 6.60})


@pytest.mark.parametrize("mutation", ["other_run", "forged_pointer", "other_symbol", "other_unit", "other_period", "duplicate"])
def test_incompatible_or_forged_calculation_never_publishes(mutation):
    evidence = prices()
    first, second = ref(evidence, "close", value=6.6), ref(evidence, "close", value=7.46)
    if mutation == "other_run":
        first = ref(prices(), "close", value=6.6).replace(first.split("/")[0], "outside")
    elif mutation == "forged_pointer":
        first += "/made_up"
    elif mutation == "duplicate":
        second = first
    else:
        fact = evidence.facts[second]
        changed = {"other_symbol": {"entity": "300406"}, "other_unit": {"unit": "点"},
                   "other_period": {"timeframe": "1h"}}[mutation]
        evidence.facts[second] = replace(fact, **changed)
    with pytest.raises(ValueError):
        calculate(Calculation(id="x", section="market", type="calculation", operation="change_pct", refs=[first, second]), evidence)


def test_conditions_do_not_assert_current_price_relationships():
    evidence = catalog(("compute_ma", {"symbol": "002415", "price": 35.17, "ma": {"MA5": 34.944}}))
    document = ReportDocument.model_validate({"schema_version": 1, "items": [{
        "id": "plan", "type": "condition", "section": "plan", "ref": ref(evidence, "MA5"),
        "comparison": "lt", "threshold": "34.94", "unit": "元/股", "action": "review"}]})
    findings, rendered, _ = validate_document(document, evidence)
    assert not findings and "条件计划" in rendered["plan"] and "若002415" in rendered["plan"]


def test_flow_identity_values_and_classification_are_not_prose_matches():
    evidence = catalog(("get_fund_flow", {"classification": "eastmoney", "industry_flow": [
        {"name": "金融行业", "net_inflow_yi": -15.03}], "concept_flow": [
        {"name": "业绩预升", "net_inflow_yi": 165.28}]}))
    facts = [fact.display() for fact in evidence.facts.values() if fact.metric == "net_inflow_yi"]
    assert any("金融行业" in f and "-15.03亿元" in f for f in facts)
    assert any("业绩预升" in f and "165.28亿元" in f for f in facts)


def test_fetch_time_sample_query_and_invalid_prices_are_not_evidence():
    evidence = catalog(
        ("get_market_pulse", {"timestamp": "2026-11-11T15:00:00", "_meta": {"retrieved_at": "2026-11-11"},
            "sectors": [{"name": "金融", "change_pct": 3}],
            "indices": [{"symbol": "sh000001", "price": 4000, "date": "2026-11-10", "time": "15:00:00"}]}),
        ("sina_realtime_quote", {"quotes": [{"symbol": "000001", "price": 0}]}),
        ("get_bars", {"symbol": "000002", "_meta": {"is_sample": True}, "bars": [{"close": 42}]}),
        ("web_search", {"query": {"content": "not evidence"}, "results": [{"content": "source text", "url": "https://example.com"}]}),
    )
    sector = evidence.facts[ref(evidence, "change_pct")]
    assert sector.observed_at is None
    index = evidence.facts[ref(evidence, "price")]
    assert index.unit == "点" and index.observed_at == "2026-11-10T07:00:00+00:00"
    assert not any(f.value in {"42", "0", "not evidence"} for f in evidence.facts.values())
    assert "https://example.com" in evidence.facts[ref(evidence, "content")].display()


def test_unknown_volume_unit_cannot_be_used_in_calculations():
    evidence = catalog(("get_bars", {"symbol": "000001", "bars": [
        {"timestamp": "2026-11-09", "volume": 10}, {"timestamp": "2026-11-10", "volume": 20}]}))
    volumes = [f for f in evidence.facts.values() if f.metric == "volume"]
    assert all(f.unit == "单位未提供" for f in volumes)
    with pytest.raises(ValueError, match="单位"):
        calculate(Calculation(id="v", section="market", type="calculation", operation="difference",
                              refs=[f.id for f in volumes]), evidence)


def test_same_instant_timezones_and_earlier_later_prices():
    evidence = catalog(("get_bars", {"symbol": "000001", "bars": [
        {"timestamp": "2026-11-10T15:00:00+08:00", "close": 10},
        {"timestamp": "2026-11-10T07:00:00Z", "close": 11}]}))
    closing = [f for f in evidence.facts.values() if f.metric == "close"]
    assert len({f.observed_at for f in closing}) == 1
    with pytest.raises(ValueError, match="先后有序"):
        calculate(Calculation(id="p", section="market", type="calculation", operation="change_pct",
                              refs=[f.id for f in closing]), evidence)


def test_balances_bind_account_identity_and_unknown_time_cannot_cross_receipts():
    evidence = catalog(("analyze_portfolio", {"account_balances": [
        {"id": "short_stock", "available_cash": 100}, {"id": "mid_term", "available_cash": 200}]}))
    cash = [f for f in evidence.facts.values() if f.metric == "available_cash"]
    item = Calculation(id="sum", type="calculation", section="positions", operation="sum", refs=[f.id for f in cash])
    assert "300.00元" in calculate(item, evidence)[0]
    evidence.facts[cash[-1].id] = replace(cash[-1], receipt_id="another-query")
    with pytest.raises(ValueError, match="观察时点"):
        calculate(item, evidence)


def test_share_uses_portfolio_capital_and_settlement_needs_fee_basis():
    evidence = catalog(("analyze_portfolio", {"total_market_value": 100, "total_account_capital": 400,
        "recent_closed_trades": [{"symbol": "000001", "pnl": 100},
            {"symbol": "000002", "pnl": -110, "pnl_basis": "fifo_after_entry_and_exit_fees"}]}))
    text, _ = calculate(Calculation(id="share", type="calculation", section="positions", operation="share_pct",
        refs=[ref(evidence, "total_market_value"), ref(evidence, "total_account_capital")]), evidence)
    assert "25.00%" in text
    pnl = [f for f in evidence.facts.values() if f.metric == "pnl"]
    assert len(pnl) == 1 and "已扣分摊买卖费用" in pnl[0].display()
