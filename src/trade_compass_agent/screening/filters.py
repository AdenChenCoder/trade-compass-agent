"""L1 hard filters — remove objectively unqualified stocks.

Only this layer uses pass/fail logic. Everything after L1 is scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from trade_compass_agent.screening.config import ScreeningConfig
from trade_compass_agent.screening.universe import StockInfo

logger = logging.getLogger(__name__)


@dataclass
class L1Result:
    passed: list[str]
    rejected: dict[str, str]  # symbol -> reason


def layer1_filter(
    stocks: list[StockInfo],
    df_map: dict[str, pd.DataFrame],
    cfg: ScreeningConfig,
    *,
    market_cap_map: dict[str, float] | None = None,
) -> L1Result:
    """Apply hard filters: board prefix, ST, market cap, liquidity.

    Args:
        stocks: full universe
        df_map: {symbol: OHLCV DataFrame with 'amount' column}
        cfg: screening config
        market_cap_map: {symbol: market_cap_in_yi}, optional
    """
    passed: list[str] = []
    rejected: dict[str, str] = {}

    name_map = {s.symbol: s.name for s in stocks}
    valid_symbols = {s.symbol for s in stocks}

    for symbol in valid_symbols:
        name = name_map.get(symbol, "")

        if cfg.boards and not any(symbol.startswith(b) for b in cfg.boards):
            rejected[symbol] = "board_excluded"
            continue

        if cfg.exclude_st and (name.upper().startswith("*ST") or name.upper().startswith("ST") or "退" in name):
            rejected[symbol] = "st_or_delisting"
            continue

        if market_cap_map:
            cap = market_cap_map.get(symbol)
            if cap is not None and cap < cfg.min_market_cap_yi:
                rejected[symbol] = f"market_cap={cap:.1f}yi < {cfg.min_market_cap_yi}"
                continue

        df = df_map.get(symbol)
        if df is None or df.empty:
            rejected[symbol] = "no_data"
            continue

        if "amount" in df.columns and len(df) >= 20:
            avg_amount = df["amount"].tail(20).mean()
            if avg_amount is not None and avg_amount < cfg.min_avg_amount_wan * 10000:
                rejected[symbol] = f"avg_amount={avg_amount/10000:.0f}wan < {cfg.min_avg_amount_wan}"
                continue

        passed.append(symbol)

    logger.info("L1 filter: %d passed, %d rejected", len(passed), len(rejected))
    return L1Result(passed=passed, rejected=rejected)
