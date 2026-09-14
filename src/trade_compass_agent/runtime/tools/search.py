from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone

from trade_compass_agent.data.network import (
    extend_no_proxy_for_eastmoney,
    patch_requests_default_timeout,
    patch_requests_for_eastmoney,
    rate_limit_domain,
    run_with_timeout,
    short_error_message,
)
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.data.sector_boards import (
    fetch_sina_board_rows as _fetch_sina_board_rows,
    fetch_eastmoney_board_rows as _fetch_em_board_rows,
)
from trade_compass_agent.data.providers import ProviderError, to_sina_code

extend_no_proxy_for_eastmoney()
patch_requests_for_eastmoney(8.0)
patch_requests_default_timeout(8.0)


_DEFAULT_TIMEOUT = 5.0
_EM_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"}
_SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn"}
_SINA_HOT_STOCK_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_ssggzj"
_EM_HOT_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"


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



def _fetch_board_rows(*, board_type: str, limit: int) -> tuple[list[dict], str]:
    try:
        rows = _fetch_em_board_rows(board_type=board_type, limit=limit)
        if not rows:
            raise ValueError("empty Eastmoney board response")
        return rows, "eastmoney"
    except Exception:
        rows = _fetch_sina_board_rows(board_type=board_type, limit=limit)
        if not rows:
            raise ValueError("industry/concept ranking unavailable from both sources")
        return rows, "sina"


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
        "turnover_pct": _safe_float(row.get("f8"), None),
    }
    if include_counts:
        item["up_count"] = _safe_int(row.get("f104"), None)
        item["down_count"] = _safe_int(row.get("f105"), None)
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
                    "sort": "time",
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
    """Free web search by default; Tavily requires an explicit provider choice."""
    provider = os.getenv("WEB_SEARCH_PROVIDER", "auto").strip().lower()
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if provider == "tavily" and api_key:
        return _web_search_tavily(query=query, limit=limit, api_key=api_key)
    if provider == "tavily":
        return json.dumps({"query": query, "error": "TAVILY_API_KEY not configured", "results": []}, ensure_ascii=False)
    if provider not in {"auto", "free", ""}:
        return json.dumps({"query": query, "error": f"Unknown WEB_SEARCH_PROVIDER: {provider}", "results": []}, ensure_ascii=False)
    return _web_search_ddg(query=query, limit=limit)


def _web_search_tavily(*, query: str, limit: int, api_key: str) -> str:
    import requests

    def fetch() -> dict:
        response = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "query": query,
                "max_results": max(1, min(limit, 20)),
                "search_depth": "basic",
                "include_answer": True,
                "include_published_date": True,
            },
            timeout=(2, 7),
        )
        response.raise_for_status()
        return response.json()

    try:
        # Use the HTTP API so a configured key works in base installs too.
        response = run_with_timeout(fetch, 10, "Tavily search")
        results: list[dict] = []
        for item in response.get("results", [])[:max(1, min(limit, 20))]:
            if not item.get("url"):
                continue
            result = {
                "title": item.get("title", ""),
                "url": item["url"],
                "snippet": (item.get("content") or "")[:300],
            }
            if item.get("published_date"):
                result["published_date"] = item["published_date"]
            results.append(result)
        if not results:
            raise ValueError("no usable search results")
    except Exception as exc:
        return json.dumps(
            {
                "error": f"Tavily search failed: {short_error_message(exc)}",
                "query": query,
                "provider": "tavily",
                "results": [],
            },
            ensure_ascii=False,
        )

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


def _web_search_proxy() -> str | None:
    """Respect the user's configured proxy; Eastmoney's NO_PROXY is unrelated."""
    from urllib import request
    import sys

    if os.getenv("DDGS_PROXY"):
        return os.environ["DDGS_PROXY"]
    proxies = request.getproxies()
    if sys.platform == "darwin" and not any(key in proxies for key in ("https", "http", "all")):
        # urllib otherwise discards macOS settings when only NO_PROXY is present.
        proxies = request.getproxies_macosx_sysconf()
    return proxies.get("https") or proxies.get("all") or proxies.get("http")


def _web_search_ddg(*, query: str, limit: int) -> str:
    import time

    try:
        from ddgs import DDGS
        from ddgs.engines import ENGINES
    except ImportError:
        return json.dumps({"error": "ddgs search dependency missing; reinstall trade-compass-agent", "query": query}, ensure_ascii=False)

    failures: list[str] = []
    deadline = time.monotonic() + 12
    # Separate attempts preserve successful results even when another engine fails.
    for backend in ("brave", "duckduckgo", "yahoo", "google"):
        # DDGS silently falls back to 'auto' for disabled engines, losing provenance.
        if backend not in ENGINES.get("text", {}):
            failures.append(f"{backend}: backend unavailable")
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append("search time budget exhausted")
            break
        try:
            def fetch(backend=backend):
                with DDGS(timeout=min(3, remaining), proxy=_web_search_proxy()) as ddgs:
                    region = "cn-zh" if any("\u4e00" <= ch <= "\u9fff" for ch in query) else "us-en"
                    return list(ddgs.text(query, max_results=limit, backend=backend, region=region))

            raw_results = run_with_timeout(fetch, min(3, remaining), f"web search {backend}")
            results = [{
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", "")[:300],
            } for item in raw_results[:limit] if item.get("href")]
            if not results:
                raise ValueError("no usable search results")
            payload = {"query": query, "provider": backend, "count": len(results), "results": results,
                       "data_status": "fallback" if failures else "available",
                       "fetched_at": datetime.now(timezone.utc).isoformat()}
            if failures:
                payload["warnings"] = failures
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            failures.append(f"{backend}: {short_error_message(exc)}")
    return json.dumps({"error": "Web search unavailable: " + "; ".join(failures), "query": query, "results": []}, ensure_ascii=False)


def _fetch_cls_flash(limit: int) -> list[dict]:
    """Use the public endpoint used by cls.cn/telegraph, with no SDK retry loop."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import time
    import requests

    response = requests.get(
        "https://www.cls.cn/api/cache",
        params={"name": "telegraph", "rn": limit, "lastTime": int(time.time())},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/telegraph"},
        timeout=(2, 3),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errno") != 0:
        raise ValueError(f"CLS error {payload.get('errno')}")
    rows = (payload.get("data") or {}).get("roll_data") or []
    items = []
    for row in rows:
        content = str(row.get("content") or row.get("brief") or "").strip()
        try:
            timestamp = datetime.fromtimestamp(float(row["ctime"]), ZoneInfo("Asia/Shanghai"))
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if content:
            items.append({"time": timestamp.strftime("%Y-%m-%d %H:%M:%S"), "content": content[:300]})
    return sorted(items, key=lambda item: item["time"], reverse=True)[:limit]


def _fetch_eastmoney_flash(limit: int) -> list[dict]:
    """Fallback to financial flash news, preserving the tool's content contract."""
    import time
    import requests

    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    rate_limit_domain(url)
    response = requests.get(
        url,
        params={"client": "web", "biz": "web_724", "fastColumn": "102",
                "sortEnd": "", "pageSize": limit, "req_trace": str(int(time.time() * 1000))},
        headers=_EM_HEADERS,
        timeout=(2, 3),
    )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "1":
        raise ValueError(f"Eastmoney flash error: {payload.get('message', 'invalid response')}")
    rows = (payload.get("data") or {}).get("fastNewsList") or []
    items = []
    for row in rows:
        content = str(row.get("summary") or row.get("title") or "").strip()
        timestamp = str(row.get("showTime") or "").strip()
        if content and timestamp:
            items.append({"time": timestamp, "content": content[:300]})
    return sorted(items, key=lambda item: item["time"], reverse=True)[:limit]


def tool_search_market_flash(*, limit: int = 20) -> str:
    """Fetch latest financial flash news, preferring 财联社 (CLS)."""
    limit = max(1, min(limit, 100))
    failures = []
    for source, fetch in (("cls", _fetch_cls_flash), ("eastmoney_flash", _fetch_eastmoney_flash)):
        try:
            items = run_with_timeout(lambda fetch=fetch: fetch(limit), 6, f"market flash {source}")
            if not items:
                raise ValueError("empty flash news response")
            payload = {"count": len(items), "alerts": items, "source": source}
            if failures:
                payload["warnings"] = failures
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            failures.append(f"{source}: {short_error_message(exc)}")
    return json.dumps({"count": 0, "alerts": [], "error": "; ".join(failures)}, ensure_ascii=False)


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
    payload: dict = {"count": len(items), "boards": items, "source": source,
                     "data_status": "available" if source == "eastmoney" else "fallback",
                     "classification": source, "fetched_at": datetime.now(timezone.utc).isoformat(),
                     "as_of": None}
    if source != "eastmoney":
        payload["warnings"] = ["使用新浪分类，与东方财富板块口径不同；接口未提供行情时间，获取时间不代表行情时间。"]
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
    payload: dict = {"count": len(items), "boards": items, "source": source,
                     "data_status": "available" if source == "eastmoney" else "fallback",
                     "classification": source, "fetched_at": datetime.now(timezone.utc).isoformat(),
                     "as_of": None}
    if source != "eastmoney":
        payload["warnings"] = ["使用新浪分类，与东方财富板块口径不同；接口未提供行情时间，获取时间不代表行情时间。"]
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
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=3.0, write=3.0, pool=3.0)) as client:
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
    try:
        sina_codes = [to_sina_code(code) for code in codes]
    except ProviderError as exc:
        return json.dumps({"error": str(exc), "symbols": codes}, ensure_ascii=False)

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
            "symbol": dict(zip(sina_codes, codes)).get(code, code[2:]),
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
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=3.0, write=3.0, pool=3.0)) as client:
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
