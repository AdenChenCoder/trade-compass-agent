from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from trade_compass_agent.channels.base import ChannelMessage
from trade_compass_agent.config import AppConfig
from trade_compass_agent.concurrency import atomic_write, get_path_lock
from trade_compass_agent.domain import Bar, Notification
from trade_compass_agent.ops.delivery import _build_channel_router
from trade_compass_agent.ops.notifications import JsonNotificationStore, NotificationCenter
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.specialists.run import run_specialist

logger = logging.getLogger(__name__)

ConditionType = Literal[
    "price_above",
    "price_below",
    "change_pct_above",
    "change_pct_below",
    "day_change_pct_above",
    "day_change_pct_below",
    "volume_ratio_above",
]
MatchPolicy = Literal["all", "any"]


@dataclass(frozen=True)
class WatchCondition:
    type: ConditionType
    threshold: float
    timeframe: str = "1m"
    lookback: int = 20
    label: str = ""


@dataclass(frozen=True)
class WatchPlan:
    id: str
    name: str
    symbols: list[str]
    conditions: list[WatchCondition]
    enabled: bool = True
    interval_seconds: int = 60
    match_policy: MatchPolicy = "all"
    analysis_prompt: str = ""
    notification_channels: list[str] = field(default_factory=lambda: ["web_log"])
    cooldown_minutes: int = 30
    created_by: str = "agent"
    updated_by: str = "agent"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class ConditionResult:
    condition: WatchCondition
    matched: bool
    actual: float | None
    message: str


@dataclass(frozen=True)
class WatchTriggerEvent:
    id: str
    plan_id: str
    plan_name: str
    symbol: str
    triggered_at: datetime
    results: list[ConditionResult]
    analysis: str


class WatchPlanStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_all(self) -> list[WatchPlan]:
        return [self._decode_plan(item) for item in self._read_raw().get("plans", [])]

    def list_enabled(self) -> list[WatchPlan]:
        return [plan for plan in self.list_all() if plan.enabled]

    def get(self, plan_id: str) -> WatchPlan | None:
        for plan in self.list_all():
            if plan.id == plan_id:
                return plan
        return None

    def create(
        self,
        *,
        name: str,
        symbols: list[str],
        conditions: list[WatchCondition],
        enabled: bool = True,
        interval_seconds: int = 60,
        match_policy: MatchPolicy = "all",
        analysis_prompt: str = "",
        notification_channels: list[str] | None = None,
        cooldown_minutes: int = 30,
        created_by: str = "agent",
    ) -> WatchPlan:
        now = datetime.now()
        plan = WatchPlan(
            id=uuid.uuid4().hex[:12],
            name=name,
            symbols=_dedupe_symbols(symbols),
            conditions=conditions,
            enabled=enabled,
            interval_seconds=_normalize_interval(interval_seconds),
            match_policy=match_policy,
            analysis_prompt=analysis_prompt,
            notification_channels=notification_channels or ["web_log"],
            cooldown_minutes=max(int(cooldown_minutes), 1),
            created_by=created_by,
            updated_by=created_by,
            created_at=now,
            updated_at=now,
        )
        plans = self.list_all()
        plans.append(plan)
        self._write_plans(plans)
        return plan

    def update(self, plan_id: str, **updates: Any) -> WatchPlan | None:
        plans = self.list_all()
        updated: WatchPlan | None = None
        result: list[WatchPlan] = []
        for plan in plans:
            if plan.id != plan_id:
                result.append(plan)
                continue
            data = _plan_to_dict(plan)
            data.update(updates)
            data["id"] = plan.id
            data["created_at"] = plan.created_at.isoformat()
            data["updated_at"] = datetime.now().isoformat()
            if "symbols" in data:
                data["symbols"] = _dedupe_symbols(list(data["symbols"]))
            if "conditions" in data:
                data["conditions"] = [_condition_to_dict(_coerce_condition(c)) for c in data["conditions"]]
            if "interval_seconds" in data:
                data["interval_seconds"] = _normalize_interval(int(data["interval_seconds"]))
            updated = self._decode_plan(data)
            result.append(updated)
        if updated is None:
            return None
        self._write_plans(result)
        return updated

    def delete(self, plan_id: str) -> bool:
        plans = self.list_all()
        remaining = [plan for plan in plans if plan.id != plan_id]
        if len(remaining) == len(plans):
            return False
        self._write_plans(remaining)
        return True

    def due_plans(self, now: datetime) -> list[WatchPlan]:
        raw = self._read_raw()
        last_checked = raw.get("last_checked", {})
        due: list[WatchPlan] = []
        for plan in self.list_enabled():
            checked_at = _parse_dt(last_checked.get(plan.id))
            if checked_at is None or (now - checked_at).total_seconds() >= plan.interval_seconds:
                due.append(plan)
        return due

    def mark_checked(self, plan_id: str, checked_at: datetime) -> None:
        with get_path_lock(self.path):
            raw = self._read_raw()
            last_checked = dict(raw.get("last_checked", {}) or {})
            last_checked[plan_id] = checked_at.isoformat()
            raw["last_checked"] = last_checked
            self._write_raw(raw)

    def should_trigger(self, plan: WatchPlan, symbol: str, now: datetime) -> bool:
        raw = self._read_raw()
        key = f"{plan.id}:{symbol}"
        last_triggered = _parse_dt((raw.get("last_triggered", {}) or {}).get(key))
        if last_triggered is None:
            return True
        return (now - last_triggered) >= timedelta(minutes=plan.cooldown_minutes)

    def record_trigger(self, event: WatchTriggerEvent) -> None:
        with get_path_lock(self.path):
            raw = self._read_raw()
            events = list(raw.get("events", []) or [])
            events.append(_event_to_dict(event))
            raw["events"] = events[-500:]
            last_triggered = dict(raw.get("last_triggered", {}) or {})
            last_triggered[f"{event.plan_id}:{event.symbol}"] = event.triggered_at.isoformat()
            raw["last_triggered"] = last_triggered
            self._write_raw(raw)

    def recent_events(self, limit: int = 50) -> list[WatchTriggerEvent]:
        raw = self._read_raw()
        events = [_decode_event(item) for item in raw.get("events", []) or []]
        return events[-limit:]

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"plans": [], "events": [], "last_checked": {}, "last_triggered": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"plans": [], "events": [], "last_checked": {}, "last_triggered": {}}
        return {
            "plans": list(raw.get("plans", []) or []),
            "events": list(raw.get("events", []) or []),
            "last_checked": dict(raw.get("last_checked", {}) or {}),
            "last_triggered": dict(raw.get("last_triggered", {}) or {}),
        }

    def _write_plans(self, plans: list[WatchPlan]) -> None:
        with get_path_lock(self.path):
            raw = self._read_raw()
            raw["plans"] = [_plan_to_dict(plan) for plan in plans]
            self._write_raw(raw)

    def _write_raw(self, raw: dict[str, Any]) -> None:
        atomic_write(self.path, json.dumps(raw, ensure_ascii=False, indent=2, default=str) + "\n")

    def _decode_plan(self, raw: dict[str, Any]) -> WatchPlan:
        return WatchPlan(
            id=str(raw["id"]),
            name=str(raw["name"]),
            symbols=_dedupe_symbols(list(raw.get("symbols", []))),
            conditions=[_coerce_condition(item) for item in raw.get("conditions", [])],
            enabled=bool(raw.get("enabled", True)),
            interval_seconds=_normalize_interval(int(raw.get("interval_seconds", 60))),
            match_policy="any" if raw.get("match_policy") == "any" else "all",
            analysis_prompt=str(raw.get("analysis_prompt", "")),
            notification_channels=list(raw.get("notification_channels", ["web_log"])),
            cooldown_minutes=max(int(raw.get("cooldown_minutes", 30)), 1),
            created_by=str(raw.get("created_by", "agent")),
            updated_by=str(raw.get("updated_by", "agent")),
            created_at=_parse_dt(raw.get("created_at")) or datetime.now(),
            updated_at=_parse_dt(raw.get("updated_at")) or datetime.now(),
        )


class WatchPlanMonitor:
    def __init__(
        self,
        config: AppConfig,
        store: WatchPlanStore | None = None,
        stack: MarketStack | None = None,
    ) -> None:
        self.config = config
        self.store = store or WatchPlanStore(config.data_dir / "watch_plans.json")
        self.stack = stack or MarketStack.from_config(config)
        self.notifications = NotificationCenter(
            config,
            store=JsonNotificationStore(
                config.data_dir / "notifications.jsonl",
                max_records=config.notifications.max_records,
            ),
        )

    def tick(self, now: datetime | None = None) -> list[WatchTriggerEvent]:
        current = now or datetime.now()
        events: list[WatchTriggerEvent] = []
        for plan in self.store.due_plans(current):
            try:
                events.extend(self.evaluate_plan(plan, now=current))
            except Exception:
                logger.exception("Watch plan %s evaluation failed", plan.id)
            finally:
                self.store.mark_checked(plan.id, current)
        return events

    def evaluate_plan(self, plan: WatchPlan, now: datetime | None = None) -> list[WatchTriggerEvent]:
        current = now or datetime.now()
        events: list[WatchTriggerEvent] = []
        for symbol in plan.symbols:
            if not self.store.should_trigger(plan, symbol, current):
                continue
            bars_by_timeframe = self._fetch_bars(symbol, plan.conditions)
            results = [evaluate_condition(condition, bars_by_timeframe[condition.timeframe]) for condition in plan.conditions]
            matched = any(r.matched for r in results) if plan.match_policy == "any" else all(r.matched for r in results)
            if not matched:
                continue
            analysis = self._analyze(plan, symbol, results)
            event = WatchTriggerEvent(
                id=uuid.uuid4().hex[:12],
                plan_id=plan.id,
                plan_name=plan.name,
                symbol=symbol,
                triggered_at=current,
                results=results,
                analysis=analysis,
            )
            self.store.record_trigger(event)
            self._notify(plan, event)
            events.append(event)
        return events

    def _fetch_bars(self, symbol: str, conditions: list[WatchCondition]) -> dict[str, list[Bar]]:
        bars_by_timeframe: dict[str, list[Bar]] = {}
        for timeframe in sorted({condition.timeframe for condition in conditions}):
            limit = max([condition.lookback for condition in conditions if condition.timeframe == timeframe] + [2])
            bars_by_timeframe[timeframe] = self.stack.provider.get_bars(symbol, timeframe=timeframe, limit=limit)
        return bars_by_timeframe

    def _analyze(self, plan: WatchPlan, symbol: str, results: list[ConditionResult]) -> str:
        facts = "\n".join(f"- {item.message}" for item in results)
        task = (
            f"盯盘计划「{plan.name}」已触发，标的 {symbol}。\n"
            f"触发条件：\n{facts}\n\n"
            "请基于盘中技术面、市场情绪和风险给出简洁结论：是否继续关注、是否需要等待确认、失效条件是什么。"
        )
        if plan.analysis_prompt.strip():
            task += f"\n\n用户补充要求：{plan.analysis_prompt.strip()}"
        return run_specialist(self.stack, "intraday_tech", task, config=self.config)

    def _notify(self, plan: WatchPlan, event: WatchTriggerEvent) -> None:
        title = f"智能盯盘触发: {event.symbol}"
        condition_text = "\n".join(f"- {item.message}" for item in event.results)
        content = (
            f"计划：{event.plan_name}\n"
            f"标的：{event.symbol}\n"
            f"时间：{event.triggered_at.isoformat(timespec='seconds')}\n\n"
            f"条件：\n{condition_text}\n\n"
            f"Agent 分析：\n{event.analysis}"
        )
        if "web_log" in plan.notification_channels:
            self.notifications.send(Notification(
                channel=f"watch_plan:{plan.id}",
                title=title,
                message=content,
                severity="warning",
            ))
        external_channels = [item for item in plan.notification_channels if item != "web_log"]
        if not external_channels:
            return
        router = _build_channel_router()
        message = ChannelMessage(title=title, content=content, severity="warning")
        for channel in external_channels:
            adapter = router.get_adapter(channel)
            if adapter is None:
                logger.warning("No adapter found for watch-plan channel %r", channel)
                continue
            try:
                adapter.send_sync(message)
            except Exception as exc:
                logger.warning("Watch-plan delivery to %s failed: %s", channel, exc)


def generate_default_watch_plan(config: AppConfig, store: WatchPlanStore) -> WatchPlan:
    stack = MarketStack.from_config(config)
    symbols = config.watchlists.premarket_symbols()
    if not symbols:
        raise ValueError("watchlists are empty")
    # Rule-based premarket seed: catch decisive intraday breakouts and volume expansion.
    conditions = [
        WatchCondition(type="day_change_pct_above", threshold=2.5, timeframe="1m", lookback=20, label="日内涨幅超过 2.5%"),
        WatchCondition(type="volume_ratio_above", threshold=1.8, timeframe="1m", lookback=20, label="分钟量能放大 1.8 倍"),
    ]
    first_symbol = symbols[0]
    try:
        latest = stack.provider.get_bars(first_symbol, timeframe="1d", limit=1)[-1].close
        prompt = f"盘前参考：{first_symbol} 昨收约 {latest:.2f}。触发后优先判断是否为有效放量突破。"
    except Exception:
        prompt = "触发后优先判断是否为有效放量突破，并给出失效条件。"
    return store.create(
        name=f"盘前智能盯盘 {datetime.now().strftime('%Y-%m-%d')}",
        symbols=symbols,
        conditions=conditions,
        interval_seconds=60,
        match_policy="all",
        analysis_prompt=prompt,
        notification_channels=list(config.notifications.channels or ["web_log"]),
        cooldown_minutes=30,
        created_by="agent",
    )


def evaluate_condition(condition: WatchCondition, bars: list[Bar]) -> ConditionResult:
    if not bars:
        return ConditionResult(condition, False, None, f"{condition.label or condition.type}: 无行情数据")
    latest = bars[-1]
    label = condition.label or condition.type
    if condition.type == "price_above":
        actual = latest.close
        return _result(condition, actual >= condition.threshold, actual, f"{label}: 最新价 {actual:.3f} >= {condition.threshold:.3f}")
    if condition.type == "price_below":
        actual = latest.close
        return _result(condition, actual <= condition.threshold, actual, f"{label}: 最新价 {actual:.3f} <= {condition.threshold:.3f}")
    if condition.type in ("change_pct_above", "change_pct_below"):
        if len(bars) < 2 or bars[-2].close == 0:
            return ConditionResult(condition, False, None, f"{label}: 可比较 K 线不足")
        actual = (latest.close / bars[-2].close - 1) * 100
        matched = actual >= condition.threshold if condition.type.endswith("above") else actual <= condition.threshold
        op = ">=" if condition.type.endswith("above") else "<="
        return _result(condition, matched, actual, f"{label}: 最近涨跌幅 {actual:.2f}% {op} {condition.threshold:.2f}%")
    if condition.type in ("day_change_pct_above", "day_change_pct_below"):
        day_open = bars[0].open
        if day_open == 0:
            return ConditionResult(condition, False, None, f"{label}: 开盘价不可用")
        actual = (latest.close / day_open - 1) * 100
        matched = actual >= condition.threshold if condition.type.endswith("above") else actual <= condition.threshold
        op = ">=" if condition.type.endswith("above") else "<="
        return _result(condition, matched, actual, f"{label}: 日内涨跌幅 {actual:.2f}% {op} {condition.threshold:.2f}%")
    if condition.type == "volume_ratio_above":
        previous = bars[:-1]
        if not previous:
            return ConditionResult(condition, False, None, f"{label}: 量能比较样本不足")
        avg_volume = sum(bar.volume for bar in previous) / len(previous)
        if avg_volume <= 0:
            return ConditionResult(condition, False, None, f"{label}: 平均成交量不可用")
        actual = latest.volume / avg_volume
        return _result(condition, actual >= condition.threshold, actual, f"{label}: 量比 {actual:.2f} >= {condition.threshold:.2f}")
    return ConditionResult(condition, False, None, f"{label}: 不支持的条件类型 {condition.type}")


def _result(condition: WatchCondition, matched: bool, actual: float, message: str) -> ConditionResult:
    return ConditionResult(condition, matched, actual, f"{message} ({'命中' if matched else '未命中'})")


def _coerce_condition(raw: WatchCondition | dict[str, Any]) -> WatchCondition:
    if isinstance(raw, WatchCondition):
        return raw
    return WatchCondition(
        type=str(raw.get("type", "price_above")),  # type: ignore[arg-type]
        threshold=float(raw.get("threshold", 0)),
        timeframe=str(raw.get("timeframe", "1m")),
        lookback=max(int(raw.get("lookback", 20)), 2),
        label=str(raw.get("label", "")),
    )


def _condition_to_dict(condition: WatchCondition) -> dict[str, Any]:
    return {
        "type": condition.type,
        "threshold": condition.threshold,
        "timeframe": condition.timeframe,
        "lookback": condition.lookback,
        "label": condition.label,
    }


def _plan_to_dict(plan: WatchPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "symbols": plan.symbols,
        "conditions": [_condition_to_dict(item) for item in plan.conditions],
        "enabled": plan.enabled,
        "interval_seconds": plan.interval_seconds,
        "match_policy": plan.match_policy,
        "analysis_prompt": plan.analysis_prompt,
        "notification_channels": plan.notification_channels,
        "cooldown_minutes": plan.cooldown_minutes,
        "created_by": plan.created_by,
        "updated_by": plan.updated_by,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }


def _event_to_dict(event: WatchTriggerEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "plan_id": event.plan_id,
        "plan_name": event.plan_name,
        "symbol": event.symbol,
        "triggered_at": event.triggered_at.isoformat(),
        "results": [
            {
                "condition": _condition_to_dict(item.condition),
                "matched": item.matched,
                "actual": item.actual,
                "message": item.message,
            }
            for item in event.results
        ],
        "analysis": event.analysis,
    }


def _decode_event(raw: dict[str, Any]) -> WatchTriggerEvent:
    return WatchTriggerEvent(
        id=str(raw["id"]),
        plan_id=str(raw["plan_id"]),
        plan_name=str(raw.get("plan_name", "")),
        symbol=str(raw["symbol"]),
        triggered_at=_parse_dt(raw.get("triggered_at")) or datetime.now(),
        results=[
            ConditionResult(
                condition=_coerce_condition(item.get("condition", {})),
                matched=bool(item.get("matched", False)),
                actual=float(item["actual"]) if item.get("actual") is not None else None,
                message=str(item.get("message", "")),
            )
            for item in raw.get("results", []) or []
        ],
        analysis=str(raw.get("analysis", "")),
    )


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = str(symbol).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _normalize_interval(value: int) -> int:
    allowed = {60, 120, 300}
    if value not in allowed:
        raise ValueError("interval_seconds must be one of 60, 120, 300")
    return value
