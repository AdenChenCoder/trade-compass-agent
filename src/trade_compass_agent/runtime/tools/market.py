from __future__ import annotations

import json
import math
import re
from datetime import datetime

from trade_compass_agent.data.quality import DataQualityLayer
from trade_compass_agent.data.metadata import bars_metadata
from trade_compass_agent.runtime.market_stack import MarketStack


def _meta_tag(source: str, freshness: str = "realtime") -> dict:
    return {
        "source": source,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "freshness": freshness,
    }


def tool_get_bars(stack: MarketStack, *, symbol: str, timeframe: str = "1d", limit: int = 60) -> str:
    symbols = [value for value in re.split(r"[,，;；\s]+", symbol.strip()) if value]
    if len(symbols) != 1:
        return json.dumps({"error": "get_bars 只接受一个标的；多个标的请调用 batch_get_bars。",
            "error_type": "invalid_arguments", "symbol": symbol, "symbols": symbols, "timeframe": timeframe,
            "suggested_tool": "batch_get_bars", "suggested_arguments": {
                "symbols": ",".join(symbols), "timeframe": timeframe, "limit": limit}}, ensure_ascii=False)
    bars = stack.provider.get_bars(symbol.strip(), timeframe=timeframe, limit=limit)
    returned = bars[-min(limit, 30):]
    quality = DataQualityLayer().check_bars(bars)
    provider_name = getattr(stack.provider, "last_resolved_provider", None) or getattr(stack.provider, "name", "unknown")
    payload = {
        "_meta": bars_metadata(bars, provider_name, timeframe),
        "as_of": bars[-1].timestamp.isoformat() if bars else None,
        "adjusted": bars[-1].adjusted if bars else None,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(bars),
        "returned_count": len(returned),
        "bars_truncated": len(returned) < len(bars),
        "provider": provider_name,
        "warnings": quality.warnings,
        "bars": [
            {
                "timestamp": bar.timestamp.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
                "adjusted": bar.adjusted,
                **({"source": bar.source} if bar.source else {}),
                **({"turnover_pct": bar.turnover_pct} if bar.turnover_pct is not None else {}),
            }
            for bar in returned
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _fetch_major_indices() -> list[dict]:
    """Use dated quotes; response time alone cannot certify today's market."""
    from trade_compass_agent.runtime.tools.search import tool_sina_realtime_quote
    data = json.loads(tool_sina_realtime_quote(symbols="sh000001,sz399001,sz399006"))
    results = []
    for quote in data.get("quotes", []):
        price, previous = quote.get("price"), quote.get("prev_close")
        valid = isinstance(price, (int, float)) and math.isfinite(price) and price > 0
        comparable = valid and isinstance(previous, (int, float)) and math.isfinite(previous) and previous > 0
        results.append({
            "symbol": quote["symbol"], "name": quote["name"],
            "price": price if valid else None,
            "change_pct": round((price / previous - 1) * 100, 2) if comparable else None,
            "date": quote.get("date"), "time": quote.get("time"),
            "data_status": "available" if valid else "unavailable",
        })
    return results


def tool_get_market_pulse(stack: MarketStack) -> str:
    pulse = stack.market_pulse_provider.get_market_pulse()
    indices = _fetch_major_indices()
    warnings = list(pulse.warnings)
    warnings.append("指数按各条行情日期和时间使用；板块未提供统一行情时点，获取时间不代表已核实的实时行情。")
    if len(indices) < 3 or any(row["data_status"] == "unavailable" for row in indices):
        warnings.append("部分指数未取得有效报价，不能按零点或零涨幅解读。")
    payload = {
        "_meta": {**_meta_tag(pulse.provider_name, "sample" if pulse.provider_name == "sample" else "mixed"),
                  "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "data_as_of": None, "timezone": "Asia/Shanghai", "is_sample": pulse.provider_name == "sample"},
        "provider_name": pulse.provider_name,
        "timestamp": pulse.timestamp.isoformat(),
        "indices": indices,
        "notes": pulse.notes,
        "sectors": [
            {"name": s.name, "change_pct": s.change_pct, "leader": s.leader}
            for s in pulse.sectors[:12]
        ],
        "limit_up": {
            "count": pulse.limit_up.count,
            "strong_count": pulse.limit_up.strong_count,
            "top_industries": pulse.limit_up.top_industries,
        },
        "warnings": warnings,
    }
    return json.dumps(payload, ensure_ascii=False)


def tool_get_fundamentals(stack: MarketStack, *, symbol: str) -> str:
    bars = stack.provider.get_bars(symbol.strip(), timeframe="1d", limit=120)
    snapshot = stack.fundamentals_provider.get_snapshot(symbol.strip(), bars=bars)
    latest_turnover = bars[-1].turnover_pct if bars and bars[-1].turnover_pct is not None else None
    payload = {
        "_meta": _meta_tag(snapshot.provider_name),
        "symbol": symbol,
        "provider": snapshot.provider_name,
        "pe_ttm": snapshot.pe_ttm,
        "pb": snapshot.pb,
        "market_cap": snapshot.market_cap,
        "roe": snapshot.roe,
        "float_shares": snapshot.float_shares,
        "total_shares": snapshot.total_shares,
        "industry": snapshot.industry,
        "turnover_pct": latest_turnover,
        "has_real_fundamentals": snapshot.has_real_fundamentals,
        "data_status": snapshot.data_status, "as_of": snapshot.as_of,
        "notes": list(snapshot.notes),
    }
    return json.dumps(payload, ensure_ascii=False)


def tool_get_events(stack: MarketStack, *, symbol: str, limit: int = 5) -> str:
    if not stack.config.data.cninfo_enabled:
        return json.dumps({"symbol": symbol, "events": [], "provider": "disabled"}, ensure_ascii=False)
    try:
        events = stack.cninfo_provider.get_events(symbol.strip(), limit=limit)
    except Exception as exc:
        return json.dumps(
            {"symbol": symbol, "events": [], "error": str(exc), "provider": getattr(stack.cninfo_provider, "name", None)},
            ensure_ascii=False,
        )
    payload = {
        "symbol": symbol,
        "provider": getattr(stack.cninfo_provider, "name", None),
        "events": [
            {
                "title": event.title,
                "event_type": event.event_type,
                "timestamp": event.timestamp.isoformat(),
                "source": event.source,
            }
            for event in events
        ],
    }
    return json.dumps(payload, ensure_ascii=False)
