"""Plain-text description of Strike Edge for AI agents (served at /llms.txt)."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import DISCLAIMER, SCHEMA_VERSION

EXAMPLES = Path(__file__).parent / "examples"


def example(name: str) -> str:
    """A real captured response (lists shortened), or a short note if not captured yet.

    Regenerate with: python scripts/capture_examples.py http://127.0.0.1:8000
    """
    try:
        data = json.loads((EXAMPLES / f"{name}.json").read_text())
    except (OSError, ValueError):
        return "   (example not captured yet)"
    text = json.dumps(data, indent=1)
    return "\n".join("   " + line for line in text.splitlines())


def captured_at() -> str:
    try:
        return json.loads((EXAMPLES / "opportunities.json").read_text())["generated_at_iso"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def llms_text(base_url: str) -> str:
    b = base_url
    return f"""# Strike Edge

> Makes Strike's competitiveness measurable. After funding, fees, spread and slippage, is
> there a real economic reason to trade perpetuals on Strike (strikefinance.org) instead of
> Hyperliquid or OKX? Results are ESTIMATES. Information only: no trading, no wallets,
> no logins, no user funds. Honest estimates after fees, funding and slippage.

schema_version: {SCHEMA_VERSION}
Interactive docs: {b}/docs   OpenAPI: {b}/openapi.json   Health: {b}/healthz
Web dashboard: {b}/   Historical report: {b}/report

## Endpoints (all GET, JSON, CORS open)

1. {b}/api/v1/snapshot
   Live funding, marks, volume and order-book depth for every Strike market on each venue.
   Params: notional (USD, default 10000), hours (default 24).
   Real response (abridged to the ETH-USD market; captured {captured_at()}):
{example("snapshot")}

2. {b}/api/v1/opportunities
   Two-leg strategy: long one venue, short another, equal notional. Always involves Strike.
   Ranked by estimated net USD after fees and slippage on all four fills (never raw spread).
   Params: min_net_usd, notional, hours, fee_mode=taker|maker,
   direction=long_strike_short_other|long_other_short_strike, symbol, include_unverified.
   Example: {b}/api/v1/opportunities?symbol=BTC-USD&notional=10000&hours=24&fee_mode=taker
   Real response (abridged: lists shortened to the top row; captured {captured_at()}):
{example("opportunities")}

3. {b}/api/v1/holding-cost
   Where is it cheapest to hold ONE long or short position? Entry + exit fees + slippage +
   funding over the holding period, per venue, with best and worst venue and the gap.
   Params: symbol (required), side=long|short (required), notional, hours, fee_mode.
   Example: {b}/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168
   Real response (captured {captured_at()}):
{example("holding_cost")}

4. {b}/api/v1/report
   30-day history: share of hours the two-leg carry was positive, days to cover round-trip
   taker fees, cumulative estimated net over 7 and 30 days, longest favorable stretch, and
   the share of hours Strike was the cheaper venue for longs and shorts.
   Returns history_loading=true while the background backfill is still running.
   Real response (abridged to ETH-USD, 3 summary lines, 1 finding; captured {captured_at()}):
{example("report")}

5. {b}/api/v1/history?symbol=BTC-USD&days=30
   Hourly-normalized funding per venue. null means no data for that hour. Also returns
   cumulative_net_usd_on_10k, the running estimated net used by the report chart.
   Real response (abridged to 3 points per series; captured {captured_at()}):
{example("history")}

6. {b}/healthz
   Per-venue status, last error, freshness and upstream request counts.
   Real response (captured {captured_at()}):
{example("healthz")}

## Units and sign conventions

- Field names carry units: *_usd (US dollars), *_bps (basis points), *_rate_per_hour
  (fraction per hour, 0.0001 = 0.01% per hour), *_hours, *_ms (UTC epoch milliseconds),
  *_iso (ISO 8601 UTC).
- Funding: a POSITIVE rate means longs pay shorts. A long on a venue with rate +r pays
  r x notional per hour. A short receives it. Negative rates reverse this.
- Raw funding is reported per interval (Strike 1h, Hyperliquid 1h, OKX usually 8h but
  derived per instrument) and normalized to per hour (per interval / interval hours).
- net_usd is an estimate: funding on both legs minus fees on four fills minus slippage on
  four fills, assuming the CURRENT funding rate persists. Past funding does not predict
  future funding. Basis and funding can change, and closing prices may differ.
- null means missing data. It is never 0. Zero funding is reported as 0.
- break_even_hours = round-trip costs / net hourly carry; null if carry is not positive.
- Maker figures assume resting orders fill (not guaranteed) with zero slippage.

## Freshness, confidence and exclusions

- Each venue has source timestamps. Data older than 5 minutes is stale (stale=true) and is
  excluded from rankings. Poll cycle is 60 seconds; order books are staggered across it.
- confidence is High, Medium or Low (with confidence_reasons) from data freshness (all legs
  under 2 minutes), book depth at your size, and how consistent the funding sign was over
  the last 24h. Low is forced when the underlying market is closed (stocks, indices,
  commodities), for ticker-only matches, and lowered one level when the Hyperliquid HIP-3
  deployer fee is unverified (hl_fee_unverified=true).
- Rows with insufficient liquidity at your notional are excluded from rankings.
- Ticker-only matches (CXMT, DRAM, MINIMAX, SPCX, UNITREE, ZHIPU) are not ranked unless you
  pass include_unverified=true; they still appear in the unranked list.

## Rate limits

- {b} allows about 60 requests per minute per client. Over the limit you get HTTP 429 with a
  Retry-After header and {{"error": "rate_limited", "detail": "..."}}.
- Responses under /api/v1 are cached for about 30 seconds; polling faster gains nothing.
- /healthz is exempt from the limit.
- Errors always look like {{"error": "<code>", "detail": "<message>"}}.

## Disclaimer

{DISCLAIMER}
Strike is not available to persons in restricted jurisdictions. See https://www.strikefinance.org/restricted
"""
