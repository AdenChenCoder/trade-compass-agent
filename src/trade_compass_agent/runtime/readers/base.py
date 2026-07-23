from __future__ import annotations

import re
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


ReaderType = Literal[
    "announcement_reader",
    "news_reader",
    "research_report_reader",
    "kol_signal_reader",
    "webpage_reader",
]

INJECTION_PATTERNS = (
    r"ignore (all )?(previous|prior) instructions",
    r"忽略(以上|之前|所有).{0,12}(规则|指令|要求)",
    r"立刻(买入|卖出|下单)",
    r"system prompt",
    r"developer message",
)


@dataclass(frozen=True)
class ReaderInput:
    reader_type: ReaderType
    content: str
    source: str
    source_url: str = ""
    source_title: str = ""
    published_at: str = ""
    symbols: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceReference:
    source: str
    source_url: str = ""
    source_title: str = ""
    published_at: str = ""
    retrieved_at: str = ""


@dataclass(frozen=True)
class ReaderClaim:
    claim: str
    source: SourceReference
    symbols: tuple[str, ...] = ()
    confidence: Literal["low", "medium", "high"] = "medium"
    raw_excerpt_ref: str = ""


@dataclass(frozen=True)
class ReaderEvent:
    event_type: str
    summary: str
    event_date: str = ""
    source: SourceReference = field(default_factory=lambda: SourceReference(source=""))
    symbols: tuple[str, ...] = ()
    confidence: Literal["low", "medium", "high"] = "medium"


@dataclass(frozen=True)
class ReaderResult:
    reader_type: ReaderType
    schema_version: int
    as_of: str
    source: SourceReference
    source_refs: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    claims: tuple[ReaderClaim, ...] = ()
    events: tuple[ReaderEvent, ...] = ()
    risks: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    confidence: Literal["low", "medium", "high"] = "medium"
    warnings: tuple[str, ...] = ()
    validation_status: Literal["validated", "degraded"] = "validated"
    trace_events: tuple[dict[str, Any], ...] = ()

    def model_dump(self) -> dict[str, Any]:
        return json.loads(json.dumps(asdict(self), ensure_ascii=False))


def read_untrusted_text(payload: ReaderInput) -> ReaderResult:
    """Extract safe, source-aware facts from untrusted text.

    This deterministic baseline is intentionally conservative. LLM-backed
    readers can replace the extraction internals later, but must preserve this
    result contract and schema boundary.
    """
    now = datetime.now(UTC).isoformat()
    source = SourceReference(
        source=payload.source or "unknown-source",
        source_url=payload.source_url,
        source_title=payload.source_title,
        published_at=payload.published_at,
        retrieved_at=now,
    )
    warnings = list(_detect_injection(payload.content))
    symbols = tuple(dict.fromkeys(payload.symbols or tuple(_extract_symbols(payload.content))))
    sentences = _sentences(payload.content)
    claims = tuple(
        ReaderClaim(
            claim=sentence[:500],
            source=source,
            symbols=symbols,
            confidence="medium" if payload.source else "low",
            raw_excerpt_ref=f"sentence:{idx}",
        )
        for idx, sentence in enumerate(sentences[:20])
        if sentence
    )
    events = tuple(_extract_events(sentences[:30], source, symbols))
    risks = tuple(sentence[:300] for sentence in sentences if _looks_like_risk(sentence))[:20]
    unsupported_claims = tuple(
        sentence[:300] for sentence in sentences if _looks_unsupported(sentence)
    )[:20]
    confidence: Literal["low", "medium", "high"] = "medium"
    if not payload.source:
        confidence = "low"
        warnings.append("missing source")
    if warnings:
        confidence = "low" if any("prompt injection" in w for w in warnings) else confidence
    validation_status: Literal["validated", "degraded"] = "validated"
    if warnings or unsupported_claims:
        validation_status = "degraded"
    source_ref = payload.source or payload.source_url or "unknown-source"
    trace_events = (
        {
            "event": "reader.completed",
            "reader_type": payload.reader_type,
            "source": source_ref,
            "claim_count": len(claims),
            "event_count": len(events),
            "warning_count": len(warnings),
            "validation_status": validation_status,
        },
    )
    return ReaderResult(
        reader_type=payload.reader_type,
        schema_version=1,
        as_of=now,
        source=source,
        source_refs=(source_ref,),
        symbols=symbols,
        entities=tuple(_extract_entities(payload.content)[:50]),
        claims=claims,
        events=events,
        risks=risks,
        unsupported_claims=unsupported_claims,
        confidence=confidence,
        warnings=tuple(warnings),
        validation_status=validation_status,
        trace_events=trace_events,
    )


def _detect_injection(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            found.append("possible prompt injection content treated as data")
            break
    return tuple(found)


def _extract_symbols(text: str) -> list[str]:
    return re.findall(r"(?<!\d)(?:[036]\d{5}|[159]\d{5})(?!\d)", text)


def _extract_entities(text: str) -> list[str]:
    matches = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}(?:股份|集团|科技|电子|银行|证券|能源)?", text)
    return list(dict.fromkeys(matches))


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", normalized)
    return [part.strip() for part in parts if 8 <= len(part.strip()) <= 600]


def _extract_events(
    sentences: list[str],
    source: SourceReference,
    symbols: tuple[str, ...],
) -> list[ReaderEvent]:
    keywords = {
        "earnings": ("财报", "业绩", "年报", "季报", "预告", "快报"),
        "unlock": ("解禁", "限售"),
        "buyback": ("回购",),
        "dividend": ("分红", "派息", "送转"),
        "ma": ("并购", "重组", "收购"),
        "policy": ("政策", "监管", "批复"),
        "meeting": ("股东大会", "董事会", "交流会", "调研"),
        "order": ("订单", "合同", "中标"),
    }
    events: list[ReaderEvent] = []
    for sentence in sentences:
        for event_type, words in keywords.items():
            if any(word in sentence for word in words):
                events.append(
                    ReaderEvent(
                        event_type=event_type,
                        summary=sentence[:300],
                        event_date=_extract_date(sentence),
                        source=source,
                        symbols=symbols,
                    )
                )
                break
    return events[:30]


def _extract_date(text: str) -> str:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _looks_like_risk(sentence: str) -> bool:
    return any(word in sentence for word in ("风险", "不确定", "下滑", "亏损", "减持", "处罚", "诉讼"))


def _looks_unsupported(sentence: str) -> bool:
    return any(word in sentence for word in ("传闻", "据说", "未经证实", "网传", "小作文"))
