from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace

import uvicorn

from trade_compass_agent.config import AppConfig, update_mobile_enabled
from trade_compass_agent.mobile.api import create_mobile_app
from trade_compass_agent.mobile.identity import ComputerIdentity, load_or_create_identity
from trade_compass_agent.mobile.pairing import DeviceStore
from trade_compass_agent.mobile.tls import browser_tls
from trade_compass_agent.mobile.tls_reload import RelayTLS
from trade_compass_agent.mobile.peer import PeerConnections
from trade_compass_agent.mobile.task_push import run_task_push

logger = logging.getLogger(__name__)


class MobileController:
    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.context = None
        self.managed = None
        self.requested = config.mobile.enabled
        self.error = ""
        self.lock = asyncio.Lock()

    async def start(self):
        self.error = ""
        if self.config.mobile.provider == "tailscale":
            from trade_compass_agent.mobile.managed import ManagedConnection
            self.managed = ManagedConnection(self.app, self.config)
            try:
                await self.managed.start()
            except (OSError, RuntimeError, ValueError) as exc:
                self.error = str(exc) if isinstance(exc, RuntimeError) else "无法启动手机连接组件，请重试"
                await self.managed.close()
                raise
            return
        if self.config.mobile.provider != "manual":
            raise ValueError("Unknown mobile connection provider")
        context = mobile_listener(replace(self.config, mobile=replace(self.config.mobile, enabled=True)))
        service = await context.__aenter__()
        self.context = context
        self.app.state.mobile_service = service

    async def close(self):
        if self.managed is not None:
            managed, self.managed = self.managed, None
            await managed.close()
        self.app.state.mobile_service = None
        if self.context is not None:
            context, self.context = self.context, None
            await context.__aexit__(None, None, None)

    async def set_enabled(self, enabled):
        async with self.lock:
            service = getattr(self.app.state, "mobile_service", None)
            if enabled:
                restart = (self.managed is None or self.managed.phase in {"error", "stopped"} or bool(self.error)
                           if self.config.mobile.provider == "tailscale" else service is None or not service.running)
                if restart:
                    await self.close()
                    await self.start()
                    try:
                        await asyncio.to_thread(update_mobile_enabled, True)
                    except Exception:
                        await self.close()
                        raise
                self.requested = True
            else:
                await asyncio.to_thread(update_mobile_enabled, False)
                self.requested = False
                self.error = ""
                await self.close()

    def status(self):
        from trade_compass_agent.mobile.api import local_status
        service = getattr(self.app.state, "mobile_service", None)
        value = local_status(service) if service else {"enabled": False}
        value.update(requested=self.requested, provider=self.config.mobile.provider)
        if self.config.mobile.provider == "tailscale":
            connection = self.managed.status() if self.managed else {
                "phase": "stopped", "message": "手机连接已关闭。", "action_url": None}
            if self.error:
                connection = {"phase": "error", "message": self.error, "action_url": None}
            value["connection"] = connection
            value["enabled"] = value["enabled"] and connection["phase"] == "ready"
        return value


class _EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The parent web server owns process signals and scheduler shutdown.
        yield


@dataclass
class MobileService:
    config: AppConfig
    identity: ComputerIdentity
    devices: DeviceStore
    server: uvicorn.Server
    task: asyncio.Task
    port: int
    push: object = None
    peers: object = None
    tls: RelayTLS | None = None

    @property
    def running(self) -> bool:
        return (self.server.started and not self.task.done() and not self.server.should_exit
                and (self.tls is None or self.tls.valid))


@asynccontextmanager
async def mobile_listener(config: AppConfig):
    """Second TLS listener in the existing process, with no scheduler/lifespan of its own."""
    if not config.mobile.enabled:
        yield None
        return
    address = ipaddress.ip_address(config.mobile.host)
    if not 0 <= config.mobile.port <= 65535:
        raise ValueError("mobile.port must be between 0 and 65535")
    sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
    task = None
    server = None
    peers = None
    push_task = None
    tls_task = None
    push_stop = asyncio.Event()
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if address.version == 6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((str(address), config.mobile.port))
        sock.listen(128)
        sock.setblocking(False)
        directory = config.data_dir / "mobile"
        identity = load_or_create_identity(directory)
        tls = RelayTLS(config.mobile, identity, directory) if config.mobile.tls_relay else None
        if tls is not None:
            identity, keyfile = tls.current.identity, config.mobile.tls_keyfile
        else:
            identity, keyfile = browser_tls(config.mobile, identity)
        devices = DeviceStore(directory)
        app = create_mobile_app(config, devices, identity)
        peers = PeerConnections(app, devices, identity)
        server = _EmbeddedServer(uvicorn.Config(
            app, host=str(address), port=sock.getsockname()[1],
            ssl_certfile=str(identity.pem_path), ssl_keyfile=keyfile,
            proxy_headers=False, access_log=False, log_config=None, lifespan="off",
            timeout_graceful_shutdown=3,
        ))
        if tls is not None:
            server.config.load()
            server.config.ssl = tls.context
        task = asyncio.create_task(server.serve(sockets=[sock]), name="mobile-https")
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Mobile HTTPS listener did not start")
                await asyncio.sleep(0.01)
        service = MobileService(config, identity, devices, server, task, sock.getsockname()[1], app.state.web_push, peers, tls)
        push_task = asyncio.create_task(run_task_push(service.push, devices, config, push_stop), name="mobile-task-push")
        if tls is not None:
            tls_task = asyncio.create_task(tls.watch(service, push_stop), name="mobile-tls-reload")
        logger.info("Mobile HTTPS listener started on %s:%s", address, service.port)
        yield service
    finally:
        push_stop.set()
        if tls_task is not None:
            await tls_task
        if push_task is not None:
            await push_task
        if peers is not None:
            await peers.close()
        if server is not None:
            server.should_exit = True
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except asyncio.TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            sock.close()
