from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trade_compass_agent.data import MarketDataProvider
from trade_compass_agent.domain import AuditEvent, Bar, EvaluationMetric, SignalFollowThrough
from trade_compass_agent.evaluation.metrics import MetricsEngine


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
        warnings: list[str] = []
        results: list[SignalFollowThrough] = []
        recommendations = [event for event in events if event.event_type == "recommendation"][-limit:]
        for event in recommendations:
            symbol = str(event.payload.get("symbol", ""))
            if not symbol:
                warnings.append(f"{event.id}: missing symbol")
                continue
            try:
                bars = self.provider.get_bars(symbol, timeframe="1d", limit=180)
                results.append(self._evaluate_event(event, bars))
            except Exception as exc:
                warnings.append(f"{event.id}/{symbol}: follow-through unavailable: {exc}")
        return FollowThroughReport(
            results=results,
            metrics=self.metrics.follow_through_metrics(results),
            warnings=warnings,
        )

    def _evaluate_event(self, event: AuditEvent, bars: list[Bar]) -> SignalFollowThrough:
        payload = event.payload
        symbol = str(payload.get("symbol", ""))
        action = str(payload.get("grade_out") or payload.get("action") or "")
        signal_date = event.timestamp.date()
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

        is_bearish = action.lower() in {"avoid", "sell", "short", "reduce", "观望", "回避"}

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
