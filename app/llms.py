"""Plain-text description of Strike Fast for AI agents (served at /llms.txt)."""

from __future__ import annotations

from app.schemas import DISCLAIMER, SCHEMA_VERSION


def llms_text(base_url: str) -> str:
    b = base_url
    return f"""# Strike Fast

> Makes Strike's competitiveness measurable. After funding, fees, spread and slippage, is
> there a real economic reason to trade perpetuals on Strike (strikefinance.org) instead of
> Hyperliquid or OKX? Results are ESTIMATES. Information only: no trading, no wallets,
> no logins, no user funds. Results can show Strike is worse, and often do.

schema_version: {SCHEMA_VERSION}
Interactive docs: {b}/docs   OpenAPI: {b}/openapi.json   Health: {b}/healthz
Web dashboard: {b}/   Historical report: {b}/report

## Endpoints (all GET, JSON, CORS open)

1. {b}/api/v1/snapshot
   Live funding, marks, volume and order-book depth for every Strike market on each venue.
   Params: notional (USD, default 10000), hours (default 24).

2. {b}/api/v1/opportunities
   Two-leg strategy: long one venue, short another, equal notional. Always involves Strike.
   Ranked by estimated net USD after fees and slippage on all four fills (never raw spread).
   Params: min_net_usd, notional, hours, fee_mode=taker|maker,
   direction=long_strike_short_other|long_other_short_strike, symbol, include_unverified.
   Example: {b}/api/v1/opportunities?symbol=BTC-USD&notional=10000&hours=24&fee_mode=taker
   Response (abridged, illustrative values):
   {{"schema_version": "{SCHEMA_VERSION}", "generated_at_ms": 1792596600000,
    "venues": {{"strike": {{"stale": false, "source_ts_ms": 1792596590000,
      "source_ts_iso": "2026-10-21T15:29:50.000Z"}}}},
    "assumptions": {{"notional_usd": 10000, "hours": 24, "fee_mode": "taker", "...": "..."}},
    "opportunities": [{{"rank": 1, "symbol": "ETH-USD", "direction": "long_strike_short_other",
      "long_venue": "strike", "short_venue": "okx", "net_usd": 7.9,
      "net_usd_by_period": {{"1d": 7.9, "1w": 140.2, "1m": 640.1}},
      "carry_usd_per_hour": 1.1, "break_even_hours": 16.2, "basis_bps": 3.1,
      "confidence": "High", "stale": false, "line_items": ["..."]}}],
    "unranked": []}}

3. {b}/api/v1/holding-cost
   Where is it cheapest to hold ONE long or short position? Entry + exit fees + slippage +
   funding over the holding period, per venue, with best and worst venue and the gap.
   Params: symbol (required), side=long|short (required), notional, hours, fee_mode.
   Example: {b}/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168

4. {b}/api/v1/report
   30-day history: share of hours the two-leg carry was positive, days to cover round-trip
   taker fees, cumulative estimated net over 7 and 30 days, longest favorable stretch, and
   the share of hours Strike was the cheaper venue for longs and shorts.
   Returns history_loading=true while the background backfill is still running.

5. {b}/api/v1/history?symbol=BTC-USD&days=30
   Hourly-normalized funding per venue. null means no data for that hour.

6. {b}/healthz
   Per-venue status, last error, freshness and upstream request counts.

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
