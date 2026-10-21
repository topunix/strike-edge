"""FastAPI application factory and lifespan (poller + background history backfill)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app import api, web
from app.adapters import hyperliquid as hl
from app.adapters import okx as okx_mod
from app.adapters.base import UpstreamClient
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.okx import OkxAdapter
from app.adapters.strike import StrikeAdapter
from app.backfill import Backfiller
from app.config import HYPERLIQUID, OKX, STRIKE, SYMBOL_MAP, Settings
from app.db import Store
from app.engine import Engine
from app.middleware import CacheMiddleware, RateLimitMiddleware, SlidingWindowLimiter
from app.poller import Poller
from app.state import LiveState

log = logging.getLogger("strikeedge")
ERROR_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    422: "validation_error",
    429: "rate_limited",
}
DESCRIPTION = """
Strike Edge makes Strike's competitiveness measurable: after funding, fees, spread and
slippage, is there a real economic reason to trade on Strike instead of Hyperliquid or OKX?

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
    settings = settings or Settings.from_env()
    now = now_ms or (lambda: int(time.time() * 1000))
    store = store or Store(db_path)
    state = state or LiveState()
    engine = Engine(settings, state, store, now)
    limiter = SlidingWindowLimiter(
        rate_limit_per_minute, 60.0, *([limiter_clock] if limiter_clock else [])
    )
    ctx = api.Context(settings, state, store, engine, now, limiter=limiter)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        clients: dict[str, UpstreamClient] = {}
        if start_background:
            markets = list(SYMBOL_MAP.values())
            f = settings.budget_fraction
            clients[STRIKE] = UpstreamClient(STRIKE, StrikeAdapter.make_bucket(fraction=f))
            clients[HYPERLIQUID] = UpstreamClient(HYPERLIQUID, hl.make_bucket(f))
            clients[OKX] = UpstreamClient(
                OKX, okx_mod.make_bucket(f), endpoint_buckets=okx_mod.make_buckets(f)
            )
            ctx.clients = clients
            adapters = {
                STRIKE: StrikeAdapter(clients[STRIKE]),
                HYPERLIQUID: HyperliquidAdapter(clients[HYPERLIQUID], markets),
                OKX: OkxAdapter(clients[OKX], markets),
            }
            enabled = {v: a for v, a in adapters.items() if v in settings.enabled_venues}

            async def start() -> None:
                with contextlib.suppress(Exception):  # sized from exchangeInfo when reachable
                    await adapters[STRIKE].load_rate_limits(f)
                with contextlib.suppress(Exception):
                    await adapters[OKX].load_instruments()
                poller = Poller(settings, state, store, enabled, markets, now)
                ctx.backfiller = Backfiller(store, enabled, markets, now_ms=now)
                tasks.append(asyncio.create_task(poller.run_forever(), name="poller"))
                tasks.append(asyncio.create_task(backfill_loop(ctx.backfiller), name="backfill"))

            tasks.append(asyncio.create_task(start(), name="startup"))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            for c in clients.values():
                await c.aclose()

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


async def backfill_loop(bf: Backfiller, every_seconds: float = 1800.0) -> None:
    """Initial history backfill, then a refresh every 30 minutes (incremental)."""
    while True:
        try:
            await bf.run()
        except Exception:
            log.exception("backfill pass failed")
        await asyncio.sleep(every_seconds)


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazy module attribute so `uvicorn app.main:app` works without side effects on import."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app(db_path=os.environ.get("DB_PATH", "strikeedge.db"))
        return _app
    raise AttributeError(name)
