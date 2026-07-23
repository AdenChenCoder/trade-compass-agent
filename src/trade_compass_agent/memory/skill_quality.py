"""Static quality gate for procedural skills.

This module intentionally returns statuses and reasons, not a weighted score.
Origin is kept out of quality decisions; ownership only affects curator policy.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

KNOWN_TOOL_NAMES = frozenset(
    {
        "analyze_portfolio",
        "batch_get_bars",
        "chart_pattern",
        "compute_bollinger",
        "compute_ma",
        "compute_macd",
        "compute_rsi",
        "compute_volume_ratio",
        "eastmoney_news",
        "emit_signal",
        "get_bars",
        "get_fund_flow",
        "get_market_constraints",
        "get_market_pulse",
        "get_risk_status",
        "load_skill",
        "map_intent_to_sell",
        "search_concept_boards",
        "search_industry_boards",
        "search_lhb",
        "search_market_flash",
        "search_memory",
        "session_search",
        "sina_realtime_quote",
        "skill_manage",
        "write_knowledge",
    }
)

QUALITY_STATES = frozenset({"draft", "active", "verified", "needs_patch", "deprecated"})
STATIC_STATUSES = frozenset({"pass", "warning", "fail"})

_ONE_OFF_PATTERNS = (
    r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}",
    r"(?:今日|昨天|昨日|明日|明天|本周|本月).{0,30}(?:股价|现价|涨跌|涨停|跌停|净流入|净流出)",
    r"(?:这次|本次|临时|一次性).{0,20}(?:结论|错误|行情|操作)",
)

_DANGEROUS_PATTERNS = (
    r"rm\s+-rf\s+/",
    r"\bcurl\b.+\|\s*(?:sh|bash)",
    r"\bwget\b.+\|\s*(?:sh|bash)",
    r"(?:BEGIN|END) (?:RSA|OPENSSH|PRIVATE) KEY",
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[A-Za-z0-9_\-]{16,}",
    r"ignore (?:previous|all) instructions",
    r"越权|外泄|泄露密钥|持久化后门",
)

_BOUNDARY_WORDS = ("只在", "适用", "不适用", "边界", "条件", "除非", "当")


@dataclass
class SkillQuality:
    quality: str = "draft"
    static_status: str = "pass"
    warnings: list[str] = field(default_factory=list)
    hard_errors: list[str] = field(default_factory=list)
    duplicate_candidates: list[str] = field(default_factory=list)
    stability: float = 0.0
    evidence_count: int = 0
    needs_patch_reason: str | None = None
    last_reviewed_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SkillQuality":
        raw = raw or {}
        return cls(
            quality=str(raw.get("quality") or "draft"),
            static_status=str(raw.get("static_status") or "pass"),
            warnings=list(raw.get("warnings") or []),
            hard_errors=list(raw.get("hard_errors") or []),
            duplicate_candidates=list(raw.get("duplicate_candidates") or []),
            stability=float(raw.get("stability") or 0.0),
            evidence_count=int(raw.get("evidence_count") or 0),
            needs_patch_reason=raw.get("needs_patch_reason"),
            last_reviewed_at=raw.get("last_reviewed_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_skill_frontmatter(content: str) -> tuple[dict[str, Any], str, str | None]:
    """Return frontmatter, body, and parse error."""
    if not content.startswith("---"):
        return {}, content, "frontmatter missing"
    end = content.find("---", 3)
    if end == -1:
        return {}, content, "frontmatter closing marker missing"
    front = content[3:end]
    body = content[end + 3 :].lstrip()
    try:
        meta = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:
        return {}, body, f"frontmatter YAML invalid: {exc.__class__.__name__}"
    if not isinstance(meta, dict):
        return {}, body, "frontmatter must be a mapping"
    return {str(k).strip(): v for k, v in meta.items()}, body, None


def normalize_skill_content(
    content: str,
    *,
    name: str,
    origin: str,
    quality: str = "draft",
    evidence_count: int = 0,
) -> str:
    """Ensure required frontmatter fields exist without changing the body."""
    meta, body, error = parse_skill_frontmatter(content)
    if error:
        return content
    meta.setdefault("name", name)
    meta.setdefault("description", "")
    meta.setdefault("category", "general")
    meta.setdefault("origin", origin)
    meta.setdefault("quality", quality)
    meta.setdefault("evidence_count", evidence_count)
    ordered = ["name", "description", "category", "origin", "quality", "evidence_count"]
    lines = ["---"]
    for key in ordered:
        lines.append(f"{key}: {_yaml_scalar(meta.get(key))}")
    for key in sorted(k for k in meta if k not in ordered):
        lines.append(f"{key}: {_yaml_scalar(meta[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def update_skill_frontmatter(content: str, updates: dict[str, Any]) -> str:
    """Update frontmatter keys while preserving body and unknown metadata."""
    meta, body, error = parse_skill_frontmatter(content)
    if error:
        return content
    meta.update(updates)
    ordered = ["name", "description", "category", "origin", "quality", "evidence_count"]
    lines = ["---"]
    for key in ordered:
        if key in meta:
            lines.append(f"{key}: {_yaml_scalar(meta.get(key))}")
    for key in sorted(k for k in meta if k not in ordered):
        lines.append(f"{key}: {_yaml_scalar(meta[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def evaluate_skill_content(
    *,
    name: str,
    content: str,
    existing: dict[str, str] | None = None,
    usage: Any | None = None,
) -> SkillQuality:
    meta, body, parse_error = parse_skill_frontmatter(content)
    hard_errors: list[str] = []
    warnings: list[str] = []
    duplicate_candidates: list[str] = []

    if parse_error:
        hard_errors.append(parse_error)

    fm_name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    evidence_count = _as_int(meta.get("evidence_count"), 0)

    if not fm_name:
        hard_errors.append("frontmatter name missing")
    elif fm_name != name:
        hard_errors.append(f"frontmatter name '{fm_name}' does not match skill directory '{name}'")
    if not description:
        hard_errors.append("frontmatter description missing")
    elif len(description) > 220:
        warnings.append("description too long for retrieval")

    known_skills = set((existing or {}).keys()) | {name}
    for target in re.findall(r"\bload_skill\s*\(\s*['\"]?([a-z0-9._-]+)", content):
        if target not in known_skills:
            hard_errors.append(f"load_skill target not found: {target}")

    for tool in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", content):
        if tool.startswith("_") or tool in {"if", "for", "while"}:
            continue
        if tool in {"load_skill"}:
            continue
        if tool.startswith(("compute_", "get_", "search_", "analyze_", "emit_", "map_")) and tool not in KNOWN_TOOL_NAMES:
            hard_errors.append(f"unknown tool reference: {tool}")

    for pattern in _ONE_OFF_PATTERNS:
        if re.search(pattern, content):
            hard_errors.append("one-off market data or transient conclusion written as reusable skill")
            break

    for pattern in _DANGEROUS_PATTERNS:
        if re.search(pattern, content, flags=re.IGNORECASE | re.DOTALL):
            hard_errors.append("dangerous command, secret, or prompt-injection content")
            break

    normalized_body = _fingerprint(body)
    for other_name, other_content in (existing or {}).items():
        if other_name == name:
            continue
        _, other_body, _ = parse_skill_frontmatter(other_content)
        if normalized_body and normalized_body == _fingerprint(other_body):
            hard_errors.append(f"hard duplicate of skill: {other_name}")
            duplicate_candidates.append(other_name)
        elif _overlap_ratio(body, other_body) >= 0.58:
            warnings.append(f"partial overlap with skill: {other_name}")
            duplicate_candidates.append(other_name)

    if body.count("\n## ") <= 1 and len(body) < 900:
        warnings.append("narrow skill; consider merging into an umbrella skill")
    if _has_thresholds(content) and not any(word in content for word in _BOUNDARY_WORDS):
        warnings.append("thresholds or parameters lack applicability boundary")
    if "案例" in content and "历史" not in content:
        warnings.append("examples should be labelled as historical examples")

    use_count = int(getattr(usage, "use_count", 0) or 0)
    patch_count = int(getattr(usage, "patch_count", 0) or 0)
    stability = _stability(evidence_count, use_count, patch_count, usage)
    static_status = "fail" if hard_errors else ("warning" if warnings else "pass")
    quality = _derive_quality(static_status, warnings, stability, use_count)
    needs_patch_reason = hard_errors[0] if hard_errors else (warnings[0] if static_status == "warning" else None)

    return SkillQuality(
        quality=quality,
        static_status=static_status,
        warnings=warnings,
        hard_errors=hard_errors,
        duplicate_candidates=sorted(set(duplicate_candidates)),
        stability=stability,
        evidence_count=evidence_count,
        needs_patch_reason=needs_patch_reason,
        last_reviewed_at=datetime.now(timezone.utc).isoformat(),
    )


def read_quality_file(skill_dir: Path) -> SkillQuality:
    path = skill_dir / ".quality.json"
    if not path.is_file():
        return SkillQuality()
    try:
        return SkillQuality.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return SkillQuality()


def write_quality_file(skill_dir: Path, quality: SkillQuality) -> None:
    (skill_dir / ".quality.json").write_text(
        json.dumps(quality.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = "" if value is None else str(value)
    if not text or any(ch in text for ch in ":#{}[],&*?|-<>=!%@`") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fingerprint(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _overlap_ratio(a: str, b: str) -> float:
    aw = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", a.lower()))
    bw = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", b.lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / min(len(aw), len(bw))


def _has_thresholds(text: str) -> bool:
    return bool(re.search(r"(?:>=|<=|≥|≤|>|<|\d+(?:\.\d+)?%)", text))


def _stability(evidence_count: int, use_count: int, patch_count: int, usage: Any | None) -> float:
    anchor_raw = (
        getattr(usage, "last_used_at", None)
        or getattr(usage, "last_patched_at", None)
        or getattr(usage, "created_at", None)
    )
    recency_decay = 1.0
    if anchor_raw:
        try:
            anchor = datetime.fromisoformat(str(anchor_raw).replace("Z", "+00:00"))
            days = max(0, (datetime.now(timezone.utc) - anchor).days)
            recency_decay = 0.5 ** (days / 30)
        except ValueError:
            recency_decay = 1.0
    return round(recency_decay * math.log1p(evidence_count + use_count + patch_count), 3)


def _derive_quality(static_status: str, warnings: list[str], stability: float, use_count: int) -> str:
    if static_status == "fail":
        return "needs_patch"
    if static_status == "warning":
        return "needs_patch"
    if use_count >= 5 and stability >= 1.5 and not warnings:
        return "verified"
    return "active"
