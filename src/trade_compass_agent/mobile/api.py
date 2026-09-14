from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.staticfiles import StaticFiles

from trade_compass_agent.config import AppConfig, load_app_config
from trade_compass_agent.mobile.identity import ComputerIdentity
from trade_compass_agent.mobile.pairing import DeviceStore, PairingError
from trade_compass_agent.mobile.turns import MobileTurns
from trade_compass_agent.mobile.push import PushStore
from trade_compass_agent.runtime.session import SessionStore
from trade_compass_agent.web.agent_api import create_session_payload, list_sessions_payload, session_messages_payload
from trade_compass_agent.web.api import notifications_payload
from trade_compass_agent.web.security import RequestSizeLimitMiddleware, is_loopback_host

Secret = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
COOKIE = "__Host-compass-device"


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invitation: Secret
    device_secret: Secret
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification_code: str = Field(pattern=r"^[0-9]{6}$")


class BrowserClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    invitation: Secret
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")


class PushSubscription(BaseModel):
    endpoint: str = Field(max_length=4096)
    keys: dict[str, str]


class PushReceipt(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


class TaskPushPreference(BaseModel):
    enabled: bool


class MobileTurnRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)


def validate_session_path(sessions: SessionStore, session_id: str):
    if (len(session_id) > 200 or session_id in {".", ".."}
            or any(c in session_id for c in ("/", "\\", "\x00"))):
        raise HTTPException(404, "session not found")
    path = sessions.path / f"{session_id}.jsonl"
    if path.resolve().parent != sessions.path.resolve():
        raise HTTPException(404, "session not found")


def _bearer(request: Request) -> str:
    scheme, _, secret = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
        raise HTTPException(401, "Device credential required", headers={"WWW-Authenticate": "Bearer"})
    return secret


def create_mobile_app(config: AppConfig, devices: DeviceStore, identity: ComputerIdentity) -> FastAPI:
    """Explicit allowlist; never mount the local administrative/business router here."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestSizeLimitMiddleware, limit=64 * 1024)
    sessions = SessionStore(config.data_dir / "agent_sessions")
    turns = MobileTurns(config.data_dir / "mobile")
    origin = config.mobile.public_origin
    push = PushStore(config.data_dir / "mobile", origin or "https://github.com/AdenChenCoder/trade-compass-agent")
    app.state.web_push = push

    @app.middleware("http")
    async def transport_boundary(request: Request, call_next):
        # Proxy headers are disabled on the listener; this must be actual TLS.
        if request.url.scheme != "https":
            return JSONResponse({"detail": "HTTPS required"}, status_code=403)
        request_origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (request_origin and request_origin != origin):
            return JSONResponse({"detail": "Untrusted browser origin"}, status_code=403)
        path = request.url.path
        browser = (COOKIE in request.cookies or path.startswith(("/phone", "/mobile/v1/browser"))
                   or path == "/mobile" or (path.startswith("/mobile/") and not path.startswith("/mobile/v1/")))
        if browser and (not origin or str(request.base_url).rstrip("/") != origin):
            return JSONResponse({"detail": "请使用电脑配置的 PWA 地址"}, status_code=403)
        if browser and request.method not in ("GET", "HEAD") and (
                request_origin != origin or request.headers.get("x-compass-pwa") != "1"):
            return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        # Never reflect a malformed body containing pairing/device secrets.
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(PairingError)
    async def invalid_pairing(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(Exception)
    async def internal_error(_request, _exc):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    def identify(request: Request) -> dict:
        secret = request.cookies.get(COOKIE) if origin and COOKIE in request.cookies else _bearer(request)
        device = devices.lookup(secret)
        if device is None or device["status"] in {"revoked", "expired"}:
            raise HTTPException(401, "Device credential is unavailable")
        return device

    def authorize(device: dict = Depends(identify)) -> dict:
        if device["status"] != "approved":
            raise HTTPException(403, "请先输入电脑上显示的配对码")
        return device

    def phone_device(device: dict) -> dict:
        # Codes must never be returned to the party asked to enter them.
        return {key: value for key, value in device.items() if key != "verification_code"}

    @app.post("/mobile/v1/pairing/claim", status_code=202)
    def claim(body: ClaimRequest):
        return phone_device(devices.claim(body.invitation, body.name, body.device_secret))

    @app.get("/mobile/v1/pairing/status")
    def pairing_status(device: dict = Depends(identify)):
        return phone_device(device)

    @app.post("/mobile/v1/pairing/verify")
    def verify_pairing(body: ApprovalRequest, device: dict = Depends(identify)):
        devices.verify(device["device_id"], body.verification_code)
        return {"status": "approved"}

    @app.post("/mobile/v1/pairing/forget")
    def forget_device(device: dict = Depends(identify)):
        devices.revoke(device["device_id"])
        if push:
            push.unsubscribe(device["device_id"])
        return {"ok": True}

    protected = APIRouter(prefix="/mobile/v1", dependencies=[Depends(authorize)])

    @protected.get("/info")
    def info():
        return {"protocol_version": 1, "computer_id": identity.computer_id,
                "capabilities": ["sessions.read", "sessions.create", "sessions.send", "notifications.read"],
                "system_push": "test_available" if push else "not_configured"}

    @protected.get("/sessions")
    def list_sessions(limit: int = Query(20, ge=1, le=100),
                      cursor: str | None = Query(None, max_length=1024)):
        return list_sessions_payload(sessions, limit, cursor)

    @protected.post("/sessions")
    def create_session():
        if load_app_config().data_dir.resolve() != config.data_dir.resolve():
            raise HTTPException(503, "电脑数据目录已变化，请重启服务后重新连接")
        return create_session_payload(sessions)

    @protected.get("/sessions/{session_id}/messages")
    def messages(session_id: str, limit: int = Query(50, ge=1, le=100),
                 before: int | None = Query(None, ge=0)):
        # SessionStore is a local trusted store; reject path syntax at this new boundary.
        # Keep existing Unicode/channel session names addressable.
        validate_session_path(sessions, session_id)
        return session_messages_payload(sessions, session_id, limit, before)

    @protected.post("/turns", status_code=202)
    def send_message(body: MobileTurnRequest, device: dict = Depends(authorize)):
        if load_app_config().data_dir.resolve() != config.data_dir.resolve():
            raise HTTPException(503, "电脑数据目录已变化，请重启服务后重新连接")
        validate_session_path(sessions, body.session_id)
        if sessions.load(body.session_id) is None:
            raise HTTPException(404, "session not found")
        if not body.message.strip():
            raise HTTPException(422, "消息不能为空")
        return turns.submit(device["device_id"], body.request_id, body.session_id, body.message)

    @protected.get("/turns/{request_id}")
    def get_request(request_id: str, device: dict = Depends(authorize)):
        record = turns.get(device["device_id"], request_id)
        if record is None:
            raise HTTPException(404, "request not found")
        return turns.public(record)

    @protected.get("/notifications")
    def notifications(limit: int = Query(30, ge=1, le=500)):
        return notifications_payload(config, limit)

    app.include_router(protected)
    if origin:
        @app.get("/mobile/v1/browser/connection")
        def browser_connection(request: Request):
            device = devices.lookup(request.cookies.get(COOKIE, ""))
            connected = bool(device and device["status"] not in ("revoked", "expired"))
            return {"connected": connected, "endpoint": origin, "computer_id": identity.computer_id}

        @app.post("/mobile/v1/browser/claim", status_code=202)
        def browser_claim(body: BrowserClaim, request: Request, response: Response):
            existing = devices.lookup(request.cookies.get(COOKIE, ""))
            if existing and existing["status"] in ("pending", "approved"):
                raise HTTPException(409, "请先移除已有连接")
            secret = secrets.token_urlsafe(32)
            result = devices.claim(body.invitation, body.name, secret)
            response.set_cookie(COOKIE, secret, max_age=365 * 86400, secure=True,
                                httponly=True, samesite="strict", path="/")
            return phone_device(result)

        @app.post("/mobile/v1/browser/forget")
        def browser_forget(request: Request, response: Response):
            device = devices.lookup(request.cookies.get(COOKIE, ""))
            if device:
                devices.revoke(device["device_id"])
                push.unsubscribe(device["device_id"])
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
            return {"ok": True}

        bundle = Path(__file__).resolve().parents[1] / "mobile_dist"
        if not (bundle / "index.html").is_file():
            raise RuntimeError("PWA bundle missing; build apps/mobile or install a complete wheel")
        app.mount("/phone", StaticFiles(directory=bundle, html=True))
    @app.get("/mobile/v1/push")
    def push_status(device: dict = Depends(authorize)):
        return push.status(device["device_id"])

    @app.post("/mobile/v1/push/subscription")
    def push_subscribe(body: PushSubscription, device: dict = Depends(authorize)):
        push.subscribe(device["device_id"], body.model_dump())
        return {"ok": True}

    @app.post("/mobile/v1/push/unsubscribe")
    def push_unsubscribe(device: dict = Depends(authorize)):
        push.unsubscribe(device["device_id"])
        return {"ok": True}

    @app.post("/mobile/v1/push/tasks")
    def task_push_preference(body: TaskPushPreference, device: dict = Depends(authorize)):
        push.tasks.set_enabled(device['device_id'], body.enabled)
        return push.status(device['device_id'])

    @app.post("/mobile/v1/push/test")
    def push_test(device: dict = Depends(authorize)):
        return push.send_test(device["device_id"])

    @app.post("/mobile/v1/push/received")
    def push_received(body: PushReceipt, device: dict = Depends(authorize)):
        push.received(device["device_id"], body.id)
        return {"ok": True}

    if origin:
        # Register after all /mobile/v1 routes so the asset mount cannot shadow APIs.
        # Keep /phone for existing Home Screen installations and their SW scope.
        app.mount("/mobile", StaticFiles(directory=bundle, html=True))
    return app


def _local_peer(request: Request):
    # Check the actual peer as well as the parent app's Host/Origin protection.
    if request.client is None or not is_loopback_host(request.client.host):
        raise HTTPException(403, "Pairing administration is computer-local only")


def _local_service(request: Request):
    _local_peer(request)
    service = getattr(request.app.state, "mobile_service", None)
    if service is None:
        raise HTTPException(503, "Mobile access is disabled")
    if not service.running:
        raise HTTPException(503, "Mobile listener is unavailable; restart the service")
    return service


admin_router = APIRouter(prefix="/api/mobile", dependencies=[Depends(_local_peer)])


class AccessRequest(BaseModel):
    enabled: bool


@admin_router.post("/access")
async def set_access(body: AccessRequest, request: Request):
    controller = getattr(request.app.state, "mobile_controller", None)
    if controller is None:
        raise HTTPException(503, "请重启电脑服务后重试")
    try:
        await controller.set_enabled(body.enabled)
    except (OSError, RuntimeError, ValueError) as exc:
        detail = controller.error if controller.config.mobile.provider == "tailscale" and controller.error else "无法开启手机连接，请检查端口占用和服务日志"
        raise HTTPException(503, detail) from exc
    return {"enabled": body.enabled}


@admin_router.get("/status")
def status_route(request: Request):
    controller = getattr(request.app.state, "mobile_controller", None)
    if controller is not None:
        return controller.status()
    service = getattr(request.app.state, "mobile_service", None)
    if service is None:
        return {"enabled": False}
    return local_status(service)


def local_status(service):
    import socket
    candidates = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = item[4][0]
            if not is_loopback_host(ip):
                candidates.add(f"https://{ip}:{service.port}")
    except OSError:
        pass
    if service.config.mobile.tls_relay:
        candidates = {service.config.mobile.public_origin}
    return {"enabled": service.running, "protocol_version": 1,
            "computer_id": service.identity.computer_id,
            "certificate_sha256": service.identity.certificate_sha256,
            "host": service.config.mobile.host, "port": service.port,
            "tls_relay": service.config.mobile.tls_relay,
            "endpoints": sorted(candidates),
            "pwa_url": f"{service.config.mobile.public_origin}/mobile/" if service.config.mobile.public_origin else None,
            "capabilities": ["sessions.read", "sessions.create", "sessions.send", "notifications.read"]}


@admin_router.post("/pairing/invitations")
def create_invitation(service=Depends(_local_service)):
    return {**local_status(service), **service.devices.create_invitation()}


@admin_router.get("/devices")
def local_devices(service=Depends(_local_service)):
    devices = service.devices.list_devices()
    push = getattr(service, "push", None)
    if push:
        for device in devices:
            state = push.status(device["device_id"])
            device["push"] = {key: state[key] for key in ('subscribed', 'last_test', 'tasks_enabled', 'last_task')}
    return {"devices": devices}


@admin_router.post("/devices/{device_id}/push/test")
def local_test_push(device_id: str, service=Depends(_local_service)):
    push = getattr(service, "push", None)
    if not push:
        raise HTTPException(503, "请先配置 PWA 的 HTTPS 地址")
    if not any(d["device_id"] == device_id and d["status"] == "approved" for d in service.devices.list_devices()):
        raise HTTPException(403, "设备尚未批准或已撤销")
    return push.send_test(device_id)


@admin_router.post("/devices/{device_id}/approve")
def approve_device(device_id: str, body: ApprovalRequest, service=Depends(_local_service)):
    try:
        service.devices.approve(device_id, body.verification_code)
    except PairingError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@admin_router.delete("/devices/{device_id}")
async def revoke_device(device_id: str, service=Depends(_local_service)):
    try:
        service.devices.revoke(device_id)
        if getattr(service, "peers", None):
            await service.peers.revoke(device_id)
        if getattr(service, "push", None):
            service.push.unsubscribe(device_id)
    except PairingError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


class PeerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sdp: str = Field(min_length=1, max_length=65536)


@admin_router.post("/peer/offer")
async def peer_offer(service=Depends(_local_service)):
    try:
        return await service.peers.offer()
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(409, str(exc) or "生成连接超时，请重试") from exc


@admin_router.post("/peer/answer")
async def peer_answer(body: PeerAnswer, service=Depends(_local_service)):
    try:
        await service.peers.answer(body.peer_id, body.sdp)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@admin_router.delete("/peer/{peer_id}")
async def cancel_peer(peer_id: str, service=Depends(_local_service)):
    await service.peers.drop(peer_id)
    return {"ok": True}
