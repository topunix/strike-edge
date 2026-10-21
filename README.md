# Strike Fast

**After funding, fees, spread and slippage, is there a real economic reason to trade perpetuals on Strike instead of Hyperliquid or OKX?**

Strike Fast makes that question measurable. It reads live funding, prices and order books from Strike, Hyperliquid and OKX, subtracts modeled fees and slippage, and shows what a position really costs or earns. It shows results honestly, including when Strike is worse.

Everything is an **estimate**, never "profit" or "guaranteed arbitrage". Information only: no trading, no wallets, no logins, no user funds.

Built by **topunix**. Development started before the official build window with approval from the Strike team.

![Dashboard on desktop](docs/screenshots/home-desktop.jpg)

## Who it is for

- **Traders and Strike holders** use the web dashboard: the top verified opportunities, the cheapest venue to hold a long or short, and a 30 day report.
- **Bots, quant desks and AI agents** use the versioned JSON API (`/api/v1`), OpenAPI docs at `/docs`, and `/llms.txt`. Both audiences get the same numbers from the same engine.

## Headline findings

> **Snapshot as of 2026-10-21 16:44 UTC.** Window: 2026-09-21 16:00 to 2026-10-21 16:00 UTC (720 hours), $10,000 position, taker fees, funding only for the "cheaper to hold" shares. These numbers are a snapshot of that window, not a forecast. **Past funding does not predict future funding.** Regenerate them any time at `/report`.

- **Strike's ETH, SOL, XRP and PUMP perps traded below their index price in most hours** (Strike funding was negative in 63%, 80%, 80% and 59% of hours). As a result, **longs were cheaper to hold on Strike and shorts were more expensive.**
  - ETH: Strike was cheaper for longs in 85% of hours vs Hyperliquid and 72% vs OKX, and cheaper for shorts in only 15% and 28%.
  - SOL: longs 93% and 90%, shorts 7% and 10%.
- **ADA went the other way.** Strike's ADA perp traded above index (funding positive in 99% of hours), so shorts were cheaper to hold on Strike and longs were more expensive.
- **BTC was a toss-up.** Strike was cheaper for BTC shorts in 48% of hours vs Hyperliquid and 51% vs OKX, and for longs in 52% and 49%.
- **Most two-leg trades did not pay.** Of 86 market and direction pairs that passed the confidence checks, 33 had a positive estimated 30 day net after taker fees and slippage and 53 did not. The best was ADA (long Hyperliquid, short Strike) at about +$484 on $10,000; the worst was PUMP (long Hyperliquid, short Strike) at about -$252. Another 8 pairs were flagged "Low confidence: thin liquidity or extreme funding" and are left out of this summary (they stay in the report tables).
- **Why a discount to index matters, and its limit.** Strike's funding follows the gap between its perp price and its index price, so a persistently negative rate means the perp traded below index. That is what makes longs cheaper. It can change quickly, and it also affects entry and exit prices (basis), which the funding figures do not capture.

![Funding history and cumulative estimated net for ETH](docs/screenshots/report-charts-desktop.jpg)

*The cumulative chart starts below zero by the cost of four fills and adds hourly funding carry. The dashed line is zero.*

## Screenshots

| Mobile dashboard (375px) | Mobile "cheapest to hold" | Mobile charts |
| --- | --- | --- |
| ![](docs/screenshots/home-mobile.jpg) | ![](docs/screenshots/home-hold-mobile.jpg) | ![](docs/screenshots/report-charts-mobile.jpg) |

Desktop: [report](docs/screenshots/report-desktop.jpg) and [cheapest to hold](docs/screenshots/home-hold-desktop.jpg). All screenshots come from Playwright runs against live data (`scripts/check_layout.py`).

## What it shows

- **Two-leg strategy (View B).** Long one venue, short another, equal notional. Net = funding on both legs minus fees on four fills minus slippage on four fills. It also shows break-even hours (round-trip costs divided by net hourly carry, only when carry is positive) and basis in bps. Rows are ranked by estimated net dollars after costs, never by raw spread.
- **Cheapest venue to hold (View A).** One long or short position: entry and exit fees plus slippage plus funding for the holding period, per venue, with the best venue, the worst venue and the dollar gap. A direction toggle switches between long and short.
- **Periods.** Rows show 1 day, 1 week and 1 month estimates side by side, assuming current funding persists. Round-trip costs are paid once, so short holds rarely cover them. The dashboard defaults to a 1 week hold.
- **Markets only on Strike.** Any Strike market with no matched competitor gets its own section. Today all 31 Strike markets match at least one venue, so it is empty.
- **Historical report (`/report`).** 30 days of funding for every matched market: share of hours the two-leg carry was positive, days to cover round-trip fees, cumulative estimated net over 7 and 30 days, longest favorable stretch, and the share of hours Strike was the cheaper venue for longs and shorts. It starts with generated key findings and has two charts.

## How it uses Strike's API

All public, no auth, base `https://api.strikefinance.org`:

| Endpoint | Used for |
| --- | --- |
| `GET /price/v2/premiumIndex` | Mark, index, hourly funding rate, next funding time for all markets in one call. Also `interestRate` and `interestRateDampener`, used to classify TradFi markets. |
| `GET /price/v2/depth?symbol=BTC-USD&limit=50` | Order book for the slippage walk, fetched only for matched markets and staggered across each 60 second cycle. |
| `GET /price/v2/ticker/24hr` | 24 hour volume for all markets in one call. |
| `GET /price/v2/exchangeInfo` | Contract list and `rateLimits`, read at startup to size the outbound limiter. |
| `GET /stat/v1/stats/coin/history/funding?symbol=BTC-USD&days=30` | Hourly funding history for the report and charts. |

Strike's funding is hourly and positive means longs pay shorts. We checked the sign independently: Strike's funding sign agrees with the sign of its own mark-versus-index premium on every market with a meaningful premium. TradFi markets sit at exactly 0 inside Strike's dampener dead-band, which we report as 0, not as missing.

**Competitors.** Hyperliquid: `POST /info` with `metaAndAssetCtxs` (main dex and the HIP-3 `xyz` dex), `l2Book`, `fundingHistory` (paginated 500 rows at a time). OKX: `GET /api/v5/public/funding-rate?instId=ANY`, `/public/mark-price`, `/market/tickers`, `/market/books`, `/public/instruments` and `/public/funding-rate-history` (paginated with `after`).

## Methodology and assumptions

### Funding conventions

| Venue | Interval | Sign | Where it comes from |
| --- | --- | --- | --- |
| Strike | 1 hour | Positive: longs pay shorts | `premiumIndex.fundingRate`, history is hourly |
| Hyperliquid | 1 hour | Positive: longs pay shorts | `metaAndAssetCtxs.funding`, `fundingHistory` |
| OKX | Per instrument (1h, 4h or 8h), derived from `nextFundingTime - fundingTime` | Positive: longs pay shorts | `funding-rate.fundingRate`; history uses the settled `realizedRate` |

Every rate is normalized to hourly (rate per interval divided by interval hours). Displays show the raw rate per interval first, then hourly, then the 24 hour dollar figure on your size, with annualized APR small and last. Missing data is `null`; zero funding is `0`. The two are never mixed up. In the report, an 8 hour OKX payment is spread evenly over its 8 hours.

### Fees (configurable base tiers)

| Venue | Taker | Maker | Source and caveat |
| --- | --- | --- | --- |
| Strike | 0.050% | -0.005% (rebate) | As configured for the project; not independently verified. |
| Hyperliquid | 0.045% | 0.015% | Hyperliquid's published base perps tier (checked in its docs on 2026-10-21). |
| OKX | 0.050% | 0.020% | Regular tier (Lv1) USDT perps; consistent across several secondary sources, but we could not fetch OKX's own fee page. Check your account tier. |

Maker figures are shown separately and labeled "if filled as maker". In maker mode slippage is assumed zero and fills are not guaranteed.

**Hyperliquid HIP-3 markets (the `xyz` dex: stocks, gold, silver, WTI, S&P 500).** The deployer fee on top of the base fee is not exposed by the API and can add 0 to 300% in the docs. We use the base fee (`HL_HIP3_FEE_MULTIPLIER=1.0`, configurable), which is generous to Hyperliquid in the comparison but possibly low. Those rows are marked "HL fee unverified (HIP-3 deployer fee not exposed)" and lose one confidence level.

### Slippage

For the chosen notional (default $10,000) we walk each venue's order book and take the average fill versus the book mid, for entry and exit. A book too thin for the size is marked "insufficient liquidity" and excluded from rankings. OKX sizes are converted from contracts to base units with `ctVal * ctMult`. The report uses each market's current book walk at $10,000 as its per-fill slippage (floor 2 bps, labeled when the floor or a fallback applies). That reflects current depth, not historical depth.

### Confidence (High, Medium, Low)

Based on data freshness (all legs under 2 minutes), book depth at your size, and how consistent the funding sign was over the last 24 hours. Then:

- **Low is forced** when the underlying market is closed (US stocks, Korean stocks, index and commodity futures hours, weekends and known holidays; a simple config, no external calendar), and for ticker-only matches.
- **One level lower** when the Hyperliquid fee is unverified (HIP-3).
- Venue data older than 5 minutes is stale: greyed out and excluded from rankings.

### Symbol map

Only exact underlying matches are mapped: all 31 Strike markets match on OKX; 28 also match on Hyperliquid (10 on the main dex, 18 on the HIP-3 `xyz` dex). NAS100, NIGHT and SKHYNIX are OKX-only. Notes on contract differences (collateral, reference price, trading hours) are in `app/config.py` and shown in the UI. Six markets (CXMT, DRAM, MINIMAX, SPCX, UNITREE, ZHIPU) are **ticker matches only**: always Low confidence, labeled "ticker match only", and left out of the default rankings (visible in the full list and with `include_unverified=true`). Strike's `underlyingType` is `COIN` for every market, so TradFi markets are identified with `interestRate == 0 and interestRateDampener > 0` from `premiumIndex`, plus an override list.

### Report guard

A market and direction whose absolute 30 day estimated net exceeds 5% of notional, or whose book is thin at the notional, is flagged "Low confidence: thin liquidity or extreme funding". It stays in the report tables but is excluded from the headline summary.

### Limits of this tool

Funding, basis and liquidity change. Estimates assume current funding persists. Basis and closing prices are not modeled. Binance is **not supported** because it geo-blocks US servers.

## API quickstart

Interactive docs: `/docs` (OpenAPI at `/openapi.json`). Agents: `/llms.txt`, which embeds real example responses.

Every response carries `schema_version`, `generated_at_ms` and `generated_at_iso`, per-venue source timestamps (UTC ms and ISO 8601) with a `stale` flag, the assumptions used, and a disclaimer. Field names carry units (`net_usd`, `funding_rate_per_hour`, `basis_bps`). `null` means missing, never 0. Errors are always `{"error": "...", "detail": "..."}`.

```bash
# Ranked two-leg opportunities (ETH, 1 week, $10k, taker fees)
curl -s "http://127.0.0.1:8000/api/v1/opportunities?symbol=ETH-USD&hours=168&notional=10000&fee_mode=taker"

# Cheapest venue to hold a long (1 week)
curl -s "http://127.0.0.1:8000/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168"

# Live funding, marks, volume and depth for every market
curl -s "http://127.0.0.1:8000/api/v1/snapshot"

# 30 day report, hourly funding history, health
curl -s "http://127.0.0.1:8000/api/v1/report"
curl -s "http://127.0.0.1:8000/api/v1/history?symbol=BTC-USD&days=30"
curl -s "http://127.0.0.1:8000/healthz"
```

Real top row from `opportunities` (captured 2026-10-21 16:44 UTC; values change every minute):

```json
{
  "rank": 1, "symbol": "ETH-USD", "direction": "long_strike_short_other",
  "long_venue": "strike", "short_venue": "hyperliquid",
  "net_usd": 29.51,
  "net_usd_by_period": {"1d": -14.39, "1w": 29.51, "1m": 197.81},
  "carry_usd_per_hour": 0.3049, "break_even_hours": 71.2, "basis_bps": -12.33,
  "confidence": "High", "stale": false
}
```

Query parameters: `opportunities` takes `min_net_usd`, `notional`, `hours`, `fee_mode=taker|maker`, `direction=long_strike_short_other|long_other_short_strike`, `symbol` and `include_unverified`. `holding-cost` takes `symbol`, `side=long|short`, `notional`, `hours` and `fee_mode`.

**A short Python example for a bot:**

```python
import time
import httpx

BASE = "http://127.0.0.1:8000"  # or your Render URL


def opportunities(**params):
    while True:
        r = httpx.get(f"{BASE}/api/v1/opportunities", params=params, timeout=20)
        if r.status_code == 429:  # inbound limit: honor Retry-After
            time.sleep(int(r.headers.get("Retry-After", "10")))
            continue
        r.raise_for_status()
        return r.json()


data = opportunities(hours=168, notional=10_000, min_net_usd=25)
for o in data["opportunities"]:
    if o["confidence"] == "High" and not o["stale"]:
        print(f'{o["symbol"]}: long {o["long_venue"]} / short {o["short_venue"]} '
              f'est. net ${o["net_usd"]:.2f} over 1w, break-even {o["break_even_hours"]:.0f}h')
```

**Rate limits.** Inbound: about 60 requests per minute per client (first `X-Forwarded-For` entry behind Render's proxy, else the socket address). Over the limit you get `429` with `Retry-After` and the `{error, detail}` body. `/healthz` is exempt. `/api/v1` responses are cached for about 30 seconds, so polling faster gains nothing. CORS is open for GET.

**Upstream limits (we are polite).** One token-bucket limiter per venue, shared by the poller and the history backfill, defaults to at most 50% of each published limit: Hyperliquid 1200 weight per minute per IP (`l2Book` weight 2, other info calls 20 plus 1 per 20 rows returned), OKX per endpoint (funding-rate-history is among the strictest at 10 requests per 2 seconds), and Strike's `rateLimits` from `exchangeInfo` (2400 weight per minute). Strike sends no usage headers and does not publish per-endpoint weights, so we assume conventional ones (`premiumIndex` 10, `ticker/24hr` 40, `depth` 5); any usage header it ever sends is recorded in `/healthz`. Batch endpoints are used wherever they exist. On `429` or `418` we honor `Retry-After`, otherwise back off exponentially with jitter, and mark that venue degraded in `/healthz`. The live poller has priority over the backfill. Per-venue upstream request counts per minute are in `/healthz`.

## How existing tools compare

As of 2026-10-21, from their public pages (check them directly; they change):

- **[Octav Perps](https://octav.fi/blog/perp-funding-rates-dashboard.md)** (post dated 2026-01-23) is a free funding-rate dashboard across Apex, Aster, Binance, Bybit, Hyperliquid, Lighter, OKX, Pacifica and Paradex. Strike is not listed. It ranks symbols by funding spread; the post does not describe fee- or slippage-adjusted figures, and the API it points to is for portfolio and funding position data, not a perps opportunities API.
- **[Otomato](https://otomato.xyz/tools/funding-rate-arbitrage)** offers a funding-rate scanner (Hyperliquid, Binance and Bybit per its listing) with delta-neutral spread ranking and alerts. Strike is not listed. We could not load the page directly (it returned 403 to our fetch), so this comes from its search listing.

Strike Fast adds what a Strike holder needs: Strike coverage, results after fees and slippage at your size, an after-cost ranking exposed as an API, and honest per-row confidence.

## Run locally

Python 3.12.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Quotes arrive within seconds, order books fill in over the first 60 second cycle, and the 30 day history backfills in the background in about 3 minutes (`/report` shows "history loading" until it is done). Data is cached in SQLite (`strikefast.db`, or `DB_PATH`), and the backfill is resumable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `STRIKE_REFERRAL_CODE` | none | If set, "Trade on Strike" links use `https://v2.strikefinance.org/referral/accept?code=CODE`, else they link to Strike without a code. |
| `ENABLED_VENUES` | `strike,hyperliquid,okx` | Venues to poll. |
| `DEFAULT_NOTIONAL` | `10000` | Position size in USD. |
| `DEFAULT_HOURS` | `24` | API default holding period. |
| `HOME_DEFAULT_HOURS` | `168` | Dashboard default holding period (1 week). |
| `HL_HIP3_FEE_MULTIPLIER` | `1.0` | Scales Hyperliquid fees on HIP-3 markets. |
| `REPORT_SLIPPAGE_BPS_PER_FILL` | `2.0` | Report slippage floor per fill. |
| `DB_PATH` | `strikefast.db` | SQLite file. |

## Deploy to Render (free tier)

1. Push the repo and create a **Blueprint** from `render.yaml` (a free Python web service), or create a web service by hand with build command `pip install -r requirements.txt` and start command:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
2. Choose the branch to deploy (for example `mvp`), set `STRIKE_REFERRAL_CODE` if you have one (never commit it), and keep `/healthz` as the health check.

**Free tier behavior to plan for:**

- The service **sleeps after about 15 minutes idle** and wakes on the next request.
- Its **disk resets on restart**, so the SQLite cache is lost. On a cold start the app re-backfills 30 days of history in **about 3 minutes**, and `/report` shows "history loading" until then. The live dashboard works from live data in the meantime.
- **Open the site 5 or more minutes before a demo** so the poller has a few cycles and the history has loaded.

## Tests and tooling

```bash
pip install -r requirements-dev.txt
ruff check .
pytest                 # unit, API, rate-limit and UI tests on saved real fixtures
pytest --live          # one live smoke test against the real APIs (skipped by default)
python scripts/check_layout.py http://127.0.0.1:8000 docs/screenshots   # Playwright, 375px and 1280px
python scripts/capture_examples.py http://127.0.0.1:8000                # refresh /llms.txt examples
```

The calculation tests include hand-worked dollar scenarios, funding sign conventions, 1h versus 8h intervals, missing versus zero funding, OKX contract-size conversion, pagination, stale exclusion and a regression test on a real ETH and SOL hour. The Playwright check loads every page at 375px and 1280px, asserts no horizontal overflow and that charts render, and saves screenshots.

## Restricted jurisdictions and disclaimer

Strike is not available to persons in restricted jurisdictions. See https://www.strikefinance.org/restricted

**Estimates only, not financial advice. Funding, prices and liquidity change; results are not guaranteed.** Strike Fast is not affiliated with Strike, Hyperliquid or OKX.

## License

MIT, see [LICENSE](LICENSE). Credit: **topunix**.
