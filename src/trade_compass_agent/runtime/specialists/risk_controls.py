"""Shared risk controls used by specialist execution strategies."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from trade_compass_agent.config import AppConfig
from trade_compass_agent.domain.signals import SignalRating, TradingSignal
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.types import TurnEvent

logger = logging.getLogger(__name__)


def apply_risk_warnings(
    stack: MarketStack,
    signal: TradingSignal,
    *,
    config: AppConfig | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
) -> TradingSignal:
    """Append portfolio risk warnings without changing the signal rating."""
    del config
    if signal.rating not in (SignalRating.BUY, SignalRating.STRONG_BUY):
        return signal

    warnings: list[str] = []
    try:
        from trade_compass_agent.runtime.tools.portfolio import tool_analyze_portfolio

        portfolio_raw = tool_analyze_portfolio(stack)
        portfolio = json.loads(portfolio_raw)

        concentration = portfolio.get("concentration_top5", [])
        for item in concentration:
            account_capital_pct = item.get("account_capital_pct")
            if isinstance(account_capital_pct, (int, float)) and account_capital_pct > 25:
                warnings.append(
                    f"集中度提示: {item.get('symbol', '?')} 占所属账户额度 "
                    f"{account_capital_pct:.0f}%"
                )
                if on_event:
                    on_event(
                        TurnEvent(
                            event="risk_warning",
                            data={
                                "symbol": signal.symbol,
                                "type": "concentration",
                                "detail": f"{account_capital_pct:.0f}%",
                            },
                        )
                    )

        total_positions = portfolio.get("total_positions", 0)
        if total_positions >= 8:
            warnings.append(f"持仓数提示: 当前持仓 {total_positions} 只")
            if on_event:
                on_event(
                    TurnEvent(
                        event="risk_warning",
                        data={
                            "symbol": signal.symbol,
                            "type": "position_count",
                            "detail": str(total_positions),
                        },
                    )
                )

    except Exception as exc:
        logger.debug("Risk warning check failed: %s", exc)

    if not warnings:
        return signal

    return TradingSignal(
        **{
            **signal.model_dump(),
            "reasoning": signal.reasoning + " " + " ".join(warnings),
        }
    )
