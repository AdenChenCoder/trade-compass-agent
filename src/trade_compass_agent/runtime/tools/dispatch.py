from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from trade_compass_agent.config import AppConfig
from trade_compass_agent.runtime.types import TurnEvent
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
from trade_compass_agent.runtime.specialists.run import run_specialist
from trade_compass_agent.runtime.specialists.situation import build_situation_summary


def tool_dispatch_specialists(
    stack: MarketStack,
    tasks: list[dict],
    *,
    config: AppConfig | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
) -> str:
    if not isinstance(tasks, list):
        return json.dumps({"error": "tasks must be a list"}, ensure_ascii=False)

    app_config = config or stack.config

    if len(tasks) <= 1:
        raw = _run_sequential(stack, tasks, app_config=app_config, on_event=on_event)
    else:
        raw = _run_parallel(stack, tasks, app_config=app_config, on_event=on_event)

    return _attach_situation_summary(raw, stack, on_event)


def _run_sequential(
    stack: MarketStack,
    tasks: list[dict],
    *,
    app_config: AppConfig,
    on_event: Callable[[TurnEvent], None] | None,
) -> str:
    results: list[dict] = []
    for item in tasks:
        if not isinstance(item, dict):
            results.append({"error": "invalid task item", "task": str(item)})
            continue
        name = str(item.get("specialist") or item.get("name") or "")
        task = str(item.get("task") or item.get("message") or "")
        if not name:
            results.append({"error": "missing specialist name", "task": task})
            continue
        if on_event:
            on_event(
                TurnEvent(
                    event="specialist_started",
                    data=_specialist_event_data(name, task),
                )
            )
        try:
            output = run_specialist(
                stack, name, task, config=app_config, on_event=on_event,
            )
        except Exception as exc:
            output = json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            )
        status = _detect_status(output)
        if on_event:
            on_event(
                TurnEvent(
                    event="specialist_done",
                    data={
                        **_specialist_event_data(name, task),
                        "preview": output[:500],
                        "status": status,
                    },
                )
            )
        results.append({"specialist": name, "task": task, "output": output})
    return json.dumps({"results": results}, ensure_ascii=False)


def _run_parallel(
    stack: MarketStack,
    tasks: list[dict],
    *,
    app_config: AppConfig,
    on_event: Callable[[TurnEvent], None] | None,
) -> str:
    results: list[dict] = [{}] * len(tasks)
    event_lock = threading.Lock()

    def _safe_emit(evt: TurnEvent) -> None:
        if on_event:
            with event_lock:
                on_event(evt)

    for item in tasks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("specialist") or item.get("name") or "")
        task_text = str(item.get("task") or item.get("message") or "")
        if name:
            _safe_emit(
                TurnEvent(
                    event="specialist_started",
                    data=_specialist_event_data(name, task_text),
                )
            )

    def _invoke(idx: int, item: dict) -> tuple[int, dict]:
        if not isinstance(item, dict):
            return idx, {"error": "invalid task item", "task": str(item)}
        name = str(item.get("specialist") or item.get("name") or "")
        task_text = str(item.get("task") or item.get("message") or "")
        if not name:
            return idx, {"error": "missing specialist name", "task": task_text}
        try:
            output = run_specialist(
                stack, name, task_text, config=app_config, on_event=_safe_emit,
            )
        except Exception as exc:
            output = json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            )
        status = _detect_status(output)
        _safe_emit(
            TurnEvent(
                event="specialist_done",
                data={
                    **_specialist_event_data(name, task_text),
                    "preview": output[:500],
                    "status": status,
                },
            )
        )
        return idx, {"specialist": name, "task": task_text, "output": output}

    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as pool:
        futures = [pool.submit(_invoke, i, item) for i, item in enumerate(tasks)]
        for future in as_completed(futures):
            try:
                idx, result = future.result()
            except Exception as exc:
                idx = 0
                result = {"error": f"future failed: {type(exc).__name__}: {exc}"}
            results[idx] = result

    return json.dumps({"results": results}, ensure_ascii=False)


def _detect_status(output: str) -> str:
    """Detect error status from structured JSON output."""
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict) and "error" in parsed:
            return "error"
    except (json.JSONDecodeError, TypeError):
        pass
    return "ok"


def _specialist_event_data(name: str, task: str) -> dict[str, str]:
    profile = load_specialist_profiles().get(name)
    data = {
        "specialist": name,
        "task": task,
        "label": name,
        "kind": "specialist",
    }
    if profile is not None:
        data["execution_model"] = profile.execution_model.type
        if profile.execution_model.plan:
            data["plan"] = profile.execution_model.plan
    return data


def _attach_situation_summary(
    raw_results: str,
    stack: MarketStack,
    on_event: Callable[[TurnEvent], None] | None,
) -> str:
    """Append a situation summary to multi-specialist dispatch results."""
    try:
        parsed = json.loads(raw_results)
        results = parsed.get("results", [])
        if len(results) < 2:
            return raw_results

        if on_event:
            on_event(TurnEvent(event="summariser_started", data={"label": "situation_summariser"}))

        summary = build_situation_summary(stack)
        parsed["situation_summary"] = summary

        if on_event:
            on_event(TurnEvent(event="summariser_done", data={"label": "situation_summariser"}))

        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return raw_results
