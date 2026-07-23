"""Enhanced memory — L2 scenario memory + L3 stock profiles.

L1: Atomic memory (existing write_memory tool)
L2: Scenario memory — group similar trading patterns for retrieval
L3: Stock profiles — per-symbol dossier with trading history and lessons

Layers:
- Three-layer memory: atomic observations, scenarios, and profiles
- Hierarchical time-based retrieval primitives
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ScenarioMemory:
    """L2: A recognized trading scenario pattern."""

    scenario_id: str
    pattern_name: str  # e.g. "均线金叉+板块轮动", "突破回踩确认"
    description: str
    signals_involved: list[str] = field(default_factory=list)
    outcome_history: list[str] = field(default_factory=list)  # win/loss records
    win_rate: float = 0.0
    last_seen: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class StockProfile:
    """L3: Per-symbol trading dossier."""

    symbol: str
    name: str = ""
    industry: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_holding_days: float = 0.0
    total_pnl_pct: float = 0.0
    characteristics: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    last_traded: str = ""
    last_updated: str = ""


class EnhancedMemory:
    """Manages L2 scenario patterns and L3 stock profiles."""

    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self.scenarios_dir = memory_dir / "scenarios"
        self.profiles_dir = memory_dir / "instruments"
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    # --- L2: Scenario Memory ---

    def record_scenario(self, scenario: ScenarioMemory) -> Path:
        """Record or update a trading scenario pattern."""
        path = self.scenarios_dir / f"{scenario.scenario_id}.json"
        data = {
            "scenario_id": scenario.scenario_id,
            "pattern_name": scenario.pattern_name,
            "description": scenario.description,
            "signals_involved": scenario.signals_involved,
            "outcome_history": scenario.outcome_history,
            "win_rate": scenario.win_rate,
            "last_seen": scenario.last_seen or datetime.now().isoformat(),
            "tags": scenario.tags,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def find_scenarios(self, tags: list[str] | None = None, limit: int = 5) -> list[ScenarioMemory]:
        """Find relevant scenarios by tags."""
        scenarios: list[ScenarioMemory] = []
        for path in self.scenarios_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if tags:
                    scenario_tags = set(data.get("tags", []))
                    if not scenario_tags & set(tags):
                        continue
                scenarios.append(ScenarioMemory(**data))
            except (json.JSONDecodeError, TypeError):
                continue

        scenarios.sort(key=lambda s: s.last_seen, reverse=True)
        return scenarios[:limit]

    def update_scenario_outcome(self, scenario_id: str, outcome: str) -> None:
        """Add an outcome to an existing scenario."""
        path = self.scenarios_dir / f"{scenario_id}.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("outcome_history", []).append(outcome)
        outcomes = data["outcome_history"]
        wins = sum(1 for o in outcomes if o == "win")
        data["win_rate"] = wins / len(outcomes) if outcomes else 0.0
        data["last_seen"] = datetime.now().isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- L3: Stock Profiles ---

    def get_profile(self, symbol: str) -> StockProfile | None:
        """Get the trading profile for a symbol."""
        path = self.profiles_dir / f"{symbol}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return StockProfile(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def update_profile(self, profile: StockProfile) -> Path:
        """Create or update a stock profile."""
        profile.last_updated = datetime.now().isoformat()
        if profile.total_trades > 0:
            profile.win_rate = profile.wins / profile.total_trades

        path = self.profiles_dir / f"{profile.symbol}.json"
        data = {
            "symbol": profile.symbol,
            "name": profile.name,
            "industry": profile.industry,
            "total_trades": profile.total_trades,
            "wins": profile.wins,
            "losses": profile.losses,
            "win_rate": profile.win_rate,
            "avg_holding_days": profile.avg_holding_days,
            "total_pnl_pct": profile.total_pnl_pct,
            "characteristics": profile.characteristics,
            "lessons": profile.lessons,
            "last_traded": profile.last_traded,
            "last_updated": profile.last_updated,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def record_trade_to_profile(
        self,
        symbol: str,
        outcome: str,
        pnl_pct: float,
        holding_days: int,
        lesson: str = "",
    ) -> None:
        """Record a completed trade into the stock's profile."""
        profile = self.get_profile(symbol) or StockProfile(symbol=symbol)
        profile.total_trades += 1
        if outcome == "win":
            profile.wins += 1
        elif outcome == "loss":
            profile.losses += 1

        n = profile.total_trades
        profile.avg_holding_days = (
            (profile.avg_holding_days * (n - 1) + holding_days) / n
        )
        profile.total_pnl_pct += pnl_pct
        profile.last_traded = date.today().isoformat()

        if lesson:
            profile.lessons.append(lesson)
            if len(profile.lessons) > 20:
                profile.lessons = profile.lessons[-20:]

        self.update_profile(profile)

    def get_all_profiles(self) -> list[StockProfile]:
        """Get all stock profiles sorted by trade count."""
        profiles: list[StockProfile] = []
        for path in self.profiles_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profiles.append(StockProfile(**data))
            except (json.JSONDecodeError, TypeError):
                continue
        profiles.sort(key=lambda p: p.total_trades, reverse=True)
        return profiles

    def build_context_for_symbol(self, symbol: str) -> str:
        """Build memory context string for a specific symbol (for LLM injection)."""
        profile = self.get_profile(symbol)
        if not profile:
            return f"（{symbol} 无历史交易记录）"

        lines = [
            f"## {symbol} 交易档案",
            f"- 历史交易: {profile.total_trades}次, 胜率{profile.win_rate:.0%}",
            f"- 平均持仓: {profile.avg_holding_days:.0f}天",
            f"- 累计收益: {profile.total_pnl_pct:+.1f}%",
        ]
        if profile.characteristics:
            lines.append(f"- 特征: {', '.join(profile.characteristics[-5:])}")
        if profile.lessons:
            lines.append("- 教训:")
            for lesson in profile.lessons[-3:]:
                lines.append(f"  • {lesson}")
        return "\n".join(lines)
