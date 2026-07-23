from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from trade_compass_agent.config import load_app_config, load_project_dotenv
from trade_compass_agent.web.api import router as api_router
from trade_compass_agent.web import dist as web_dist_module
from trade_compass_agent.web.security import (
    LocalOriginMiddleware,
    RequestSizeLimitMiddleware,
    TrustedLocalHostMiddleware,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>交易罗盘</title></head>
<body>
  <h1>Trade Compass web UI not bundled</h1>
  <p>Build the Vite bundle (<code>pnpm --dir apps/web build</code>) or run
     <code>pnpm --dir apps/web dev</code> with the API on port 19704.</p>
  <p>Open the agent UI at <a href="/agent">/agent</a> after building, or use the Vite dev server.</p>
  <p>API: <a href="/api/agent/skills">/api/agent/skills</a></p>
</body>
</html>"""


_gateway_daemon = None


class SPAStaticFiles(StaticFiles):
    """Serve index.html for extensionless client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not _is_spa_route(path):
                raise
        else:
            if response.status_code != 404 or not _is_spa_route(path):
                return response

        return await super().get_response("index.html", scope)


def _is_spa_route(path: str) -> bool:
    candidate = path.strip("/")
    return (
        bool(candidate)
        and candidate != "api"
        and not candidate.startswith("api/")
        and not PurePosixPath(candidate).suffix
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop scheduler and gateway with the web server lifecycle."""
    global _gateway_daemon
    from trade_compass_agent.ops.tick_scheduler import (
        TickScheduler,
        get_active_scheduler,
        set_active_scheduler,
    )

    config = load_app_config()

    scheduler = None
    skip = os.getenv("TRADE_COMPASS_NO_SCHEDULER", "").lower() in {"1", "true", "yes"}
    if not skip and get_active_scheduler() is None:
        if config.scheduler.enabled:
            scheduler = TickScheduler(config)
            scheduler.start_background()
            set_active_scheduler(scheduler)
            logger.info("Scheduler started via lifespan")

    # Start messaging gateway if enabled
    if config.channels.gateway_enabled:
        _gateway_daemon = await _start_gateway(config)
        from trade_compass_agent.channels.gateway import set_active_gateway
        set_active_gateway(_gateway_daemon)

    yield

    if _gateway_daemon is not None:
        from trade_compass_agent.channels.gateway import set_active_gateway
        set_active_gateway(None)
        await _gateway_daemon.stop()
        _gateway_daemon = None
        logger.info("Gateway stopped via lifespan")

    if scheduler is not None:
        scheduler.shutdown(wait=False)
        set_active_scheduler(None)
        logger.info("Scheduler stopped via lifespan")


async def _start_gateway(config) -> Any:
    """Build and start the messaging gateway daemon."""
    from trade_compass_agent.channels.gateway import GatewayDaemon, agent_message_handler

    gateway = GatewayDaemon(on_message=agent_message_handler)

    if config.channels.feishu_enabled:
        app_id = os.environ.get("FEISHU_APP_ID", "")
        app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        if app_id and app_secret:
            from trade_compass_agent.channels.feishu import FeishuBotAdapter
            gateway.register(FeishuBotAdapter(app_id=app_id, app_secret=app_secret))
        else:
            logger.warning("Feishu bot enabled but FEISHU_APP_ID/FEISHU_APP_SECRET not set")

    if config.channels.wecom_enabled:
        bot_id = os.environ.get("WECOM_BOT_ID", "")
        secret = os.environ.get("WECOM_SECRET", "")
        if bot_id and secret:
            from trade_compass_agent.channels.wecom import WecomBotAdapter
            gateway.register(WecomBotAdapter(bot_id=bot_id, secret=secret))
        else:
            logger.warning("WeCom bot enabled but WECOM_BOT_ID/WECOM_SECRET not set")

    if config.channels.weixin_enabled:
        from trade_compass_agent.channels.weixin import WeixinBotAdapter
        gateway.register(WeixinBotAdapter())

    await gateway.start()
    logger.info("Gateway daemon started with %d platform(s)", len(gateway.platforms))
    return gateway


def create_app() -> FastAPI:
    load_project_dotenv()
    from trade_compass_agent.logging_config import setup_logging
    setup_logging()
    application = FastAPI(title="交易罗盘", lifespan=_lifespan)
    application.add_middleware(RequestSizeLimitMiddleware)
    application.add_middleware(TrustedLocalHostMiddleware)
    application.add_middleware(LocalOriginMiddleware)

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            clean = {k: v for k, v in err.items() if k != "ctx"}
            errors.append(clean)
        return JSONResponse(status_code=422, content={"detail": errors})

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            detail = exc.detail
            if not isinstance(detail, str):
                detail = str(detail)
            return JSONResponse(status_code=exc.status_code, content={"detail": detail})
        logger.error(
            "Unhandled HTTP request error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    application.include_router(api_router)

    @application.get("/health")
    def health() -> dict:
        from trade_compass_agent.web.monitoring import build_health_report
        return build_health_report()

    if os.getenv("TRADE_COMPASS_DEV_CORS", "").lower() in {"1", "true", "yes"}:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    web_dist = web_dist_module.resolve_web_dist()
    if web_dist is not None:
        application.mount("/", SPAStaticFiles(directory=str(web_dist), html=True), name="web")
    else:

        @application.get("/")
        def web_placeholder() -> HTMLResponse:
            return HTMLResponse(_PLACEHOLDER_HTML)

    return application


app = create_app()
