import httpx
import pytest

from app.adapters.base import TokenBucket, UpstreamClient, UpstreamDegraded, UpstreamError
from app.adapters.okx import ENDPOINT_LIMITS, make_buckets
from app.adapters.strike import StrikeAdapter, parse_rate_limit_weight


def bucket(ft, cap, rate):
    return TokenBucket(cap, rate, clock=ft.clock, sleep=ft.sleep)


async def test_bucket_allows_burst_then_throttles(ft):
    b = bucket(ft, 10, 1.0)  # 10 tokens, 1 per second
    for _ in range(10):
        assert await b.acquire(1) == 0
    waited = await b.acquire(1)
    assert waited == pytest.approx(1.0)


async def test_bucket_never_exceeds_budget_in_any_window(ft):
    # 50% of 1200/min = 600/min budget. 200 calls of weight 20 = 4000 weight.
    b = TokenBucket.per_window(1200, 60.0, 0.5, clock=ft.clock, sleep=ft.sleep)
    stamps: list[float] = []
    for _ in range(200):
        await b.acquire(20)
        stamps.append(ft.now)
    worst = 0
    for i, t in enumerate(stamps):
        in_window = sum(20 for u in stamps[i:] if u < t + 60)
        worst = max(worst, in_window)
    assert worst <= 600 * 1.1 + 20  # budget plus the small burst allowance
    assert worst < 1200  # always well under the published limit
    assert b.capacity == 60


def test_okx_budgets_are_half_of_published():
    buckets = make_buckets()
    assert set(buckets) == set(ENDPOINT_LIMITS)
    hist = buckets["funding-rate-history"]  # 10 per 2s published -> 5 per 2s budget
    assert hist.refill_per_sec == pytest.approx(2.5)
    assert buckets["books"].refill_per_sec == pytest.approx(10.0)  # 40 per 2s -> 20 per 2s


def test_strike_rate_limit_read_from_exchange_info(fx):
    assert parse_rate_limit_weight(fx("strike_exchangeInfo.json")) == 2400


async def test_strike_bucket_sized_from_exchange_info(fx, ft):
    handler = lambda req: httpx.Response(200, json=fx("strike_exchangeInfo.json"))  # noqa: E731
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = UpstreamClient("strike", bucket(ft, 1, 1), http, clock=ft.clock, sleep=ft.sleep)
    assert await StrikeAdapter(c).load_rate_limits() == 2400
    assert c.bucket.refill_per_sec == pytest.approx(20.0)  # 50% of 2400/min


def make_client(ft, handler, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return UpstreamClient("test", bucket(ft, 100, 100), http, clock=ft.clock, sleep=ft.sleep, **kw)


async def test_429_with_retry_after_triggers_backoff_then_succeeds(ft):
    calls = []

    def handler(req):
        calls.append(ft.now)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        return httpx.Response(200, json={"ok": 1})

    c = make_client(ft, handler)
    assert await c.get("http://x/y") == {"ok": 1}
    assert 3.0 in ft.sleeps  # honored Retry-After
    assert calls[1] - calls[0] >= 3.0
    assert "429" in (c.last_error or "")
    assert c.last_rate_limited_ts is not None


async def test_long_retry_after_marks_degraded_and_blocks_calls(ft):
    n = 0

    def handler(req):
        nonlocal n
        n += 1
        return httpx.Response(418, headers={"retry-after": "120"})

    c = make_client(ft, handler)
    with pytest.raises(UpstreamDegraded):
        await c.get("http://x/y")
    assert c.degraded
    sent = n
    with pytest.raises(UpstreamDegraded):  # no tight retry loop: nothing is sent
        await c.get("http://x/y")
    assert n == sent
    ft.now += 121
    assert not c.degraded


async def test_429_without_header_uses_exponential_backoff_not_tight_loop(ft):
    n = 0

    def handler(req):
        nonlocal n
        n += 1
        return httpx.Response(429)

    c = make_client(ft, handler, max_attempts=3, backoff_base=2.0)
    with pytest.raises(UpstreamDegraded):
        await c.get("http://x/y")
    assert n <= 3
    assert all(s > 0 for s in ft.sleeps)


async def test_5xx_retries_then_fails_and_records_error(ft):
    n = 0

    def handler(req):
        nonlocal n
        n += 1
        return httpx.Response(503)

    c = make_client(ft, handler, max_attempts=3)
    with pytest.raises(UpstreamError):
        await c.get("http://x/y")
    assert n == 3
    assert "503" in c.last_error


async def test_timeout_is_isolated_error(ft):
    def handler(req):
        raise httpx.ReadTimeout("slow")

    c = make_client(ft, handler, max_attempts=2)
    with pytest.raises(UpstreamError):
        await c.get("http://x/y")
    assert "ReadTimeout" in c.last_error


async def test_request_counts_per_minute(ft):
    c = make_client(ft, lambda r: httpx.Response(200, json={}))
    for _ in range(5):
        await c.get("http://x/y")
    assert c.requests_last_minute() == 5
    assert sum(c.request_counts().values()) == 5


async def test_endpoint_bucket_is_applied(ft):
    c = make_client(ft, lambda r: httpx.Response(200, json={}))
    c.endpoint_buckets["e"] = bucket(ft, 1, 0.5)
    await c.get("http://x/y", endpoint="e")
    await c.get("http://x/y", endpoint="e")
    assert ft.sleeps and ft.sleeps[0] == pytest.approx(2.0)
