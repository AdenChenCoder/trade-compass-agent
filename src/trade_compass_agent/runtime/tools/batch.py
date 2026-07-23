"""Batch data retrieval tools for multi-symbol scenarios.

- batch_get_bars: parallel get_bars via ThreadPoolExecutor (no upstream batch API)
- batch_get_fundamentals: native East Money ulist.np/get API (single HTTP call)
- batch_search_news: parallel per-symbol news fetch
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from trade_compass_agent.data.network import (
    extend_no_proxy_for_eastmoney,
    rate_limit_domain,
    run_with_timeout,
    short_error_message,
)
from trade_compass_agent.runtime.market_stack import MarketStack

logger = logging.getLogger(__name__)

_MAX_BARS_SYMBOLS = 20
_MAX_FUNDAMENTALS_SYMBOLS = 30
_MAX_NEWS_SYMBOLS = 10


def _parse_symbols(raw: str, max_count: int) -> list[str]:
    codes = [s.strip() for s in raw.split(",") if s.strip()]
    return codes[:max_count]


def _to_secid(symbol: str) -> str:
    """Convert A-share code to East Money secid (e.g. 600519 -> 1.600519)."""
    return f"0.{symbol}" if symbol.startswith(("0", "3")) else f"1.{symbol}"


# ---------------------------------------------------------------------------
# batch_get_bars
# ---------------------------------------------------------------------------

def tool_batch_get_bars(
    stack: MarketStack,
    *,
    symbols: str,
    timeframe: str = "1d",
    limit: int = 60,
    summary_only: bool = True,
) -> str:
    """Fetch OHLCV bars for multiple symbols in parallel.

    symbols: comma-separated stock codes, e.g. "600519,300750,000001"
    summary_only: if true, return compact per-symbol summaries instead of full bar data
    """
    codes = _parse_symbols(symbols, _MAX_BARS_SYMBOLS)
    if not codes:
        return json.dumps({"error": "no valid symbols"}, ensure_ascii=False)

    results: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def fetch_one(sym: str) -> tuple[str, list | None, str | None]:
        try:
            bars = stack.provider.get_bars(sym, timeframe=timeframe, limit=limit)
            return sym, bars, None
        except Exception as exc:
            return sym, None, short_error_message(exc)

    workers = min(8, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            sym, bars, err = fut.result()
            if err or not bars:
                errors[sym] = err or "no data"
                continue
            if summary_only:
                latest = bars[-1]
                prev_close = bars[-2].close if len(bars) >= 2 else latest.open
                change_pct = ((latest.close - prev_close) / prev_close * 100) if prev_close else 0
                results[sym] = {
                    "close": latest.close,
                    "change_pct": round(change_pct, 2),
                    "high": max(b.high for b in bars[-5:]),
                    "low": min(b.low for b in bars[-5:]),
                    "volume": latest.volume,
                    "date": latest.timestamp.strftime("%Y-%m-%d"),
                    "bars_count": len(bars),
                }
            else:
                tail = bars[-min(limit, 30):]
                results[sym] = {
                    "bars_count": len(bars),
                    "bars": [
                        {
                            "date": b.timestamp.strftime("%Y-%m-%d"),
                            "O": b.open, "H": b.high, "L": b.low, "C": b.close,
                            "V": b.volume,
                        }
                        for b in tail
                    ],
                }

    return json.dumps(
        {
            "timeframe": timeframe,
            "summary_only": summary_only,
            "count": len(results),
            "results": results,
            **({"errors": errors} if errors else {}),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# batch_get_fundamentals  (native East Money ulist.np/get)
# ---------------------------------------------------------------------------

_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_ULIST_FIELDS = "f2,f3,f12,f14,f116,f117,f127,f162,f167"
_ULIST_FIELD_MAP = {
    "f12": "symbol",
    "f14": "name",
    "f2": "price",
    "f3": "change_pct",
    "f116": "total_market_cap",
    "f117": "float_market_cap",
    "f127": "industry",
    "f162": "pe_ttm",
    "f167": "pb",
}


def _fetch_ulist_batch(secids: list[str]) -> list[dict] | None:
    """One HTTP call to East Money ulist for multiple secids."""
    extend_no_proxy_for_eastmoney()
    rate_limit_domain(_ULIST_URL)
    params = {
        "fltt": "2",
        "secids": ",".join(secids),
        "fields": _ULIST_FIELDS,
    }
    try:
        resp = requests.get(
            _ULIST_URL,
            params=params,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"},
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("ulist batch failed: %s", short_error_message(exc))
        return None

    diff = (data.get("data") or {}).get("diff")
    if not diff:
        return None

    rows: list[dict] = []
    for item in diff:
        row = {}
        for field_key, name in _ULIST_FIELD_MAP.items():
            val = item.get(field_key)
            if val == "-" or val is None:
                row[name] = None
            elif isinstance(val, (int, float)) and name in ("pe_ttm", "pb", "change_pct"):
                row[name] = round(float(val), 2)
            elif isinstance(val, (int, float)):
                row[name] = val
            else:
                row[name] = val
        rows.append(row)
    return rows


def _fallback_fundamentals(stack: MarketStack, codes: list[str]) -> dict[str, dict]:
    """Parallel per-symbol fallback via existing ChainFundamentalsProvider."""
    results: dict[str, dict] = {}

    def fetch_one(sym: str):
        try:
            bars = stack.provider.get_bars(sym, timeframe="1d", limit=120)
            snap = stack.fundamentals_provider.get_snapshot(sym, bars=bars)
            return sym, {
                "symbol": sym,
                "pe_ttm": snap.pe_ttm,
                "pb": snap.pb,
                "total_market_cap": snap.market_cap,
                "industry": snap.industry,
                "provider": snap.provider_name,
            }, None
        except Exception as exc:
            return sym, None, short_error_message(exc)

    workers = min(6, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, c) for c in codes]
        for fut in as_completed(futures):
            sym, data, err = fut.result()
            if data:
                results[sym] = data
            else:
                results[sym] = {"symbol": sym, "error": err or "no data"}
    return results


def tool_batch_get_fundamentals(stack: MarketStack, *, symbols: str) -> str:
    """Fetch fundamentals (PE, PB, market cap, industry) for multiple symbols.

    Uses East Money batch API (one HTTP call) with per-symbol fallback.
    symbols: comma-separated stock codes, e.g. "600519,300750,000001"
    """
    codes = _parse_symbols(symbols, _MAX_FUNDAMENTALS_SYMBOLS)
    if not codes:
        return json.dumps({"error": "no valid symbols"}, ensure_ascii=False)

    secids = [_to_secid(c) for c in codes]
    rows = _fetch_ulist_batch(secids)

    if rows:
        result_map: dict[str, dict] = {}
        for row in rows:
            sym = str(row.get("symbol") or "")
            if sym:
                result_map[sym] = row
        missing = [c for c in codes if c not in result_map]
        if missing:
            fallback = _fallback_fundamentals(stack, missing)
            result_map.update(fallback)
        return json.dumps(
            {
                "source": "eastmoney_ulist_batch",
                "count": len(result_map),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "results": result_map,
            },
            ensure_ascii=False,
        )

    logger.info("ulist batch unavailable, falling back to parallel per-symbol fetch")
    fallback = _fallback_fundamentals(stack, codes)
    return json.dumps(
        {
            "source": "fallback_parallel",
            "count": len(fallback),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "results": fallback,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# batch_search_news
# ---------------------------------------------------------------------------

def tool_batch_search_news(stack: MarketStack, *, symbols: str, limit_per_symbol: int = 5) -> str:
    """Search recent news for multiple symbols in parallel.

    symbols: comma-separated stock codes, e.g. "600519,300750"
    limit_per_symbol: max news items per symbol (default 5)
    """
    from trade_compass_agent.runtime.tools.search import tool_search_stock_news

    codes = _parse_symbols(symbols, _MAX_NEWS_SYMBOLS)
    if not codes:
        return json.dumps({"error": "no valid symbols"}, ensure_ascii=False)

    results: dict[str, list] = {}
    errors: dict[str, str] = {}

    def fetch_news(sym: str):
        try:
            raw = run_with_timeout(
                lambda: tool_search_stock_news(stack, symbol=sym, limit=limit_per_symbol),
                timeout=8.0,
                description=f"search-news:{sym}",
            )
            parsed = json.loads(raw)
            items = parsed.get("news") or parsed.get("articles") or []
            compact = [
                {"title": it.get("title", ""), "date": it.get("date", "")}
                for it in items[:limit_per_symbol]
            ]
            return sym, compact, None
        except Exception as exc:
            return sym, None, short_error_message(exc)

    workers = min(4, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_news, c): c for c in codes}
        for fut in as_completed(futures):
            sym, items, err = fut.result()
            if err:
                errors[sym] = err
            else:
                results[sym] = items or []

    return json.dumps(
        {
            "count": len(results),
            "limit_per_symbol": limit_per_symbol,
            "results": results,
            **({"errors": errors} if errors else {}),
        },
        ensure_ascii=False,
    )
