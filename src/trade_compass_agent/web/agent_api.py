from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from trade_compass_agent.command_catalog import command_catalog
from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.ops.audit import JsonAuditLog
from trade_compass_agent.ops.session_cleanup import SCHEDULER_SESSION_PREFIX
from trade_compass_agent.runtime.exceptions import AgentTurnError, AgentUnavailableError
from trade_compass_agent.runtime.loop import AgentLoop
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.session import SessionStore
from trade_compass_agent.runtime.types import TurnEvent, TurnResponse
from trade_compass_agent.runtime.mcp.loader import load_mcp_config
from trade_compass_agent.runtime.skills import discover_external_skills, load_agent_skills_config
from trade_compass_agent.runtime.run_trace import TurnTraceWriter, write_run_card
from trade_compass_agent.runtime.stream_buffer import SessionStreamBuffer
from trade_compass_agent.runtime.turn_control import get_turn_registry

router = APIRouter(prefix="/agent")

_stream_subscribers: dict[str, list[queue.Queue[TurnEvent | None]]] = {}
_stream_buffers: dict[str, SessionStreamBuffer] = {}
_stream_lock = threading.Lock()


def _buffer_for_session(session_id: str) -> SessionStreamBuffer:
    with _stream_lock:
        buffer = _stream_buffers.get(session_id)
        if buffer is None:
            buffer = SessionStreamBuffer()
            _stream_buffers[session_id] = buffer
        return buffer


def _format_sse_event(evt: TurnEvent) -> str:
    payload = json.dumps(evt.data, ensure_ascii=False)
    lines = []
    if evt.id:
        lines.append(f"id: {evt.id}")
    lines.append(f"event: {evt.event}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


def _resolve_last_event_id(request: Request) -> str | None:
    header = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    if header:
        return header.strip() or None
    query = request.query_params.get("Last-Event-ID")
    if query:
        return query.strip() or None
    return None


class AttachmentPayload(BaseModel):
    type: str
    content: str | None = None
    url: str | None = None
    mime: str | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"text", "url", "image", "pdf"}:
            raise ValueError("attachment type must be text, url, image, or pdf")
        return normalized


class TurnRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    attachments: list[AttachmentPayload] | None = None


class TurnSectionPayload(BaseModel):
    title: str
    content: str
    specialist: str | None = None
    symbols: list[str] = Field(default_factory=list)
    kind: str | None = None
    forecast_data: dict | None = None


class TurnResponsePayload(BaseModel):
    session_id: str
    turn_id: str
    summary: str
    sections: list[TurnSectionPayload] = Field(default_factory=list)
    interrupted: bool = False


class ControlRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    action: Literal["interrupt"]
    turn_id: str | None = None


class ControlResponsePayload(BaseModel):
    ok: bool
    session_id: str
    turn_id: str | None = None


class SkillPayload(BaseModel):
    name: str
    description: str = ""
    path: str | None = None
    source: str | None = None
    enabled: bool = True


class SkillsResponse(BaseModel):
    skills: list[SkillPayload]


class CommandPayload(BaseModel):
    path: list[str]
    command: str
    summary: str
    category: str
    aliases: list[list[str]] = Field(default_factory=list)
    mutates_state: bool = False
    supports_json: bool = False


class CommandsResponse(BaseModel):
    commands: list[CommandPayload]


class McpServerPayload(BaseModel):
    name: str
    status: str
    transport: str | None = None
    command: str | None = None
    tools: list[str] | None = None
    error: str | None = None


class McpResponse(BaseModel):
    servers: list[McpServerPayload]


class SessionSectionPayload(BaseModel):
    title: str
    content: str
    specialist: str | None = None
    symbols: list[str] = Field(default_factory=list)
    kind: str | None = None
    forecast_data: dict | None = None


class ToolCallPayload(BaseModel):
    name: str
    arguments: str = ""


class SessionMessagePayload(BaseModel):
    role: str
    content: str
    timestamp: str | None = None
    sections: list[SessionSectionPayload] | None = None
    tool_calls: list[ToolCallPayload] | None = None


class SessionDetailPayload(BaseModel):
    session_id: str
    title: str | None = None
    updated_at: str
    messages: list[SessionMessagePayload]
    has_active_turn: bool = False


class SessionMessagePageInfoPayload(BaseModel):
    start_index: int
    total_messages: int
    next_before: int | None = None


class SessionMessagesPagePayload(SessionDetailPayload):
    page: SessionMessagePageInfoPayload


class SessionCreatedPayload(BaseModel):
    session_id: str
    updated_at: str


class SessionListItemPayload(BaseModel):
    session_id: str
    title: str | None = None
    created_at: str
    updated_at: str
    message_count: int
    preview: str | None = None


class SessionUpdateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)


class SessionsListPayload(BaseModel):
    sessions: list[SessionListItemPayload]


_cached_stack: MarketStack | None = None
_cached_config: AppConfig | None = None
_stack_lock = __import__("threading").Lock()


def _get_cached_stack() -> tuple[AppConfig, MarketStack]:
    global _cached_stack, _cached_config
    config = load_app_config()
    with _stack_lock:
        if (
            _cached_stack is None
            or _cached_config is None
            or _cached_config.data_dir != config.data_dir
        ):
            _cached_config = config
            _cached_stack = MarketStack.from_config(config)
        return _cached_config, _cached_stack


def _session_store() -> SessionStore:
    config = load_app_config()
    return SessionStore(config.data_dir / "agent_sessions")


def _loop_for_session(session_id: str, on_event) -> AgentLoop:
    config, stack = _get_cached_stack()
    return AgentLoop(
        config=config,
        stack=stack,
        session_store=SessionStore(config.data_dir / "agent_sessions"),
        on_event=on_event,
    )


@router.post("/turn", response_model=TurnResponsePayload)
def agent_turn(body: TurnRequest) -> TurnResponsePayload:
    events: list[TurnEvent] = []
    store = _session_store()
    session = store.get_or_create(body.session_id)
    session_id = session.session_id
    registry = get_turn_registry()
    if registry.has_active_turn(session_id):
        raise HTTPException(
            status_code=409,
            detail="A turn is already in progress for this session",
        )
    turn_id = str(uuid.uuid4())
    is_cancelled = registry.register(turn_id, session_id)
    _buffer_for_session(session_id).clear()
    config = load_app_config()
    run_dir = config.data_dir / "runs" / turn_id
    trace_writer = TurnTraceWriter(run_dir)
    started_at = datetime.now(timezone.utc).isoformat()

    def capture(evt: TurnEvent) -> None:
        events.append(evt)
        trace_writer.record(evt)
        _publish_stream(session_id, evt)

    try:
        agent_loop = _loop_for_session(session_id, capture)
        attachments = [a.model_dump(exclude_none=True) for a in (body.attachments or [])]
        result = agent_loop.run_turn(
            body.message,
            session_id=session_id,
            attachments=attachments or None,
            turn_id=turn_id,
            is_cancelled=is_cancelled,
        )
    except AgentUnavailableError as exc:
        _publish_stream(
            session_id,
            TurnEvent(event="error", data={"message": str(exc), "ok": False}),
        )
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except AgentTurnError as exc:
        _publish_stream(
            session_id,
            TurnEvent(event="error", data={"message": str(exc), "ok": False}),
        )
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        _publish_stream(
            session_id,
            TurnEvent(
                event="error",
                data={"message": f"Agent turn failed: {exc}", "ok": False},
            ),
        )
        raise HTTPException(status_code=502, detail=f"Agent turn failed: {exc}") from exc
    finally:
        registry.unregister(turn_id)

    write_run_card(
        config.data_dir / "runs",
        turn_id=turn_id,
        session_id=session_id,
        started_at=started_at,
        result=result,
        trace_writer=trace_writer,
    )

    _record_audit_turn(config, turn_id, session_id, body.message, result, events)

    return TurnResponsePayload(
        session_id=result.session_id,
        turn_id=turn_id,
        summary=result.summary,
        interrupted=result.interrupted,
        sections=[
            TurnSectionPayload(
                title=s.title,
                content=s.content,
                specialist=s.specialist,
                symbols=list(s.symbols),
                kind=s.kind,
                forecast_data=s.forecast_data,
            )
            for s in result.sections
        ],
    )


@router.post("/control", response_model=ControlResponsePayload)
def agent_control(body: ControlRequest) -> ControlResponsePayload:
    if body.action != "interrupt":
        raise HTTPException(status_code=400, detail="unsupported action")
    registry = get_turn_registry()
    if not registry.interrupt(body.session_id, body.turn_id):
        raise HTTPException(status_code=404, detail="no active turn")
    return ControlResponsePayload(
        ok=True,
        session_id=body.session_id,
        turn_id=body.turn_id,
    )


def _publish_stream(session_id: str, evt: TurnEvent) -> None:
    normalized = _buffer_for_session(session_id).append(evt)
    with _stream_lock:
        subscribers = _stream_subscribers.get(session_id, [])
    for q in subscribers:
        q.put(normalized)


def _record_audit_turn(
    config,
    turn_id: str,
    session_id: str,
    user_message: str,
    result: TurnResponse,
    events: list[TurnEvent],
) -> None:
    """Record an agent_turn event in the audit log for traceability."""
    symbols: list[str] = []
    specialists: list[str] = []
    for s in result.sections:
        symbols.extend(s.symbols)
        if s.specialist:
            specialists.append(s.specialist)
    for evt in events:
        if evt.event == "specialist_done":
            name = evt.data.get("specialist")
            if name and name not in specialists:
                specialists.append(name)

    audit = JsonAuditLog(config.data_dir / "audit.jsonl")
    audit.record(
        event_type="agent_turn",
        summary=result.summary[:200],
        payload={
            "turn_id": turn_id,
            "session_id": session_id,
            "user_message": user_message[:300],
            "symbols": sorted(set(symbols)),
            "specialists": specialists,
            "section_count": len(result.sections),
            "interrupted": result.interrupted,
        },
    )


@router.get("/stream")
async def agent_stream(
    request: Request,
    session_id: str = Query(..., min_length=1),
) -> StreamingResponse:
    event_queue: queue.Queue[TurnEvent | None] = queue.Queue()
    with _stream_lock:
        _stream_subscribers.setdefault(session_id, []).append(event_queue)
    last_event_id = _resolve_last_event_id(request)
    replay = _buffer_for_session(session_id).replay_after(last_event_id)

    async def event_generator():
        try:
            for evt in replay:
                yield _format_sse_event(evt)
                if evt.event in {"done", "error", "interrupted"}:
                    return
            while True:
                try:
                    evt = await asyncio.to_thread(event_queue.get, True, 30.0)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if evt is None:
                    break
                yield _format_sse_event(evt)
                if evt.event in {"done", "error", "interrupted"}:
                    break
        finally:
            with _stream_lock:
                subs = _stream_subscribers.get(session_id, [])
                if event_queue in subs:
                    subs.remove(event_queue)
                if not subs:
                    _stream_subscribers.pop(session_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions", response_model=SessionCreatedPayload)
def create_agent_session() -> SessionCreatedPayload:
    store = _session_store()
    session = store.create()
    return SessionCreatedPayload(
        session_id=session.session_id,
        updated_at=session.updated_at.isoformat(),
    )


@router.get("/sessions", response_model=SessionsListPayload)
def list_agent_sessions(limit: int = Query(20, ge=1, le=100)) -> SessionsListPayload:
    store = _session_store()
    return SessionsListPayload(
        sessions=[
            SessionListItemPayload(
                session_id=item.session_id,
                title=item.title,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
                message_count=item.message_count,
                preview=item.preview,
            )
            for item in store.list_recent(limit, exclude_prefix=SCHEDULER_SESSION_PREFIX)
        ]
    )


def _session_message_payload(message) -> SessionMessagePayload:
    sections = None
    if message.sections:
        sections = [
            SessionSectionPayload(
                title=str(section.get("title") or ""),
                content=str(section.get("content") or ""),
                specialist=section.get("specialist"),
                symbols=list(section.get("symbols") or []),
                kind=section.get("kind"),
                forecast_data=section.get("forecast_data"),
            )
            for section in message.sections
            if isinstance(section, dict)
        ]
    tool_calls = None
    if message.tool_calls:
        tool_calls = [
            ToolCallPayload(
                name=tc.get("function", {}).get("name", "") if isinstance(tc, dict) else "",
                arguments=tc.get("function", {}).get("arguments", "")
                if isinstance(tc, dict)
                else "",
            )
            for tc in message.tool_calls
            if isinstance(tc, dict)
        ]
    return SessionMessagePayload(
        role=message.role,
        content=message.content,
        timestamp=message.timestamp.isoformat() if message.timestamp else None,
        sections=sections or None,
        tool_calls=tool_calls or None,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailPayload)
def get_agent_session(session_id: str) -> SessionDetailPayload:
    store = _session_store()
    session = store.load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    registry = get_turn_registry()
    return SessionDetailPayload(
        session_id=session.session_id,
        title=session.title,
        updated_at=session.updated_at.isoformat(),
        has_active_turn=registry.has_active_turn(session_id),
        messages=[
            _session_message_payload(message)
            for message in session.messages
            if message.role in {"user", "assistant"}
        ],
    )


@router.get("/sessions/{session_id}/messages", response_model=SessionMessagesPagePayload)
def get_agent_session_messages(
    session_id: str,
    limit: int = Query(50, ge=1, le=100),
    before: int | None = Query(None, ge=0),
) -> SessionMessagesPagePayload:
    store = _session_store()
    page = store.load_display_page(session_id, limit=limit, before=before)
    if page is None:
        raise HTTPException(status_code=404, detail="session not found")
    registry = get_turn_registry()
    return SessionMessagesPagePayload(
        session_id=page.session_id,
        title=page.title,
        updated_at=page.updated_at.isoformat(),
        has_active_turn=registry.has_active_turn(session_id),
        messages=[_session_message_payload(message) for message in page.messages],
        page=SessionMessagePageInfoPayload(
            start_index=page.start_index,
            total_messages=page.total_messages,
            next_before=page.next_before,
        ),
    )


@router.patch("/sessions/{session_id}", response_model=SessionDetailPayload)
def update_agent_session(session_id: str, body: SessionUpdateRequest) -> SessionDetailPayload:
    store = _session_store()
    session = store.load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    store.set_title(session, body.title)
    session.title = body.title.strip()
    return SessionDetailPayload(
        session_id=session.session_id,
        title=session.title,
        updated_at=session.updated_at.isoformat(),
        messages=[
            _session_message_payload(message)
            for message in session.messages
            if message.role in {"user", "assistant"}
        ],
    )


@router.delete("/sessions/{session_id}", status_code=204)
def delete_agent_session(session_id: str) -> None:
    store = _session_store()
    if not store.delete(session_id):
        raise HTTPException(status_code=404, detail="session not found")


@router.get("/skills", response_model=SkillsResponse)
def agent_skills() -> SkillsResponse:
    """Return only external (project-level) skills for settings display.
    Agent self-evolved skills are managed internally and not shown here."""
    skills_cfg = load_agent_skills_config()
    skills = discover_external_skills(skills_config=skills_cfg)
    return SkillsResponse(
        skills=[
            SkillPayload(
                name=s.name,
                description=skills_cfg.default_summaries.get(s.name, s.description),
                path=str(s.path),
                source=s.source,
                enabled=True,
            )
            for s in skills
        ]
    )


@router.get("/commands", response_model=CommandsResponse)
def agent_commands() -> CommandsResponse:
    """Return the shared shell-command catalog for help and integrations."""
    return CommandsResponse(
        commands=[CommandPayload.model_validate(item) for item in command_catalog()]
    )


@router.get("/mcp", response_model=McpResponse)
def agent_mcp() -> McpResponse:
    servers = load_mcp_config()
    return McpResponse(
        servers=[
            McpServerPayload(
                name=s.name,
                status=s.status,
                transport=s.transport,
                command=s.command,
                tools=s.tools,
                error=s.error,
            )
            for s in servers
        ]
    )


@router.get("/runs")
def list_agent_runs(
    limit: int = Query(20, ge=1, le=100),
    session_id: str | None = Query(None),
):
    """List recent turn run cards for observability."""
    import json as _json

    config = load_app_config()
    runs_dir = config.data_dir / "runs"
    if not runs_dir.is_dir():
        return {"runs": []}
    cards: list[dict] = []
    for card_path in sorted(runs_dir.glob("*/run_card.json"), reverse=True):
        if len(cards) >= limit:
            break
        try:
            raw = _json.loads(card_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if session_id and raw.get("session_id") != session_id:
            continue
        cards.append(
            {
                "turn_id": raw.get("turn_id"),
                "session_id": raw.get("session_id"),
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
                "interrupted": raw.get("interrupted", False),
                "summary": (raw.get("summary") or "")[:200],
                "section_count": raw.get("section_count", 0),
                "event_count": raw.get("event_count", 0),
            }
        )
    return {"runs": cards}
