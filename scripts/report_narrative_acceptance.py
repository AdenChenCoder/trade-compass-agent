"""Real publication acceptance from frozen first prose, no business tools.

This deliberately varies prose meaning independently of evidence identity.
Invalid controls must be rejected without a rewrite. Legitimate controls may use
the production pipeline's single real feedback revision and fresh reviews.
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
    "wrong_rounded_percentage": ("价格变动比例为4.56%。", False),
    "unit_in_body_context": ("这里的变动比例以百分数计，为4.55。", True),
    "explicit_ratio_scale": ("价格变动比例约为0.0455，即4.55%。", True),
    "percentage_as_price": ("后一日收盘价为4.55%。", False),
    "natural": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", True),
    "reordered": ("后续走势不能据此判断；两个观察日之间收盘价上升。", True),
    "bounded_limitation": ("这些资料只覆盖两次收盘观察，尚不足以确定后续价格方向。", True),
    "unwarranted_forecast": ("根据这两个观察日，可以确定接下来还会上涨。", False),
    "wrong_unit_with_limitation": ("前一日收盘价为22美元，后一日为23美元；不能据此预测后续走势。", False),
    "unsupported_cause_with_limitation": ("营收增长导致了这次上涨；不能据此预测后续走势。", False),
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
    "bound_duplicate_gap": ("002415在2025-01-01的收盘价为22元/股。", True, _prices,
                            "说明002415在2025年1月1日的收盘价。"),
    "bound_genuine_gap": ("002415在2025-01-01的收盘价为22元/股。", True, _prices,
                          "说明002415在2025年1月1日的收盘价及该公司的营收资料。"),
    "holding_observation": (_position_prose, True, _holding, "说明当前持仓观察。"),
    "currency_missing": ("002415在2025年1月1日的收盘价为22。", False, _prices, "说明资料中002415在2025年1月1日的收盘价。"),
    "currency_shared_unit": ("以下价格均以元/股计。002415在2025年1月1日的收盘价为22。", True, _prices, "说明资料中002415在2025年1月1日的收盘价。"),
    "dimensionless_valuation": ("600928 西安银行的市净率为0.8。", True, _company, "说明资料中的市净率。"),
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
    "reporting_is_not_nonexecution": (_price_prose + "本文不报告成交情况，不能据此认定本轮没有交易。", True, _prices,
                                     "说明两日收盘价，并保持本轮交易状态的不确定性。"),
    "reporting_rule_with_noop": (_price_prose + "本文不报告成交情况，因此本轮没有交易。", False, _prices,
                                "说明两日收盘价，并保持本轮交易状态的不确定性。"),
    "current_execution_receipt": (_price_prose + "本轮已买入002415共100股，成交价22元/股。", True,
        [*_prices, ("place_paper_trade", json.dumps({"status": "executed", "trade_id": "frozen-current-fill",
            "symbol": "002415", "account": "test", "side": "buy", "quantity": 100, "price": 22,
            "timestamp": "2025-01-02T10:00:00+08:00"}))], "说明两日收盘价和本轮执行结果。"),
}
CASES.update({name: (prose, expected) for name, (prose, expected, _, _) in SCOPE_CASES.items()})

_dated_price = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-02", "close": 22}]}))]
_utc_price = [("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
    {"timestamp": "2025-01-01T16:00:00+00:00", "close": 22}]}))]
TIME_CASES = {
    "relative_current": ("今天002415收盘价为22元/股。", True, _dated_price, "2025-01-02"),
    "relative_stale": ("今天002415收盘价为22元/股。", False, _dated_price, "2025-01-03"),
    "relative_unknown": ("今天002415收盘价为22元/股。", False, _dated_price, None),
    "utc_display_date": ("002415在2025-01-02的收盘价为22元/股。", True, _utc_price, "2025-01-03"),
    "utc_wrong_date": ("002415在2025-01-01的收盘价为22元/股。", False, _utc_price, "2025-01-03"),
}
CASES.update({name: (prose, expected) for name, (prose, expected, _, _) in TIME_CASES.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fact-model", default="", help="Independent local LM Studio factual reviewer")
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES),
                        help="Run named controls and record unselected cases explicitly")
    args = parser.parse_args()
    selected = dict.fromkeys(args.cases or CASES)
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
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client, create_local_report_fact_check, llm_request_deadline
    from trade_compass_agent.runtime.report_publication import publish_report

    verify = runpy.run_path(str(Path(__file__).with_name("release_acceptance.py")))["verify_installed_wheel"]
    result = {"passed": False, "artifact": verify(wheel), "mode": "frozen first prose / real review and bounded normal revision",
              "model": args.model, "fact_model": args.fact_model, "cases": [], "plan": CASES, "retries": 0, "business_calls": 0,
              "selected_cases": list(selected), "unselected_cases": [name for name in CASES if name not in selected],
              "full_corpus": len(selected) == len(CASES),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (root/"plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    config = AppConfig(data_dir=root/"data", memory_dir=root/"memory",
                       llm=LLMConfig(provider="lmstudio", model=args.model, api_key_env="", max_retries=0, timeout=120,
                                     local_report_review_model=args.fact_model))
    client = create_chat_client(config)
    factual_client = create_local_report_fact_check(config)
    receipts = [
        ("get_bars", json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
            {"timestamp": "2025-01-01", "close": 22}, {"timestamp": "2025-01-02", "close": 23}]})),
        ("analyze_portfolio", json.dumps({"recent_trades": [{"symbol": "002415", "account": "test",
            "trade_id": "historical-fill", "side": "buy", "quantity": 100, "price": 22,
            "timestamp": "2024-12-31T14:00:00+08:00"}]})),
    ]
    for name in selected:
        prose, expected = CASES[name]
        case_receipts = list(TIME_CASES[name][2] if name in TIME_CASES else
                             SCOPE_CASES[name][2] if name in SCOPE_CASES else receipts)
        if name == "changed_direction_evidence":
            changed = json.loads(case_receipts[0][1])
            changed["bars"][0]["close"], changed["bars"][1]["close"] = 23, 22
            case_receipts[0] = ("get_bars", json.dumps(changed))
        normal_publication = expected and name != "hidden_result"
        # The full task harness uses the same 240-second parent budget. Negative
        # controls retain their 120-second first-review limit and cannot rewrite.
        deadline_seconds = 240 if normal_publication else 120
        primary_limit = 4 if normal_publication else 2
        calls, factual_calls, writing_calls = [], [], []
        def factual_call(system, user):
            payload = json.loads(user)
            row = {"input": payload, "stage": "body_meaning" if "paragraphs" in payload else "entailment"}
            factual_calls.append(row)
            row["output"] = factual_client(system, user)
            return row["output"]
        def call(system, user):
            payload = json.loads(user)
            if "schema" in payload:
                writing_calls.append({"input": payload, "frozen": "revision" not in payload})
                if "revision" in payload:
                    if not normal_publication or len(writing_calls) != 2:
                        raise RuntimeError("only one normal publication revision is allowed")
                    answer = client.stream_complete([ChatMessage(role="system", content=system),
                        ChatMessage(role="user", content=user)], tools=None)
                    writing_calls[-1]["output"] = answer.content
                    if answer.tool_calls:
                        raise RuntimeError("revision requested a tool")
                    return answer.content
                if len(writing_calls) != 1:
                    raise RuntimeError("frozen first composition cannot be resampled")
                if name in SCOPE_CASES or name in TIME_CASES:
                    refs = [row[0] for group in payload["facts"] for row in group["rows"]]
                    parts = [{"type": "text", "text": prose}]
                    if name == "utc_display_date":
                        close_id = next(group["rows"][0][0] for group in payload["facts"] if group["metric"] == "close")
                        parts = [{"type": "text", "text": "002415在"},
                                 {"type": "value", "ref": close_id, "field": "date"},
                                 {"type": "text", "text": "的收盘价为22元/股。"}]
                    items = [{"id": "answer", "section": "overview", "type": "paragraph", "parts": parts, "refs": refs}]
                    if name in {"bound_duplicate_gap", "bound_genuine_gap"}:
                        first = next(group["rows"][0][0] for group in payload["facts"] if group["metric"] == "close")
                        items[0]["parts"] = [
                            {"type": "value", "ref": first, "field": "subject"}, {"type": "text", "text": "在"},
                            {"type": "value", "ref": first, "field": "date"}, {"type": "text", "text": "的收盘价为"},
                            {"type": "value", "ref": first, "field": "value_with_unit"}, {"type": "text", "text": "。"}]
                        items.append({"id": "gap", "section": "overview", "type": "gap",
                            "text": "缺少营收资料。" if name == "bound_genuine_gap" else "缺少002415在2025年1月1日的收盘价。"})
                    return json.dumps({"schema_version": 1, "items": items}, ensure_ascii=False)
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
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price", "wrong_rounded_percentage"}:
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
            if len(calls) >= (2 if normal_publication else 1):
                raise RuntimeError("review call budget exceeded")
            calls.append({"input": payload})
            answer = client.stream_complete([ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user)], tools=None)
            calls[-1]["output"] = answer.content
            if answer.tool_calls:
                raise RuntimeError("review requested a tool")
            return answer.content
        entry = {"case": name, "expected_supported": expected, "passed": False,
                 "deadline_seconds": deadline_seconds, "primary_call_limit": primary_limit,
                 "revision_allowed": normal_publication}
        started = time.monotonic()
        try:
            with llm_request_deadline(started + deadline_seconds):
                task = ("说明所给日线资料中的收盘价及其日期。" if name in TIME_CASES else
                        SCOPE_CASES[name][3] if name in SCOPE_CASES else
                        "说明这两个观察日收盘价变化，仅在存在本轮执行记录时报告成交。")
                if name in {"hidden_result", "compact_percentage", "visible_result", "missing_percentage_unit", "correct_percentage",
                            "unit_in_body_context", "explicit_ratio_scale", "percentage_as_price", "wrong_rounded_percentage"}:
                    task += "给出收盘价变动比例。"
                report = publish_report(task=task,
                    draft="", tool_results=case_receipts, current_tool_results=case_receipts,
                    run_scope="semantic-"+name, call=call, max_calls=primary_limit,
                    fact_review_call=factual_call if factual_client else None,
                    reference_date=TIME_CASES[name][3] if name in TIME_CASES else None)
            (root/(name+"-artifact.json")).write_text(json.dumps(report.artifact, ensure_ascii=False, indent=2))
            reviews = (report.artifact.get("review") or {}).get("reviews", [])
            actual = next((r["supported"] for r in reviews if r["id"] == "answer"), None)
            attempts = report.artifact.get("revision_attempts", [report.artifact])
            first = attempts[0]
            first_reviews = (first.get("review") or {}).get("reviews", [])
            first_supported = next((r["supported"] for r in first_reviews if r["id"] == "answer"), None)
            paragraph_ids = {item["id"] for item in report.artifact["document"]["items"] if item["type"] == "paragraph"}
            supported_ids = {row["id"] for row in reviews if row["supported"]}
            supported_ids.update(key for key, projection in report.artifact["execution_projections"].items()
                                 if projection.get("analysis_supported"))
            final_supported = bool(paragraph_ids) and paragraph_ids <= supported_ids
            first_prose = [row["text"] for row in (first.get("body_review_input") or {}).get("paragraphs", [])]
            final_prose = [row["text"] for row in (report.artifact.get("body_review_input") or {}).get("paragraphs", [])]
            revised = len(attempts) == 2
            # Changing IDs or asking the reviewer again is not a correction.
            changed_prose = first_prose != final_prose if factual_client else first["document"] != report.artifact["document"]
            coverage_complete = (report.artifact.get("review") or {}).get("coverage", {}).get("complete")
            local_calls = report.artifact["factual_model_calls"] + report.artifact["body_review_model_calls"]
            entry.update(actual_supported=actual, coverage_complete=coverage_complete, status=report.verification["status"],
                         first_supported=first_supported, final_supported=final_supported,
                         revision_attempted=revised, selected_attempt=report.artifact.get("selected_attempt", 0),
                         changed_prose=changed_prose, model_calls=report.artifact["model_calls"],
                         body_model_calls=report.artifact["body_review_model_calls"],
                         factual_model_calls=report.artifact["factual_model_calls"],
                         passed=report.artifact["review_valid"]
                         and (final_supported if normal_publication else actual is expected)
                         and 1 <= len(calls) <= (2 if normal_publication else 1)
                         and len(factual_calls) == local_calls
                         and report.artifact["model_calls"] == len(calls) + len(writing_calls) + local_calls
                         and report.artifact["model_calls"] <= primary_limit + 4 * int(bool(factual_client))
                         and report.artifact["body_review_model_calls"] == (1 + int(revised)) * int(bool(factual_client))
                         and (not revised or changed_prose and report.artifact.get("selected_attempt") == 1)
                         and ((coverage_complete is False and report.verification["report"] == "partial")
                              if name == "hidden_result" else
                              (report.verification["status"] == "incomplete") if name == "bound_genuine_gap" else
                              (report.verification["status"] == "complete") if expected else
                              (prose not in report.text
                               and report.verification["report"] == "partial")))
            if name in {"bound_duplicate_gap", "bound_genuine_gap"}:
                gap = (report.artifact.get("review") or {}).get("gap_reviews", [])
                facts_by_id = {row["id"]: row for row in report.artifact["facts"]}
                price_visible = any(
                    part.get("field") == "value_with_unit"
                    and (fact := facts_by_id.get(part.get("ref"), {})).get("subject") == "002415"
                    and fact.get("metric") == "close" and str(fact.get("value")) == "22"
                    and fact.get("observed_at", "").startswith("2025-01-01")
                    for item in report.artifact["document"]["items"] if item["type"] == "paragraph"
                    for part in item["parts"])
                correct_gap = len(gap) == 1 and gap[0]["id"] == "gap" and gap[0]["status"] == (
                    "required" if name == "bound_genuine_gap" else "already_present")
                if name == "bound_genuine_gap":
                    entry["passed"] = (entry["passed"] and final_supported and correct_gap and price_visible
                        and report.verification["status"] == "incomplete" and "缺少营收资料" in report.text
                        and any(f["code"] == "declared_gap" for f in report.verification["findings"]))
                else:
                    entry["passed"] = entry["passed"] and price_visible and (correct_gap or revised and not gap)
            if name in {"missing_percentage_unit", "currency_missing"} and factual_client:
                # The body-only boundary must catch this, even if evidence
                # entailment would silently supply the omitted reader unit.
                entry["passed"] = entry["passed"] and bool(report.artifact["body_quantity_issues"].get("answer"))
                entry["passed"] = entry["passed"] and report.artifact["factual_model_calls"] == 0
            if name == "current_execution_receipt" and factual_client:
                fill = next(json.loads(row["result"]) for row in report.artifact["receipts"]
                            if row["tool"] == "place_paper_trade")
                entry["passed"] = entry["passed"] and fill["quantity"] == 100
                entry["passed"] = entry["passed"] and report.artifact["execution_records"][0]["status"] == "executed"
                entry["passed"] = entry["passed"] and "买入100股" in report.text
            if name in {"historical_execution", "absence_as_nonexecution", "compound_absence_as_nonexecution", "reporting_rule_with_noop"} and factual_client:
                projection = report.artifact["execution_projections"].get("answer", {})
                entry["passed"] = entry["passed"] and projection.get("has_execution_records") is False
                if name == "compound_absence_as_nonexecution":
                    entry["passed"] = entry["passed"] and projection.get("analysis_supported") is True and _price_prose in report.text
        except Exception as exc:
            entry["error"] = str(exc)
        entry["seconds"] = round(time.monotonic() - started, 2)
        (root/(name+"-calls.json")).write_text(json.dumps(calls, ensure_ascii=False, indent=2))
        (root/(name+"-writing-calls.json")).write_text(json.dumps(writing_calls, ensure_ascii=False, indent=2))
        (root/(name+"-factual-calls.json")).write_text(json.dumps(factual_calls, ensure_ascii=False, indent=2))
        result["cases"].append(entry)
        (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        if not entry["passed"]:
            break
    result["not_run"] = [name for name in selected if name not in {row["case"] for row in result["cases"]}]
    result["passed"] = not result["not_run"] and all(row["passed"] for row in result["cases"])
    (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
