from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from trade_compass_agent.config import load_app_config, update_scheduler_config
from trade_compass_agent.data import ChainProvider, DataQualityLayer
from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.evaluation import RulePerformanceEvaluator
from trade_compass_agent.memory.rules_store import RulesStore
from trade_compass_agent.ops.audit import JsonAuditLog
from trade_compass_agent.ops.run_store import SqliteRunStore
from trade_compass_agent.ops.tick_scheduler import TickScheduler
from trade_compass_agent.ops.notifications import JsonNotificationStore
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.risk import CooldownTracker
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.workflows import (
    WorkflowError,
    list_workflow_runs,
    load_workflow_assets,
    run_workflow_asset_by_id,
)
from trade_compass_agent.evaluation.workflow_artifacts import load_latest_workflow_evaluation

from . import schemas as s
from . import serializers as ser
from .agent_api import router as agent_router

router = APIRouter(prefix="/api")
router.include_router(agent_router)
logger = logging.getLogger(__name__)


def _stack() -> MarketStack:
    return MarketStack.from_config()


def _rules_response(store: RulesStore) -> s.RulesResponse:
    entries = store.list_entries()
    return s.RulesResponse(
        content=store.read_for_prompt(),
        entries=[ser.to_rule_entry_payload(item) for item in entries],
        chars_used=store.chars_used(),
        limit=store.char_limit,
        version=store.version(),
    )


def _rules_store() -> RulesStore:
    config = load_app_config()
    return RulesStore(config.memory_dir, char_limit=config.rules.char_limit)


def _workflow_payload(manifest) -> dict:
    return {
        "id": manifest.id,
        "version": manifest.version,
        "asset_version": 2,
        "name": manifest.name,
        "description": manifest.description,
        "owner": manifest.owner,
        "steps": [
            {
                "id": step.id,
                "type": step.type,
                "uses": step.uses,
                "depends_on": list(step.depends_on),
                "persist_artifact": step.persist_artifact,
                "primary_output": step.primary_output,
                "when": step.when,
            }
            for step in manifest.steps
        ],
        "output_schema": manifest.output_schema,
        "persistence": manifest.persistence,
        "risk_policy": manifest.risk_policy,
        "timeout_seconds": manifest.timeout_seconds,
        "retry_policy": manifest.retry_policy,
        "degradation_policy": manifest.degradation_policy,
        "evaluation_hooks": list(manifest.evaluation_hooks),
    }


def _artifact_paths_for_workflow(data_dir, manifest, as_of: str | None = None) -> list:
    paths = []
    persistence = manifest.persistence
    template = persistence.get("path_template", "") if isinstance(persistence, dict) else persistence.path_template
    if template:
        date_part = as_of or "*"
        rendered = template.format(date=date_part, mode="*", week="*", workflow_id=manifest.id)
        paths.append(data_dir / rendered.removeprefix("data/") if rendered.startswith("data/") else data_dir.parent / rendered)
    paths.extend(_compat_artifact_paths(data_dir, manifest.id, as_of=as_of))
    return list(dict.fromkeys(paths))


def _compat_artifact_paths(data_dir, workflow_id: str, as_of: str | None = None) -> list:
    if workflow_id == "catalyst_calendar_cn":
        return [data_dir / "catalysts" / f"{as_of or '*'}.jsonl"]
    if workflow_id == "idea_generation_cn":
        root = data_dir / "ideas"
        if as_of:
            week = _week_key(as_of)
            paths = [
                root / f"{as_of}-morning.jsonl",
                root / f"{as_of}-manual.jsonl",
                root / f"{as_of}.jsonl",
            ]
            if week:
                paths.append(root / f"{week}-weekend.jsonl")
            paths.append(root / f"{as_of}-weekly.jsonl")
            return paths
        return [
            root / "*-morning.jsonl",
            root / "*-manual.jsonl",
            root / "*.jsonl",
            root / "*-weekend.jsonl",
            root / "*-weekly.jsonl",
        ]
    return []


def _week_key(value: str) -> str:
    from datetime import date

    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ""
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _read_artifact_rows(paths, limit: int) -> list[dict]:
    if not paths:
        return []
    expanded = []
    for path in paths:
        expanded.extend(sorted(path.parent.glob(path.name)) if "*" in path.name else [path])
    rows: list[dict] = []
    for item in sorted(dict.fromkeys(expanded)):
        if not item.is_file():
            continue
        for line in item.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_artifact"] = str(item)
            rows.append(row)
    return rows[-limit:]


def _validate_workflow_manifest_for_api(manifest) -> list[dict]:
    from trade_compass_agent.config import PROJECT_ROOT
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    schema_path = (manifest.path.parent / manifest.output_schema) if getattr(manifest, "path", None) else PROJECT_ROOT / manifest.output_schema
    if not schema_path.is_file():
        schema_path = PROJECT_ROOT / manifest.output_schema
    add("output_schema", schema_path.is_file(), manifest.output_schema)
    if schema_path.is_file():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            validator_for(schema).check_schema(schema)
        except (json.JSONDecodeError, SchemaError) as exc:
            add("output_schema_schema", False, str(exc))
        else:
            add("output_schema_schema", True, manifest.output_schema)
    add("steps", bool(manifest.steps), f"{len(manifest.steps)} step(s)")
    try:
        from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles
        from trade_compass_agent.runtime.tools.policy import default_tool_policy

        tool_names = default_tool_policy().names()
        specialist_names = set(load_specialist_profiles())
        workflow_names = set(load_workflow_assets())
    except Exception:
        tool_names = set()
        specialist_names = set()
        workflow_names = set()
    missing_refs: list[str] = []
    for step in manifest.steps:
        if step.type == "tool":
            ref = step.uses.removeprefix("tool:")
            if not step.uses.startswith("tool:") or ref not in tool_names:
                missing_refs.append(step.uses)
        elif step.type == "specialist":
            ref = step.uses.removeprefix("specialist:")
            if not step.uses.startswith("specialist:") or ref not in specialist_names:
                missing_refs.append(step.uses)
        elif step.type == "workflow":
            ref = step.uses.removeprefix("workflow:")
            if not step.uses.startswith("workflow:") or ref not in workflow_names:
                missing_refs.append(step.uses)
        elif step.type in {"compose", "evaluate"}:
            continue
        else:
            missing_refs.append(step.uses or step.type)
    add("step_references", not missing_refs, ", ".join(missing_refs))
    add("risk_policy", manifest.risk_policy.get("may_recommend_trade") is False, "may_recommend_trade must be false")
    retry = manifest.retry_policy or {}
    add(
        "retry_policy",
        "max_retries" in retry and "backoff_seconds" in retry,
        "max_retries/backoff_seconds required",
    )
    add("degradation_policy", isinstance(manifest.degradation_policy, dict), "")
    add("evaluation_hooks", isinstance(manifest.evaluation_hooks, tuple), "")
    return checks


def _workflow_catalog() -> dict:
    assets = load_workflow_assets()
    return dict(assets)


@router.get("/events", response_model=s.EventsResponse)
def get_events(
    symbol: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
) -> s.EventsResponse:
    config = load_app_config()
    stack = _stack()
    if not config.data.cninfo_enabled:
        return s.EventsResponse(symbol=symbol.strip(), events=[], provider_name="disabled")
    events = stack.cninfo_provider.get_events(symbol.strip(), limit=limit)
    return s.EventsResponse(
        symbol=symbol.strip(),
        events=[ser.to_event_payload(item) for item in events],
        provider_name=getattr(stack.cninfo_provider, "name", None),
    )


@router.get("/market-pulse", response_model=s.MarketPulseResponse)
def get_market_pulse() -> s.MarketPulseResponse:
    pulse = _stack().market_pulse_provider.get_market_pulse()
    payload = ser.to_pulse_payload(pulse)
    assert payload is not None
    return payload


@router.get("/bars", response_model=s.BarsResponse)
def get_bars(
    symbol: str = Query(..., min_length=1),
    timeframe: str = "1d",
    limit: int = Query(120, ge=1, le=1000),
) -> s.BarsResponse:
    stack = _stack()
    bars = stack.provider.get_bars(symbol.strip(), timeframe=timeframe, limit=limit)
    quality = DataQualityLayer().check_bars(bars)
    warnings = [f"{symbol}: {warning}" for warning in quality.warnings]
    if isinstance(stack.provider, ChainProvider):
        warnings.extend(stack.provider.last_warnings)
        stack.provider.last_warnings.clear()
    return s.BarsResponse(
        symbol=symbol.strip(),
        timeframe=timeframe,
        limit=limit,
        bars=[ser.to_bar_payload(bar) for bar in bars],
        quality_warnings=warnings,
        provider_name=getattr(stack.provider, "name", None),
    )


@router.get("/forecast")
def get_forecast(
    symbol: str = Query(..., min_length=1),
    horizon: int = Query(10, ge=1, le=60),
    model_size: str = "small",
    sample_count: int = Query(5, ge=1, le=20),
    lookback: int = Query(120, ge=30, le=400),
):
    """Predict future K-line bars using Kronos foundation model."""
    try:
        from trade_compass_agent.data.kronos_adapter import (
            forecast_install_command,
            forecast_kline,
            is_kronos_available,
        )
    except ImportError:
        from trade_compass_agent import __version__

        raise HTTPException(
            status_code=503,
            detail={
                "code": "forecast_unavailable",
                "message": "预测引擎尚未安装。",
                "recovery": {
                    "command": (
                        "uv tool install --force --python 3.12 "
                        f"'trade-compass-agent[forecast]=={__version__}'"
                    ),
                    "restart_required": True,
                },
            },
        ) from None

    if not is_kronos_available():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "forecast_unavailable",
                "message": "预测引擎尚未安装。",
                "recovery": {
                    "command": forecast_install_command(),
                    "restart_required": True,
                },
            },
        )

    try:
        stack = _stack()
        bars = stack.provider.get_bars(symbol.strip(), timeframe="1d", limit=lookback)
    except Exception:
        logger.exception("Forecast history loading failed for %s", symbol.strip())
        raise HTTPException(
            status_code=500,
            detail={
                "code": "forecast_failed",
                "message": "预测执行失败，请稍后重试。",
            },
        ) from None

    if not bars or len(bars) < 30:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "insufficient_history",
                "message": f"历史数据不足：当前 {len(bars) if bars else 0} 条，需要至少 30 条。",
            },
        )

    try:
        result = forecast_kline(
            bars=bars,
            symbol=symbol.strip(),
            horizon=horizon,
            model_size=model_size,
            sample_count=sample_count,
        )
        if (
            len(result.mean_bars) != horizon
            or len(result.confidence_upper) != horizon
            or len(result.confidence_lower) != horizon
        ):
            raise ValueError("forecast result lengths do not match requested horizon")

        last_close = bars[-1].close
        pred_close = result.mean_bars[-1].close
        payload = {
            "symbol": symbol.strip(),
            "model": result.model_id,
            "lookback_used": result.lookback_used,
            "horizon": result.horizon,
            "current_close": last_close,
            "forecast_bars": [
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in result.mean_bars
            ],
            "confidence_band": {
                "upper": result.confidence_upper,
                "lower": result.confidence_lower,
            },
            "change_pct": round((pred_close - last_close) / last_close * 100, 2),
            "quality_status": "experimental",
            "parameters": {
                "horizon": horizon,
                "model_size": model_size,
                "sample_count": sample_count,
                "lookback": lookback,
            },
        }
    except Exception:
        logger.exception("Kronos forecast failed for %s", symbol.strip())
        raise HTTPException(
            status_code=500,
            detail={
                "code": "forecast_failed",
                "message": "预测执行失败，请稍后重试。",
            },
        ) from None
    return payload


@router.get("/portfolio", response_model=s.PortfolioResponse)
def get_portfolio() -> s.PortfolioResponse:
    config = load_app_config()
    portfolio_store = JsonPaperPortfolio(
        config.data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    try:
        from trade_compass_agent.data import create_market_data_provider
        provider = create_market_data_provider(
            config.data_provider,
            cache_dir=config.data_dir / "market_cache",
            data=config.data,
        )
        positions = portfolio_store.positions_with_market_prices(provider=provider)
    except Exception:
        positions = None
    return ser.to_portfolio_response(portfolio_store, costs=config.trading_costs, live_positions=positions)


@router.post(
    "/portfolio/trades",
    response_model=s.PortfolioResponse,
    responses={400: {"model": s.ErrorResponse}},
)
def create_portfolio_trade(body: s.PaperTradeCreate):
    from datetime import datetime
    from uuid import uuid4

    config = load_app_config()
    portfolio_store = JsonPaperPortfolio(
        config.data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    try:
        account = AccountKind(body.account)
    except ValueError:
        return JSONResponse({"error": f"unknown account: {body.account}"}, status_code=400)

    from trade_compass_agent.portfolio.market_rules import infer_market_rules
    rules = infer_market_rules(body.symbol.strip())

    realized_before = len(portfolio_store.realized_trades())
    trade = PaperTrade(
        symbol=body.symbol.strip(),
        account=account,
        side=body.side,
        quantity=body.quantity,
        price=body.price,
        timestamp=datetime.now(),
        reason=body.reason,
        trade_id=uuid4().hex[:16],
        price_source="user_confirmed",
        requested_price=body.price,
        previous_close=body.previous_close,
        suspended=body.suspended,
        is_st=body.is_st,
        is_t0=rules.is_t0,
        price_limit_pct=rules.price_limit_pct,
    )
    try:
        portfolio_store.record(trade, skip_t1=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    from trade_compass_agent.memory.decision_reconciler import reconcile_decisions

    reconcile_decisions(config.data_dir, config.trading_costs)
    _update_cooldown_from_trades(config, portfolio_store, realized_before)
    return ser.to_portfolio_response(portfolio_store, costs=config.trading_costs)


def _update_cooldown_from_trades(config, portfolio_store, realized_before: int) -> None:
    realized = portfolio_store.realized_trades()
    new_trades = realized[realized_before:]
    if not new_trades:
        return
    tracker = CooldownTracker(
        config.data_dir / "cooldown_state.json",
        threshold=3,
    )
    for trade in new_trades:
        if trade.pnl > 0:
            tracker.record_win()
        elif trade.pnl < 0:
            tracker.record_loss()


@router.get("/portfolio/cooldown")
def get_cooldown_status():
    config = load_app_config()
    tracker = CooldownTracker(
        config.data_dir / "cooldown_state.json",
        threshold=3,
    )
    return {
        "active": tracker.is_active(),
        "consecutive_losses": tracker.state.consecutive_losses,
        "threshold": tracker.threshold,
        "updated_at": tracker.state.updated_at,
    }


# --- Account CRUD ---

@router.get("/accounts")
def list_accounts():
    from trade_compass_agent.portfolio.accounts import AccountStore
    config = load_app_config()
    store = AccountStore(config.data_dir / "accounts.json")
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    positions = portfolio.positions()
    accounts = store.list()
    result = []
    for a in accounts:
        used = sum(p.market_value for p in positions if p.account.value == a.id or p.account.value == a.kind.value)
        result.append({
            "id": a.id,
            "kind": a.kind.value,
            "name": a.name,
            "description": a.description,
            "capital": a.capital,
            "used": round(used, 2),
            "utilization_pct": round(used / a.capital * 100, 1) if a.capital > 0 else 0,
            "created_at": a.created_at,
        })
    return result


@router.post("/accounts")
def create_account(body: s.AccountCreate):
    from trade_compass_agent.portfolio.accounts import AccountStore
    config = load_app_config()
    store = AccountStore(config.data_dir / "accounts.json")
    try:
        AccountKind(body.kind)
    except ValueError:
        return JSONResponse({"error": f"无效账户类型: {body.kind}"}, status_code=400)
    account = store.create(
        kind=AccountKind(body.kind),
        name=body.name,
        description=body.description or "",
        capital=body.capital,
    )
    return {"id": account.id, "kind": account.kind.value, "name": account.name, "capital": account.capital}


@router.put("/accounts/{account_id}")
def update_account(account_id: str, body: s.AccountUpdate):
    from trade_compass_agent.portfolio.accounts import AccountStore
    config = load_app_config()
    store = AccountStore(config.data_dir / "accounts.json")
    updates = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.capital is not None:
        updates["capital"] = body.capital
    if body.kind is not None:
        updates["kind"] = body.kind
    if not updates:
        return JSONResponse({"error": "无更新字段"}, status_code=400)
    account = store.update(account_id, **updates)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账户不存在: {account_id}")
    return {"id": account.id, "kind": account.kind.value, "name": account.name, "capital": account.capital}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    from trade_compass_agent.portfolio.accounts import AccountStore
    config = load_app_config()
    store = AccountStore(config.data_dir / "accounts.json")
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    positions = portfolio.positions()
    has_positions = any(p.account.value == account_id for p in positions)
    if has_positions:
        return JSONResponse({"error": "该账户仍有持仓，无法删除"}, status_code=400)
    ok = store.delete(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"账户不存在: {account_id}")
    return {"deleted": True}


@router.get("/audit", response_model=list[s.AuditEventPayload])
def get_audit(
    limit: int = Query(50, ge=1, le=1000),
    event_type: str | None = Query(None),
) -> list[s.AuditEventPayload]:
    config = load_app_config()
    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    if event_type == "recommendation":
        events = audit.recommendations(limit)
    else:
        events = audit.recent(limit)
        if event_type:
            events = [e for e in events if e.event_type == event_type]
    return [ser.to_audit_payload(event) for event in events]


@router.get("/audit/{event_id}", response_model=s.AuditEventPayload)
def get_audit_event(event_id: str) -> s.AuditEventPayload:
    config = load_app_config()
    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    event = audit.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"audit event not found: {event_id}")
    return ser.to_audit_payload(event)


@router.get("/rules", response_model=s.RulesResponse)
def get_rules(response: Response) -> s.RulesResponse:
    store = _rules_store()
    payload = _rules_response(store)
    response.headers["X-Rules-Version"] = payload.version
    return payload


@router.put("/rules", response_model=s.RulesResponse, responses={412: {"model": s.ErrorResponse}})
def replace_rules(body: s.RulesReplace, request: Request, response: Response) -> s.RulesResponse:
    store = _rules_store()
    expected = request.headers.get("if-match")
    current = store.version()
    if expected and expected != current:
        raise HTTPException(status_code=412, detail="RULES.md version mismatch")
    result = store.replace_all(body.content, actor="web")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to replace rules"))
    payload = _rules_response(store)
    response.headers["X-Rules-Version"] = payload.version
    return payload


@router.post("/rules/entries", response_model=s.RulesResponse)
def add_rule_entry(body: s.RuleEntryCreate, response: Response) -> s.RulesResponse:
    store = _rules_store()
    result = store.add(body.text, actor="web")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to add rule"))
    payload = _rules_response(store)
    response.headers["X-Rules-Version"] = payload.version
    return payload


@router.patch(
    "/rules/entries/{entry_id}",
    response_model=s.RulesResponse,
    responses={404: {"model": s.ErrorResponse}},
)
def update_rule_entry(entry_id: str, body: s.RuleEntryUpdate, response: Response) -> s.RulesResponse:
    store = _rules_store()
    result = store.replace(entry_id, body.text, actor="web")
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Rule not found"))
    payload = _rules_response(store)
    response.headers["X-Rules-Version"] = payload.version
    return payload


@router.delete(
    "/rules/entries/{entry_id}",
    response_model=s.RulesResponse,
    responses={404: {"model": s.ErrorResponse}},
)
def delete_rule_entry(entry_id: str, response: Response) -> s.RulesResponse:
    store = _rules_store()
    result = store.remove(entry_id, actor="web")
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Rule not found"))
    payload = _rules_response(store)
    response.headers["X-Rules-Version"] = payload.version
    return payload


@router.get("/evaluation/rules", response_model=s.RulePerformanceReportPayload)
def get_rule_performance(limit: int = Query(500, ge=1, le=5000)) -> s.RulePerformanceReportPayload:
    config = load_app_config()
    report = RulePerformanceEvaluator(
        data_dir=config.data_dir,
        memory_dir=config.memory_dir,
    ).evaluate(limit=limit)
    return ser.to_rule_performance_payload(report)


# --- Skills & Memory ---------------------------------------------------------


@router.get("/skills", response_model=s.SkillsResponse)
def get_skills(include_stale: bool = False):
    config = load_app_config()
    from trade_compass_agent.memory.skill_store import SkillStore

    store = SkillStore(config.memory_dir / "skills")
    skills = store.list_skills(include_stale=include_stale)
    return s.SkillsResponse(
        skills=[
            s.SkillPayload(
                name=sk.name,
                description=sk.description,
                category=sk.category,
                state=sk.usage.state,
                pinned=sk.usage.pinned,
                use_count=sk.usage.use_count,
                patch_count=sk.usage.patch_count,
                last_used_at=sk.usage.last_used_at,
                created_at=sk.usage.created_at,
                created_by=sk.usage.created_by,
            )
            for sk in skills
        ],
        total=len(skills),
    )


@router.get("/skills/{name}", response_model=s.SkillDetailPayload)
def get_skill_detail(name: str):
    config = load_app_config()
    from trade_compass_agent.memory.skill_store import SkillStore

    store = SkillStore(config.memory_dir / "skills")
    record = store.get(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    content = store.read_full(name) or ""
    return s.SkillDetailPayload(
        name=record.name,
        description=record.description,
        category=record.category,
        state=record.usage.state,
        pinned=record.usage.pinned,
        use_count=record.usage.use_count,
        patch_count=record.usage.patch_count,
        last_used_at=record.usage.last_used_at,
        created_at=record.usage.created_at,
        created_by=record.usage.created_by,
        content=content,
    )


@router.post("/skills/{name}/pin")
def pin_skill(name: str, body: s.SkillPinRequest):
    config = load_app_config()
    from trade_compass_agent.memory.skill_store import SkillStore

    store = SkillStore(config.memory_dir / "skills")
    if body.pinned:
        result = store.pin(name)
    else:
        result = store.unpin(name)
    return result


def _memory_response(target: str, store) -> s.MemoryResponse:
    entries_meta = store.get_entries_with_meta(target)
    char_limit = store._memory_char_limit if target == "memory" else store._user_char_limit
    content = "\n§\n".join(m.text for m in entries_meta)
    return s.MemoryResponse(
        target=target,
        entries=[
            s.MemoryEntryPayload(
                index=i,
                text=m.text,
                confidence=round(m.confidence, 3),
                access_count=m.access_count,
                source=m.source,
                status=m.status,
                content_hash=m.content_hash or m.dedup_hash,
                created_at=m.created_at,
                last_accessed=m.last_accessed,
            )
            for i, m in enumerate(entries_meta)
        ],
        chars_used=len(content),
        char_limit=char_limit,
    )


def _memory_store_for_api():
    config = load_app_config()
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.skill_store import SkillStore
    from trade_compass_agent.memory.write_gate import SemanticWriteGate

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    return MemoryStore(
        config.memory_dir,
        write_gate=gate,
        min_inject_confidence=config.memory.governance.min_inject_confidence,
    ), config


@router.get("/memory/{target}", response_model=s.MemoryResponse)
def get_memory(target: str = "memory"):
    if target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")
    store, _config = _memory_store_for_api()
    return _memory_response(target, store)


@router.post("/memory/{target}/pin", response_model=s.MemoryResponse)
def pin_memory(target: str, body: s.MemoryActionRequest):
    if target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")
    from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
    import json

    store, config = _memory_store_for_api()
    result = json.loads(
        tool_memory_write(
            store,
            action="pin",
            content=body.content,
            target=target,
            actor="user",
            governance=config.memory.governance,
        )
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "pin failed"))
    return _memory_response(target, store)


@router.post("/memory/{target}/forget", response_model=s.MemoryResponse)
def forget_memory(target: str, body: s.MemoryActionRequest):
    if target not in ("memory", "user"):
        raise HTTPException(status_code=400, detail="target must be 'memory' or 'user'")
    from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
    import json

    store, config = _memory_store_for_api()
    result = json.loads(
        tool_memory_write(
            store,
            action="forget",
            content=body.content,
            target=target,
            actor="user",
            governance=config.memory.governance,
        )
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "forget failed"))
    return _memory_response(target, store)


@router.post("/memory/merge")
def trigger_memory_merge():
    """Merge similar KNOWLEDGE.md entries via LLM. Bypasses 24h cooldown."""
    config = load_app_config()
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.memory.memory_store import MemoryStore
    from trade_compass_agent.memory.semantic_merge import merge_similar_entries
    from trade_compass_agent.memory.write_gate import SemanticWriteGate
    from trade_compass_agent.memory.skill_store import SkillStore

    skill_store = SkillStore(config.memory_dir / "skills")
    gate = SemanticWriteGate(skill_store=skill_store)
    mem_store = MemoryStore(config.memory_dir, write_gate=gate)

    def _llm_call(system_prompt: str, user_content: str) -> str:
        client = create_chat_client(config)
        msgs = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)]
        return client.complete(msgs).content or ""

    merged = merge_similar_entries(mem_store, _llm_call, force=True)
    return {"ok": True, "merged_clusters": merged, "remaining_entries": len(mem_store.memory_entries)}


@router.get("/decisions")
def get_decisions(symbol: str | None = None, status: str | None = None, limit: int = Query(20, ge=1, le=100)):
    config = load_app_config()
    from trade_compass_agent.memory.decision_reconciler import reconcile_decisions
    from trade_compass_agent.memory.decision_store import DecisionStore

    reconcile_decisions(config.data_dir, config.trading_costs)
    store = DecisionStore(config.data_dir)
    results = store.search(symbol=symbol, status=status, limit=limit)
    return {
        "decisions": [_decision_payload(d) for d in results],
        "stats": store.stats(),
    }


def _decision_payload(d) -> dict:
    return {
        "id": d.id,
        "symbol": d.symbol,
        "side": d.side,
        "quantity": d.quantity,
        "price": d.price,
        "account": d.account,
        "reasoning": d.reasoning,
        "status": d.status,
        "decided_at": d.decided_at,
        "outcome_pnl_pct": d.outcome_pnl_pct,
        "holding_days": d.holding_days,
        "reflection": d.reflection,
        "resolved_at": d.resolved_at,
        "outcome_price": d.outcome_price,
        "resolved_quantity": d.resolved_quantity,
        "outcome_cost_basis": d.outcome_cost_basis,
        "outcome_proceeds": d.outcome_proceeds,
        "outcome_fees": d.outcome_fees,
        "outcome_net_pnl": d.outcome_net_pnl,
        "outcome_net_pnl_pct": d.outcome_net_pnl_pct,
        "outcome_trade_ids": d.outcome_trade_ids,
        "outcome_source": d.outcome_source,
        "reconciliation_status": d.reconciliation_status,
        "reflection_stale": d.reflection_stale,
    }


def _decision_llm_call(config):
    from trade_compass_agent.llm.providers import ChatMessage, create_chat_client
    from trade_compass_agent.runtime.exceptions import AgentUnavailableError

    try:
        client = create_chat_client(config)
    except AgentUnavailableError:
        return None

    def _call(system_prompt: str, user_content: str) -> str:
        msgs = [ChatMessage(role="system", content=system_prompt), ChatMessage(role="user", content=user_content)]
        return client.complete(msgs).content or ""

    return _call


@router.post("/decisions/curate")
def curate_decisions_api(body: dict | None = None):
    """Batch-generate reflections for settled decisions awaiting review."""
    config = load_app_config()
    from trade_compass_agent.ops import curate_decisions

    payload = body or {}
    max_reflect = int(payload.get("max_reflect", 20))
    llm_call = _decision_llm_call(config)
    reflected_ids = curate_decisions(
        config.data_dir,
        max_reflect=max_reflect,
        llm_call=llm_call,
        trading_costs=config.trading_costs,
    )
    from trade_compass_agent.memory.decision_store import DecisionStore

    store = DecisionStore(config.data_dir)
    return {
        "ok": True,
        "reflected_count": len(reflected_ids),
        "reflected_ids": reflected_ids,
        "stats": store.stats(),
    }


@router.post("/decisions/{decision_id}/reflect")
def reflect_decision(decision_id: str, body: dict | None = None):
    """Reflect on a single settled decision (manual text or auto-generated)."""
    config = load_app_config()
    from trade_compass_agent.memory.decision_reconciler import reconcile_decisions
    from trade_compass_agent.memory.decision_store import DecisionStore
    from trade_compass_agent.ops import generate_decision_reflection

    reconcile_decisions(config.data_dir, config.trading_costs)
    store = DecisionStore(config.data_dir)
    matches = [d for d in store.search(limit=500) if d.id == decision_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    decision = matches[0]
    if decision.status != "resolved":
        raise HTTPException(status_code=409, detail=f"Decision {decision_id} is not awaiting reflection")

    payload = body or {}
    manual_text = payload.get("reflection")
    llm_call = None if manual_text else _decision_llm_call(config)
    reflection = generate_decision_reflection(decision, llm_call=llm_call, manual_text=manual_text)
    if not reflection:
        raise HTTPException(status_code=422, detail="Could not generate reflection")

    if not store.add_reflection(decision_id, reflection):
        raise HTTPException(status_code=409, detail=f"Decision {decision_id} could not be reflected")

    updated = next(d for d in store.search(limit=500) if d.id == decision_id)
    return {"ok": True, "decision": _decision_payload(updated), "stats": store.stats()}


@router.get("/instruments")
def get_instruments():
    config = load_app_config()
    from trade_compass_agent.memory.instrument_store import InstrumentStore

    store = InstrumentStore(config.memory_dir)
    symbols = store.list_instruments()
    return {
        "instruments": symbols,
        "created_at": {symbol: store.created_at(symbol) for symbol in symbols},
    }


@router.get("/instruments/{symbol}")
def get_instrument(symbol: str):
    config = load_app_config()
    from trade_compass_agent.memory.instrument_store import InstrumentStore

    store = InstrumentStore(config.memory_dir)
    page = store.recall(symbol)
    if page is None:
        raise HTTPException(status_code=404, detail=f"No instrument page for {symbol}")
    return {"symbol": symbol, "content": page}


# --- Workflow governance ----------------------------------------------------

@router.get("/workflows")
def list_workflows():
    workflows = _workflow_catalog()
    return [_workflow_payload(item) for item in workflows.values()]


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str):
    workflows = _workflow_catalog()
    manifest = workflows.get(workflow_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="workflow not found")
    return _workflow_payload(manifest)


@router.post("/workflows/{workflow_id}/run")
async def run_workflow_api(workflow_id: str, request: Request):
    workflows = _workflow_catalog()
    manifest = workflows.get(workflow_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    inputs = body.get("inputs") if isinstance(body, dict) else {}
    if not isinstance(inputs, dict):
        raise HTTPException(status_code=422, detail="inputs must be an object")
    config = load_app_config()
    try:
        output = run_workflow_asset_by_id(
            workflow_id,
            inputs,
            config=config,
            trigger="api",
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "workflow_id": workflow_id,
        "run_id": output.get("run_id"),
        "artifact_id": output.get("artifact_id"),
        "output": output,
    }


@router.get("/workflows/{workflow_id}/runs")
def get_workflow_runs(workflow_id: str, limit: int = Query(20, ge=1, le=200)):
    workflows = _workflow_catalog()
    if workflow_id not in workflows:
        raise HTTPException(status_code=404, detail="workflow not found")
    config = load_app_config()
    return {
        "workflow_id": workflow_id,
        "runs": list_workflow_runs(config.data_dir, workflow_id=workflow_id, limit=limit),
    }


@router.get("/workflows/{workflow_id}/validation")
def get_workflow_validation(workflow_id: str):
    workflows = _workflow_catalog()
    manifest = workflows.get(workflow_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="workflow not found")
    checks = _validate_workflow_manifest_for_api(manifest)
    return {
        "workflow_id": workflow_id,
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
    }


@router.get("/workflows/{workflow_id}/artifacts")
def get_workflow_artifacts(
    workflow_id: str,
    as_of: str | None = None,
    limit: int = Query(20, ge=1, le=200),
):
    workflows = _workflow_catalog()
    manifest = workflows.get(workflow_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="workflow not found")
    config = load_app_config()
    paths = _artifact_paths_for_workflow(config.data_dir, manifest, as_of=as_of)
    return {
        "workflow_id": workflow_id,
        "as_of": as_of,
        "artifacts": _read_artifact_rows(paths, limit),
    }


@router.get("/workflows/evaluation/latest")
def get_latest_workflow_evaluation():
    config = load_app_config()
    report = load_latest_workflow_evaluation(config.data_dir)
    if report is None:
        return {"evaluation": None}
    return {"evaluation": report}


@router.get("/jobs", response_model=list[s.ScheduledJobPayload])
def get_jobs() -> list[s.ScheduledJobPayload]:
    config = load_app_config()
    scheduler = TickScheduler(config, reap_on_init=False)
    return [
        s.ScheduledJobPayload(
            id=job.id,
            name=job.name,
            cadence=job.schedule,
            enabled=job.enabled,
            workflow_id=job.workflow_id,
            delivery_channels=list(job.delivery.channels),
        )
        for job in scheduler.list_jobs()
    ]


@router.get("/scheduler/status")
def get_scheduler_status():
    from trade_compass_agent.ops.tick_scheduler import get_active_scheduler
    active = get_active_scheduler()
    if active is None:
        return {"running": False, "jobs_count": 0, "custom_jobs_count": 0}
    return {
        "running": active.running,
        "jobs_count": len(active.list_jobs()),
        "custom_jobs_count": len(active.prompt_store.list_all()),
        "custom_jobs_enabled": len(active.prompt_store.list_enabled()),
    }


@router.get("/jobs/runs", response_model=list[s.JobRunPayload])
def get_job_runs(
    limit: int = Query(20, ge=1, le=500),
    job_id: str | None = None,
) -> list[s.JobRunPayload]:
    config = load_app_config()
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    return [ser.to_job_run_payload_v2(run) for run in store.recent_runs(limit, job_id=job_id)]


@router.get("/jobs/runs/{run_id}")
def get_run_detail(run_id: str):
    import json as _json
    config = load_app_config()
    store = SqliteRunStore(config.data_dir / "scheduler.db")
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = store.step_runs_for(run_id)

    def _parse_data(raw: str | None) -> dict | None:
        if not raw:
            return None
        try:
            return _json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _workflow_trace_steps() -> list[dict]:
        trace_path = config.data_dir / "workflow_runs" / run_id / "trace.jsonl"
        states: dict[str, dict] = {}
        if trace_path.is_file():
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = _json.loads(line)
                except ValueError:
                    continue
                name = event.get("event")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                step_id = str(data.get("step_id") or "")
                if not step_id:
                    continue
                item = states.setdefault(
                    step_id,
                    {
                        "id": f"workflow:{run_id}:{step_id}",
                        "step_id": step_id,
                        "status": "pending",
                        "started_at": None,
                        "finished_at": None,
                        "output": "",
                        "error": None,
                        "data": {
                            "type": data.get("type"),
                            "uses": data.get("uses"),
                        },
                    },
                )
                if name == "builtin.step_started":
                    item["status"] = "running"
                    item["started_at"] = event.get("recorded_at")
                    item["data"] = {
                        **(item.get("data") or {}),
                        "type": data.get("type"),
                        "uses": data.get("uses"),
                    }
                elif name == "builtin.step_finished":
                    item["status"] = str(data.get("status") or "completed")
                    item["finished_at"] = event.get("recorded_at")
                    item["output"] = str(data.get("error") or "completed")
                    if data.get("error"):
                        item["error"] = str(data.get("error"))
                    if item.get("started_at") is None:
                        item["started_at"] = event.get("recorded_at")

        return list(states.values())

    def _analysis_from_steps() -> str | None:
        from trade_compass_agent.ops.run_content import extract_analysis_from_artifact, extract_analysis_from_step_data

        artifact_analysis = extract_analysis_from_artifact(run.artifact, run_id=run.id)
        if artifact_analysis:
            return artifact_analysis
        for sr in reversed(steps):
            if sr.step_id == "workflow":
                continue
            analysis = extract_analysis_from_step_data(sr.data_json)
            if analysis:
                return analysis
        return None

    payload = {
        **ser.to_job_run_payload_v2(run).model_dump(),
        "step_runs": [
            {
                "id": sr.id,
                "step_id": sr.step_id,
                "status": sr.status,
                "started_at": sr.started_at.isoformat() if sr.started_at else None,
                "finished_at": sr.finished_at.isoformat() if sr.finished_at else None,
                "output": sr.output,
                "error": sr.error,
                "data": _parse_data(sr.data_json),
            }
            for sr in steps
        ] + _workflow_trace_steps(),
    }
    analysis = _analysis_from_steps()
    if analysis:
        payload["analysis"] = analysis
    if run.job_id.startswith("custom:"):
        from trade_compass_agent.ops.prompt_jobs import PromptJobStore
        custom_id = run.job_id.removeprefix("custom:")
        prompt_job = PromptJobStore(config.data_dir / "scheduler.db").get(custom_id)
        payload["job_type"] = "custom"
        if prompt_job:
            payload["job_name"] = prompt_job.name
        if not analysis and not steps and run.message:
            payload["analysis"] = run.message
    return payload


_VALID_DELIVERY_CHANNELS = {"web_log", "feishu", "wecom", "weixin"}


def _normalize_delivery_channels(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ("web_log",)
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=422, detail="delivery_channels must be a non-empty list")
    channels: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise HTTPException(status_code=422, detail="delivery_channels must contain strings")
        ch = item.strip()
        if ch not in _VALID_DELIVERY_CHANNELS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid delivery channel: {ch}. Allowed: {sorted(_VALID_DELIVERY_CHANNELS)}",
            )
        if ch not in channels:
            channels.append(ch)
    return tuple(channels)


@router.get("/jobs/custom")
def list_custom_jobs():
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    store = PromptJobStore(config.data_dir / "scheduler.db")
    return [
        {
            "id": j.id, "name": j.name, "prompt": j.prompt,
            "schedule": j.schedule, "enabled": j.enabled,
            "trading_day_only": j.trading_day_only,
            "delivery_channels": list(j.delivery_channels),
            "created_by": j.created_by,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in store.list_all()
    ]


@router.post("/jobs/custom")
def create_custom_job(body: dict):
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    store = PromptJobStore(config.data_dir / "scheduler.db")
    name = body.get("name")
    prompt = body.get("prompt")
    schedule = body.get("schedule")
    if not name or not prompt or not schedule:
        raise HTTPException(status_code=422, detail="name, prompt, and schedule are required")
    delivery_channels = _normalize_delivery_channels(body.get("delivery_channels"))
    job = store.create(
        name=name, prompt=prompt, schedule=schedule,
        trading_day_only=body.get("trading_day_only", False),
        delivery_channels=delivery_channels,
        created_by="web",
    )
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler
    reload_active_scheduler()
    return {
        "id": job.id,
        "name": job.name,
        "schedule": job.schedule,
        "delivery_channels": list(job.delivery_channels),
    }


@router.patch("/jobs/custom/{job_id}")
def update_custom_job(job_id: str, body: dict):
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    store = PromptJobStore(config.data_dir / "scheduler.db")
    updates = dict(body)
    if "delivery_channels" in updates:
        updates["delivery_channels"] = _normalize_delivery_channels(updates["delivery_channels"])
    job = store.update(job_id, **updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler
    reload_active_scheduler()
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "delivery_channels": list(job.delivery_channels),
    }


@router.delete("/jobs/custom/{job_id}")
def delete_custom_job(job_id: str):
    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobStore
    store = PromptJobStore(config.data_dir / "scheduler.db")
    if not store.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler
    reload_active_scheduler()
    return {"ok": True}


@router.post("/jobs/custom/{job_id}/run")
def run_custom_job(job_id: str):
    import time as _time

    config = load_app_config()
    from trade_compass_agent.ops.prompt_jobs import PromptJobExecutor, PromptJobStore
    store = PromptJobStore(config.data_dir / "scheduler.db")
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    run_store = SqliteRunStore(config.data_dir / "scheduler.db")
    executor = PromptJobExecutor(config, run_store)

    threading.Thread(
        target=executor.execute,
        args=(job,),
        kwargs={"trigger": "api"},
        daemon=True,
        name=f"custom-job-{job_id}",
    ).start()

    _time.sleep(0.3)
    runs = run_store.recent_runs(limit=1, job_id=f"custom:{job.id}")
    if runs:
        return ser.to_job_run_payload_v2(runs[0])
    return {"ok": True}


@router.get("/jobs/{job_id}")
def get_job_detail(job_id: str):
    config = load_app_config()
    scheduler = TickScheduler(config, reap_on_init=False)
    job = scheduler.registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    runs = scheduler.run_store.recent_runs(limit=5, job_id=job_id)
    return {
        "id": job.id,
        "name": job.name,
        "description": job.description,
        "schedule": job.schedule,
        "enabled": job.enabled,
        "trading_day_only": job.trading_day_only,
        "timeout_seconds": job.timeout_seconds,
        "workflow_id": job.workflow_id,
        "steps": [],
        "recent_runs": [ser.to_job_run_payload_v2(r).model_dump() for r in runs],
        "delivery_channels": list(job.delivery.channels),
    }


@router.patch("/jobs/{job_id}")
def patch_builtin_job(job_id: str, body: dict):
    """Update built-in job settings (currently delivery_channels only)."""
    config = load_app_config()
    scheduler = TickScheduler(config, reap_on_init=False)
    job = scheduler.registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if "delivery_channels" not in body:
        raise HTTPException(status_code=422, detail="No supported fields to update")

    channels = _normalize_delivery_channels(body["delivery_channels"])
    from trade_compass_agent.ops.builtin_job_delivery import BuiltinJobDeliveryStore

    BuiltinJobDeliveryStore(config.data_dir / "scheduler.db").set(job_id, channels)
    from trade_compass_agent.ops.tick_scheduler import reload_active_scheduler

    reload_active_scheduler()
    return {
        "id": job_id,
        "delivery_channels": list(channels),
    }


@router.post("/jobs/{job_id}/run", response_model=s.JobRunPayload)
def run_job(job_id: str) -> s.JobRunPayload:
    """Trigger a job in the background and return immediately with 'running' state."""
    import time as _time

    config = load_app_config()
    scheduler = TickScheduler(config, reap_on_init=False)
    if not scheduler.registry.get(job_id):
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")

    threading.Thread(
        target=scheduler.run_job_now,
        args=(job_id, "api"),
        daemon=True,
        name=f"job-{job_id}",
    ).start()

    # Give the executor a moment to insert the run record
    _time.sleep(0.3)
    runs = scheduler.run_store.recent_runs(limit=1, job_id=job_id)
    if not runs:
        raise HTTPException(status_code=500, detail="Run not recorded")
    return ser.to_job_run_payload_v2(runs[0])


def _validate_hhmm(value: str, field: str) -> None:
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=422, detail=f"{field} must be HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(status_code=422, detail=f"{field} must be a valid time")


_VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


@router.get("/config/scheduler", response_model=s.SchedulerConfigPayload)
def get_scheduler_config() -> s.SchedulerConfigPayload:
    config = load_app_config()
    return ser.to_scheduler_config_payload(config.scheduler)


@router.patch("/config/scheduler", response_model=s.SchedulerConfigUpdateResponse)
def patch_scheduler_config(body: s.SchedulerConfigUpdateRequest) -> s.SchedulerConfigUpdateResponse:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        config = load_app_config()
        return s.SchedulerConfigUpdateResponse(
            config=ser.to_scheduler_config_payload(config.scheduler),
            reloaded=False,
            message="No changes submitted.",
        )

    for field in (
        "premarket_time",
        "morning_plan_time",
        "close_time",
        "eod_review_time",
        "postmarket_time",
        "weekly_time",
    ):
        if field in updates:
            _validate_hhmm(str(updates[field]), field)

    if "weekly_day" in updates:
        day = str(updates["weekly_day"]).lower()
        if day not in _VALID_WEEKDAYS:
            raise HTTPException(status_code=422, detail="weekly_day must be mon-sun")
        updates["weekly_day"] = day

    config = update_scheduler_config(updates)

    from trade_compass_agent.ops.tick_scheduler import get_active_scheduler, reload_active_scheduler

    reloaded = reload_active_scheduler()
    message = "Scheduler reloaded in-process." if reloaded else "Config saved; restart serve to apply if scheduler was not running."

    active = get_active_scheduler()
    if active is not None and not config.scheduler.enabled:
        active.shutdown(wait=False)
        message = "Scheduler disabled and stopped."

    return s.SchedulerConfigUpdateResponse(
        config=ser.to_scheduler_config_payload(config.scheduler),
        reloaded=reloaded,
        message=message,
    )


@router.get("/notifications", response_model=list[s.NotificationPayload])
def get_notifications(limit: int = Query(30, ge=1, le=500)) -> list[s.NotificationPayload]:
    config = load_app_config()
    store = JsonNotificationStore(
        config.data_dir / "notifications.jsonl",
        max_records=config.notifications.max_records,
    )
    return [ser.to_notification_payload(item) for item in store.recent(limit)]


@router.get("/config/watchlists", response_model=s.WatchlistsResponse)
def get_watchlists() -> s.WatchlistsResponse:
    return ser.to_watchlists_payload(load_app_config().watchlists)


# --- Monitoring ---

@router.get("/metrics")
def get_metrics():
    """Operational metrics: signals, portfolio, scheduler, jobs."""
    from trade_compass_agent.web.monitoring import build_metrics
    return build_metrics()


# --- Channel inbound webhook ---

@router.post("/channels/inbound/{platform}")
async def channel_inbound(platform: str) -> JSONResponse:
    """Fail closed until platform-specific HTTP callback verification is implemented."""
    del platform
    return JSONResponse(
        status_code=501,
        content={
            "detail": "Inbound HTTP callbacks are disabled; use the authenticated gateway connection"
        },
    )


# --- Gateway status ---

@router.get("/channels/gateway/status")
async def gateway_status() -> JSONResponse:
    """Return status of all bidirectional messaging platform connections."""
    from trade_compass_agent.web.app import _gateway_daemon

    if _gateway_daemon is None:
        return JSONResponse(content={
            "enabled": False,
            "platforms": {},
        })

    platforms = {}
    for name, conn in _gateway_daemon.platforms.items():
        info = {
            "connected": conn.connected,
            "last_error": conn.last_error,
            "started_at": conn.started_at,
        }
        adapter = conn.adapter
        if name == "feishu_bot":
            subs = sorted(getattr(adapter, "_subscriber_chats", set()))
            info["subscriber_count"] = len(subs)
            info["subscriber_chat_ids"] = subs
            info["subscribers_file"] = str(getattr(adapter, "_subscribers_path", ""))
        platforms[name] = info

    return JSONResponse(content={
        "enabled": True,
        "platforms": platforms,
    })
