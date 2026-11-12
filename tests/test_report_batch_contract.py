"""The real batch producer's compact bars must reach the published report."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from report_contract_helpers import report_facts
from trade_compass_agent.domain import Bar
from trade_compass_agent.runtime.report_contract import EvidenceCatalog
from trade_compass_agent.runtime.report_publication import publish_report
from trade_compass_agent.runtime.tools.batch import tool_batch_get_bars


def receipt(*, count=30, source="acceptance-fixture"):
    class Provider:
        name = source

        def get_bars(self, symbol, *, timeframe, limit):
            return [Bar(symbol=symbol, timestamp=datetime(2025, 1, 1)+timedelta(days=i),
                        open=20+i, high=24+i, low=18+i, close=21+i, volume=1000+i,
                        source=self.name) for i in range(count)][-limit:]

    return tool_batch_get_bars(SimpleNamespace(provider=Provider()), symbols="000001,002415",
                              timeframe="1d", limit=30, summary_only=False)


@pytest.mark.parametrize("count", [0, 1, 30])
def test_actual_batch_bars_keep_metric_identity_units_dates_and_original_pointer(count):
    raw = receipt(count=count)
    catalog = EvidenceCatalog.build([("batch_get_bars", raw)], run_scope="batch")
    closes = [f for f in catalog.facts.values() if f.metric == "close"]
    assert len(closes) == 2*count
    for fact in closes:
        index = int(fact.pointer.split("/")[-2])
        assert fact.pointer == f"/results/{fact.subject}/bars/{index}/C"
        assert fact.entity == fact.subject and fact.timeframe == "1d"
        assert fact.value == str(21+index) and fact.unit == "元/股"
        assert fact.observed_at.startswith((datetime(2025, 1, 1)+timedelta(days=index)).date().isoformat())
    assert all(f.unit == "单位未提供" for f in catalog.facts.values() if f.metric == "volume")


def test_actual_batch_prices_can_publish_verified_change():
    def call(_system, user):
        payload = json.loads(user)
        if "schema" not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        facts = [f for f in report_facts(payload) if f["subject"] == "002415" and f["metric"] == "close"]
        return json.dumps({"schema_version": 1, "items": [{"id": "change", "type": "calculation",
            "section": "market", "operation": "change_pct", "refs": [f["id"] for f in facts[-2:]]}]})
    result = publish_report(task="计算002415最后两日收盘价变化", draft="", tool_results=[("batch_get_bars", receipt())],
                            run_scope="batch", call=call)
    assert result.verification["report"] == "complete"
    assert "2.04%" in result.text and "002415" in result.text
    assert "2025-01-29" in result.text and "2025-01-30" in result.text


@pytest.mark.parametrize("source", ["sample", "synthetic"])
def test_batch_aliases_cannot_promote_sample_prices(source):
    catalog = EvidenceCatalog.build([("batch_get_bars", receipt(source=source))], run_scope="sample")
    assert not any(f.metric in {"open", "high", "low", "close", "volume"} for f in catalog.facts.values())


def test_aliases_are_scoped_to_batch_rows_and_do_not_inherit_latest_date():
    raw = json.loads(receipt(count=1))
    raw["results"]["002415"]["bars"][0].pop("date")
    raw["results"]["002415"]["C"] = 999
    raw["C"] = 999
    catalog = EvidenceCatalog.build([("batch_get_bars", json.dumps(raw))], run_scope="unknown")
    closes = [f for f in catalog.facts.values() if f.metric == "close"]
    assert len(closes) == 2 and not any(f.value == "999" for f in closes)
    assert next(f for f in closes if f.subject == "002415").observed_at is None
    other = EvidenceCatalog.build([("get_bars", json.dumps(raw))], run_scope="wrong-tool")
    assert not any(f.metric == "close" for f in other.facts.values())
