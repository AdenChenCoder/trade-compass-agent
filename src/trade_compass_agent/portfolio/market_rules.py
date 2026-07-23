"""Auto-detect A-share market rules based on symbol code prefix."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketRules:
    board: str
    price_limit_pct: float
    min_lot: int
    is_t0: bool


def infer_market_rules(symbol: str) -> MarketRules:
    """Infer trading rules from symbol code prefix.

    Returns board name, price limit %, minimum lot size, and T+0 eligibility.
    """
    if not symbol or len(symbol) < 3:
        return MarketRules(board="主板", price_limit_pct=0.10, min_lot=100, is_t0=False)

    prefix3 = symbol[:3]
    prefix2 = symbol[:2]

    # STAR Market (科创板)
    if prefix3 in ("688", "689"):
        return MarketRules(board="科创板", price_limit_pct=0.20, min_lot=200, is_t0=False)

    # ChiNext (创业板)
    if prefix3 in ("300", "301"):
        return MarketRules(board="创业板", price_limit_pct=0.20, min_lot=100, is_t0=False)

    # BSE (北交所)
    if symbol[0] == "8" and len(symbol) == 6:
        return MarketRules(board="北交所", price_limit_pct=0.30, min_lot=100, is_t0=False)

    # Bond ETF (T+0)
    if prefix3 == "511":
        return MarketRules(board="债券ETF", price_limit_pct=0.10, min_lot=100, is_t0=True)

    # Cross-border ETF (T+0)
    if prefix3 == "513":
        return MarketRules(board="跨境ETF", price_limit_pct=0.10, min_lot=100, is_t0=True)

    # Gold ETF (T+0)
    if prefix3 == "518":
        return MarketRules(board="黄金ETF", price_limit_pct=0.10, min_lot=100, is_t0=True)

    # Currency ETF (T+0)
    if symbol in ("511880", "511990", "511660"):
        return MarketRules(board="货币ETF", price_limit_pct=0.10, min_lot=100, is_t0=True)

    # Other ETFs (T+1)
    if prefix3 in ("510", "512", "515", "516", "159", "560", "588"):
        return MarketRules(board="ETF", price_limit_pct=0.10, min_lot=100, is_t0=False)

    # Convertible bonds (T+0, 10 units)
    if prefix2 in ("11", "12"):
        return MarketRules(board="可转债", price_limit_pct=0.20, min_lot=10, is_t0=True)

    # Default: main board SH/SZ
    return MarketRules(board="主板", price_limit_pct=0.10, min_lot=100, is_t0=False)
