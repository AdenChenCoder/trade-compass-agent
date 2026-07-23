from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain.signals import SignalRating, TradingSignal, parse_signal_rating
from trade_compass_agent.llm.providers import ChatClient, ChatMessage, create_chat_client
from trade_compass_agent.runtime.exceptions import AgentUnavailableError
from trade_compass_agent.runtime.specialists.multi_agent.assets import (
    MultiAgentPlanAsset,
    PlanAgentAsset,
    PlanAssetError,
    load_multi_agent_plan_asset,
)
from trade_compass_agent.runtime.specialists.multi_agent.types import (
    MultiAgentRunResult,
    RunState,
)
from trade_compass_agent.runtime.specialists.nested_loop import run_react_loop
from trade_compass_agent.runtime.specialists.situation import build_situation_summary
from trade_compass_agent.runtime.tools.policy import default_tool_policy
from trade_compass_agent.runtime.tools.readers import READER_TOOL_SCHEMAS, run_reader_tool
from trade_compass_agent.runtime.tools.registry import ToolRegistry
from trade_compass_agent.runtime.types import TurnEvent


class DebateTeamStrategy:
    name = "debate_team"

    def run(self, state: RunState) -> MultiAgentRunResult:
        plan = state.team.plan
        app_config = state.config or state.stack.config
        try:
            plan_asset = load_multi_agent_plan_asset(state.team.id, plan)
            _validate_plan_asset(plan_asset, strategy=self.name)
        except PlanAssetError as exc:
            return _error_result(
                state,
                plan=plan,
                error=f"plan asset unavailable: {exc}",
                warning=str(exc),
            )

        symbols = _extract_symbols(state.task)
        if not symbols:
            return MultiAgentRunResult(
                output='{"error": "no symbol found in task"}',
                metadata={
                    "symbols": [],
                    "strategy": self.name,
                    "plan": plan,
                    "plan_asset": plan_asset.id,
                    "plan_asset_version": plan_asset.version,
                },
                warnings=("no symbol found in task",),
            )

        try:
            client = _chat_client(state.client, app_config)
        except AgentUnavailableError as exc:
            return _error_result(
                state,
                plan=plan,
                plan_asset=plan_asset,
                error=f"debate_team unavailable: {exc}",
                warning=str(exc),
            )

        reports: list[str] = []
        for symbol in symbols:
            state.record("multi_agent.symbol_started", {"symbol": symbol})
            report = _run_symbol_debate(
                state,
                plan_asset=plan_asset,
                symbol=symbol,
                client=client,
                app_config=app_config,
            )
            if len(symbols) > 1:
                report = f"# {symbol}\n\n{report}"
            reports.append(report)
            state.record(
                "multi_agent.symbol_finished",
                {"symbol": symbol, "bytes": len(report.encode("utf-8"))},
            )

        return MultiAgentRunResult(
            output="\n\n---\n\n".join(reports),
            metadata={
                "symbols": symbols,
                "strategy": self.name,
                "plan": plan,
                "plan_asset": plan_asset.id,
                "plan_asset_version": plan_asset.version,
            },
        )


def _run_symbol_debate(
    state: RunState,
    *,
    plan_asset: MultiAgentPlanAsset,
    symbol: str,
    client: ChatClient,
    app_config: AppConfig,
) -> str:
    context = str(state.metadata.get("context") or "")
    if not context:
        context = build_situation_summary(state.stack)

    registry = ToolRegistry(state.stack, on_event=state.on_event)
    variables: dict[str, str] = {
        "symbol": symbol,
        "context": context,
        "research_context": "",
        "rebuttal_context": "",
        "pm_verdict": "",
        "risk_challenge": "",
        "portfolio_state": "",
    }

    research_reports = _run_research_stage(
        state,
        plan_asset=plan_asset,
        symbol=symbol,
        context=context,
        client=client,
        registry=registry,
        variables=variables,
    )
    research_brief = _format_research(research_reports)
    variables["research_context"] = research_brief

    rounds = _run_debate_stage(
        state,
        plan_asset=plan_asset,
        symbol=symbol,
        context=context,
        client=client,
        registry=registry,
        variables=variables,
        research_brief=research_brief,
    )
    debate_record = _format_debate(rounds)
    variables["rebuttal_context"] = debate_record

    pm_verdict = _run_synthesis_stage(
        state,
        plan_asset=plan_asset,
        symbol=symbol,
        context=context,
        client=client,
        registry=registry,
        variables=variables,
        research_brief=research_brief,
        debate_record=debate_record,
    )
    variables["pm_verdict"] = pm_verdict

    signal = _parse_verdict(symbol, pm_verdict)
    risk_note = ""
    if signal and _risk_stage_enabled(plan_asset) and signal.rating in (
        SignalRating.BUY,
        SignalRating.STRONG_BUY,
    ):
        risk_challenge, pm_defense = _run_risk_stage(
            state,
            plan_asset=plan_asset,
            symbol=symbol,
            context=context,
            client=client,
            registry=registry,
            variables=variables,
            pm_verdict=pm_verdict,
        )
        if risk_challenge:
            risk_note += f"\n\n## 风控质询\n\n{risk_challenge}"
        if pm_defense:
            risk_note += f"\n\n## PM 回应\n\n{pm_defense}"
            defense_signal = _parse_verdict(symbol, pm_defense)
            if defense_signal:
                signal = defense_signal

    report = _format_report(research_reports, rounds, pm_verdict) + risk_note
    if signal and signal.rating in (SignalRating.BUY, SignalRating.STRONG_BUY):
        from trade_compass_agent.runtime.specialists.risk_controls import apply_risk_warnings

        warned = apply_risk_warnings(
            state.stack,
            signal,
            config=app_config,
            on_event=state.on_event,
        )
        if warned.reasoning != signal.reasoning:
            report += f"\n\n> **风险提示**: {warned.reasoning[len(signal.reasoning):]}"
    return report


def _run_research_stage(
    state: RunState,
    *,
    plan_asset: MultiAgentPlanAsset,
    symbol: str,
    context: str,
    client: ChatClient,
    registry: ToolRegistry,
    variables: dict[str, str],
) -> dict[str, str]:
    if not plan_asset.stage_enabled("research", True):
        return {}
    agents = _stage_agents(plan_asset, "research")
    if not agents:
        return {}

    results: dict[str, str] = {}
    event_lock = threading.Lock()

    def _emit(evt: TurnEvent) -> None:
        if state.on_event:
            with event_lock:
                state.on_event(evt)

    def _run(agent: PlanAgentAsset) -> tuple[str, str]:
        _emit(
            TurnEvent(
                event="multi_agent.agent_started",
                data={
                    "phase": "research",
                    "agent": agent.id,
                    "symbol": symbol,
                    "team_id": state.team.id,
                },
            )
        )
        output = _run_agent(
            client=client,
            registry=registry,
            agent=agent,
            variables=variables,
            user_content=f"## 市场情境\n{context}\n\n请分析 {symbol}。",
            max_rounds=_int_default(plan_asset, "analyst_max_tool_rounds", 4),
            on_event=_emit,
        )
        _emit(
            TurnEvent(
                event="multi_agent.agent_finished",
                data={
                    "phase": "research",
                    "agent": agent.id,
                    "symbol": symbol,
                    "bytes": len(output.encode("utf-8")),
                    "team_id": state.team.id,
                },
            )
        )
        return agent.role or agent.id, output

    with ThreadPoolExecutor(max_workers=max(1, min(len(agents), 4))) as pool:
        futures = [pool.submit(_run, agent) for agent in agents]
        for future in as_completed(futures):
            try:
                label, output = future.result()
            except Exception as exc:
                label = "agent_error"
                output = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            results[label] = output
    return results


def _run_debate_stage(
    state: RunState,
    *,
    plan_asset: MultiAgentPlanAsset,
    symbol: str,
    context: str,
    client: ChatClient,
    registry: ToolRegistry,
    variables: dict[str, str],
    research_brief: str,
) -> list[dict[str, str]]:
    if not plan_asset.stage_enabled("debate", True):
        return []
    agents = _stage_agents(plan_asset, "debate")
    rounds: list[dict[str, str]] = []
    for round_index in range(_int_default(plan_asset, "max_debate_rounds", 2)):
        current: dict[str, str] = {}
        variables["rebuttal_context"] = _format_debate(rounds)
        for agent in agents:
            output = _run_agent(
                client=client,
                registry=registry,
                agent=agent,
                variables=variables,
                user_content=(
                    f"## 市场情境\n{context}\n\n"
                    f"{research_brief}\n\n"
                    f"{_format_debate(rounds)}\n\n"
                    f"请完成第 {round_index + 1} 轮观点。"
                ),
                max_rounds=_int_default(plan_asset, "debater_max_tool_rounds", 3),
                on_event=state.on_event,
            )
            current[agent.id] = output
        if current:
            rounds.append(current)
    return rounds


def _run_synthesis_stage(
    state: RunState,
    *,
    plan_asset: MultiAgentPlanAsset,
    symbol: str,
    context: str,
    client: ChatClient,
    registry: ToolRegistry,
    variables: dict[str, str],
    research_brief: str,
    debate_record: str,
) -> str:
    if not plan_asset.stage_enabled("synthesis", True):
        return ""
    agents = _stage_agents(plan_asset, "synthesis")
    if not agents:
        return ""
    return _run_agent(
        client=client,
        registry=registry,
        agent=agents[0],
        variables=variables,
        user_content=(
            f"## 市场情境\n{context}\n\n"
            f"{research_brief}\n\n"
            f"{debate_record}\n\n"
            f"请给出 {symbol} 的最终判决。"
        ),
        max_rounds=1,
        on_event=state.on_event,
    )


def _run_risk_stage(
    state: RunState,
    *,
    plan_asset: MultiAgentPlanAsset,
    symbol: str,
    context: str,
    client: ChatClient,
    registry: ToolRegistry,
    variables: dict[str, str],
    pm_verdict: str,
) -> tuple[str, str]:
    agents = _stage_agents(plan_asset, "risk_crossexam")
    if not agents:
        return "", ""
    try:
        portfolio_state = registry.execute("analyze_portfolio", {})
    except Exception as exc:
        portfolio_state = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    variables["portfolio_state"] = portfolio_state
    variables["pm_verdict"] = pm_verdict

    risk_challenge = _run_agent(
        client=client,
        registry=registry,
        agent=agents[0],
        variables=variables,
        user_content=(
            f"## 市场情境\n{context}\n\n"
            f"## PM 判决\n{pm_verdict}\n\n"
            f"## 当前持仓\n{portfolio_state}\n\n"
            f"请质询 {symbol} 的风险。"
        ),
        max_rounds=2,
        on_event=state.on_event,
    )
    variables["risk_challenge"] = risk_challenge
    pm_defense = ""
    if len(agents) > 1:
        pm_defense = _run_agent(
            client=client,
            registry=registry,
            agent=agents[1],
            variables=variables,
            user_content=(
                f"## 市场情境\n{context}\n\n"
                f"## 原始判决\n{pm_verdict}\n\n"
                f"## 风控质询\n{risk_challenge}\n\n"
                "请回应风控质询并给出最终决定。"
            ),
            max_rounds=1,
            on_event=state.on_event,
        )
    return risk_challenge, pm_defense


def _run_agent(
    *,
    client: ChatClient,
    registry: ToolRegistry,
    agent: PlanAgentAsset,
    variables: dict[str, str],
    user_content: str,
    max_rounds: int,
    on_event: Callable[[TurnEvent], None] | None,
) -> str:
    messages = [
        ChatMessage(role="system", content=_render(agent.prompt, variables)),
        ChatMessage(role="user", content=user_content),
    ]
    tool_schemas = _tool_schemas(registry, set(agent.tools))

    def _emit(event: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(TurnEvent(event=event, data={**data, "agent": agent.id}))

    return run_react_loop(
        client=client,
        messages=messages,
        tool_schemas=tool_schemas,
        execute_tool=lambda name, args: _execute_tool(registry, agent, name, args),
        max_rounds=max_rounds,
        on_tool_event=_emit,
    )


def _execute_tool(
    registry: ToolRegistry,
    agent: PlanAgentAsset,
    name: str,
    args: dict[str, Any],
) -> str:
    try:
        descriptor = default_tool_policy().require_allowed(name, set(agent.tools))
    except Exception as exc:
        return json.dumps({"error": str(exc), "tool": name, "agent": agent.id}, ensure_ascii=False)
    if descriptor.category == "reader":
        try:
            return run_reader_tool(name, **args)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}", "tool": name}, ensure_ascii=False)
    return registry.execute(name, args)


def _tool_schemas(registry: ToolRegistry, allowed_tools: set[str]) -> list[dict[str, Any]]:
    schemas = [*registry.schemas, *READER_TOOL_SCHEMAS]
    return [
        schema
        for schema in schemas
        if str(schema.get("function", {}).get("name") or "") in allowed_tools
    ]


def _stage_agents(
    plan_asset: MultiAgentPlanAsset,
    stage_id: str,
) -> list[PlanAgentAsset]:
    return [
        plan_asset.agents[agent_id]
        for agent_id in plan_asset.stage_agents(stage_id)
        if agent_id in plan_asset.agents
    ]


def _format_report(
    research_reports: dict[str, str],
    rounds: list[dict[str, str]],
    pm_verdict: str,
) -> str:
    sections: list[str] = []
    if research_reports:
        sections.append("## 研报\n\n" + _format_research(research_reports, heading_level=3))
    if rounds:
        last_round = rounds[-1]
        for agent_id, output in last_round.items():
            sections.append(f"## {agent_id}\n\n{output}")
    if pm_verdict:
        sections.append(f"## PM 决策\n\n{pm_verdict}")
    return "\n\n".join(sections)


def _format_research(
    research_reports: dict[str, str],
    *,
    heading_level: int = 2,
) -> str:
    if not research_reports:
        return ""
    prefix = "#" * heading_level
    return "\n\n".join(
        f"{prefix} {label}\n{report}" for label, report in research_reports.items()
    )


def _format_debate(rounds: list[dict[str, str]]) -> str:
    if not rounds:
        return ""
    parts: list[str] = []
    for index, round_item in enumerate(rounds, 1):
        parts.append(f"### 第 {index} 轮辩论")
        for agent_id, output in round_item.items():
            parts.append(f"**{agent_id}:**\n{output}")
    return "## 多空辩论\n\n" + "\n\n".join(parts)


def _parse_verdict(symbol: str, verdict: str) -> TradingSignal | None:
    if not verdict.strip():
        return None

    rating_match = re.search(
        r"Final Rating\*\*:\s*(strong_buy|buy|hold|sell|strong_sell)|Final Rating:\s*(strong_buy|buy|hold|sell|strong_sell)",
        verdict,
        re.I,
    )
    confidence_match = re.search(r"Confidence\*\*:\s*([0-9.]+)|Confidence:\s*([0-9.]+)", verdict, re.I)
    entry_match = re.search(r"Entry Price\*\*:\s*([0-9.]+|N/A)|Entry Price:\s*([0-9.]+|N/A)", verdict, re.I)
    stop_match = re.search(r"Stop Loss\*\*:\s*([0-9.]+|N/A)|Stop Loss:\s*([0-9.]+|N/A)", verdict, re.I)
    target_match = re.search(r"Target Price\*\*:\s*([0-9.]+|N/A)|Target Price:\s*([0-9.]+|N/A)", verdict, re.I)
    reasoning_match = re.search(r"Reasoning\*\*:\s*(.+)|Reasoning:\s*(.+)", verdict, re.I | re.S)

    rating_text = _first_match(rating_match)
    rating = SignalRating(rating_text.lower()) if rating_text else parse_signal_rating(verdict)
    confidence_text = _first_match(confidence_match)
    confidence = float(confidence_text) if confidence_text else 0.5

    entry = _parse_price(_first_match(entry_match))
    stop = _parse_price(_first_match(stop_match))
    target = _parse_price(_first_match(target_match))
    rr = round((target - entry) / (entry - stop), 2) if entry and stop and target and entry != stop else None
    reasoning = (_first_match(reasoning_match) or verdict).strip()

    return TradingSignal(
        symbol=symbol,
        rating=rating,
        confidence=max(0.0, min(1.0, confidence)),
        entry_price=entry,
        stop_loss=stop,
        target_price=target,
        risk_reward_ratio=rr,
        reasoning=reasoning,
        source_specialist="debate_team",
        source_tools=["multi_agent_debate"],
    )


def _first_match(match: re.Match[str] | None) -> str:
    if match is None:
        return ""
    for item in match.groups():
        if item:
            return item.strip()
    return ""


def _parse_price(value: str) -> float | None:
    if not value or value.upper() == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _render(template: str, variables: dict[str, str]) -> str:
    return template.format_map(_SafeFormatDict(variables))


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _risk_stage_enabled(plan_asset: MultiAgentPlanAsset) -> bool:
    return plan_asset.stage_enabled(
        "risk_crossexam",
        bool(plan_asset.defaults.get("enable_risk_crossexam", False)),
    )


def _int_default(plan_asset: MultiAgentPlanAsset, key: str, fallback: int) -> int:
    return int(plan_asset.defaults.get(key, fallback))


def _chat_client(client: ChatClient | None, app_config: AppConfig) -> ChatClient:
    if client is not None:
        return client
    debate_llm = replace(app_config.llm, timeout=max(app_config.llm.timeout, 120.0))
    debate_app = replace(app_config, llm=debate_llm)
    return create_chat_client(debate_app)


def _validate_plan_asset(
    plan_asset: MultiAgentPlanAsset,
    *,
    strategy: str,
) -> None:
    if plan_asset.strategy != strategy:
        raise PlanAssetError(
            f"plan asset {plan_asset.id} declares {plan_asset.strategy}, expected {strategy}"
        )


def _error_result(
    state: RunState,
    *,
    plan: str,
    error: str,
    warning: str,
    plan_asset: MultiAgentPlanAsset | None = None,
) -> MultiAgentRunResult:
    return MultiAgentRunResult(
        output=json.dumps({"error": error, "specialist": state.team.id}, ensure_ascii=False, sort_keys=True),
        metadata={
            "symbols": [],
            "strategy": "debate_team",
            "plan": plan,
            "plan_asset": plan_asset.id if plan_asset else "",
            "plan_asset_version": plan_asset.version if plan_asset else 0,
        },
        warnings=(warning,),
    )


def _extract_symbols(task: str) -> list[str]:
    codes = re.findall(r"\b(\d{6})\b", task)
    if codes:
        return list(dict.fromkeys(codes))
    parts = [p.strip() for p in task.replace(",", " ").split() if p.strip()]
    return parts[:3]
