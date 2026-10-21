"""Serve the app on fixture data (no network). Usage: python scripts/serve_fixture.py PORT"""

import sys

import uvicorn

sys.path.insert(0, ".")
from app.config import Settings  # noqa: E402
from app.db import Store  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.helpers import WED_NOON_UTC, build_state, seed_history  # noqa: E402

store = Store(":memory:")
seed_history(store, WED_NOON_UTC)
app = create_app(
    Settings(), store=store, state=build_state(WED_NOON_UTC), now_ms=lambda: WED_NOON_UTC,
    start_background=False, cache_ttl_seconds=0, rate_limit_per_minute=100000,
)  # fmt: skip
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
