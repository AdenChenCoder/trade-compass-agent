"""Local model curation against frozen receipts, using an exact installed wheel.

Each case starts a fresh process; continued use retains the same isolated vault.
No business tools, production state, cloud models or delivery are available.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time


CASES = {
    "natural": "收盘价由22元升至23元，涨幅4.55%。",
    "terse": "22元，后23元。涨了4.55%。",
    "continued": "最新两个观察日收盘价先32元，后33元，涨幅3.12%。",
    "missing_source": "收盘价由22元升至23元，涨幅4.55%。",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def worker(args):
    from trade_compass_agent.config import AppConfig, LLMConfig
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client, llm_request_deadline
    from trade_compass_agent.runtime.learning import curate_turn_insight
    from trade_compass_agent.runtime.report_publication import Publication, persist_publication
    from trade_compass_agent.runtime.tools.memory import tool_search_memory
    from trade_compass_agent.runtime.types import TurnResponse

    root = args.output
    config = AppConfig(data_dir=root / "data", memory_dir=root / "memory",
        llm=LLMConfig(provider="lmstudio", model=args.model, api_key_env="", max_retries=0, timeout=240))
    config = replace(config, agent=replace(config.agent, learning_enabled=True), allow_external_llm_memory=True)
    before = {str(p): p.read_bytes() for p in config.memory_dir.rglob("*.md")}
    base = 32 if args.case == "continued" else 22
    dates = ["2026-11-12", "2026-11-13"] if args.case == "continued" else ["2026-11-10", "2026-11-11"]
    raw = json.dumps({"symbol": "002415", "timeframe": "1d", "bars": [
        {"timestamp": day, "close": base + i} for i, day in enumerate(dates)]})
    run_id = f"learning-acceptance-{args.case}"
    verification = {"status": "complete"}
    # A frozen upstream record is the fixture, not evidence of model accuracy.
    original = Publication("frozen source", verification, {"schema_version": 1, "run_id": run_id,
        "verification": verification, "receipts": [{"tool": "get_bars", "result": raw,
                                                     "sha256": hashlib.sha256(raw.encode()).hexdigest()}]})
    reference = {**verification, **persist_publication(config.data_dir, original)}
    if args.case == "missing_source":
        (config.data_dir / "report_artifacts" / (reference["artifact_id"] + ".json")).unlink()
    client = create_chat_client(config)
    calls = []
    def call(system, user):
        started = time.monotonic()
        row = {"system": system, "input": json.loads(user)}
        calls.append(row)
        try:
            result = client.stream_complete([ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=user)], tools=None, on_delta=lambda _: None)
            row["output"] = result.content
            return result.content or ""
        finally:
            row["elapsed"] = time.monotonic() - started
    result = {"case": args.case, "passed": False, "calls": calls, "prior_memories": len(before)}
    try:
        with llm_request_deadline(time.monotonic() + 240):
            path = curate_turn_insight(config=config, user_message="比较两个观察日的收盘价与变化",
                response=TurnResponse(session_id="acceptance", turn_id=args.case,
                    summary=CASES[args.case], verification=reference), report_run_id=run_id, report_call=call)
        require(all(Path(p).read_bytes() == content for p, content in before.items()), "existing memory changed")
        if args.case == "missing_source":
            require(path is None and not calls, "missing evidence reached curation or storage")
        else:
            require(path is not None, "normal curation did not create an accepted memory")
            hits = json.loads(tool_search_memory(config.memory_dir, "002415"))["results"]
            memory = next((h for h in hits if h["path"] == path), None)
            require(memory is not None, "saved memory is not searchable after reopen")
            artifacts = [json.loads(p.read_text()) for p in (config.data_dir / "report_artifacts").glob("*.json")]
            curated = [a for a in artifacts if a.get("learning_source", {}).get("turn_id") == args.case]
            require(len(curated) == 1, "missing or duplicate curation artifact")
            artifact = curated[0]
            require(artifact["verification"]["status"] == "complete", "curation not complete")
            require(artifact["published_text"] == memory["content"], "archive differs from verified result")
            facts = {f["id"]: f for f in artifact["facts"]}
            items = artifact["document"]["items"]
            refs = [i["ref"] for i in items if i["type"] == "fact"]
            refs += [r for i in items if i["type"] == "calculation" for r in i["refs"]]
            require(bool(refs), "curation has no retained factual observation")
            require(all(facts[r]["subject"] == "002415" and facts[r]["metric"] == "close"
                        and facts[r]["unit"] == "元/股" and facts[r]["observed_at"] in dates
                        and facts[r]["value"] == str(base + dates.index(facts[r]["observed_at"]))
                        for r in refs), "curated facts changed identity, unit, date or value")
            require(len(calls) <= 4, "curation exceeded call budget")
            result["memory"] = memory
            result["artifact"] = artifact
            # Observe real local provider calls; this wrapper does not supply
            # model responses. Invalid JSON/fallback cannot pass normal scenes.
            from unittest.mock import patch
            from trade_compass_agent.runtime import learning
            from trade_compass_agent.memory.tree.storage import MemoryTreeStore
            original_provider = learning.create_llm_provider
            selection_calls = []
            result["compaction"] = {"calls": selection_calls}
            def recorded_provider(**kwargs):
                provider = original_provider(**kwargs)
                complete = provider.complete
                def recorded(request):
                    record = {"input": request.prompt}
                    selection_calls.append(record)
                    try:
                        answer = complete(request)
                        record["output"] = answer.text
                        return answer
                    except Exception as exc:
                        record["error_type"] = type(exc).__name__
                        raise
                provider.complete = recorded
                return provider
            records = MemoryTreeStore(config.memory_dir).recent_chunks(limit=20, max_chars=6000,
                exclude_scopes=frozenset({"compaction"}))
            with patch.object(learning, "create_llm_provider", recorded_provider), llm_request_deadline(time.monotonic() + 90):
                compacted = learning.compact_memory_summary(config)
            require(compacted is not None and len(selection_calls) == 1, "real compaction did not select records")
            selection = json.loads(selection_calls[0]["output"])
            require(set(selection) == {"selected"} and isinstance(selection["selected"], list)
                    and 0 < len(selection["selected"]) <= 3
                    and all(key in {f"m{i}" for i in range(len(records))} for key in selection["selected"])
                    and len(set(selection["selected"])) == len(selection["selected"]),
                    "compaction fell back from invalid model output")
            content = compacted.read_text()
            require(all(records[int(key[1:])].content in content for key in selection["selected"]),
                    "compaction changed or truncated a selected record")
            require(all(record.path.relative_to(config.memory_dir).as_posix() not in content
                        for i, record in enumerate(records) if f"m{i}" not in selection["selected"]),
                    "compaction added unselected records")
            require(all(Path(p).read_bytes() == data for p, data in before.items()), "compaction changed existing history")
            result["compaction"] = {"calls": selection_calls, "path": str(compacted), "content": content}
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        (root / f"{args.case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.wheel = args.wheel.resolve()
    script = Path(__file__).resolve()
    require(not args.output.is_relative_to(script.parents[1]), "output must be outside checkout")
    if args.case:
        raise SystemExit(0 if worker(args) else 1)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chdir(args.output)
    verify = runpy.run_path(str(script.with_name("release_acceptance.py")))["verify_installed_wheel"]
    result = {"passed": False, "artifact": verify(args.wheel), "model": args.model, "cases": [],
              "fixtures": "frozen upstream receipts", "model_execution": "real local LM Studio",
              "business_calls": 0, "retries": 0, "plan": CASES,
              "harness_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
    (args.output / "plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRADE_COMPASS_") and k != "PYTHONPATH"}
    (args.output / "config.yaml").write_text("llm:\n  provider: disabled\ndata_provider: sample\n")
    (args.output / ".env").touch()
    env.update(TRADE_COMPASS_HOME=str(args.output), TRADE_COMPASS_CONFIG=str(args.output / "config.yaml"),
        TRADE_COMPASS_ENV_FILE=str(args.output / ".env"), TRADE_COMPASS_DATA_DIR=str(args.output / "data"),
        TRADE_COMPASS_MEMORY_DIR=str(args.output / "memory"), TRADE_COMPASS_NO_SCHEDULER="true")
    for case in CASES:
        with (args.output / f"{case}.log").open("w") as log:
            try:
                completed = subprocess.run([sys.executable, "-I", str(script), "--wheel", str(args.wheel),
                    "--output", str(args.output), "--model", args.model, "--case", case], env=env,
                    stdout=log, stderr=subprocess.STDOUT, timeout=270)
                result["cases"].append({"case": case, "passed": completed.returncode == 0})
            except subprocess.TimeoutExpired:
                result["cases"].append({"case": case, "passed": False, "error": "worker timeout"})
        (args.output / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    result["passed"] = all(row["passed"] for row in result["cases"])
    (args.output / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
