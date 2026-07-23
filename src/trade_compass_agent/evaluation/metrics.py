from __future__ import annotations

from statistics import mean

from trade_compass_agent.domain import EvaluationMetric, SignalFollowThrough


class MetricsEngine:
    def signal_return_metrics(self, returns: list[float]) -> list[EvaluationMetric]:
        if not returns:
            return [EvaluationMetric(name="sample_count", value=0, unit="signals")]
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value <= 0]
        return [
            EvaluationMetric(name="sample_count", value=len(returns), unit="signals"),
            EvaluationMetric(name="average_return", value=round(mean(returns), 4), unit="pct"),
            EvaluationMetric(name="hit_rate", value=round(len(wins) / len(returns), 4), unit="ratio"),
            EvaluationMetric(name="average_loss", value=round(mean(losses), 4) if losses else 0, unit="pct"),
        ]

    def follow_through_metrics(self, results: list[SignalFollowThrough]) -> list[EvaluationMetric]:
        completed = [item for item in results if item.status == "complete"]
        returns_1d = [item.return_1d for item in completed if item.return_1d is not None]
        returns_3d = [item.return_3d for item in completed if item.return_3d is not None]
        returns_5d = [item.return_5d for item in completed if item.return_5d is not None]
        runups = [item.max_runup for item in completed if item.max_runup is not None]
        drawdowns = [item.max_drawdown for item in completed if item.max_drawdown is not None]

        metrics = [
            EvaluationMetric(name="sample_count", value=len(results), unit="signals"),
            EvaluationMetric(name="completed_count", value=len(completed), unit="signals"),
        ]
        metrics.extend(_return_metrics("return_1d", returns_1d))
        metrics.extend(_return_metrics("return_3d", returns_3d))
        metrics.extend(_return_metrics("return_5d", returns_5d))
        if runups:
            metrics.append(EvaluationMetric(name="average_max_runup", value=round(mean(runups), 4), unit="pct"))
        if drawdowns:
            metrics.append(
                EvaluationMetric(name="average_max_drawdown", value=round(mean(drawdowns), 4), unit="pct")
            )
        return metrics


def _return_metrics(prefix: str, values: list[float]) -> list[EvaluationMetric]:
    if not values:
        return [
            EvaluationMetric(name=f"{prefix}_count", value=0, unit="signals"),
            EvaluationMetric(name=f"{prefix}_hit_rate", value=0, unit="ratio"),
        ]
    wins = [value for value in values if value > 0]
    return [
        EvaluationMetric(name=f"{prefix}_count", value=len(values), unit="signals"),
        EvaluationMetric(name=f"{prefix}_average", value=round(mean(values), 4), unit="pct"),
        EvaluationMetric(name=f"{prefix}_hit_rate", value=round(len(wins) / len(values), 4), unit="ratio"),
    ]
