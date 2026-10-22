"""Shared runtime: build the app context and run the live poller and history backfill.

Used by the FastAPI app (app.main) and by the stdio MCP server (python -m app.mcp), so both
serve the same engine from the same kind of live state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from app import api
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
from app.middleware import SlidingWindowLimiter
from app.poller import Poller
from app.state import LiveState

log = logging.getLogger("strikeedge")


def build_context(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    state: LiveState | None = None,
    now_ms: Callable[[], int] | None = None,
    db_path: str = "strikeedge.db",
    limiter: SlidingWindowLimiter | None = None,
) -> api.Context:
    settings = settings or Settings.from_env()
    now = now_ms or (lambda: int(time.time() * 1000))
    store = store or Store(db_path)
    state = state or LiveState()
    engine = Engine(settings, state, store, now)
    return api.Context(settings, state, store, engine, now, limiter=limiter)


async def backfill_loop(bf: Backfiller, every_seconds: float = 1800.0) -> None:
    """Initial history backfill, then a refresh every 30 minutes (incremental)."""
    while True:
        try:
            await bf.run()
        except Exception:
            log.exception("backfill pass failed")
        await asyncio.sleep(every_seconds)


@asynccontextmanager
async def background(ctx: api.Context) -> AsyncIterator[None]:
    """Run the 60 second poller and the history backfill until the context exits."""
    settings, state, store, now = ctx.settings, ctx.state, ctx.store, ctx.now_ms
    markets = list(SYMBOL_MAP.values())
    f = settings.budget_fraction
    clients: dict[str, UpstreamClient] = {
        STRIKE: UpstreamClient(STRIKE, StrikeAdapter.make_bucket(fraction=f)),
        HYPERLIQUID: UpstreamClient(HYPERLIQUID, hl.make_bucket(f)),
        OKX: UpstreamClient(OKX, okx_mod.make_bucket(f), endpoint_buckets=okx_mod.make_buckets(f)),
    }
    ctx.clients = clients
    adapters = {
        STRIKE: StrikeAdapter(clients[STRIKE]),
        HYPERLIQUID: HyperliquidAdapter(clients[HYPERLIQUID], markets),
        OKX: OkxAdapter(clients[OKX], markets),
    }
    enabled = {v: a for v, a in adapters.items() if v in settings.enabled_venues}
    tasks: list[asyncio.Task] = []

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
