from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from trade_compass_agent.runtime.types import TurnEvent
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools.dispatch import tool_dispatch_specialists
from trade_compass_agent.runtime.tools.fetch_url import tool_fetch_url
from trade_compass_agent.runtime.tools.market import (
    tool_get_bars,
    tool_get_events,
    tool_get_fundamentals,
    tool_get_market_pulse,
)
from trade_compass_agent.runtime.tools.memory import tool_search_memory, tool_write_memory
from trade_compass_agent.runtime.tools.skills import tool_load_skill
from trade_compass_agent.runtime.tools.search import (
    tool_search_announcements,
    tool_search_concept_boards,
    tool_search_hot_stocks,
    tool_search_industry_boards,
    tool_search_institute_recommend,
    tool_search_lhb,
    tool_search_market_activity,
    tool_search_market_flash,
    tool_search_research_reports,
    tool_search_stock_news,
    tool_search_x,
    tool_search_x_kol,
    tool_search_xueqiu_hot,
    tool_sina_realtime_quote,
    tool_eastmoney_news,
    tool_web_search,
)
from trade_compass_agent.runtime.tools.ta import (
    tool_compute_bollinger,
    tool_compute_ma,
    tool_compute_macd,
    tool_compute_rsi,
    tool_compute_volume_ratio,
)
from trade_compass_agent.runtime.tools.signals import tool_emit_signal
from trade_compass_agent.runtime.tools.portfolio import (
    tool_analyze_portfolio,
    tool_check_exit_signals,
    tool_place_paper_trade,
)
from trade_compass_agent.runtime.tools.stock_structure import (
    tool_get_block_trades,
    tool_get_chip_distribution,
    tool_get_institutional_holdings,
    tool_get_margin_data,
    tool_get_shareholder_structure,
)
from trade_compass_agent.runtime.tools.batch import (
    tool_batch_get_bars,
    tool_batch_get_fundamentals,
    tool_batch_search_news,
)
from trade_compass_agent.runtime.tools.scheduler_tool import (
    LIST_SCHEDULED_TASKS_SCHEMA,
    REMOVE_SCHEDULED_TASK_SCHEMA,
    SCHEDULE_TASK_SCHEMA,
)
from trade_compass_agent.runtime.tools.self_improve import MEMORY_WRITE_SCHEMA, SKILL_MANAGE_SCHEMA
from trade_compass_agent.runtime.tools.catalyst_calendar import (
    CATALYST_CALENDAR_TOOL_SCHEMA,
    tool_build_catalyst_calendar,
)
from trade_compass_agent.runtime.tools.idea_generation import (
    IDEA_GENERATION_TOOL_SCHEMA,
    tool_build_idea_generation,
)
from trade_compass_agent.runtime.tools.operations import (
    is_builtin_operation_tool,
    tool_run_builtin_operation,
)

logger = logging.getLogger(__name__)


PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset({
    # Market data reads (may write to bar cache — different files per symbol, benign)
    "get_bars", "get_market_pulse", "get_fundamentals", "get_events",
    # TA computations (fetch bars internally)
    "compute_ma", "compute_rsi", "compute_macd", "compute_bollinger", "compute_volume_ratio",
    # Search / quote (standalone HTTP, no shared state)
    "search_stock_news", "search_announcements", "search_market_flash",
    "search_hot_stocks", "search_lhb", "search_concept_boards",
    "search_xueqiu_hot", "search_market_activity", "search_industry_boards",
    "search_research_reports", "search_institute_recommend", "search_x", "search_x_kol",
    "sina_realtime_quote", "eastmoney_news", "web_search", "fetch_url",
    # Structure / flow reads
    "get_fund_flow", "get_shareholder_structure",
    "get_institutional_holdings", "get_margin_data", "get_block_trades",
    # Read-only state queries
    "get_risk_status", "get_market_constraints", "check_exit_signals",
    "analyze_portfolio", "search_decisions", "list_scheduled_tasks",
})

BASE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_bars",
            "description": "Fetch OHLCV bars for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "limit": {"type": "integer", "default": 60},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_pulse",
            "description": "Fetch sector strength and limit-up summary.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Fetch fundamentals for a symbol: PE(TTM), PB, ROE, market cap, float/total shares, industry, latest turnover rate.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "Fetch recent disclosure events for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and extract plain text from an HTTP(S) URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load skill content by name. Optionally load a reference sub-document (e.g. reference='buffett' loads references/buffett.md).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reference": {"type": "string", "description": "Optional reference file name (without .md) to load from the skill's references/ directory."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_specialists",
            "description": (
                "Run one or more named specialist/subagent assets. Built-in specialists include "
                "equity_research, intraday_tech, risk_advisor, screener, debate, "
                "macro_sentiment, and chokepoint_analyst."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "specialist": {"type": "string"},
                                "task": {"type": "string"},
                            },
                            "required": ["specialist", "task"],
                        },
                    }
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search the hierarchical memory tree (tree/ directory) by keyword. Returns time-scoped notes and insights from past sessions. Use to recall prior analysis, market notes, or signal logs. For searching KNOWLEDGE.md/USER.md entries, use write_knowledge action=list instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords (e.g. '半导体', 'RSI策略', '止损经验')"},
                    "limit": {"type": "integer", "default": 8, "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": "Write a note to the hierarchical memory tree (tree/ directory) under a scope bucket. Use for session-level notes, market observations, and working drafts. For writing persistent knowledge or user profile, use write_knowledge instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Scope bucket (e.g. 'market-notes', 'trade-log', 'analysis')"},
                    "content": {"type": "string", "description": "Note content to write"},
                },
                "required": ["scope", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_status",
            "description": "Check risk cooldown status: consecutive losses, whether cooldown is active, and portfolio realized PnL summary.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_constraints",
            "description": "Check A-share trading constraints for a symbol. Returns sellable_today_qty (shares you can sell TODAY — already accounts for T+1), position_qty, min_lot, price limits. ALWAYS use sellable_today_qty to determine sell feasibility; never infer T+1 status from memory.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_rsi",
            "description": "Compute RSI for a symbol. Returns current value and overbought/oversold interpretation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "period": {"type": "integer", "default": 14},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_macd",
            "description": "Compute MACD line, signal, histogram, and cross status for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_bollinger",
            "description": "Compute Bollinger Bands (upper/middle/lower) and %B position for a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "period": {"type": "integer", "default": 20},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_volume_ratio",
            "description": "Compute current volume vs N-day average volume ratio (expansion/shrinkage).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "period": {"type": "integer", "default": 5},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_ma",
            "description": "Compute moving averages (MA) deterministically. Returns MA values, price position vs each MA, and trend alignment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "periods": {"type": "string", "default": "5,10,20,60", "description": "Comma-separated MA periods"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_stock_news",
            "description": "Search recent news articles for a specific stock (东方财富). Returns titles, summaries, sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_announcements",
            "description": "Search recent company announcements and disclosures (巨潮/东方财富). Returns announcement titles and dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "General web search for market research, industry analysis, or any topic. Uses Tavily (if key set) or DuckDuckGo (zero config).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query in Chinese or English"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_market_flash",
            "description": "Get latest 财联社 flash news alerts — real-time market events, sector moves, policy signals.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_hot_stocks",
            "description": "Get trending/hot stocks ranking (东方财富人气榜) — indicates market attention and crowding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 15},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_lhb",
            "description": "Get Dragon-Tiger list (龙虎榜) — shows institutional and hot-money buy/sell activity for recent days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Optional: filter by stock code"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_concept_boards",
            "description": "Get concept/theme board ranking (概念板块) — identifies which market themes are hot today.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 15},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_xueqiu_hot",
            "description": "Get Xueqiu (雪球) stock popularity/follow ranking — social media sentiment indicator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 15},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_market_activity",
            "description": "Get intraday unusual activity (盘口异动) — sudden volume spikes, rapid price moves, large block orders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_industry_boards",
            "description": "Get industry sector ranking (行业板块) — shows which industry sectors are leading or lagging today.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 15},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_research_reports",
            "description": "Get recent analyst research reports for a stock (个股研报) — report titles, institutions, ratings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_institute_recommend",
            "description": "Get institutional buy/hold/sell recommendations for a stock (机构推荐) — ratings and target prices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_x",
            "description": "Search X (Twitter) via xAI Grok — real-time posts from trading community, KOLs, company accounts. Requires XAI_API_KEY.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for on X (e.g., 'NVDA earnings', '茅台 分析')"},
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: specific X handles to search (without @, max 20)",
                    },
                    "days_back": {"type": "integer", "default": 7, "description": "How many days back to search"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_x_kol",
            "description": "Extract structured investment signals from X KOL posts (default: Serenity/白毛股神). Returns stock mentions, thesis summaries, conviction levels, and chokepoint indicators.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "X handles to track (default: aleabitoreddit/Serenity)",
                    },
                    "topic": {"type": "string", "description": "Optional topic filter (e.g., 'CPO', '人形机器人', '稀土')"},
                    "days_back": {"type": "integer", "default": 14, "description": "How many days back to search"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sina_realtime_quote",
            "description": "Get real-time stock quotes from Sina Finance. Works 24/7 (returns last close after hours). Fast, no token needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "Comma-separated stock codes, e.g. '600519,300750,000001'"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "eastmoney_news",
            "description": "Get latest A-share financial news (24/7 available). Covers market updates, policy, sector moves.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10, "description": "Number of news items"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emit_signal",
            "description": "Record a structured trading signal after completing analysis. This supplements — never replaces — the user-facing report: always still write the full structured analysis in your reply before or after calling this tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code, e.g. '600519'"},
                    "rating": {
                        "type": "string",
                        "enum": ["strong_buy", "buy", "hold", "sell", "strong_sell"],
                        "description": "Directional view: strong_buy/buy/hold/sell/strong_sell",
                    },
                    "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                    "entry_price": {"type": "number", "description": "Suggested entry price"},
                    "stop_loss": {"type": "number", "description": "Stop-loss price"},
                    "target_price": {"type": "number", "description": "Target/take-profit price"},
                    "reasoning": {"type": "string", "description": "2-4 sentence justification citing tool results"},
                    "source_specialist": {"type": "string", "description": "Which specialist produced this"},
                    "source_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tools used (e.g. ['get_bars', 'compute_rsi'])",
                    },
                    "source_skills": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Skills that influenced this signal (e.g. ['momentum-scan', 'risk-veto'])",
                    },
                },
                "required": ["symbol", "rating", "confidence", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_portfolio",
            "description": "Get comprehensive portfolio analysis: all positions, P&L, win rate, concentration, recent closed trades.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_ocr",
            "description": "Extract text from an image file using local OCR. Use when the user attached an image containing text (K-line screenshots, tables, announcements).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Image file path (from [附件·图片] placeholder)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_analyze",
            "description": "Analyze/describe an image using a vision model. Use when the user attached an image and you need semantic understanding (chart patterns, UI screenshots, diagrams).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Image file path (from [附件·图片] placeholder)"},
                    "prompt": {"type": "string", "description": "Analysis prompt (default: describe trading-related content)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_paper_trade",
            "description": "Execute a paper trade (buy or sell). The price is always the actual execution price: buy execution price for buys, sell execution price for sells. Auto-detects market rules (T+0/T+1, price limits) from symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "quantity": {"type": "integer", "description": "Number of shares"},
                    "price": {"type": "number", "description": "Required only for broker_fill, user_confirmed, or compatibility mode. Ignored when price_source=market_quote because the server determines the fill."},
                    "reason": {"type": "string", "description": "Trade rationale"},
                    "price_source": {"type": "string", "enum": ["market_quote", "broker_fill", "user_confirmed"], "default": "market_quote", "description": "Use market_quote for paper execution. broker_fill requires a broker-provided fill; user_confirmed is allowed only when the user explicitly supplied the executed price."},
                    "record_decision": {"type": "boolean", "default": True, "description": "Create a Decision Journal entry for a buy. Set false when only importing an existing position."},
                    "account": {"type": "string", "default": "short_stock", "description": "Account: short_stock, etf_rotation, mid_term, long_term, mixed"},
                    "previous_close": {"type": "number", "description": "Previous close for price limit check"},
                    "is_st": {"type": "boolean", "default": False},
                    "suspended": {"type": "boolean", "default": False},
                    "is_t0": {"type": "boolean", "description": "Override T+0 flag (auto-detected if omitted)"},
                    "price_limit_pct": {"type": "number", "description": "Override price limit % (auto-detected if omitted)"},
                },
                "required": ["symbol", "side", "quantity", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_paper_trades",
            "description": "Execute multiple paper trades in one call. Useful for bulk position sync or real account mirroring. Each price is the actual execution price. A provided cost/average price may be used only for a buy that syncs an existing position; a sell must use its actual sell price or a fresh market quote, never the entry cost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trades": {
                        "type": "array",
                        "description": "List of trades to execute",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {"type": "string"},
                                "side": {"type": "string", "enum": ["buy", "sell"], "default": "buy"},
                                "quantity": {"type": "integer"},
                                "price": {"type": "number", "description": "Actual execution price per share. For buys that sync existing positions, use the actual cost basis. For sells, MUST use the actual sell price or a fresh market quote; NEVER use entry/average cost."},
                                "reason": {"type": "string", "default": "批量录入"},
                                "price_source": {"type": "string", "enum": ["market_quote", "broker_fill", "user_confirmed", "external_import"], "default": "external_import"},
                                "record_decision": {"type": "boolean", "default": False, "description": "Position imports do not create decisions unless explicitly enabled."},
                                "account": {"type": "string", "default": "short_stock"},
                                "previous_close": {"type": "number"},
                                "is_st": {"type": "boolean", "default": False},
                            },
                            "required": ["symbol", "quantity", "price"],
                        },
                    },
                },
                "required": ["trades"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_decisions",
            "description": "Search past trade decisions from the Decision Journal. Use to recall why you bought/sold a stock, review past wins/losses for a symbol, or get context before making a new trade.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Filter by stock symbol (optional)"},
                    "status": {"type": "string", "enum": ["pending", "partial", "resolved", "reflected"], "description": "Filter by decision status"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_instrument",
            "description": "Recall the instrument page for a stock. Contains key levels, trade history, and notes accumulated over time. Use before trading a previously tracked stock.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code (e.g. 002938)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_instrument_page",
            "description": "Update a section of the instrument page for a stock. Sections: 关注理由, 关键价位, 交易历史, 笔记. Creates page if not exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code"},
                    "section": {"type": "string", "description": "Section to update (关注理由/关键价位/交易历史/笔记)"},
                    "content": {"type": "string", "description": "New content for the section"},
                    "name": {"type": "string", "description": "Stock name (used if creating new page)"},
                },
                "required": ["symbol", "section", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_timeline",
            "description": "Recall time-scoped trading summaries from the TimeTree hierarchy, or trace a concept's appearance across days. Use to review what happened today/this_week/a specific date, or to see when a concept was active.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Time scope: 'today', 'this_week', 'this_month', 'YYYY-MM-DD', 'YYYY-WNN'. Returns the sealed summary for that period.",
                    },
                    "concept": {
                        "type": "string",
                        "description": "Optional concept/symbol to trace across days. When provided, returns day nodes mentioning this concept within lookback_days.",
                    },
                    "lookback_days": {
                        "type": "integer",
                        "description": "How many days back to search for concept timeline (default 30).",
                        "default": 30,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_exit_signals",
            "description": "Check all held positions for exit signals: stop-loss breach, target reached, drawdown threshold.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kline_forecast",
            "description": "Predict future K-line bars using Kronos foundation model. Returns predicted OHLCV bars with confidence bands. Requires PyTorch + Kronos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code to forecast"},
                    "horizon": {"type": "integer", "default": 10, "description": "Number of future bars to predict (default 10)"},
                    "model_size": {"type": "string", "default": "small", "description": "Model size: mini/small/base"},
                    "sample_count": {"type": "integer", "default": 5, "description": "Number of sample paths for confidence estimation"},
                    "lookback": {"type": "integer", "default": 120, "description": "Number of historical bars to use (max 400)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chart_pattern",
            "description": "Render K-line chart with Bollinger Bands and MA overlays, compute TA indicators (RSI/MACD/MA/Bollinger), then analyze visual candlestick/technical patterns using Vision LLM. Returns structured report with indicator cross-validation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock code to analyze"},
                    "bars": {"type": "integer", "default": 40, "description": "Number of K-line bars to render (default 40)"},
                    "multi_timeframe": {"type": "boolean", "default": False, "description": "If true, also include weekly chart for multi-timeframe analysis"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fund_flow",
            "description": "Get fund flow data: main force inflow and industry/concept sector flows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["main_force", "industry", "concept", "summary"],
                        "default": "summary",
                        "description": "Data category. 'summary' returns all categories.",
                    },
                    "limit": {"type": "integer", "default": 10, "description": "Number of items to return."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shareholder_structure",
            "description": "Get shareholder structure: top-10 circulating holders, holder count trend, institutional vs retail ratio. Data from quarterly reports.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chip_distribution",
            "description": "Get chip distribution (CYQ): profit ratio, avg cost, 70%/90% cost concentration zones. Pre-computed by Eastmoney.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_institutional_holdings",
            "description": "Get fund/institutional holdings: which funds hold the stock, position sizes, quarter-over-quarter changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_margin_data",
            "description": "Get margin trading data (融资融券): financing balance, short selling balance. Exchange official data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_block_trades",
            "description": "Get block trades (大宗交易): recent large off-market trades with premium/discount. Exchange official data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_get_bars",
            "description": "Fetch OHLCV bars for multiple symbols in parallel. Much faster than calling get_bars one by one. Returns compact summaries by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "Comma-separated stock codes, e.g. '600519,300750,000001' (max 20)"},
                    "timeframe": {"type": "string", "default": "1d"},
                    "limit": {"type": "integer", "default": 60},
                    "summary_only": {"type": "boolean", "default": True, "description": "If true, return compact per-symbol summaries; if false, return full bar data"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_get_fundamentals",
            "description": "Fetch fundamentals (PE, PB, market cap, industry) for multiple symbols in one call. Uses East Money batch API for speed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "Comma-separated stock codes, e.g. '600519,300750,000001' (max 30)"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_search_news",
            "description": "Search recent news for multiple symbols in parallel. Returns top headlines per symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "string", "description": "Comma-separated stock codes, e.g. '600519,300750' (max 10)"},
                    "limit_per_symbol": {"type": "integer", "default": 5, "description": "Max news items per symbol"},
                },
                "required": ["symbols"],
            },
        },
    },
]

BASE_TOOL_SCHEMAS.extend([CATALYST_CALENDAR_TOOL_SCHEMA, IDEA_GENERATION_TOOL_SCHEMA])
TOOL_SCHEMAS = BASE_TOOL_SCHEMAS

def _refresh_dynamic_schema(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function", {})
    if function.get("name") != "dispatch_specialists":
        return schema
    refreshed = json.loads(json.dumps(schema, ensure_ascii=False))
    refreshed["function"]["description"] = _dispatch_specialists_description()
    return refreshed


def _dispatch_specialists_description() -> str:
    try:
        from trade_compass_agent.runtime.specialists.assets import load_specialist_profiles

        profiles = load_specialist_profiles()
    except Exception:
        profiles = {}
    if not profiles:
        return "Run one or more named specialist/subagent assets."
    names = ", ".join(sorted(profiles))
    return (
        "Run one or more named specialist/subagent assets. Available folder-backed "
        f"specialists: {names}."
    )

SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": "Search past conversation sessions (chat history) by keyword. Returns session summaries with timestamps. Use to recall what was discussed, what decisions were made, or what the user said. Different from search_memory (which searches the memory tree/notes).",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (e.g. '半导体', 'risk management', '上次讨论')",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 5)",
            },
        },
        "required": ["query"],
    },
}

SCHEDULER_TOOL_SCHEMAS = [
    {"type": "function", "function": SCHEDULE_TASK_SCHEMA},
    {"type": "function", "function": LIST_SCHEDULED_TASKS_SCHEMA},
    {"type": "function", "function": REMOVE_SCHEDULED_TASK_SCHEMA},
]

SELF_IMPROVE_SCHEMAS = [
    {"type": "function", "function": MEMORY_WRITE_SCHEMA},
    {"type": "function", "function": SKILL_MANAGE_SCHEMA},
    {"type": "function", "function": SESSION_SEARCH_SCHEMA},
]


_CRITICAL_TOOLS = {"place_paper_trade", "batch_paper_trades"}


class ToolRegistry:
    def __init__(
        self,
        stack: MarketStack,
        on_event: Callable[[TurnEvent], None] | None = None,
        *,
        mcp_registry=None,
        memory_store=None,
        skill_store=None,
        session_summary_store=None,
        observation_store=None,
        exclude_tools: set[str] | None = None,
        memory_actor: str = "agent",
        skill_actor: str = "user",
    ) -> None:
        self.stack = stack
        self.on_event = on_event
        self._mcp = mcp_registry
        self._memory_store = memory_store
        self._skill_store = skill_store
        self._session_summary_store = session_summary_store
        self._obs_store = observation_store
        self._exclude_tools: set[str] = exclude_tools or set()
        self._consecutive_failures: dict[str, int] = {}
        self._memory_actor: str = memory_actor
        self._skill_actor: str = skill_actor

    @property
    def schemas(self) -> list[dict[str, Any]]:
        schemas = [_refresh_dynamic_schema(schema) for schema in BASE_TOOL_SCHEMAS]
        schemas.extend(SCHEDULER_TOOL_SCHEMAS)
        if self._memory_store or self._skill_store:
            schemas.extend(SELF_IMPROVE_SCHEMAS)
        if self._mcp is not None:
            try:
                schemas.extend(self._mcp.tool_schemas)
            except Exception:
                pass
        if self._exclude_tools:
            schemas = [
                s for s in schemas
                if s.get("function", {}).get("name") not in self._exclude_tools
                and s.get("name") not in self._exclude_tools
            ]
        return schemas

    def execute(self, name: str, arguments: str | dict | None) -> str:
        if name in self._exclude_tools:
            return json.dumps({"error": f"Tool '{name}' is not available in this context"}, ensure_ascii=False)
        try:
            result = self._execute(name, arguments)
            self._consecutive_failures.pop(name, None)
            return result
        except Exception as exc:
            error_payload: dict[str, Any] = {"error": str(exc), "tool": name}
            if name in _CRITICAL_TOOLS:
                count = self._consecutive_failures.get(name, 0) + 1
                self._consecutive_failures[name] = count
                if count >= 2:
                    error_payload["escalation"] = (
                        f"⚠️ {name} 已连续失败{count}次，疑似系统bug。"
                        "请向用户报告此故障，不要用 write_memory 替代交易操作。"
                    )
            return json.dumps(error_payload, ensure_ascii=False)

    def _execute(self, name: str, arguments: str | dict | None) -> str:
        args: dict[str, Any] = {}
        if isinstance(arguments, str) and arguments.strip():
            args = json.loads(arguments)
        elif isinstance(arguments, dict):
            args = arguments
        memory_dir = self.stack.config.memory_dir

        if name == "get_bars":
            return tool_get_bars(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
                limit=int(args.get("limit", 60)),
            )
        if name == "get_market_pulse":
            return tool_get_market_pulse(self.stack)
        if is_builtin_operation_tool(name):
            return tool_run_builtin_operation(self.stack.config, name, args)
        if name == "get_fundamentals":
            return tool_get_fundamentals(self.stack, symbol=str(args.get("symbol", "")))
        if name == "get_events":
            return tool_get_events(
                self.stack,
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 5)),
            )
        if name == "load_skill":
            skill_name = str(args.get("name", ""))
            reference = args.get("reference") or None
            result = tool_load_skill(memory_dir=memory_dir, name=skill_name, reference=reference)
            if not result.lstrip().startswith("{"):
                from trade_compass_agent.memory.skill_store import SkillStore
                SkillStore(memory_dir / "skills").record_use(skill_name)
                if self.on_event:
                    self.on_event(
                        TurnEvent(
                            event="skill_loaded",
                            data={"name": skill_name, "status": "ok"},
                        )
                    )
            return result
        if name == "fetch_url":
            return tool_fetch_url(str(args.get("url", "")))
        if name == "dispatch_specialists":
            tasks = args.get("tasks") or []
            return tool_dispatch_specialists(
                self.stack,
                list(tasks),
                config=self.stack.config,
                on_event=self.on_event,
            )
        if name == "search_memory":
            query = str(args.get("query", ""))
            self._reinforce_on_search(query)
            self._cross_recall_observations(query)
            return tool_search_memory(
                memory_dir,
                query,
                limit=int(args.get("limit", 8)),
            )
        if name == "write_memory":
            return tool_write_memory(
                memory_dir,
                str(args.get("scope", "general")),
                str(args.get("content", "")),
            )
        if name == "write_knowledge" and self._memory_store:
            from trade_compass_agent.runtime.tools.self_improve import tool_memory_write
            legacy_source = str(args.get("source", "") or args.get("scope", "") or "")
            return tool_memory_write(
                self._memory_store,
                action=str(args.get("action", "")),
                content=str(args.get("content", "")),
                target=str(args.get("target", "memory")),
                old_text=str(args.get("old_text", "")),
                source=legacy_source,
                actor=self._memory_actor,
                governance=self.stack.config.memory.governance,
            )
        if name == "skill_manage" and self._skill_store:
            from trade_compass_agent.runtime.tools.self_improve import tool_skill_manage
            return tool_skill_manage(
                self._skill_store,
                action=str(args.get("action", "")),
                name=str(args.get("name", "")),
                content=str(args.get("content", "")),
                old_text=str(args.get("old_text", "")),
                new_text=str(args.get("new_text", "")),
                actor=self._skill_actor,
            )
        if name == "session_search" and self._session_summary_store:
            query = str(args.get("query", ""))
            self._cross_recall_observations(query)
            return self._tool_session_search(
                query=query,
                limit=int(args.get("limit", 5)),
            )
        if name == "schedule_task":
            from trade_compass_agent.runtime.tools.scheduler_tool import tool_schedule_task
            return tool_schedule_task(
                self.stack.config,
                name=str(args.get("name", "")),
                prompt=str(args.get("prompt", "")),
                schedule=str(args.get("schedule", "")),
                trading_day_only=bool(args.get("trading_day_only", False)),
            )
        if name == "list_scheduled_tasks":
            from trade_compass_agent.runtime.tools.scheduler_tool import tool_list_scheduled_tasks
            return tool_list_scheduled_tasks(self.stack.config)
        if name == "remove_scheduled_task":
            from trade_compass_agent.runtime.tools.scheduler_tool import tool_remove_scheduled_task
            return tool_remove_scheduled_task(self.stack.config, task_id=str(args.get("task_id", "")))
        if name == "get_risk_status":
            return self._tool_get_risk_status()
        if name == "get_market_constraints":
            return self._tool_get_market_constraints(str(args.get("symbol", "")))
        if name == "compute_rsi":
            return tool_compute_rsi(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
                period=int(args.get("period", 14)),
            )
        if name == "compute_macd":
            return tool_compute_macd(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
            )
        if name == "compute_bollinger":
            return tool_compute_bollinger(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
                period=int(args.get("period", 20)),
            )
        if name == "compute_volume_ratio":
            return tool_compute_volume_ratio(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
                period=int(args.get("period", 5)),
            )
        if name == "compute_ma":
            return tool_compute_ma(
                self.stack,
                symbol=str(args.get("symbol", "")),
                timeframe=str(args.get("timeframe", "1d")),
                periods=str(args.get("periods", "5,10,20,60")),
            )
        if name == "search_stock_news":
            return tool_search_stock_news(
                self.stack,
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 10)),
            )
        if name == "search_announcements":
            return tool_search_announcements(
                self.stack,
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 8)),
            )
        if name == "web_search":
            return tool_web_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 5)),
            )
        if name == "search_market_flash":
            return tool_search_market_flash(
                limit=int(args.get("limit", 20)),
            )
        if name == "search_hot_stocks":
            return tool_search_hot_stocks(
                limit=int(args.get("limit", 15)),
            )
        if name == "search_lhb":
            return tool_search_lhb(
                symbol=args.get("symbol") or None,
                limit=int(args.get("limit", 10)),
            )
        if name == "search_concept_boards":
            return tool_search_concept_boards(
                limit=int(args.get("limit", 15)),
            )
        if name == "search_xueqiu_hot":
            return tool_search_xueqiu_hot(
                limit=int(args.get("limit", 15)),
            )
        if name == "search_market_activity":
            return tool_search_market_activity(
                limit=int(args.get("limit", 20)),
            )
        if name == "search_industry_boards":
            return tool_search_industry_boards(
                limit=int(args.get("limit", 15)),
            )
        if name == "search_research_reports":
            return tool_search_research_reports(
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 8)),
            )
        if name == "search_institute_recommend":
            return tool_search_institute_recommend(
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 8)),
            )
        if name == "search_x":
            handles_raw = args.get("handles")
            return tool_search_x(
                query=str(args.get("query", "")),
                handles=handles_raw,
                days_back=int(args.get("days_back", 7)),
            )
        if name == "search_x_kol":
            handles_raw = args.get("handles")
            return tool_search_x_kol(
                handles=handles_raw if isinstance(handles_raw, list) else None,
                topic=str(args.get("topic", "")),
                days_back=int(args.get("days_back", 14)),
            )
        if name == "sina_realtime_quote":
            return tool_sina_realtime_quote(
                symbols=str(args.get("symbols", "")),
            )
        if name == "eastmoney_news":
            return tool_eastmoney_news(
                limit=int(args.get("limit", 10)),
            )
        if name == "emit_signal":
            return tool_emit_signal(
                self.stack,
                symbol=args.get("symbol"),
                rating=args.get("rating"),
                confidence=args.get("confidence"),
                entry_price=args.get("entry_price"),
                stop_loss=args.get("stop_loss"),
                target_price=args.get("target_price"),
                reasoning=args.get("reasoning"),
                source_specialist=args.get("source_specialist", "agent"),
                source_tools=args.get("source_tools"),
                source_skills=args.get("source_skills"),
            )
        if name == "analyze_portfolio":
            return tool_analyze_portfolio(self.stack)
        if name == "place_paper_trade":
            return tool_place_paper_trade(
                self.stack,
                symbol=args.get("symbol"),
                side=args.get("side"),
                quantity=args.get("quantity"),
                price=args.get("price"),
                price_source=args.get("price_source", "market_quote"),
                record_decision=args.get("record_decision", True),
                reason=args.get("reason", ""),
                account=args.get("account", "short_stock"),
                previous_close=args.get("previous_close"),
                is_st=args.get("is_st", False),
                suspended=args.get("suspended", False),
                is_t0=args.get("is_t0"),
                price_limit_pct=args.get("price_limit_pct"),
            )
        if name == "batch_paper_trades":
            from trade_compass_agent.runtime.tools.portfolio import tool_batch_paper_trades
            return tool_batch_paper_trades(self.stack, trades=args.get("trades", []))
        if name == "search_decisions":
            from trade_compass_agent.memory.decision_reconciler import reconcile_decisions
            from trade_compass_agent.memory.decision_store import DecisionStore
            reconcile_decisions(self.stack.config.data_dir, self.stack.config.trading_costs)
            store = DecisionStore(self.stack.config.data_dir)
            results = store.search(symbol=args.get("symbol"), status=args.get("status"), limit=args.get("limit", 10))
            return json.dumps([r.__dict__ for r in results], ensure_ascii=False, default=str)
        if name == "recall_timeline":
            from trade_compass_agent.memory.time_tree import TimeTree
            tt = TimeTree(self.stack.config.data_dir / "time_tree.db")
            concept = args.get("concept")
            if concept:
                nodes = tt.concept_timeline(concept, lookback_days=args.get("lookback_days", 30))
                if not nodes:
                    return json.dumps({"found": False, "concept": concept}, ensure_ascii=False)
                return json.dumps(
                    [{"date": n.id, "summary": n.summary[:200], "concepts": n.key_concepts, "symbols": n.key_symbols} for n in nodes],
                    ensure_ascii=False,
                )
            scope = args.get("scope", "today")
            return tt.recall(scope) or json.dumps({"found": False, "scope": scope}, ensure_ascii=False)
        if name == "recall_instrument":
            from trade_compass_agent.memory.instrument_store import InstrumentStore
            inst_store = InstrumentStore(self.stack.config.memory_dir)
            page = inst_store.recall(args["symbol"])
            if page is None:
                return json.dumps({"found": False, "symbol": args["symbol"]}, ensure_ascii=False)
            return page
        if name == "update_instrument_page":
            from trade_compass_agent.memory.instrument_store import InstrumentStore
            inst_store = InstrumentStore(self.stack.config.memory_dir)
            return json.dumps(
                inst_store.update_section(
                    symbol=args["symbol"], section=args["section"],
                    content=args["content"], name=args.get("name", ""),
                ),
                ensure_ascii=False,
            )
        if name == "check_exit_signals":
            return tool_check_exit_signals(self.stack)
        if name == "kline_forecast":
            from trade_compass_agent.runtime.tools.kline_forecast import tool_kline_forecast
            return tool_kline_forecast(self.stack, **args)
        if name == "chart_pattern":
            from trade_compass_agent.runtime.tools.chart_pattern import tool_chart_pattern
            return tool_chart_pattern(self.stack, **args)
        if name == "get_fund_flow":
            from trade_compass_agent.data.fund_flow import tool_get_fund_flow
            import json as _json
            category = str(args.get("category", "summary"))
            limit = int(args.get("limit", 10))
            return _json.dumps(tool_get_fund_flow(category=category, limit=limit), ensure_ascii=False)
        if name == "get_shareholder_structure":
            return tool_get_shareholder_structure(symbol=str(args.get("symbol", "")))
        if name == "get_chip_distribution":
            return tool_get_chip_distribution(symbol=str(args.get("symbol", "")))
        if name == "get_institutional_holdings":
            return tool_get_institutional_holdings(
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 10)),
            )
        if name == "get_margin_data":
            return tool_get_margin_data(
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 10)),
            )
        if name == "get_block_trades":
            return tool_get_block_trades(
                symbol=str(args.get("symbol", "")),
                limit=int(args.get("limit", 10)),
            )
        if name == "batch_get_bars":
            return tool_batch_get_bars(
                self.stack,
                symbols=str(args.get("symbols", "")),
                timeframe=str(args.get("timeframe", "1d")),
                limit=int(args.get("limit", 60)),
                summary_only=bool(args.get("summary_only", True)),
            )
        if name == "batch_get_fundamentals":
            return tool_batch_get_fundamentals(
                self.stack,
                symbols=str(args.get("symbols", "")),
            )
        if name == "batch_search_news":
            return tool_batch_search_news(
                self.stack,
                symbols=str(args.get("symbols", "")),
                limit_per_symbol=int(args.get("limit_per_symbol", 5)),
            )
        if name == "build_catalyst_calendar":
            return tool_build_catalyst_calendar(**args)
        if name == "build_idea_generation":
            return tool_build_idea_generation(**args)
        if name == "image_ocr":
            from trade_compass_agent.runtime.tools.image_tools import tool_image_ocr
            return tool_image_ocr(config=self.stack.config, **args)
        if name == "image_analyze":
            from trade_compass_agent.runtime.tools.image_tools import tool_image_analyze
            return tool_image_analyze(config=self.stack.config, **args)
        if self._mcp is not None and self._mcp.is_mcp_tool(name):
            return self._mcp.execute(name, args)
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)

    def _tool_get_risk_status(self) -> str:
        from trade_compass_agent.risk import CooldownTracker
        from trade_compass_agent.portfolio import JsonPaperPortfolio

        config = self.stack.config
        tracker = CooldownTracker(
            config.data_dir / "cooldown_state.json",
            threshold=3,
        )
        portfolio = JsonPaperPortfolio(
            config.data_dir / "paper_trades.jsonl",
            costs=config.trading_costs,
        )
        realized = portfolio.realized_trades()
        recent_pnl = [t.pnl for t in realized[-10:]] if realized else []
        return json.dumps(
            {
                "cooldown_active": tracker.is_active(),
                "consecutive_losses": tracker.state.consecutive_losses,
                "cooldown_threshold": tracker.threshold,
                "total_realized_trades": len(realized),
                "recent_10_pnl": recent_pnl,
            },
            ensure_ascii=False,
        )

    def _tool_get_market_constraints(self, symbol: str) -> str:
        from datetime import date as date_type

        from trade_compass_agent.portfolio import JsonPaperPortfolio
        from trade_compass_agent.portfolio.market_rules import infer_market_rules
        from trade_compass_agent.portfolio.accounts import AccountStore

        config = self.stack.config
        portfolio = JsonPaperPortfolio(
            config.data_dir / "paper_trades.jsonl",
            costs=config.trading_costs,
        )
        rules = infer_market_rules(symbol)
        today = date_type.today()
        position_qty = 0
        bought_today_qty = 0
        for trade in portfolio.trades:
            if trade.symbol != symbol:
                continue
            if trade.side == "buy":
                position_qty += trade.quantity
                if trade.timestamp.date() == today:
                    bought_today_qty += trade.quantity
            else:
                position_qty -= trade.quantity
        position_qty = max(position_qty, 0)
        sellable_qty = max(position_qty - bought_today_qty, 0) if not rules.is_t0 else position_qty

        account_store = AccountStore(config.data_dir / "accounts.json")
        accounts = account_store.list()
        total_capital = sum(a.capital for a in accounts)

        is_st = any(
            t.is_st for t in portfolio.trades if t.symbol == symbol
        )
        is_min_lot = position_qty > 0 and position_qty <= rules.min_lot
        return json.dumps(
            {
                "symbol": symbol,
                "board": rules.board,
                "is_st": is_st,
                "position_qty": position_qty,
                "sellable_today_qty": sellable_qty,
                "bought_today_qty": bought_today_qty,
                "price_limit_pct": rules.price_limit_pct,
                "min_lot": rules.min_lot,
                "is_min_lot": is_min_lot,
                "is_t0": rules.is_t0,
                "total_capital": total_capital,
                "lot_note": (
                    f"持仓{position_qty}股=最小手数{rules.min_lot}股，无法部分减仓，只能全部卖出或继续持有"
                    if is_min_lot
                    else None
                ),
            },
            ensure_ascii=False,
        )

    def _tool_session_search(self, query: str, limit: int = 5) -> str:
        if not self._session_summary_store:
            return json.dumps({"error": "Session search not available"}, ensure_ascii=False)
        results = self._session_summary_store.search(query, limit=limit)
        if not results:
            return json.dumps({"results": [], "message": f"No past sessions found for '{query}'"}, ensure_ascii=False)
        return json.dumps(
            {
                "results": [
                    {
                        "session_id": r.session_id,
                        "title": r.title,
                        "summary": r.summary,
                        "turn_count": r.turn_count,
                        "symbols": r.symbols,
                        "started_at": r.started_at,
                    }
                    for r in results
                ],
                "total": len(results),
            },
            ensure_ascii=False,
        )

    def _reinforce_on_search(self, query: str) -> None:
        """Reinforce KNOWLEDGE/USER entries that relate to the search query."""
        if not self._memory_store:
            return
        try:
            query_lower = query.lower()
            for target in ("memory", "user"):
                for entry in self._memory_store.list_active(
                    target,
                    min_confidence=self.stack.config.memory.governance.min_inject_confidence,
                ):
                    if any(term in entry.text.lower() for term in query_lower.split() if len(term) >= 2):
                        self._memory_store.reinforce(entry.text[:50], target)
                        break
        except Exception as exc:
            logger.debug("Reinforce on search failed: %s", exc)

    def _cross_recall_observations(self, query: str) -> None:
        """Cross-recall: bump observation recall when the agent searches any store.

        ObservationStore.search() already tracks recall for its own results
        (inline recall). This method provides a second signal path: when the
        agent searches memory tree or session summaries, we also search
        observations with the same query to accumulate cross-store signals.

        Guards: skip overly broad queries (< 4 chars) and limit to 3 results
        to prevent false recall_count inflation.
        """
        if not self._obs_store:
            return
        query = query.strip()
        if len(query) < 4:
            return
        try:
            self._obs_store.search(query, limit=3)
        except Exception as exc:
            logger.debug("Cross-recall observations failed: %s", exc)
