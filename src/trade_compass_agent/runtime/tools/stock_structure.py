"""Tools for stock structure analysis: shareholders, chip distribution, margin, block trades."""
from __future__ import annotations

import json
import threading
from datetime import datetime

import requests

from trade_compass_agent.data.network import rate_limit_domain, run_with_timeout, short_error_message

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com",
}
_TIMEOUT = 8.0

_MINI_RACER_LOCK = threading.Lock()
_MINI_RACER_CTX = None


def _em_market_code(symbol: str) -> str:
    """Return Eastmoney market prefix: SH/SZ."""
    s = symbol.strip()
    if s.startswith(("6", "5", "9")):
        return "SH"
    return "SZ"


def _meta_tag(source: str) -> dict:
    return {
        "source": source,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Phase 2: Shareholder Structure
# ---------------------------------------------------------------------------

def tool_get_shareholder_structure(*, symbol: str) -> str:
    """Get top-10 circulating shareholders + holder count change."""
    sym = symbol.strip()
    market = _em_market_code(sym)
    result: dict = {"_meta": _meta_tag("eastmoney"), "symbol": sym}

    # 1. Top-10 circulating shareholders
    top10 = _fetch_top10_holders(sym, market)
    result["top10_holders"] = top10

    # 2. Holder count trend
    holder_count = _fetch_holder_count(sym, market)
    result["holder_count_trend"] = holder_count

    # 3. Derive institutional ratio from top-10
    if top10:
        inst_pct = sum(h.get("hold_pct", 0) for h in top10 if h.get("is_institution"))
        total_pct = sum(h.get("hold_pct", 0) for h in top10)
        result["institutional_top10_pct"] = round(inst_pct, 2)
        result["top10_total_pct"] = round(total_pct, 2)
        result["retail_vs_institution"] = (
            "机构主导" if inst_pct > 50 else "散户为主" if inst_pct < 20 else "混合"
        )

    return json.dumps(result, ensure_ascii=False)


def _fetch_top10_holders(symbol: str, market: str) -> list[dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_EH_FREEHOLDERS",
        "columns": "HOLDER_NAME,HOLD_NUM,HOLD_RATIO,HOLDER_TYPE,END_DATE,IS_HOLDORG",
        "filter": f'(SECUCODE="{symbol}.{market}")',
        "pageSize": "10",
        "sortColumns": "END_DATE,HOLD_NUM",
        "sortTypes": "-1,-1",
        "pageNumber": "1",
    }

    def fetch():
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    try:
        data = run_with_timeout(fetch, _TIMEOUT, f"top10 holders {symbol}")
    except Exception:
        return []

    rows = (data.get("result") or {}).get("data") or []
    holders = []
    for row in rows[:10]:
        holders.append({
            "name": row.get("HOLDER_NAME", ""),
            "shares": row.get("HOLD_NUM"),
            "hold_pct": round(float(row.get("HOLD_RATIO") or 0), 2),
            "is_institution": row.get("IS_HOLDORG") == "1",
            "type": row.get("HOLDER_TYPE", ""),
            "report_date": str(row.get("END_DATE", ""))[:10],
        })
    return holders


def _fetch_holder_count(symbol: str, market: str) -> list[dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_HOLDERNUM_DET",
        "columns": "END_DATE,HOLDER_NUM,HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,AVG_HOLD_NUM,AVG_MARKET_CAP",
        "filter": f'(SECUCODE="{symbol}.{market}")',
        "pageSize": "5",
        "sortColumns": "END_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
    }

    def fetch():
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    try:
        data = run_with_timeout(fetch, _TIMEOUT, f"holder count {symbol}")
    except Exception:
        return []

    rows = (data.get("result") or {}).get("data") or []
    trend = []
    for row in rows[:5]:
        trend.append({
            "date": str(row.get("END_DATE", ""))[:10],
            "holder_count": row.get("HOLDER_NUM"),
            "change": row.get("HOLDER_NUM_CHANGE"),
            "change_pct": row.get("HOLDER_NUM_RATIO"),
            "avg_shares_per_holder": row.get("AVG_HOLD_NUM"),
        })
    return trend


# ---------------------------------------------------------------------------
# Phase 3: Chip Distribution (Eastmoney CYQ pre-computed)
# ---------------------------------------------------------------------------

def tool_get_chip_distribution(*, symbol: str) -> str:
    """Get chip distribution (CYQ) data — profit ratio, avg cost, concentration."""
    sym = symbol.strip()
    result: dict = {"symbol": sym}

    # Prefer Eastmoney pre-computed CYQ (authoritative) when available
    em_cyq = _fetch_cyq_from_eastmoney(sym)
    if em_cyq:
        result["_meta"] = _meta_tag("eastmoney_cyq")
        result.update(em_cyq)
        result["note"] = "东方财富官方CYQ数据"
        return json.dumps(result, ensure_ascii=False)

    # Fallback: standard CYQ algorithm on baostock K-line + turnover
    try:
        cyq_data = _compute_cyq(sym)
    except Exception as exc:
        result["_meta"] = _meta_tag("eastmoney_cyq_algorithm")
        result["unavailable"] = True
        result["reason"] = f"CYQ calculation failed: {short_error_message(exc)}"
        return json.dumps(result, ensure_ascii=False)

    if not cyq_data or cyq_data.get("avg_cost") is None:
        result["_meta"] = _meta_tag("eastmoney_cyq_algorithm")
        result["unavailable"] = True
        result["reason"] = "CYQ returned empty data"
        return json.dumps(result, ensure_ascii=False)

    result["_meta"] = _meta_tag("eastmoney_cyq_algorithm")
    result.update(cyq_data)
    result["note"] = "基于K线+换手率的标准CYQ算法（东方财富官方接口不可用时）"
    return json.dumps(result, ensure_ascii=False)


def _fetch_cyq_from_eastmoney(symbol: str) -> dict | None:
    """Fetch pre-computed CYQ from Eastmoney via akshare when network allows."""
    try:
        import akshare as ak

        def fetch():
            return ak.stock_cyq_em(symbol=symbol, adjust="")

        df = run_with_timeout(fetch, 10.0, f"cyq {symbol}")
    except Exception:
        return None

    if df is None or getattr(df, "empty", True):
        return None

    row = df.iloc[-1]
    profit = _safe_num(row.get("获利比例"))
    avg_cost = _safe_num(row.get("平均成本"))
    if profit is None and avg_cost is None:
        return None

    cost_90_low = _safe_num(row.get("90成本-低"))
    cost_90_high = _safe_num(row.get("90成本-高"))
    cost_70_low = _safe_num(row.get("70成本-低"))
    cost_70_high = _safe_num(row.get("70成本-高"))

    out = {
        "date": str(row.get("日期", ""))[:10],
        "profit_ratio": profit,
        "avg_cost": avg_cost,
        "cost_90_low": cost_90_low,
        "cost_90_high": cost_90_high,
        "cost_70_low": cost_70_low,
        "cost_70_high": cost_70_high,
    }
    if cost_70_low and cost_70_high and cost_70_low > 0:
        spread = (cost_70_high - cost_70_low) / cost_70_low
        out["concentration"] = "集中" if spread < 0.15 else "分散"
    return out


def _compute_cyq(symbol: str) -> dict | None:
    """Fetch K-line data with turnover and compute CYQ using JS algorithm.

    Uses baostock for K-line + turnover data (exchange official, works 24/7),
    then runs the standard Eastmoney CYQ algorithm via py_mini_racer.

    MiniRacer must be a process-wide singleton — concurrent ``MiniRacer()``
    construction crashes the V8 address pool (libmini_racer FATAL).
    """
    records = _fetch_kline_with_turnover(symbol)
    if not records or len(records) < 30:
        return None

    idx = len(records) - 1
    with _MINI_RACER_LOCK:
        ctx = _get_mini_racer_ctx()
        mcode = ctx.call("CYQCalculator", idx, records)

    if not isinstance(mcode, dict):
        return None

    last_record = records[-1]
    result = {
        "date": str(last_record.get("date", ""))[:10],
        "profit_ratio": _safe_pct(mcode.get("winnerRate")),
        "avg_cost": _safe_num(mcode.get("avgCost")),
        "cost_90_low": _safe_num(mcode.get("cost90Low")),
        "cost_90_high": _safe_num(mcode.get("cost90High")),
        "cost_70_low": _safe_num(mcode.get("cost70Low")),
        "cost_70_high": _safe_num(mcode.get("cost70High")),
    }

    if result["cost_70_low"] and result["cost_70_high"] and result["cost_70_low"] > 0:
        spread = (result["cost_70_high"] - result["cost_70_low"]) / result["cost_70_low"]
        result["concentration"] = "集中" if spread < 0.15 else "分散"

    close = float(last_record.get("close", 0))
    if result["avg_cost"] and close:
        result["price_vs_avg_cost"] = "above" if close > result["avg_cost"] else "below"

    return result


def _get_mini_racer_ctx():
    global _MINI_RACER_CTX
    if _MINI_RACER_CTX is not None:
        return _MINI_RACER_CTX
    import py_mini_racer

    _MINI_RACER_CTX = py_mini_racer.MiniRacer()
    _MINI_RACER_CTX.eval(_CYQ_JS_CODE)
    return _MINI_RACER_CTX


def _fetch_kline_with_turnover(symbol: str) -> list[dict]:
    """Fetch K-line with turnover rate from baostock (reliable, exchange-level data)."""
    import io
    import contextlib

    try:
        import baostock as bs
    except ImportError:
        return []

    bs_code = f"sh.{symbol}" if symbol.startswith(("6", "5", "9")) else f"sz.{symbol}"
    end = datetime.now()
    start = end - __import__("datetime").timedelta(days=250)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    def fetch():
        with contextlib.redirect_stdout(io.StringIO()):
            bs.login()
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn",
                start_date=start_str,
                end_date=end_str,
                frequency="d",
                adjustflag="2",
            )
        if rs.error_code != "0":
            return []
        rows = []
        with contextlib.redirect_stdout(io.StringIO()):
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()
        return rows

    try:
        rows = run_with_timeout(fetch, 10.0, f"baostock cyq {symbol}")
    except Exception:
        return []

    if not rows:
        return []

    records = []
    for row in rows:
        if len(row) < 8 or not row[4]:
            continue
        turn = float(row[7]) if row[7] else 0
        records.append({
            "date": row[0],
            "open": float(row[1]) if row[1] else 0,
            "high": float(row[2]) if row[2] else 0,
            "low": float(row[3]) if row[3] else 0,
            "close": float(row[4]) if row[4] else 0,
            "volume": float(row[5]) if row[5] else 0,
            "hsl": turn,
            "index": len(records),
        })
    return records


# The CYQ JavaScript algorithm — same algorithm used by Eastmoney's web client
_CYQ_JS_CODE = """
function CYQCalculator(index, klinedata) {
    var maxprice = 0;
    var minprice = 0;
    var factor = 150;
    var range = 120;
    var start = range ? Math.max(0, index - range + 1) : 0;
    var kdata = klinedata.slice(start, Math.max(1, index + 1));
    if (kdata.length === 0) return {};
    for (var i = 0; i < kdata.length; i++) {
        var elements = kdata[i];
        maxprice = !maxprice ? elements.high : Math.max(maxprice, elements.high);
        minprice = !minprice ? elements.low : Math.min(minprice, elements.low);
    }
    var accuracy = Math.max(0.01, (maxprice - minprice) / (factor - 1));
    var yrange = [];
    for (var i = 0; i < factor; i++) {
        yrange.push((minprice + accuracy * i).toFixed(2) / 1);
    }
    var xdata = [];
    for (var i = 0; i < factor; i++) xdata.push(0);

    for (var i = 0; i < kdata.length; i++) {
        var eles = kdata[i];
        var open = eles.open, close = eles.close, high = eles.high, low = eles.low;
        var spread = high - low;
        if (spread <= 0) {
            spread = Math.max(0.01, Math.abs(close) * 0.001);
        }
        var avg = (open + close + high + low) / 4;
        var turnoverRate = Math.min(1, eles.hsl / 100 || 0);
        var proportion = turnoverRate / spread;

        for (var j = 0; j < factor; j++) {
            xdata[j] *= (1 - turnoverRate);
        }

        var lowIdx = Math.max(0, Math.round((low - minprice) / accuracy));
        var highIdx = Math.min(factor - 1, Math.round((high - minprice) / accuracy));
        var avgIdx = Math.round((avg - minprice) / accuracy);

        for (var j = lowIdx; j <= highIdx; j++) {
            if (j === avgIdx) {
                xdata[j] += proportion * accuracy * 2;
            } else {
                xdata[j] += proportion * accuracy;
            }
        }
    }

    var currentprice = klinedata[index].close;
    var totalChips = 0, winnerChips = 0;
    for (var i = 0; i < factor; i++) {
        totalChips += xdata[i];
        if (yrange[i] < currentprice) winnerChips += xdata[i];
    }
    var winnerRate = totalChips > 0 ? (winnerChips / totalChips * 100).toFixed(2) / 1 : 0;

    var avgCost = 0, totalWeight = 0;
    for (var i = 0; i < factor; i++) {
        avgCost += yrange[i] * xdata[i];
        totalWeight += xdata[i];
    }
    avgCost = totalWeight > 0 ? (avgCost / totalWeight).toFixed(2) / 1 : 0;

    // 90% and 70% cost range
    var cumulative = 0;
    var cost90Low = 0, cost90High = 0, cost70Low = 0, cost70High = 0;
    for (var i = 0; i < factor; i++) {
        cumulative += xdata[i];
        var pct = cumulative / totalChips;
        if (!cost90Low && pct >= 0.05) cost90Low = yrange[i];
        if (!cost70Low && pct >= 0.15) cost70Low = yrange[i];
        if (!cost70High && pct >= 0.85) cost70High = yrange[i];
        if (!cost90High && pct >= 0.95) { cost90High = yrange[i]; break; }
    }

    return {
        winnerRate: winnerRate,
        avgCost: avgCost,
        cost90Low: cost90Low,
        cost90High: cost90High,
        cost70Low: cost70Low,
        cost70High: cost70High
    };
}
"""


# ---------------------------------------------------------------------------
# Phase 4A: Institutional Holdings (fund positions)
# ---------------------------------------------------------------------------

def tool_get_institutional_holdings(*, symbol: str, limit: int = 10) -> str:
    """Get fund/institutional holdings for a stock — which funds hold it, position sizes."""
    sym = symbol.strip()
    result: dict = {"_meta": _meta_tag("eastmoney_via_akshare"), "symbol": sym}

    try:
        import akshare as ak

        def fetch():
            return ak.stock_fund_stock_holder(symbol=sym)

        df = run_with_timeout(fetch, 10.0, f"fund holdings {sym}")
    except Exception as exc:
        result["unavailable"] = True
        result["reason"] = short_error_message(exc)
        return json.dumps(result, ensure_ascii=False)

    if df is None or getattr(df, "empty", True):
        result["unavailable"] = True
        result["reason"] = "No fund holding data"
        return json.dumps(result, ensure_ascii=False)

    holdings = []
    for _, row in df.head(limit).iterrows():
        holdings.append({
            "fund_name": str(row.get("基金名称", "")),
            "fund_code": str(row.get("基金代码", "")),
            "shares": row.get("持仓数量"),
            "float_ratio_pct": row.get("占流通股比例"),
            "market_value": row.get("持股市值"),
            "nav_ratio_pct": row.get("占净值比例"),
            "report_date": str(row.get("截止日期", ""))[:10],
        })
    result["holdings"] = holdings
    result["total_funds"] = len(df)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Phase 4B: Margin Trading Data
# ---------------------------------------------------------------------------

def tool_get_margin_data(*, symbol: str, limit: int = 10) -> str:
    """Get margin trading data (融资融券) from exchange — financing balance, short selling."""
    sym = symbol.strip()
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_RZRQ_GGMX",
        "columns": "DATE,SCODE,SECNAME,RZYE,RZMRE,RZCHE,RQYE,RQYL,RQMCL,RZRQYE",
        "filter": f'(SCODE="{sym}")',
        "pageSize": str(limit),
        "sortColumns": "DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
    }

    def fetch():
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    result: dict = {"_meta": _meta_tag("eastmoney_exchange"), "symbol": sym}
    try:
        data = run_with_timeout(fetch, _TIMEOUT, f"margin {sym}")
    except Exception as exc:
        result["unavailable"] = True
        result["reason"] = short_error_message(exc)
        return json.dumps(result, ensure_ascii=False)

    rows = (data.get("result") or {}).get("data") or []
    margin_data = []
    for row in rows[:limit]:
        margin_data.append({
            "date": str(row.get("DATE", ""))[:10],
            "financing_balance": row.get("RZYE"),
            "financing_buy": row.get("RZMRE"),
            "financing_repay": row.get("RZCHE"),
            "short_balance": row.get("RQYE"),
            "short_volume": row.get("RQYL"),
            "total_balance": row.get("RZRQYE"),
        })
    result["data"] = margin_data

    if len(margin_data) >= 2:
        latest = margin_data[0].get("financing_balance") or 0
        prev = margin_data[1].get("financing_balance") or 0
        if latest and prev:
            result["financing_trend"] = "增加" if latest > prev else "减少" if latest < prev else "持平"

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Phase 4C: Block Trades (大宗交易)
# ---------------------------------------------------------------------------

def tool_get_block_trades(*, symbol: str, limit: int = 10) -> str:
    """Get recent block trades (大宗交易) — off-market large trades with premium/discount info."""
    sym = symbol.strip()
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_DATA_BLOCKTRADE",
        "columns": "TRADE_DATE,SECUCODE,SECURITY_NAME_ABBR,DEAL_PRICE,PREMIUM_RATIO,DEAL_VOLUME,DEAL_AMT,BUYER_NAME,SELLER_NAME",
        "filter": f'(SECURITY_CODE="{sym}")',
        "pageSize": str(limit),
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
    }

    def fetch():
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    result: dict = {"_meta": _meta_tag("eastmoney_exchange"), "symbol": sym}
    try:
        data = run_with_timeout(fetch, _TIMEOUT, f"block trades {sym}")
    except Exception as exc:
        result["unavailable"] = True
        result["reason"] = short_error_message(exc)
        return json.dumps(result, ensure_ascii=False)

    rows = (data.get("result") or {}).get("data") or []
    trades = []
    for row in rows[:limit]:
        trades.append({
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "deal_price": row.get("DEAL_PRICE"),
            "premium_pct": row.get("PREMIUM_RATIO"),
            "amount": row.get("DEAL_AMT"),
            "volume": row.get("DEAL_VOLUME"),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        })
    result["trades"] = trades
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_num(val) -> float | None:
    if val is None:
        return None
    try:
        v = float(val)
        if v != v:  # NaN
            return None
        return v
    except (ValueError, TypeError):
        return None


def _safe_pct(val) -> float | None:
    n = _safe_num(val)
    if n is not None:
        return round(n, 2)
    return None
