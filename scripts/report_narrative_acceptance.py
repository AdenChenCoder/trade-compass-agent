"""Real semantic review acceptance; frozen composition, no business tools.

This deliberately varies prose meaning independently of evidence identity.
It does not claim to prove all natural-language judgments are reliable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import time


CASES = {
    "hidden_result": ("收盘价上升。", True),
    "compact_percentage": ("收盘价上升。", True),
    "visible_result": ("收盘价上升。", True),
    "missing_percentage_unit": ("价格变动比例为4.55。", False),
    "correct_percentage": ("价格变动比例为4.55%。", True),
    "unit_in_body_context": ("这里的变动比例以百分数计，为4.55。", True),
    "explicit_ratio_scale": ("价格变动比例约为0.0455，即4.55%。", True),
    "percentage_as_price": ("后一日收盘价为4.55%。", False),
    "natural": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", True),
    "reordered": ("后续走势不能据此判断；两个观察日之间收盘价上升。", True),
    "correct_unit": ("前一日收盘价为22元/股，后一日为23元/股。", True),
    "changed_direction_evidence": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", False),
    "wrong_identity": ("上述价格属于000001。", False),
    "wrong_date": ("上述两项收盘价分别来自2025年1月3日和1月4日。", False),
    "wrong_unit": ("前一日收盘价为22美元，后一日为23美元。", False),
    "unsupported_cause": ("营收增长导致了这次上涨。", False),
    "historical_execution": ("本轮已经买入002415共100股，成交价22元/股。", False),
}


# Scope changes must be rejected even when the copied number is correct. The
# identical sector sentence is also accepted when its source actually says it.
_company = [("get_fundamentals", json.dumps({"symbol": "600928", "name": "西安银行",
             "as_of": "2025-01-02", "pb": 0.8}))]
_sector = [("fetch_url", json.dumps({"url": "https://example.invalid/frozen-sector",
            "title": "冻结的行业统计样本", "content": "银行板块整体的市净率为0.8倍。该指标适用于整个板块，不是单一银行。"}))]
_holding = [*_company, ("analyze_portfolio", json.dumps({"positions": [
    {"symbol": "600928", "name": "西安银行", "account": "test", "quantity": 1000,
     "avg_cost": 10, "last_price": 11, "unrealized_pnl": 1000}]}))]
_prices = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]}))]
_position_prose = "600928 西安银行持有1000股，成本10元/股，估值11元/股，浮动盈亏1000元。"
_price_prose = "002415在2025年1月1日和1月2日的收盘价分别为22元/股和23元/股。"
SCOPE_CASES = {
    "individual_scope": ("600928 西安银行的市净率为0.8倍。", True, _company, "说明资料中的市净率及其适用主体。"),
    "company_as_sector": ("银行板块整体的市净率为0.8倍。", False, _company, "说明资料中的市净率及其适用主体。"),
    "sector_source_same_words": ("银行板块整体的市净率为0.8倍。", True, _sector, "说明资料中的市净率及其适用主体。"),
    "absence_as_nonexecution": ("本轮未执行新的买卖操作，因为没有本轮交易回执。", False, _company,
                                "说明现有资料能否确认本轮买卖执行情况。"),
    "absence_as_unknown": ("提供的资料中没有本轮交易回执，无法据此确认本轮是否执行了买卖。", True, _company,
                          "说明现有资料能否确认本轮买卖执行情况。"),
    # Correct adjacent quantities must not hide the only incorrect assertion.
    # Do not add a second unsupported claim that could mask a missed defect.
    "compound_company_as_sector": (_position_prose + "银行板块的市净率为0.8倍。", False, _holding,
                                   "说明持仓情况和资料中的市净率及其适用主体。"),
    "compound_individual_scope": (_position_prose + "600928 西安银行的市净率为0.8倍。", True, _holding,
                                 "说明持仓情况和资料中的市净率及其适用主体。"),
    "compound_absence_as_nonexecution": (_price_prose + "本轮未执行新的买卖操作。", False, _prices,
                                        "说明两日收盘价和现有资料能否确认本轮买卖执行情况。"),
    "compound_absence_as_unknown": (_price_prose + "没有本轮交易回执，无法确认本轮是否执行了买卖。", True, _prices,
                                   "说明两日收盘价和现有资料能否确认本轮买卖执行情况。"),
}
CASES.update({name: (prose, expected) for name, (prose, expected, _, _) in SCOPE_CASES.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    wheel = args.wheel.resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[1]):
        raise RuntimeError("acceptance must run outside checkout")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chdir(root)
    for key in list(os.environ):
        if key.startswith("TRADE_COMPASS_"):
            del os.environ[key]
    (root / "config.yaml").write_text("llm:\n  provider: disabled\ndata_provider: sample\n")
    (root / ".env").touch()
    os.environ.update(TRADE_COMPASS_HOME=str(root), TRADE_COMPASS_CONFIG=str(root/"config.yaml"),
                      TRADE_COMPASS_ENV_FILE=str(root/".env"), TRADE_COMPASS_DATA_DIR=str(root/"data"),
                      TRADE_COMPASS_MEMORY_DIR=str(root/"memory"), TRADE_COMPASS_NO_SCHEDULER="true")
    from trade_compass_agent.config import AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client, llm_request_deadline
    from trade_compass_agent.runtime.report_publication import publish_report

    verify = runpy.run_path(str(Path(__file__).with_name("release_acceptance.py")))["verify_installed_wheel"]
    result = {"passed": False, "artifact": verify(wheel), "mode": "real semantic reviewer / frozen composition",
              "model": args.model, "cases": [], "plan": CASES, "retries": 0, "business_calls": 0,
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (root/"plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    config = AppConfig(data_dir=root/"data", memory_dir=root/"memory",
                       llm=LLMConfig(provider="lmstudio", model=args.model, api_key_env="", max_retries=0, timeout=120))
    client = create_chat_client(config)
    receipts = [
        ("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
            {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]})),
        ("analyze_portfolio", json.dumps({"recent_trades": [{"symbol": "002415", "account": "test",
            "trade_id": "historical-fill", "side": "buy", "quantity": 100, "price": 22,
            "timestamp": "2024-12-31T14:00:00+08:00"}]})),
    ]
    for name, (prose, expected) in CASES.items():
        case_receipts = list(SCOPE_CASES[name][2] if name in SCOPE_CASES else receipts)
        if name == "changed_direction_evidence":
            changed = json.loads(case_receipts[0][1])
            changed["bars"][0]["close"], changed["bars"][1]["close"] = 23, 22
            case_receipts[0] = ("get_bars", json.dumps(changed))
        calls = []
        def call(system, user):
            payload = json.loads(user)
            if "schema" in payload:
                if name in SCOPE_CASES:
                    refs = [row[0] for group in payload["facts"] for row in group["rows"]]
                    return json.dumps({"schema_version": 1, "items": [{"id": "answer", "section": "overview",
                        "type": "paragraph", "parts": [{"type": "text", "text": prose}], "refs": refs}]}, ensure_ascii=False)
                facts = [dict(zip(payload["fact_columns"], row, strict=True))
                         for group in payload["facts"] if group["metric"] == "close" for row in group["rows"]]
                parts = [{"type": "value", "ref": facts[0]["id"], "field": "subject"},
                         {"type": "text", "text": "的收盘价从"},
                         {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                         {"type": "text", "text": "变为"},
                         {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                         {"type": "text", "text": "。" + prose}]
                historical_refs = [row[0] for group in payload["facts"]
                                   if group["source"] == "analyze_portfolio" for row in group["rows"]]
                if name == "historical_execution" and not historical_refs:
                    raise RuntimeError("historical execution fixture lost its source")
                definitions = []
                refs = historical_refs if name == "historical_execution" else []
                if name in {"hidden_result", "compact_percentage", "visible_result", "missing_percentage_unit", "correct_percentage",
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price"}:
                    definitions = [{"type": "calculation", "id": "change", "section": "overview",
                                    "operation": "change_pct", "refs": [facts[0]["id"], facts[1]["id"]]}]
                    refs = ["change"]
                    if name == "visible_result":
                        parts.extend([{"type": "text", "text": "这两日收盘价上涨了"},
                                      {"type": "value", "ref": "change", "field": "value_with_unit"},
                                      {"type": "text", "text": "。"}])
                if name == "compact_percentage":
                    parts = [{"type": "value", "ref": facts[0]["id"], "field": "subject"},
                             {"type": "text", "text": "收盘价："},
                             {"type": "value", "ref": facts[0]["id"], "field": "value_with_unit"},
                             {"type": "text", "text": " → "},
                             {"type": "value", "ref": facts[1]["id"], "field": "value_with_unit"},
                             {"type": "text", "text": "（+"},
                             {"type": "value", "ref": "change", "field": "value_with_unit"},
                             {"type": "text", "text": "）。"}]
                return json.dumps({"schema_version": 1, "items": [*definitions,
                    {"type": "paragraph", "id": "answer", "section": "overview", "parts": parts,
                     "refs": refs}]})
            if calls:
                raise RuntimeError("review call budget exceeded")
            calls.append({"input": payload})
            answer = client.stream_complete([ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user)], tools=None)
            calls[-1]["output"] = answer.content
            if answer.tool_calls:
                raise RuntimeError("review requested a tool")
            return answer.content
        entry = {"case": name, "expected_supported": expected, "passed": False}
        started = time.monotonic()
        try:
            with llm_request_deadline(started + 120):
                task = (SCOPE_CASES[name][3] if name in SCOPE_CASES else
                        "说明这两个观察日收盘价变化，仅在存在本轮执行记录时报告成交。")
                if name in {"hidden_result", "compact_percentage", "visible_result", "missing_percentage_unit", "correct_percentage",
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price"}:
                    task += "给出收盘价变动比例。"
                report = publish_report(task=task,
                    draft="", tool_results=case_receipts, current_tool_results=case_receipts,
                    run_scope="semantic-"+name, call=call, max_calls=2)
            (root/(name+"-artifact.json")).write_text(json.dumps(report.artifact, ensure_ascii=False, indent=2))
            reviews = (report.artifact.get("review") or {}).get("reviews", [])
            actual = next((r["supported"] for r in reviews if r["id"] == "answer"), None)
            coverage_complete = (report.artifact.get("review") or {}).get("coverage", {}).get("complete")
            entry.update(actual_supported=actual, coverage_complete=coverage_complete, status=report.verification["status"],
                         passed=actual is expected and len(calls) == 1
                         and ((coverage_complete is False and report.verification["report"] == "partial")
                              if name == "hidden_result" else
                              (report.verification["status"] == "complete") if expected else
                              (prose not in report.text
                               and report.verification["report"] == "partial")))
        except Exception as exc:
            entry["error"] = str(exc)
        entry["seconds"] = round(time.monotonic() - started, 2)
        (root/(name+"-calls.json")).write_text(json.dumps(calls, ensure_ascii=False, indent=2))
        result["cases"].append(entry)
        (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    result["passed"] = all(row["passed"] for row in result["cases"])
    (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
