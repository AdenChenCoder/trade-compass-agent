"""Reader-facing publication remains faithful to the evidence and outcome."""
import json

from report_contract_helpers import report_facts
from trade_compass_agent.runtime.report_publication import publish_report


def example_publication(*, missing=False):
    """Frozen end-to-end example also consumed by the mobile browser check."""
    receipts = [("get_bars", json.dumps({"symbol": "002415", "name": "海康威视", "timeframe": "1d",
        "bars": [{"timestamp": "2026-11-10", "close": 10}, {"timestamp": "2026-11-11", "close": 11}]}))]

    def call(system, user):
        payload = json.loads(user)
        if "schema" not in payload:
            return json.dumps({"reviews": [{"id": "direction", "supported": True, "reason": ""}],
                               "coverage": {"complete": True, "missing": []}})
        facts = [f for f in report_facts(payload) if f["metric"] == "close"]
        return json.dumps({"schema_version": 1, "items": [
            *[{"id": f"price{i}", "type": "fact", "section": "market", "ref": f["id"]} for i, f in enumerate(facts)],
            {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct", "refs": [f["id"] for f in facts]},
            {"id": "direction", "type": "inference", "section": "assessment", "text": "这两日收盘价上升。", "refs": ["change"]},
            *([{"id": "gap", "type": "gap", "section": "gaps", "text": "缺少营收资料"}] if missing else []),
        ]})

    return publish_report(task="比较收盘价" + ("及营收" if missing else ""), draft="", tool_results=receipts,
                          run_scope="phone-example", call=call)


def test_notification_consumer_reopens_exact_report_without_changing_old_history(tmp_path):
    from trade_compass_agent.config import AppConfig
    from trade_compass_agent.domain import Notification
    from trade_compass_agent.ops.notifications import JsonNotificationStore
    from trade_compass_agent.web.api import notifications_payload
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    path = config.data_dir / "notifications.jsonl"
    store = JsonNotificationStore(path)
    old = Notification(channel="scheduler:legacy", title="历史报告", message="原始记录，保持可访问。")
    store.append(old, event_id="job-run:old")
    original = path.read_text().splitlines()[0]
    for missing in (False, True):
        report = example_publication(missing=missing)
        store.append(Notification(channel="scheduler:example", title="价格观察", message=report.text,
                                  severity="warning" if missing else "info",
                                  task_status="degraded" if missing else "completed"))
        # Mobile /notifications uses this same persisted consumer after restart.
        notice = notifications_payload(config, 100)[-1]
        assert notice.message == report.text
        assert notice.task_status == ("degraded" if missing else "completed")
        assert ("缺少营收资料" in notice.message) == missing
        assert "10.00%" in notice.message
        assert path.read_text().splitlines()[0] == original


def test_grouped_report_keeps_identity_dates_scope_and_invalid_outcomes():
    receipts = [("get_bars", json.dumps({"symbol": symbol, "timeframe": "1d", "adjustment": "unadjusted",
            "bars": [{"timestamp": "2026-11-10T07:00:00+00:00", "close": start, "adjusted": False},
                 {"timestamp": "2026-11-11T07:00:00+00:00", "close": start + 1, "adjusted": False}]}))
        for symbol, start in [("002415", 10), ("000001", 20)]]
    requests = []

    def call(system, user):
        payload = json.loads(user)
        requests.append(payload)
        if "schema" not in payload:
            # The audit presentation sent to the reviewer stays unchanged.
            assert "2026-11-10T07:00:00+00:00" in payload["report"]
            assert "数据来源与口径" not in payload["report"]
            return json.dumps({"reviews": [{"id": "bad", "supported": False, "reason": "价格不能证明业绩改善"}],
                               "coverage": {"complete": True, "missing": []}})
        facts = [f for f in report_facts(payload) if f["metric"] == "close"]
        return json.dumps({"schema_version": 1, "items": [
            {"id": "f0", "type": "fact", "section": "market", "ref": facts[0]["id"]},
            {"id": "bad", "type": "inference", "section": "assessment", "text": "业绩改善推动上涨", "refs": [facts[0]["id"]]},
            *[{"id": f"f{i}", "type": "fact", "section": "market", "ref": f["id"]} for i, f in enumerate(facts[1:], 1)],
            {"id": "change", "type": "calculation", "section": "market", "operation": "change_pct",
             "refs": [f["id"] for f in facts[:2]]},
            {"id": "gap", "type": "gap", "section": "gaps", "text": "缺少营收资料"},
        ]})

    report = publish_report(task="比较收盘价和营收", draft="", tool_results=receipts, run_scope="reader", call=call)
    text = report.text
    assert len(requests) == 2
    assert text.count("## 市场\n") == 1
    assert "### 002415" in text and "### 000001" in text
    assert "| 收盘价 · 2026-11-10〔1〕 | 10元/股 |" in text
    assert "| 收盘价 · 2026-11-10〔1〕 | 20元/股 |" in text
    assert "10.00%" in text and "11元/股" in text
    # Exact timestamps/scope remain accessible once per shared source context.
    assert text.count("2026-11-10T07:00:00+00:00") == 1
    assert "未复权数据" in text
    assert "业绩改善推动上涨" not in text
    assert "价格不能证明业绩改善" in text and "缺少营收资料" in text
    assert report.verification["status"] == "incomplete"
    assert len(report.artifact["document"]["items"]) == 7


def test_execution_history_and_unknown_result_survive_reader_grouping():
    receipts = [("analyze_portfolio", json.dumps({"positions": [
        {"symbol": "002415", "account": "one", "quantity": 100},
        {"symbol": "002415", "account": "two", "quantity": 200}],
        "trades_as_of": "2026-11-12", "recent_trades": [{"symbol": "000001", "account": "two",
        "trade_id": "old-fill", "side": "buy", "quantity": 200, "price": 9,
        "timestamp": "2026-11-12T10:00:00+08:00"}]})),
        ("place_paper_trade", json.dumps({"error": "timeout", "execution_unconfirmed": True})),
        ("place_paper_trade", json.dumps({"status": "executed", "trade_id": "fill-1", "symbol": "002415",
         "side": "sell", "quantity": 100, "price": 10, "account": "one", "timestamp": "2026-11-12T14:00:00+08:00"}))]

    def call(system, user):
        payload = json.loads(user)
        if "schema" not in payload:
            return '{"reviews":[],"coverage":{"complete":true,"missing":[]}}'
        facts = [f for f in report_facts(payload) if f["metric"] == "quantity" and f["source"] == "analyze_portfolio"]
        return json.dumps({"schema_version": 1, "items": [
            {"id": f"pos{i}", "type": "fact", "section": "execution", "ref": f["id"]} for i, f in enumerate(facts)]})

    report = publish_report(task="核对操作", draft="", tool_results=receipts, run_scope="execution", call=call)
    assert report.text.count("## 执行记录\n") == 1
    assert "002415 · one" in report.text and "002415 · two" in report.text
    assert "时点未提供" in report.text
    assert "fill-1" in report.text and "卖出100股" in report.text
    assert "操作结果尚未确认" in report.text
    assert "| 依据 |" not in report.text
    assert "**000001 · 买入**  \n  数量：200；成交价：9" in report.text
    assert "当日账本；账户：two" in report.text and "成交编号：old-fill" in report.text
    assert "本轮回执；账户：one" in report.text
    assert "账本记录不表示本轮新下单" in report.text
    assert report.verification["execution"] == "unknown"


def test_unvalidated_references_cannot_gain_values_in_reader_layer():
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog, ReportDocument, validate_document
    from trade_compass_agent.runtime.report_reading import render_readable_document
    catalog = EvidenceCatalog.build([], run_scope="empty")
    doc = ReportDocument.model_validate({"schema_version": 1, "items": [
        {"id": "invalid", "type": "fact", "section": "market", "ref": "missing"}]})
    findings, rendered, _ = validate_document(doc, catalog)
    text = render_readable_document(doc, rendered, findings, catalog)
    assert "引用不属于本轮可核验资料" in text
    assert "| 指标" not in text


def test_literal_values_and_midnight_timezone_are_not_reinterpreted():
    from trade_compass_agent.runtime.report_contract import EvidenceCatalog, ReportDocument, validate_document
    from trade_compass_agent.runtime.report_reading import render_readable_document
    catalog = EvidenceCatalog.build([("sina_realtime_quote", json.dumps({"symbol": "002415",
        "name": "A|B<script>", "price": 0.42287699999999995, "timestamp": "2026-11-11T16:01:02+00:00"}))], run_scope="literal")
    fact = next(f for f in catalog.facts.values() if f.metric == "price")
    doc = ReportDocument.model_validate({"schema_version": 1, "items": [
        {"id": "quote", "type": "fact", "section": "market", "ref": fact.id}]})
    findings, rendered, _ = validate_document(doc, catalog)
    text = render_readable_document(doc, rendered, findings, catalog)
    assert "2026-11-12 00:01:02（北京时间）" in text
    assert "2026-11-11T16:01:02+00:00" in text
    assert "0.42287699999999995元/股" in text
    assert r"A\|B&lt;script&gt;" in text
