from __future__ import annotations

import json
from datetime import datetime

from trade_compass_agent.data.quality import DataQualityLayer
from trade_compass_agent.runtime.market_stack import MarketStack


def _meta_tag(source: str, freshness: str = "realtime") -> dict:
    return {
        "source": source,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "freshness": freshness,
    }


def tool_get_bars(stack: MarketStack, *, symbol: str, timeframe: str = "1d", limit: int = 60) -> str:
    bars = stack.provider.get_bars(symbol.strip(), timeframe=timeframe, limit=limit)
    quality = DataQualityLayer().check_bars(bars)
    provider_name = getattr(stack.provider, "last_resolved_provider", None) or getattr(stack.provider, "name", "unknown")
    payload = {
        "_meta": _meta_tag(provider_name),
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(bars),
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
                **({"turnover_pct": bar.turnover_pct} if bar.turnover_pct is not None else {}),
            }
            for bar in bars[-min(limit, 30) :]
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _fetch_major_indices() -> list[dict]:
    """Fetch major A-share indices via Sina (works 24/7)."""
    import requests

    index_codes = "s_sh000001,s_sz399001,s_sz399006"
    url = f"https://hq.sinajs.cn/list={index_codes}"
    try:
        resp = requests.get(url, timeout=5, headers={"Referer": "https://finance.sina.com.cn"})
        resp.raise_for_status()
    except Exception:
        return []

    results = []
    for line in resp.text.strip().split("\n"):
        if "=" not in line:
            continue
        data = line.split("=", 1)[1].strip('";').split(",")
        if len(data) < 4:
            continue
        results.append({
            "name": data[0],
            "price": float(data[1] or 0),
            "change_pct": float(data[3] or 0),
        })
    return results


def tool_get_market_pulse(stack: MarketStack) -> str:
    pulse = stack.market_pulse_provider.get_market_pulse()
    indices = _fetch_major_indices()
    payload = {
        "_meta": _meta_tag(pulse.provider_name),
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
        "warnings": pulse.warnings,
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
