from __future__ import annotations

import json
import math
import os

from trade_compass_agent.data.network import (
    extend_no_proxy_for_eastmoney,
    patch_requests_default_timeout,
    patch_requests_for_eastmoney,
    rate_limit_domain,
    run_with_timeout,
    short_error_message,
)
from trade_compass_agent.runtime.market_stack import MarketStack

extend_no_proxy_for_eastmoney()
patch_requests_for_eastmoney(8.0)
patch_requests_default_timeout(8.0)


_DEFAULT_TIMEOUT = 5.0
_EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"}
_SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn"}
_SINA_BOARD_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"
_SINA_HOT_STOCK_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_ssggzj"
_EM_HOT_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
_EM_BOARD_SPECS: dict[str, tuple[str, str]] = {
    "concept": ("https://79.push2.eastmoney.com/api/qt/clist/get", "m:90 t:3 f:!50"),
    "industry": ("https://17.push2.eastmoney.com/api/qt/clist/get", "m:90 t:2 f:!50"),
}


def _safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        f = float(v)
        return default if math.isnan(f) else int(f)
    except (TypeError, ValueError):
        return default


def _fetch_em_board_rows(*, board_type: str, limit: int) -> list[dict]:
    import requests

    url, fs = _EM_BOARD_SPECS[board_type]
    params = {
        "pn": "1",
        "pz": str(max(1, min(limit, 100))),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs,
        "fields": "f2,f3,f8,f12,f14,f104,f105,f128",
    }
    rate_limit_domain(url)
    resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=(1.0, 1.5))
    resp.raise_for_status()
    diff = resp.json().get("data", {}).get("diff") or []
    return diff if isinstance(diff, list) else []


def _fetch_sina_board_rows(*, board_type: str, limit: int) -> list[dict]:
    import requests

    fenlei = "0" if board_type == "industry" else "1"
    params = {
        "page": "1",
        "num": str(max(1, min(limit, 100))),
        "sort": "avg_changeratio",
        "asc": "0",
        "fenlei": fenlei,
    }
    resp = requests.get(_SINA_BOARD_URL, params=params, headers=_SINA_HEADERS, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []

    rows: list[dict] = []
    for item in data[:limit]:
        rows.append({
            "f14": item.get("name", ""),
            "f3": _safe_float(item.get("avg_changeratio")) * 100,
            "f128": item.get("ts_name", ""),
            "f8": _safe_float(item.get("turnover")),
            "f104": 0,
            "f105": 0,
        })
    return rows


def _fetch_board_rows(*, board_type: str, limit: int) -> tuple[list[dict], str]:
    try:
        return _fetch_em_board_rows(board_type=board_type, limit=limit), "eastmoney"
    except Exception:
        return _fetch_sina_board_rows(board_type=board_type, limit=limit), "sina"


def _fetch_em_hot_stock_rows(*, limit: int) -> list[dict]:
    import requests

    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": max(limit, 20),
    }
    rate_limit_domain(_EM_HOT_RANK_URL)
    resp = requests.post(_EM_HOT_RANK_URL, json=payload, headers=_EM_HEADERS, timeout=(1.0, 2.5))
    resp.raise_for_status()
    rank_data = resp.json().get("data") or []
    if not isinstance(rank_data, list) or not rank_data:
        return []

    marks: list[str] = []
    rank_by_mark: dict[str, int] = {}
    for item in rank_data[:limit]:
        sc = str(item.get("sc", "")).strip()
        if sc.startswith("SZ"):
            mark = f"0.{sc[2:]}"
        elif sc.startswith("SH"):
            mark = f"1.{sc[2:]}"
        else:
            continue
        marks.append(mark)
        rank_by_mark[mark] = _safe_int(item.get("rk"))

    if not marks:
        return []

    quote_url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "fltt": "2",
        "invt": "2",
        "fields": "f14,f3,f12,f2",
        "secids": ",".join(marks),
    }
    quote_resp = requests.get(quote_url, params=params, headers=_EM_HEADERS, timeout=1.5)
    quote_resp.raise_for_status()
    diff = quote_resp.json().get("data", {}).get("diff") or []
    if not isinstance(diff, list):
        return []

    rows: list[dict] = []
    for row in diff:
        code = str(row.get("f12") or "").strip()
        mark = f"1.{code}" if code.startswith("6") else f"0.{code}"
        rows.append({
            "symbol": code[-6:] if len(code) >= 6 else code,
            "name": str(row.get("f14") or "").strip(),
            "rank": rank_by_mark.get(mark, 0),
            "change_pct": _safe_float(row.get("f3")),
        })
    rows.sort(key=lambda item: item["rank"] if item["rank"] > 0 else 9999)
    return rows[:limit]


def _fetch_sina_hot_stock_rows(*, limit: int) -> list[dict]:
    import requests

    params = {"page": "1", "num": str(max(1, min(limit, 100))), "sort": "netamount", "asc": "0"}
    resp = requests.get(_SINA_HOT_STOCK_URL, params=params, headers=_SINA_HEADERS, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []

    rows: list[dict] = []
    for idx, item in enumerate(data[:limit], start=1):
        symbol = str(item.get("symbol", "")).strip()
        if symbol.startswith(("sh", "sz")):
            symbol = symbol[2:]
        rows.append({
            "symbol": symbol,
            "name": str(item.get("name", "")).strip(),
            "rank": idx,
            "change_pct": round(_safe_float(item.get("changeratio")) * 100, 2),
        })
    return rows


def _fetch_hot_stock_rows(*, limit: int) -> tuple[list[dict], str]:
    try:
        rows = _fetch_sina_hot_stock_rows(limit=limit)
        if rows:
            return rows, "sina"
    except Exception:
        pass
    return _fetch_em_hot_stock_rows(limit=limit), "eastmoney"


def _parse_em_board_row(row: dict, *, include_counts: bool) -> dict:
    item = {
        "name": str(row.get("f14") or "").strip(),
        "change_pct": _safe_float(row.get("f3")),
        "leader": str(row.get("f128") or "").strip(),
        "turnover_pct": _safe_float(row.get("f8")),
    }
    if include_counts:
        item["up_count"] = _safe_int(row.get("f104"))
        item["down_count"] = _safe_int(row.get("f105"))
    return item


def tool_search_stock_news(stack: MarketStack, *, symbol: str, limit: int = 10) -> str:
    """Fetch recent news for a specific stock via Eastmoney search API."""
    import requests

    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": "",
        "param": json.dumps({
            "uid": "",
            "keyword": symbol.strip(),
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }, ensure_ascii=False),
    }

    try:
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=6)
        resp.raise_for_status()
        text = resp.text.strip().strip("();")
        data = json.loads(text)
    except Exception as exc:
        return json.dumps(
            {"symbol": symbol, "news": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    articles_raw = data.get("result", {}).get("cmsArticleWebOld", [])
    if isinstance(articles_raw, dict):
        articles_raw = articles_raw.get("list", [])

    articles: list[dict] = []
    for item in articles_raw[:limit]:
        articles.append({
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("content", "")).strip()[:200],
            "time": str(item.get("date", "")).strip(),
            "source": str(item.get("mediaName", "")).strip(),
            "url": str(item.get("url", "")).strip(),
        })

    return json.dumps(
        {"symbol": symbol, "count": len(articles), "news": articles},
        ensure_ascii=False,
    )


def tool_search_announcements(stack: MarketStack, *, symbol: str, limit: int = 8) -> str:
    """Fetch recent company announcements via Eastmoney announcement API."""
    import requests

    if not symbol.strip():
        return json.dumps({"error": "symbol required"}, ensure_ascii=False)

    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {
        "sr": "-1",
        "page_size": str(limit),
        "page_index": "1",
        "ann_type": "SHA,SZA",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
        "stock_list": symbol.strip(),
    }

    try:
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps(
            {"symbol": symbol, "announcements": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items_raw = data.get("data", {}).get("list", [])
    items: list[dict] = []
    for ann in items_raw[:limit]:
        items.append({
            "title": str(ann.get("title", "")).strip(),
            "time": str(ann.get("notice_date", "")).strip()[:16],
        })

    return json.dumps(
        {"symbol": symbol, "count": len(items), "announcements": items},
        ensure_ascii=False,
    )


def tool_web_search(*, query: str, limit: int = 5) -> str:
    """General web search. Uses Tavily if TAVILY_API_KEY is set, else DuckDuckGo (zero config)."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()

    if api_key:
        return _web_search_tavily(query=query, limit=limit, api_key=api_key)
    return _web_search_ddg(query=query, limit=limit)


def _web_search_tavily(*, query: str, limit: int, api_key: str) -> str:
    try:
        from tavily import TavilyClient
    except ImportError:
        return _web_search_ddg(query=query, limit=limit)

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            max_results=limit,
            search_depth="basic",
            include_answer=True,
        )
    except Exception as exc:
        return json.dumps(
            {"error": f"Tavily search failed: {short_error_message(exc)}", "query": query},
            ensure_ascii=False,
        )

    results: list[dict] = []
    for item in response.get("results", [])[:limit]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", "")[:300],
        })

    return json.dumps(
        {
            "query": query,
            "provider": "tavily",
            "answer": response.get("answer", ""),
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


def _web_search_ddg(*, query: str, limit: int) -> str:
    try:
        from ddgs import DDGS  # type: ignore[import-untyped]
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return json.dumps(
                {
                    "error": "duckduckgo-search not installed",
                    "hint": "Run: pip install duckduckgo-search",
                    "query": query,
                },
                ensure_ascii=False,
            )

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=limit))
    except Exception as exc:
        return json.dumps(
            {"error": f"DuckDuckGo search failed: {short_error_message(exc)}", "query": query},
            ensure_ascii=False,
        )

    results: list[dict] = []
    for item in raw_results[:limit]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("href", ""),
            "snippet": item.get("body", "")[:300],
        })

    return json.dumps(
        {
            "query": query,
            "provider": "duckduckgo",
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


def _cls_fallback_ddg(limit: int) -> str:
    """Fallback: fetch latest market announcements from East Money when CLS is unavailable."""
    import requests

    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {
        "sr": "-1",
        "page_size": str(limit),
        "page_index": "1",
        "ann_type": "SHA,SZA",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
    }
    try:
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps({"alerts": [], "error": f"eastmoney_fallback: {short_error_message(exc)}"}, ensure_ascii=False)

    ann_list = data.get("data", {}).get("list", [])
    if not ann_list:
        return json.dumps({"alerts": [], "count": 0, "source": "eastmoney_ann"}, ensure_ascii=False)

    items = []
    for ann in ann_list[:limit]:
        codes = ann.get("codes", [])
        stock_info = f"{codes[0].get('short_name', '')}({codes[0].get('stock_code', '')})" if codes else ""
        title = ann.get("title", "")
        items.append({"time": ann.get("notice_date", "")[:16], "content": f"[{stock_info}] {title}" if stock_info else title})

    return json.dumps({"count": len(items), "alerts": items, "source": "eastmoney_ann"}, ensure_ascii=False)


def tool_search_market_flash(*, limit: int = 20) -> str:
    """Fetch latest flash news from 财联社 (CLS) — real-time market alerts."""
    try:
        import akshare as ak
    except ImportError:
        return _cls_fallback_ddg(limit)

    def fetch():
        return ak.stock_info_global_cls()

    try:
        df = run_with_timeout(fetch, _DEFAULT_TIMEOUT + 3, "cls_flash")
    except Exception:
        return _cls_fallback_ddg(limit)

    if df is None or getattr(df, "empty", True):
        return _cls_fallback_ddg(limit)

    items: list[dict] = []
    for _, row in df.head(limit).iterrows():
        time_val = row.get("发布时间") or row.get("时间") or row.get("time") or ""
        content_val = (
            row.get("内容") or row.get("快讯信息") or row.get("title") or row.get("content") or ""
        )
        items.append({
            "time": str(time_val).strip(),
            "content": str(content_val).strip()[:300],
        })

    return json.dumps({"count": len(items), "alerts": items}, ensure_ascii=False)


def tool_search_hot_stocks(*, limit: int = 15) -> str:
    """Fetch trending/hot stocks ranking from 东方财富 — market sentiment gauge."""
    try:
        rows, source = run_with_timeout(
            lambda: _fetch_hot_stock_rows(limit=limit),
            _DEFAULT_TIMEOUT,
            "hot_rank",
        )
    except Exception as exc:
        return json.dumps(
            {"stocks": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items = rows[:limit]
    payload: dict = {"count": len(items), "stocks": items}
    if source != "eastmoney":
        payload["source"] = source
    return json.dumps(payload, ensure_ascii=False)


def tool_search_lhb(*, symbol: str | None = None, limit: int = 10) -> str:
    """Fetch Dragon-Tiger list (龙虎榜) data via Eastmoney datacenter API."""
    import requests

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params: dict = {
        "reportName": "RPT_DAILYBILLBOARD_DETAILS",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION,BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,CHANGE_RATE,ACCUM_AMOUNT",
        "pageNumber": "1",
        "pageSize": str(limit),
        "sortColumns": "TRADE_DATE,BILLBOARD_NET_AMT",
        "sortTypes": "-1,-1",
        "source": "WEB",
        "client": "WEB",
    }
    if symbol:
        params["filter"] = f'(SECURITY_CODE="{symbol.strip()}")'

    try:
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps(
            {"entries": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    if not data.get("success"):
        return json.dumps(
            {"entries": [], "error": data.get("message", "API error")},
            ensure_ascii=False,
        )

    items_raw = data.get("result", {}).get("data", [])
    items: list[dict] = []
    for row in items_raw[:limit]:
        net_buy = row.get("BILLBOARD_NET_AMT") or 0
        items.append({
            "symbol": str(row.get("SECURITY_CODE", "")).strip(),
            "name": str(row.get("SECURITY_NAME_ABBR", "")).strip(),
            "date": str(row.get("TRADE_DATE", "")).strip()[:10],
            "reason": str(row.get("EXPLANATION", "")).strip(),
            "net_buy": round(float(net_buy) / 1e8, 2) if net_buy else 0.0,
            "change_pct": _safe_float(row.get("CHANGE_RATE")),
        })

    return json.dumps({"count": len(items), "entries": items}, ensure_ascii=False)


def tool_search_concept_boards(*, limit: int = 15) -> str:
    """Fetch concept/theme board ranking (东方财富概念板块) — identifies hot market themes."""
    try:
        rows, source = run_with_timeout(
            lambda: _fetch_board_rows(board_type="concept", limit=limit),
            _DEFAULT_TIMEOUT,
            "concept_boards",
        )
    except Exception as exc:
        return json.dumps(
            {"boards": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items = [_parse_em_board_row(row, include_counts=False) for row in rows[:limit]]
    payload: dict = {"count": len(items), "boards": items}
    if source != "eastmoney":
        payload["source"] = source
    return json.dumps(payload, ensure_ascii=False)


def tool_search_xueqiu_hot(*, limit: int = 15) -> str:
    """Fetch stock popularity ranking — social/retail sentiment indicator (via Sina net inflow)."""
    import requests

    url = _SINA_HOT_STOCK_URL
    params = {"page": "1", "num": str(max(1, min(limit, 100))), "sort": "netamount", "asc": "0"}
    try:
        resp = requests.get(url, params=params, headers=_SINA_HEADERS, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps(
            {"stocks": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    if not isinstance(data, list):
        return json.dumps({"stocks": [], "count": 0}, ensure_ascii=False)

    items: list[dict] = []
    for item in data[:limit]:
        symbol = str(item.get("symbol", "")).strip()
        if symbol.startswith(("sh", "sz")):
            symbol = symbol[2:]
        net_inflow = _safe_float(item.get("netamount"))
        items.append({
            "symbol": symbol,
            "name": str(item.get("name", "")).strip(),
            "followers": int(abs(net_inflow / 1e4)) if net_inflow else 0,
            "new_followers": 0,
            "net_inflow_yi": round(net_inflow / 1e8, 2),
            "change_pct": round(_safe_float(item.get("changeratio")) * 100, 2),
        })

    return json.dumps({"count": len(items), "stocks": items, "source": "sina_moneyflow"}, ensure_ascii=False)


def tool_search_market_activity(*, limit: int = 20) -> str:
    """Fetch intraday unusual market activity (盘口异动) — large orders, rapid price moves, etc."""
    try:
        import akshare as ak
    except ImportError:
        return json.dumps({"error": "akshare not installed"}, ensure_ascii=False)

    event_types = ["火箭发射", "快速反弹", "大笔买入", "封涨停板", "打开跌停板", "有大买盘", "竞价上涨", "高开5日线", "向上缺口", "60日新高"]

    all_items: list[dict] = []
    for event in event_types:
        if len(all_items) >= limit:
            break
        try:
            def _fetch(ev=event):
                return ak.stock_changes_em(symbol=ev)
            df = run_with_timeout(_fetch, _DEFAULT_TIMEOUT, f"activity_{event}")
            if df is None or getattr(df, "empty", True):
                continue
            for _, row in df.head(max(3, limit // len(event_types))).iterrows():
                if len(all_items) >= limit:
                    break
                code = row.get("代码") or row.get("股票代码") or ""
                name = row.get("名称") or row.get("股票名称") or ""
                time_val = row.get("时间") or ""
                all_items.append({
                    "symbol": str(code).strip(),
                    "name": str(name).strip(),
                    "time": str(time_val).strip(),
                    "event_type": event,
                })
        except Exception:
            continue

    return json.dumps({"count": len(all_items), "activities": all_items}, ensure_ascii=False)


def tool_search_industry_boards(*, limit: int = 15) -> str:
    """Fetch industry board ranking (东方财富行业板块) — shows which sectors lead/lag today."""
    try:
        rows, source = run_with_timeout(
            lambda: _fetch_board_rows(board_type="industry", limit=limit),
            _DEFAULT_TIMEOUT,
            "industry_boards",
        )
    except Exception as exc:
        return json.dumps(
            {"boards": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items = [_parse_em_board_row(row, include_counts=True) for row in rows[:limit]]
    payload: dict = {"count": len(items), "boards": items}
    if source != "eastmoney":
        payload["source"] = source
    return json.dumps(payload, ensure_ascii=False)


def tool_search_research_reports(*, symbol: str, limit: int = 8) -> str:
    """Fetch recent analyst research reports for a stock (东方财富研报 API)."""
    import requests

    if not symbol.strip():
        return json.dumps({"error": "symbol required"}, ensure_ascii=False)

    url = "https://reportapi.eastmoney.com/report/list"
    params = {
        "industryCode": "*",
        "pageNo": "1",
        "pageSize": str(limit),
        "fields": "",
        "qType": "0",
        "code": symbol.strip(),
        "orgCode": "",
        "ratingChange": "",
        "beginTime": "",
        "endTime": "",
    }

    try:
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps(
            {"symbol": symbol, "reports": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items_raw = data.get("data", [])
    if not items_raw:
        return json.dumps({"symbol": symbol, "reports": [], "count": 0}, ensure_ascii=False)

    items: list[dict] = []
    for item in items_raw[:limit]:
        items.append({
            "title": str(item.get("title", "")).strip()[:100],
            "org": str(item.get("orgSName", "")).strip(),
            "author": str(item.get("researcher", "")).strip(),
            "date": str(item.get("publishDate", "")).strip()[:10],
            "rating": str(item.get("emRatingName", "")).strip(),
        })

    return json.dumps(
        {"symbol": symbol, "count": len(items), "reports": items},
        ensure_ascii=False,
    )


def tool_search_institute_recommend(*, symbol: str, limit: int = 8) -> str:
    """Fetch institutional recommendations/ratings for a stock (东方财富研报 API, 评级视角)."""
    import requests

    if not symbol.strip():
        return json.dumps({"error": "symbol required"}, ensure_ascii=False)

    url = "https://reportapi.eastmoney.com/report/list"
    params = {
        "industryCode": "*",
        "pageNo": "1",
        "pageSize": str(limit),
        "fields": "",
        "qType": "0",
        "code": symbol.strip(),
        "orgCode": "",
        "ratingChange": "",
        "beginTime": "",
        "endTime": "",
    }

    try:
        rate_limit_domain(url)
        resp = requests.get(url, params=params, headers=_EM_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps(
            {"symbol": symbol, "recommendations": [], "error": short_error_message(exc)},
            ensure_ascii=False,
        )

    items_raw = data.get("data", [])
    if not items_raw:
        return json.dumps(
            {"symbol": symbol, "recommendations": [], "count": 0}, ensure_ascii=False
        )

    items: list[dict] = []
    for item in items_raw[:limit]:
        items.append({
            "org": str(item.get("orgSName", "")).strip(),
            "rating": str(item.get("emRatingName", "")).strip(),
            "target_price": str(item.get("predictThisYearEps", "")).strip(),
            "date": str(item.get("publishDate", "")).strip()[:10],
            "title": str(item.get("title", "")).strip()[:60],
        })

    return json.dumps(
        {"symbol": symbol, "count": len(items), "recommendations": items},
        ensure_ascii=False,
    )


def tool_search_x(
    *,
    query: str,
    handles: list[str] | str | None = None,
    days_back: int = 7,
) -> str:
    """Search X (Twitter) via xAI Grok API — real-time posts, trading community insights.

    Requires XAI_API_KEY in .env.
    handles: optional list of X handles to focus on (without @).
    """
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return json.dumps(
            {
                "error": "XAI_API_KEY not configured",
                "hint": "Set XAI_API_KEY in .env to enable X search. Get a key at https://console.x.ai",
                "query": query,
            },
            ensure_ascii=False,
        )

    if isinstance(handles, str):
        handles = [h.strip() for h in handles.split(",") if h.strip()]
    elif handles is None:
        handles = []

    try:
        import httpx
    except ImportError:
        return json.dumps({"error": "httpx not installed", "query": query}, ensure_ascii=False)

    from datetime import date, timedelta

    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days_back)).isoformat()

    tool_config: dict = {
        "type": "x_search",
        "from_date": from_date,
        "to_date": to_date,
    }
    if handles:
        tool_config["allowed_x_handles"] = handles[:20]

    final_query = query
    _KNOWN_KOL_HANDLES = {"aleabitoreddit", "seabornetrading"}
    if handles and set(h.lower() for h in handles) & _KNOWN_KOL_HANDLES:
        final_query += (
            "\n\n请在回答中明确标注：(1) 提到的所有股票代码/公司名 "
            "(2) 核心论点摘要 (3) 看多/看空/中性倾向"
        )

    payload = {
        "model": "grok-3-fast",
        "input": [{"role": "user", "content": final_query}],
        "tools": [tool_config],
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.x.ai/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return json.dumps(
            {"error": f"xAI API failed: {short_error_message(exc)}", "query": query},
            ensure_ascii=False,
        )

    output_text = ""
    citations: list[dict] = []

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text", "")
                    for ann in content.get("annotations", []):
                        if ann.get("type") == "url_citation":
                            citations.append({
                                "title": ann.get("title", ""),
                                "url": ann.get("url", ""),
                            })

    return json.dumps(
        {
            "query": query,
            "provider": "xai_x_search",
            "handles_filter": handles or [],
            "date_range": f"{from_date} → {to_date}",
            "answer": output_text[:2000],
            "citations": citations[:10],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Direct HTTP data sources (bypass akshare, high availability)
# ---------------------------------------------------------------------------

def tool_sina_realtime_quote(*, symbols: str) -> str:
    """Get real-time quotes from Sina Finance API. Works 24/7 (returns last close after hours).

    symbols: comma-separated stock codes, e.g. "600519,300750,000001"
    """
    import requests

    codes = [s.strip() for s in symbols.split(",") if s.strip()]
    sina_codes = []
    for code in codes:
        if code.startswith("6"):
            sina_codes.append(f"sh{code}")
        else:
            sina_codes.append(f"sz{code}")

    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    try:
        resp = requests.get(url, timeout=5, headers={"Referer": "https://finance.sina.com.cn"})
        resp.raise_for_status()
    except Exception as exc:
        return json.dumps({"error": short_error_message(exc), "symbols": codes}, ensure_ascii=False)

    results = []
    for line in resp.text.strip().split("\n"):
        if "=" not in line:
            continue
        var_part, data_part = line.split("=", 1)
        data = data_part.strip('";').split(",")
        if len(data) < 32:
            continue
        code = var_part.split("_")[-1]
        results.append({
            "symbol": code[2:],
            "name": data[0],
            "open": float(data[1] or 0),
            "prev_close": float(data[2] or 0),
            "price": float(data[3] or 0),
            "high": float(data[4] or 0),
            "low": float(data[5] or 0),
            "volume": int(float(data[8] or 0)),
            "amount": float(data[9] or 0),
            "date": data[30],
            "time": data[31],
        })

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)


_DEFAULT_KOL_HANDLES: list[str] = ["aleabitoreddit"]

_KOL_EXTRACTION_PROMPT = """\
你是一个投资信号提取专家。分析以下 X/Twitter KOL 的帖文，提取结构化投资信号。

要求：
1. 识别帖文中提到的所有股票代码或公司名（A股代码如600519、美股如AXTI）
2. 对每个提及，提取以下字段：
   - symbol: 股票代码（如有）
   - company: 公司名称
   - thesis: 一句话论点摘要
   - conviction: high/medium/low（根据语言强度判断）
   - sector_theme: 所属产业链主题（如 CPO/光互连、人形机器人、稀土、先进封装 等）
   - chokepoint_signal: 是否涉及卡脖子/瓶颈概念（true/false）
3. 如果帖文不涉及具体股票，提取产业趋势观点

{topic_filter}

请用以下 JSON 格式输出（直接输出 JSON，无需额外说明）：
{{
  "signals": [
    {{
      "symbol": "AXTI",
      "company": "AXT Inc",
      "thesis": "InP衬底全球仅2-3家供应商，AI光互连需求爆发",
      "conviction": "high",
      "sector_theme": "CPO/光互连",
      "chokepoint_signal": true
    }}
  ],
  "macro_view": "对整体市场/行业的宏观观点（如有）",
  "post_count": 0
}}
"""


def tool_search_x_kol(
    *,
    handles: list[str] | None = None,
    topic: str = "",
    days_back: int = 14,
) -> str:
    """Search X KOL posts and extract structured investment signals.

    Targets Serenity (白毛股神) by default. Returns structured signals
    with stock mentions, thesis summaries, and chokepoint indicators.
    """
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        return json.dumps(
            {
                "error": "XAI_API_KEY not configured",
                "hint": "Set XAI_API_KEY in .env to enable X search",
            },
            ensure_ascii=False,
        )

    target_handles = handles or _DEFAULT_KOL_HANDLES

    try:
        import httpx
    except ImportError:
        return json.dumps({"error": "httpx not installed"}, ensure_ascii=False)

    from datetime import date, timedelta

    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days_back)).isoformat()

    topic_filter = f"重点关注与以下主题相关的帖文：{topic}" if topic else ""
    extraction_prompt = _KOL_EXTRACTION_PROMPT.format(topic_filter=topic_filter)

    user_query = (
        f"查找以下 X 用户最近的投资分析帖文：{', '.join(target_handles)}。"
        f"重点关注他们提到的股票、产业链分析、卡脖子/瓶颈理论相关内容。"
    )
    if topic:
        user_query += f" 特别关注与「{topic}」相关的内容。"

    tool_config: dict = {
        "type": "x_search",
        "from_date": from_date,
        "to_date": to_date,
        "allowed_x_handles": target_handles[:20],
    }

    payload = {
        "model": "grok-3-fast",
        "input": [
            {"role": "system", "content": extraction_prompt},
            {"role": "user", "content": user_query},
        ],
        "tools": [tool_config],
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                "https://api.x.ai/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return json.dumps(
            {"error": f"xAI API failed: {short_error_message(exc)}"},
            ensure_ascii=False,
        )

    output_text = ""
    citations: list[dict] = []

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text", "")
                    for ann in content.get("annotations", []):
                        if ann.get("type") == "url_citation":
                            citations.append({
                                "title": ann.get("title", ""),
                                "url": ann.get("url", ""),
                            })

    # Try to parse structured JSON from Grok's response
    parsed_signals = None
    try:
        clean = output_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed_signals = json.loads(clean)
    except (json.JSONDecodeError, IndexError):
        pass

    result: dict[str, Any] = {
        "provider": "xai_kol_signals",
        "handles": target_handles,
        "topic": topic or "(all)",
        "date_range": f"{from_date} → {to_date}",
        "citations": citations[:15],
    }

    if parsed_signals and isinstance(parsed_signals, dict):
        result["signals"] = parsed_signals.get("signals", [])
        result["macro_view"] = parsed_signals.get("macro_view", "")
        result["post_count"] = parsed_signals.get("post_count", 0)
    else:
        result["raw_analysis"] = output_text[:3000]
        result["signals"] = []
        result["parse_note"] = "Grok returned narrative; structured extraction failed"

    return json.dumps(result, ensure_ascii=False)


def tool_eastmoney_news(*, limit: int = 10) -> str:
    """Get latest A-share financial news from East Money (东方财富) 7x24 feed. Works 24/7."""
    import requests

    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {
        "sr": "-1",
        "page_size": str(limit),
        "page_index": "1",
        "ann_type": "SHA,SZA",
        "client_source": "web",
        "f_node": "0",
        "s_node": "0",
    }
    try:
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return json.dumps({"news": [], "error": short_error_message(exc)}, ensure_ascii=False)

    ann_list = data.get("data", {}).get("list", [])
    if not ann_list:
        return json.dumps({"news": [], "count": 0, "source": "eastmoney_ann"}, ensure_ascii=False)

    items = []
    for ann in ann_list[:limit]:
        codes = ann.get("codes", [])
        stock_info = f"{codes[0].get('short_name', '')}({codes[0].get('stock_code', '')})" if codes else ""
        items.append({
            "title": ann.get("title", ""),
            "time": ann.get("notice_date", ""),
            "stock": stock_info,
        })

    return json.dumps({"count": len(items), "news": items, "source": "eastmoney_ann"}, ensure_ascii=False)
