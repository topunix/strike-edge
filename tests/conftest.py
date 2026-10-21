import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def fx():
    return load


class FakeTime:
    """Deterministic clock; sleep advances time instead of waiting."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ft():
    return FakeTime()


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run live smoke tests")


def pytest_collection_modifyitems(config, items):
    import os

    if config.getoption("--live") or os.environ.get("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: use --live or RUN_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
