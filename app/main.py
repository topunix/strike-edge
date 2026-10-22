"""FastAPI application factory and lifespan (poller + background history backfill)."""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException

from app import api, web
from app.config import TAGLINE, Settings
from app.db import Store
from app.mcp import build_mcp_server
from app.middleware import CacheMiddleware, RateLimitMiddleware, SlidingWindowLimiter
from app.runtime import background, build_context
from app.state import LiveState

log = logging.getLogger("strikeedge")
ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    422: "validation_error",
    429: "rate_limited",
}
DESCRIPTION = f"""
**{TAGLINE}**

Strike Edge shows where a perp trade costs less across Strike, Hyperliquid and OKX, after
funding, fees, spread and slippage. Every number is an honest estimate.

**All figures are estimates**, never profit or guaranteed arbitrage. Information only.
Positive funding means longs pay shorts. `null` means missing data, never 0.
Rate limit: about 60 requests/minute per client; responses cached about 30 seconds.
"""


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    state: LiveState | None = None,
    now_ms: Callable[[], int] | None = None,
    start_background: bool = True,
    cache_ttl_seconds: float = 30.0,
    rate_limit_per_minute: int = 60,
    limiter_clock: Callable[[], float] | None = None,
    db_path: str = "strikeedge.db",
) -> FastAPI:
    limiter = SlidingWindowLimiter(
        rate_limit_per_minute, 60.0, *([limiter_clock] if limiter_clock else [])
    )
    ctx = build_context(
        settings, store=store, state=state, now_ms=now_ms, db_path=db_path, limiter=limiter
    )
    mcp_server = build_mcp_server(ctx)
    # Stateless JSON mode: every POST is self-contained, which suits a free, restartable host.
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        # A public service, not a local one: DNS rebinding protection would reject our hostname.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            if start_background:
                await stack.enter_async_context(background(ctx))
            await stack.enter_async_context(mcp_server.session_manager.run())
            yield

    app = FastAPI(
        title="Strike Edge",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        contact={"name": "topunix"},
        license_info={"name": "MIT"},
    )
    app.state.ctx = ctx
    app.include_router(api.router)
    app.include_router(web.router)
    app.router.routes.extend(mcp_app.routes)  # exactly /mcp, no catch-all (keeps JSON 404s)
    app.mount("/static", StaticFiles(directory=str(web.BASE / "static")), name="static")

    # Order matters: the last added runs first. Rate limit, then cache, then CORS outermost.
    app.add_middleware(CacheMiddleware, ttl=cache_ttl_seconds)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Cache"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # outermost: cache stores plain bodies

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": ERROR_CODES.get(exc.status_code, "error"), "detail": str(exc.detail)},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()
        )
        return JSONResponse({"error": "validation_error", "detail": detail}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(
            {"error": "internal_error", "detail": "Unexpected server error."}, status_code=500
        )

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazy module attribute so `uvicorn app.main:app` works without side effects on import."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(db_path=os.environ.get("DB_PATH", "strikeedge.db"))
        return _app
    raise AttributeError(name)
