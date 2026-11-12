"""Fixed, fail-closed LM Studio acceptance using synthetic receipts and real inference.

Run with the candidate wheel's Python, outside the checkout. No production config,
credentials, history, tools or deliveries are read or executed. Each continuation
uses a new Python process. Every planned case runs once; failures remain failures.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time


CASES = ("first", "continued", "large", "missing")
LARGE_SYMBOLS = ("000001", "000002", "000003", "000004", "000005", "000006", "000007", "000008", "000009", "002415")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def receipt_for(case):
    count = 30 if case == "large" else 2
    base = 31 if case == "continued" else 21
    return {"symbol": "002415", "timeframe": "1d", "adjustment": "unadjusted",
            "bars": [{"timestamp": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                      "open": base+i, "high": base+i+3, "low": base+i-2,
                      "close": base+i+1, "volume": 1000+i} for i in range(count)]}


def large_payload(arguments):
    """Use the production serializer and its 30-bar cap, freezing only the provider."""
    from datetime import datetime
    from types import SimpleNamespace
    from trade_compass_agent.domain import Bar
    from trade_compass_agent.runtime.tools.batch import tool_batch_get_bars

    class Provider:
        name = "acceptance-fixture"

        def get_bars(self, symbol, *, timeframe, limit):
            require(symbol in LARGE_SYMBOLS and timeframe == "1d", "invalid frozen request")
            return [Bar(symbol=symbol, timestamp=datetime.fromisoformat(b["timestamp"]),
                        open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                        volume=b["volume"], source=self.name) for b in receipt_for("large")["bars"]][-limit:]

    return tool_batch_get_bars(SimpleNamespace(provider=Provider()), **arguments)


def check_report(detail, artifact, receipt, *, missing):
    """Independent business oracle, not the model's own 'complete' assertion."""
    verification = detail["step_runs"][0]["data"]["verification"]
    require(verification["execution"] == "completed", "execution did not complete")
    if missing:
        require(detail["status"] == "degraded" and verification["report"] == "partial",
                "real missing revenue must stay partial")
        require(bool(verification["findings"]), "missing revenue has no finding")
        require("营收" in detail["analysis"], "missing revenue is not visible")
    else:
        require(detail["status"] == "completed" and verification["report"] == "complete"
                and not verification["findings"], "normal report failed availability criterion")
    bars = receipt["bars"][-2:]
    facts = {f["id"]: f for f in artifact["facts"]}
    for item in artifact["document"]["items"]:
        if item["type"] == "fact":
            require(facts[item["ref"]]["subject"] == "002415", "published observation belongs to another instrument")
        if item["type"] == "calculation":
            require(all(facts[ref]["subject"] == "002415" for ref in item["refs"]),
                    "published calculation belongs to another instrument")
    expected = [(str(b["close"]), b["timestamp"]) for b in bars]
    found = False
    for calc in artifact["calculations"].values():
        if calc["operation"] != "change_pct":
            continue
        operands = [facts.get(ref, {}) for ref in calc["refs"]]
        if all(f.get("metric") == "close" and f.get("subject") == "002415" for f in operands):
            actual = [(str(f["value"]), f["observed_at"][:10]) for f in operands]
            if actual == expected:
                wanted = (Decimal(expected[1][0]) / Decimal(expected[0][0]) - 1) * 100
                found = abs(Decimal(calc["result"]) - wanted) < Decimal("0.000001")
    require(found, "requested closing-price change is absent or uses wrong metrics/dates")
    for value, timestamp in expected:
        require(value in detail["analysis"] and timestamp in detail["analysis"],
                "dated closing prices are absent from the published report")
    if not missing:
        require("研判：" in detail["analysis"], "requested price-direction judgment is absent")


def worker(root, case, model):
    from dataclasses import replace
    from unittest.mock import patch

    state = root / ("continued-state" if case in {"first", "continued"} else case + "-state")
    state.mkdir(exist_ok=True)
    os.chdir(state)
    for key in list(os.environ):
        if key.startswith("TRADE_COMPASS_"):
            del os.environ[key]
    (state / ".env").touch()
    (state / "config.yaml").write_text("llm:\n  provider: disabled\n")
    os.environ.update(TRADE_COMPASS_HOME=str(state), TRADE_COMPASS_CONFIG=str(state/"config.yaml"),
                      TRADE_COMPASS_ENV_FILE=str(state/".env"),
                      TRADE_COMPASS_DATA_DIR=str(state/"data"),
                      TRADE_COMPASS_MEMORY_DIR=str(state/"memory"), TRADE_COMPASS_NO_SCHEDULER="true")
    from fastapi.testclient import TestClient
    from trade_compass_agent.config import AppConfig, AgentConfig, LLMConfig
    from trade_compass_agent.llm.providers import create_chat_client, llm_request_deadline
    from trade_compass_agent.ops.prompt_jobs import PromptJob, PromptJobExecutor
    from trade_compass_agent.ops.run_store import SqliteRunStore
    from trade_compass_agent.runtime.loop import AgentLoop
    from trade_compass_agent.web.app import create_app

    config = AppConfig(data_dir=state/"data", memory_dir=state/"memory", data_provider="sample",
                       llm=LLMConfig(provider="lmstudio", model=model, api_key_env="", max_retries=0, timeout=180),
                       agent=AgentConfig(learning_enabled=False, llm_session_titles=False, max_tool_rounds=3))
    config = replace(config, scheduler=replace(config.scheduler, enabled=False),
                     mobile=replace(config.mobile, enabled=False))
    receipt = receipt_for(case)
    dates = [b["timestamp"] for b in receipt["bars"][-2:]]
    prompt = (f"只调用一次get_bars读取002415日K线。仅报告返回数据中{dates[0]}和{dates[1]}的日期及收盘价，"
              "计算这两日收盘价变动比例并判断上升或下降。不预测未来，不交易、不写记忆、不通知。")
    expected_tool = "get_bars"
    if case == "large":
        expected_tool = "batch_get_bars"
        prompt = prompt.replace("只调用一次get_bars读取002415日K线。",
            "只调用一次batch_get_bars，symbols=" + ",".join(LARGE_SYMBOLS)
            + "，limit=30，summary_only=false，读取这些标的日K线。仅分析002415。")
    if case == "missing":
        prompt += "还须比较2025年与2024年全年营收；若回执没有营收，明确保留缺口，不调用其他工具。"
    job = PromptJob(id="release-report", name="release acceptance", prompt=prompt,
                    schedule="sat 23:59", enabled=False, delivery_channels=())
    previous = {p.name: p.read_bytes() for p in (state/"data/agent_sessions").glob("*.jsonl")}
    tools, calls = [], []
    real = create_chat_client(config)
    profile = {"provider": real.name, "model": real.model,
               "context_window": real.context_window, "response_token_limit": real.response_token_limit}

    class Client:
        def __getattr__(self, name):
            return getattr(real, name)

        def stream_complete(self, messages, **kwargs):
            require(len(calls) < 8, "fixed model-call budget exceeded")
            row = {"tools_available": bool(kwargs.get("tools"))}
            if row["tools_available"]:
                row["granted_tools"] = [tool["function"]["name"] for tool in kwargs["tools"]]
                require(row["granted_tools"] == [expected_tool], "executor capability scope was not applied")
            calls.append(row)
            answer = real.stream_complete(messages, **kwargs)
            row.update(content=answer.content, tool_calls=[{"name": t.name, "arguments": t.arguments} for t in answer.tool_calls])
            (root/f"{case}-calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2))
            return answer

        complete = stream_complete

    def frozen(_registry, name, arguments):
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        tools.append(name)
        require(tools == [expected_tool] and args.get("timeframe", "1d") == "1d"
                and type(args.get("limit", 60)) is int and args.get("limit", 60) >= len(receipt["bars"]),
                "unexpected business tool, insufficient window or repeated read")
        if case == "large":
            require(set(args.get("symbols", "").split(",")) == set(LARGE_SYMBOLS)
                    and args.get("summary_only") is False, "large fixture requires full batch data")
            return large_payload(args)
        require(args.get("symbol") == "002415", "unexpected instrument")
        return json.dumps(receipt)

    def deny_delivery(_router, _run, delivery):
        require(not delivery.channels, "acceptance cannot deliver notifications")

    with patch("trade_compass_agent.runtime.loop.create_chat_client", lambda _: Client()), \
         patch("trade_compass_agent.runtime.loop.shared_consolidation_worker", lambda **_: None), \
         patch.object(AgentLoop, "_maybe_background_review", lambda *a, **kw: None), \
         patch("trade_compass_agent.runtime.tools.registry.ToolRegistry.execute", frozen), \
         patch("trade_compass_agent.ops.delivery.DeliveryRouter.deliver", deny_delivery), \
         patch("trade_compass_agent.web.api.load_app_config", lambda: config), \
         patch("trade_compass_agent.web.agent_api.load_app_config", lambda: config), \
         llm_request_deadline(time.monotonic()+240):
        runs = SqliteRunStore(config.data_dir/"scheduler.db")
        PromptJobExecutor(config, runs, tool_whitelist={expected_tool}).execute(job)
        run = runs.recent_runs(job_id="custom:"+job.id)[0]
        api = TestClient(create_app())
        response = api.get("/api/jobs/runs/"+run.id)
        require(response.status_code == 200, "run unavailable through API")
        detail = response.json()
        (root/f"{case}-run.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2))
        require(tools == [expected_tool], "current run did not fetch its own evidence")
        for name, contents in previous.items():
            require((state/"data/agent_sessions"/name).read_bytes().startswith(contents), "history rewritten")
        verification = detail["step_runs"][0]["data"]["verification"]
        artifact = json.loads((state/"data/report_artifacts"/(verification["artifact_id"]+".json")).read_text())
        check_report(detail, artifact, receipt, missing=case == "missing")
        sid = detail["step_runs"][0]["data"]["session_id"]
        if case == "continued":
            first = json.loads((root/"first-run.json").read_text())
            require(first["step_runs"][0]["data"]["session_id"] == sid, "continuation changed daily session")
            require(api.get("/api/jobs/runs/"+first["id"]).json() == first, "prior run changed after restart")
        messages = api.get("/api/agent/sessions/"+sid).json()["messages"]
        require(messages[-1]["content"] == detail["analysis"], "session and report disagree")
        return {"case": case, "passed": True, "run_id": run.id, "status": detail["status"],
                "model_calls": len(calls), "tools": tools, "real_business_calls": 0, "profile": profile}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--worker", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root, wheel = args.output.resolve(), args.wheel.resolve()
    os.umask(0o077)
    if args.worker:
        try:
            result = worker(root, args.worker, args.model)
        except Exception as exc:
            result = {"case": args.worker, "passed": False, "error": str(exc)}
        (root/f"{args.worker}-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["passed"] else 1)
    require(not root.is_relative_to(Path(__file__).resolve().parents[1]), "output must be outside checkout")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chdir(root)
    verify = runpy.run_path(str(Path(__file__).with_name("release_acceptance.py")))["verify_installed_wheel"]
    result = {"passed": False, "mode": "real-model", "provider": "lmstudio", "model": args.model,
              "cases": [], "case_plan": list(CASES), "retries": 0, "business_tools": "synthetic frozen",
              "capability_profile": "executor whitelist: only each case's frozen query tool",
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        result.update(verify(wheel))
        # Freeze the plan before any inference; retain every result, including failures.
        (root/"plan.json").write_text(json.dumps(result, indent=2))
        for case in CASES:
            require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == result["harness_sha256"],
                    "harness changed during acceptance")
            with (root/f"{case}.log").open("w") as log:
                try:
                    child = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()),
                        "--worker", case, "--wheel", str(wheel), "--output", str(root), "--model", args.model],
                        stdout=log, stderr=subprocess.STDOUT, timeout=270)
                    path = root/f"{case}-result.json"
                    entry = json.loads(path.read_text()) if path.exists() else {"case": case, "passed": False}
                    entry["passed"] = entry["passed"] and child.returncode == 0
                except subprocess.TimeoutExpired:
                    entry = {"case": case, "passed": False, "error": "wall deadline exceeded"}
            result["cases"].append(entry)
        result["passed"] = all(c["passed"] for c in result["cases"])
    finally:
        (root/"acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
