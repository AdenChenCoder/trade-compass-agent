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
    "natural": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", True),
    "reordered": ("后续走势不能据此判断；两个观察日之间收盘价上升。", True),
    "correct_unit": ("前一日收盘价为22元/股，后一日为23元/股。", True),
    "changed_direction_evidence": ("在这两个观察日，收盘价走高；不能据此预测后续走势。", False),
    "wrong_identity": ("上述价格属于000001。", False),
    "wrong_date": ("上述两项收盘价分别来自2025年1月3日和1月4日。", False),
    "wrong_unit": ("前一日收盘价为22美元，后一日为23美元。", False),
    "unsupported_cause": ("营收增长导致了这次上涨。", False),
    "hidden_result": ("收盘价上升。", True),
    "visible_result": ("收盘价上升。", True),
    "historical_execution": ("本轮已经买入002415共100股，成交价22元/股。", False),
}


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
        case_receipts = list(receipts)
        if name == "changed_direction_evidence":
            changed = json.loads(case_receipts[0][1])
            changed["bars"][0]["close"], changed["bars"][1]["close"] = 23, 22
            case_receipts[0] = ("get_bars", json.dumps(changed))
        calls = []
        def call(system, user):
            payload = json.loads(user)
            if "schema" in payload:
                facts = [dict(zip(payload["fact_columns"], row, strict=True))
                         for group in payload["facts"] if group["metric"] == "close" for row in group["rows"]]
                parts = [{"type": "value", "ref": facts[0]["id"], "field": "subject"},
                         {"type": "text", "text": "的收盘价从"},
                         {"type": "value", "ref": facts[0]["id"], "field": "value"},
                         {"type": "text", "text": "变为"},
                         {"type": "value", "ref": facts[1]["id"], "field": "value"},
                         {"type": "text", "text": "。" + prose}]
                historical_refs = [row[0] for group in payload["facts"]
                                   if group["source"] == "analyze_portfolio" for row in group["rows"]]
                if name == "historical_execution" and not historical_refs:
                    raise RuntimeError("historical execution fixture lost its source")
                definitions = []
                refs = historical_refs if name == "historical_execution" else []
                if name in {"hidden_result", "visible_result"}:
                    definitions = [{"type": "calculation", "id": "change", "section": "overview",
                                    "operation": "change_pct", "refs": [facts[0]["id"], facts[1]["id"]]}]
                    refs = ["change"]
                    if name == "visible_result":
                        parts.extend([{"type": "text", "text": "收盘价变动比例为"},
                                      {"type": "value", "ref": "change", "field": "value"},
                                      {"type": "text", "text": "。"}])
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
                task = "说明这两个观察日收盘价变化，仅在存在本轮执行记录时报告成交。"
                if name in {"hidden_result", "visible_result"}:
                    task += "给出收盘价变动比例。"
                report = publish_report(task=task,
                    draft="", tool_results=case_receipts, current_tool_results=case_receipts,
                    run_scope="semantic-"+name, call=call)
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
