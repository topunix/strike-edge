"""Run the MCP server over stdio for desktop clients: python -m app.mcp

By default it runs its own live poller (same as the web app), so tools return real data after
the first 60 second cycle; get_report needs about 3 minutes of history backfill. The poller
logs to stderr only; stdout carries the MCP protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from app.mcp import build_mcp_server
from app.runtime import background, build_context


async def serve(background_enabled: bool, db_path: str) -> None:
    ctx = build_context(db_path=db_path)
    server = build_mcp_server(ctx)
    if background_enabled:
        async with background(ctx):
            await server.run_stdio_async()
    else:
        await server.run_stdio_async()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.mcp", description=__doc__)
    ap.add_argument(
        "--no-background",
        action="store_true",
        help="do not poll the venues (tools return empty, stale data); for tests and debugging",
    )
    ap.add_argument("--db-path", default=os.environ.get("DB_PATH", "strikeedge.db"))
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    asyncio.run(serve(not args.no_background, args.db_path))


if __name__ == "__main__":
    main()
