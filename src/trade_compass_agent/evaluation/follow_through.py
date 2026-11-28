from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from trade_compass_agent.data import MarketDataProvider
from trade_compass_agent.domain import AuditEvent, Bar, EvaluationMetric, SignalFollowThrough
from trade_compass_agent.evaluation.metrics import MetricsEngine


NEUTRAL_SIGNAL_ACTIONS = frozenset({"hold", "observe", "neutral", "wait_pullback", "观望"})
BEARISH_SIGNAL_ACTIONS = frozenset(
    {"avoid", "sell", "strong_sell", "short", "reduce", "reject", "exit_trigger", "回避"}
)


def extract_signal_action(payload: dict) -> str:
    raw = payload.get("grade_out") or payload.get("action") or payload.get("rating") or ""
    val = raw.value if hasattr(raw, "value") else str(raw)
    if "." in val:
        val = val.rsplit(".", 1)[-1]
    return val.strip()


@dataclass(frozen=True)
class FollowThroughReport:
    results: list[SignalFollowThrough]
    metrics: list[EvaluationMetric]
    warnings: list[str]


class FollowThroughEvaluator:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider
        self.metrics = MetricsEngine()

    def evaluate(self, events: list[AuditEvent], limit: int = 200) -> FollowThroughReport:
        from trade_compass_agent.data.providers import _market_now, _prev_trading_date
        from trade_compass_agent.ops.trading_calendar import is_trading_day

        warnings: list[str] = []
        results: list[SignalFollowThrough] = []
        recommendations = select_signal_audit_events(events, limit=limit)
        now = _market_now()
        last_closed = _prev_trading_date(now.date(), now.hour)
        for event in recommendations:
            symbol = str(event.payload.get("symbol", ""))
            if not symbol:
                warnings.append(f"{event.id}: missing symbol")
                continue
            try:
                days_ago = max(0, (now.date() - event.timestamp.date()).days)
                bar_limit = 180 if days_ago <= 180 else min(1000, days_ago + 30)
                bars = self.provider.get_bars(symbol, timeframe="1d", limit=bar_limit)
                ft = self._evaluate_event(event, bars)
                results.append(ft)
                if bars and ft.status in {"no_entry_bar", "pending"}:
                    first_day = bars[0].timestamp.date()
                    latest_day = bars[-1].timestamp.date()
                    if first_day > ft.signal_date and _has_trading_day_between(ft.signal_date, first_day, is_trading_day):
                        warnings.append(
                            f"{event.id}/{symbol}: cached bars start at {first_day}, after signal date {ft.signal_date}"
                        )
                    elif latest_day < last_closed and ft.signal_date < last_closed:
                        warnings.append(
                            f"{event.id}/{symbol}: cached bars end at {latest_day} (last closed {last_closed}), follow-through incomplete"
                        )
            except Exception as exc:
                warnings.append(f"{event.id}/{symbol}: follow-through unavailable: {exc}")
        return FollowThroughReport(
            results=results,
            metrics=self.metrics.follow_through_metrics(results),
            warnings=warnings,
        )

    def _evaluate_event(self, event: AuditEvent, bars: list[Bar]) -> SignalFollowThrough:
        from trade_compass_agent.ops.trading_calendar import is_trading_day

        payload = event.payload
        symbol = str(payload.get("symbol", ""))
        action = extract_signal_action(payload)
        signal_date = event.timestamp.date()
        if bars and bars[0].timestamp.date() > signal_date and _has_trading_day_between(
            signal_date, bars[0].timestamp.date(), is_trading_day
        ):
            start_idx = None
        else:
            start_idx = _first_bar_on_or_after(bars, signal_date)
        if start_idx is None:
            return SignalFollowThrough(
                audit_id=event.id,
                symbol=symbol,
                action=action,
                signal_date=signal_date,
                entry_close=0.0,
                return_1d=None,
                return_3d=None,
                return_5d=None,
                max_runup=None,
                max_drawdown=None,
                status="no_entry_bar",
            )

        entry_idx = start_idx + 1
        if entry_idx >= len(bars):
            return SignalFollowThrough(
                audit_id=event.id,
                symbol=symbol,
                action=action,
                signal_date=signal_date,
                entry_close=0.0,
                return_1d=None,
                return_3d=None,
                return_5d=None,
                max_runup=None,
                max_drawdown=None,
                status="no_entry_bar",
            )

        entry = bars[entry_idx]
        entry_close = entry.close
        future = bars[entry_idx + 1 : entry_idx + 6]

        is_bearish = action.lower() in BEARISH_SIGNAL_ACTIONS

        r1 = _return_at(future, entry_close, 1)
        r3 = _return_at(future, entry_close, 3)
        r5 = _return_at(future, entry_close, 5)
        runup = _max_runup(future, entry_close)
        drawdown = _max_drawdown(future, entry_close)

        if is_bearish:
            r1 = round(-r1, 4) if r1 is not None else None
            r3 = round(-r3, 4) if r3 is not None else None
            r5 = round(-r5, 4) if r5 is not None else None
            runup, drawdown = (
                round(-drawdown, 4) if drawdown is not None else None,
                round(-runup, 4) if runup is not None else None,
            )

        status = "complete" if len(future) >= 5 else "pending"

        return SignalFollowThrough(
            audit_id=event.id,
            symbol=symbol,
            action=action,
            signal_date=signal_date,
            entry_close=entry_close,
            return_1d=r1,
            return_3d=r3,
            return_5d=r5,
            max_runup=runup,
            max_drawdown=drawdown,
            status=status,
        )


def select_signal_audit_events(events: list[AuditEvent], *, limit: int = 200) -> list[AuditEvent]:
    seen_ids: set[str] = set()
    selected: list[AuditEvent] = []
    for event in events:
        if event.event_type not in {"recommendation", "trading_signal"}:
            continue
        sig_id = str(event.payload.get("signal_id") or "")
        if sig_id:
            if sig_id in seen_ids:
                continue
            seen_ids.add(sig_id)
        selected.append(event)
    return selected[-limit:] if limit > 0 else []


def _has_trading_day_between(start_inclusive: date, end_exclusive: date, is_trading_day) -> bool:
    if (end_exclusive - start_inclusive).days > 10:
        return True
    cursor = start_inclusive
    while cursor < end_exclusive:
        if is_trading_day(cursor):
            return True
        cursor += timedelta(days=1)
    return False


def _first_bar_on_or_after(bars: list[Bar], target_date: date) -> int | None:
    for idx, bar in enumerate(bars):
        if bar.timestamp.date() >= target_date:
            return idx
    return None


def _return_at(future: list[Bar], entry_close: float, days: int) -> float | None:
    if entry_close <= 0 or len(future) < days:
        return None
    return round((future[days - 1].close - entry_close) / entry_close, 4)


def _max_runup(future: list[Bar], entry_close: float) -> float | None:
    if entry_close <= 0 or not future:
        return None
    return round((max(bar.high for bar in future) - entry_close) / entry_close, 4)


def _max_drawdown(future: list[Bar], entry_close: float) -> float | None:
    if entry_close <= 0 or not future:
        return None
    return round((min(bar.low for bar in future) - entry_close) / entry_close, 4)
