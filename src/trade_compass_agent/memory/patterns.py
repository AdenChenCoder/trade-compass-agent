"""Cross-session pattern discovery — Dreaming Phase 2.

Two-stage algorithm:
1. Deterministic preprocessing — aggregate observations by day, extract concept stats
2. LLM semantic discovery — identify trading patterns from the structured data

Fallback: deterministic concept co-occurrence when LLM is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from trade_compass_agent.memory.decision_store import DecisionStore
from trade_compass_agent.memory.observation_store import ObservationStore

logger = logging.getLogger(__name__)

LLM_CALL = Callable[[str, str], str]  # (system_prompt, user_content) -> response


@dataclass
class TradingPattern:
    id: str
    theme: str
    description: str
    concepts: list[str]
    days_seen: int
    total_observations: int
    strength: float
    significance: str
    first_seen: str
    last_seen: str
    evidence: list[str] = field(default_factory=list)
    related_decisions: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# Stage 1: Deterministic preprocessing
# ------------------------------------------------------------------

def _build_daily_summaries(
    obs_store: ObservationStore,
    lookback_days: int,
) -> tuple[dict[str, list[dict]], Counter]:
    """Group observations by date; return per-day summaries and concept freq."""
    obs_list = obs_store.recent_by_days(lookback_days)
    daily: dict[str, list[dict]] = defaultdict(list)
    concept_freq: Counter = Counter()

    for obs in obs_list:
        day = obs.created_at[:10]  # "YYYY-MM-DD"
        daily[day].append({
            "id": obs.id,
            "summary": obs.summary[:200],
            "concepts": obs.concepts,
            "importance": obs.importance,
            "tool": obs.tool_name,
        })
        for c in obs.concepts:
            concept_freq[c] += 1

    return dict(daily), concept_freq


def _collect_decisions(
    dec_store: DecisionStore,
    lookback_days: int,
) -> list[dict[str, Any]]:
    all_decisions = dec_store.search(limit=200)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()[:10]
    recent: list[dict[str, Any]] = []
    for d in all_decisions:
        d_date = d.decided_at[:10] if d.decided_at else ""
        if d_date < cutoff:
            continue
        recent.append({
            "id": d.id,
            "symbol": d.symbol,
            "side": d.side,
            "status": d.status,
            "pnl_pct": d.outcome_pnl_pct,
            "reasoning": (d.reasoning or "")[:100],
        })
    return recent[-50:]


# ------------------------------------------------------------------
# Stage 2a: LLM-driven pattern discovery
# ------------------------------------------------------------------

PATTERN_DISCOVERY_SYSTEM = "你是一个交易记忆分析引擎。只返回合法 JSON 数组。"

PATTERN_DISCOVERY_PROMPT = """\
分析以下 {lookback_days} 天的交易观察记录，发现跨 session 的重复模式和趋势。

## 每日观察摘要
{daily_summaries}

## 概念频率统计
{concept_stats}

## 同期交易决策
{decisions}

## 任务
识别 3-7 个有意义的交易模式。每个模式必须：
- 跨越至少 2 天出现
- 有明确的交易含义（不是无关噪音）
- 包含具体的标的、板块或策略主题

返回 JSON 数组：
[{{
  "theme": "模式主题（简短）",
  "description": "模式描述（1-2 句）",
  "concepts": ["涉及的概念/标的/板块"],
  "strength": 0.0-1.0,
  "significance": "为什么这个模式对交易决策重要",
  "evidence_days": ["YYYY-MM-DD"]
}}]

只返回 JSON，不要其他文字。
"""


def _discover_via_llm(
    daily: dict[str, list[dict]],
    concept_freq: Counter,
    decisions: list[dict],
    llm_call: LLM_CALL,
    lookback_days: int,
) -> list[TradingPattern]:
    daily_text = ""
    for day in sorted(daily.keys()):
        items = daily[day]
        summaries = "; ".join(o["summary"][:80] for o in items[:10])
        concepts = sorted({c for o in items for c in o["concepts"]})
        daily_text += f"\n### {day} ({len(items)} observations)\n概念: {', '.join(concepts)}\n摘要: {summaries}\n"

    top_concepts = concept_freq.most_common(30)
    concept_text = "\n".join(f"- {c}: {n}次" for c, n in top_concepts)
    decisions_text = json.dumps(decisions[:20], ensure_ascii=False, indent=2) if decisions else "无"

    prompt = PATTERN_DISCOVERY_PROMPT.format(
        lookback_days=lookback_days,
        daily_summaries=daily_text[:4000],
        concept_stats=concept_text[:1000],
        decisions=decisions_text[:2000],
    )

    raw = llm_call(PATTERN_DISCOVERY_SYSTEM, prompt)
    return _parse_llm_patterns(raw, daily)


def _parse_llm_patterns(
    raw: str,
    daily: dict[str, list[dict]],
) -> list[TradingPattern]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM pattern output not valid JSON: %s", raw[:200])
        return []

    if not isinstance(items, list):
        return []

    patterns: list[TradingPattern] = []
    for item in items[:7]:
        if not isinstance(item, dict):
            continue
        theme = item.get("theme", "")
        if not theme:
            continue
        evidence_days = item.get("evidence_days", [])
        pattern_concepts = set(item.get("concepts", []))
        evidence_obs: list[str] = []
        for day in evidence_days:
            for obs_item in daily.get(day, []):
                if pattern_concepts & set(obs_item.get("concepts", [])):
                    evidence_obs.append(obs_item["id"])

        pid = hashlib.sha256(theme.encode()).hexdigest()[:12]
        patterns.append(TradingPattern(
            id=pid,
            theme=theme,
            description=item.get("description", ""),
            concepts=item.get("concepts", []),
            days_seen=len(evidence_days),
            total_observations=len(evidence_obs),
            strength=max(0.0, min(1.0, float(item.get("strength", 0.5)))),
            significance=item.get("significance", ""),
            first_seen=min(evidence_days) if evidence_days else "",
            last_seen=max(evidence_days) if evidence_days else "",
            evidence=evidence_obs[:50],
        ))
    return patterns


# ------------------------------------------------------------------
# Stage 2b: Deterministic fallback (concept co-occurrence)
# ------------------------------------------------------------------

def _discover_deterministic(
    daily: dict[str, list[dict]],
    concept_freq: Counter,
) -> list[TradingPattern]:
    """Fallback: concept pair co-occurrence across days."""
    pair_days: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_obs: dict[tuple[str, str], list[str]] = defaultdict(list)

    for day, items in daily.items():
        day_concepts: set[str] = set()
        for obs_item in items:
            for c in obs_item["concepts"]:
                day_concepts.add(c)
        for a, b in combinations(sorted(day_concepts), 2):
            pair_days[(a, b)].add(day)
            for obs_item in items:
                if a in obs_item["concepts"] or b in obs_item["concepts"]:
                    pair_obs[(a, b)].append(obs_item["id"])

    patterns: list[TradingPattern] = []
    for (a, b), days in sorted(pair_days.items(), key=lambda x: -len(x[1])):
        if len(days) < 2:
            continue
        sorted_days = sorted(days)
        strength = min(1.0, len(days) / 5.0)
        pid = hashlib.sha256(f"{a}:{b}".encode()).hexdigest()[:12]
        patterns.append(TradingPattern(
            id=pid,
            theme=f"{a} + {b} 共现",
            description=f"{a} 和 {b} 在 {len(days)} 天内同时出现",
            concepts=[a, b],
            days_seen=len(days),
            total_observations=len(set(pair_obs[(a, b)])),
            strength=strength,
            significance=f"概念频率 {a}={concept_freq[a]}, {b}={concept_freq[b]}",
            first_seen=sorted_days[0],
            last_seen=sorted_days[-1],
            evidence=list(set(pair_obs[(a, b)]))[:30],
        ))
        if len(patterns) >= 7:
            break

    return patterns


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def _load_previous_patterns(memory_dir: Path) -> list[TradingPattern]:
    """Load the most recent past patterns file for persistence tracking."""
    patterns_dir = memory_dir / "dreaming" / "patterns"
    if not patterns_dir.is_dir():
        return []
    files = sorted(patterns_dir.glob("*.json"), reverse=True)
    today = datetime.now().strftime("%Y-%m-%d")
    for f in files:
        if f.stem == today:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return [
                TradingPattern(
                    id=item.get("id", ""),
                    theme=item.get("theme", ""),
                    description=item.get("description", ""),
                    concepts=item.get("concepts", []),
                    days_seen=item.get("days_seen", 0),
                    total_observations=item.get("total_observations", 0),
                    strength=item.get("strength", 0),
                    significance=item.get("significance", ""),
                    first_seen=item.get("first_seen", ""),
                    last_seen=item.get("last_seen", ""),
                    evidence=item.get("evidence", []),
                    related_decisions=item.get("related_decisions", []),
                )
                for item in data
                if isinstance(item, dict)
            ]
        except (json.JSONDecodeError, KeyError):
            continue
    return []


def _annotate_persistence(
    current: list[TradingPattern],
    previous: list[TradingPattern],
) -> None:
    """Annotate patterns with persistence info based on previous day's patterns."""
    prev_themes = {p.theme for p in previous}
    prev_concepts = {c for p in previous for c in p.concepts}
    for p in current:
        if p.theme in prev_themes:
            p.significance = f"[持续] {p.significance}"
        elif p.concepts and set(p.concepts) & prev_concepts:
            p.significance = f"[相关] {p.significance}"


def discover_patterns(
    obs_store: ObservationStore,
    dec_store: DecisionStore,
    llm_call: LLM_CALL | None = None,
    *,
    lookback_days: int = 7,
    memory_dir: Path | None = None,
) -> list[TradingPattern]:
    """Discover cross-session trading patterns.

    Uses LLM when available; falls back to deterministic co-occurrence.
    Loads previous patterns for persistence tracking if memory_dir is provided.
    """
    daily, concept_freq = _build_daily_summaries(obs_store, lookback_days)
    if not daily:
        return []

    decisions = _collect_decisions(dec_store, lookback_days)

    if llm_call is not None:
        try:
            patterns = _discover_via_llm(daily, concept_freq, decisions, llm_call, lookback_days)
        except Exception as exc:
            logger.warning("LLM pattern discovery failed: %s; falling back to deterministic", exc)
            patterns = _discover_deterministic(daily, concept_freq)
    else:
        patterns = _discover_deterministic(daily, concept_freq)

    if memory_dir and patterns:
        prev = _load_previous_patterns(memory_dir)
        if prev:
            _annotate_persistence(patterns, prev)

    return patterns


def persist_patterns(memory_dir: Path, patterns: list[TradingPattern]) -> Path:
    """Save patterns snapshot to memory_vault/dreaming/patterns/{date}.json."""
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = memory_dir / "dreaming" / "patterns"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"
    data = [asdict(p) for p in patterns]
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Persisted %d patterns to %s", len(patterns), out_path)
    return out_path
