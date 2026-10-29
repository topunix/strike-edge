# Liquidity quality: data sources (Phase 1 findings)

Status: discovery only. No feature code written. Date: 2026-10-29.

## Read this first: what could and could not be verified

This session's network policy blocks `api.strikefinance.org`, `docs.strikefinance.org` and `defillama.com` / `api.llama.fi` (proxy answered 403 on CONNECT; fetch tools could not resolve the hosts). So no live Strike endpoint, docs page or network call from the Strike app could be inspected. Nothing below is presented as a verified response unless marked VERIFIED. Where an example response would be needed for an unverified source, none is given; inventing one would be misleading.

Status labels: **VERIFIED** (in this repo, with a recorded fixture), **REPORTED** (seen only in search snippets from third parties, not fetched), **UNKNOWN** (must be checked once the hosts are reachable).

## How the repo fetches Strike data today

- `app/adapters/strike.py`: `StrikeAdapter` over `UpstreamClient` (token bucket, retries, degraded tracking). Base `https://api.strikefinance.org`. Public, no auth.
- Endpoints in use: `/price/v2/premiumIndex`, `/price/v2/ticker/24hr`, `/price/v2/exchangeInfo`, `/price/v2/depth`, `/stat/v1/stats/coin/history/funding`.
- Poller (`app/poller.py`): 60 second cycle, per-venue failure isolation. Writes one compressed snapshot per cycle to SQLite `snapshots` (7 day retention, `app/db.py`). Snapshot payload holds quotes and ranked opportunities only.
- Output: JSON API in `app/api.py`, schemas in `app/schemas.py`, dashboard in `app/web.py` and templates, MCP tools in `app/mcp/__init__.py` (`get_opportunities`, `get_holding_cost`, `get_report`, `get_switch_estimate`). `get_liquidity_quality` would be a fifth tool following the same `_call(api.<fn>_data, ctx, ...)` pattern.
- No existing code reads Cardano or Ethereum, and nothing in the repo mentions SLP, Borrow & Earn, TVL or vaults.
- Storage gap: snapshots are retained 7 days. A 30 day liquidity series needs its own table with longer retention (Phase 2 design point).

## Metric by metric

### d. Strike perps 24h volume (and over time)

| Item | Detail |
|---|---|
| Source | `GET /price/v2/ticker/24hr` (no symbol), field `quoteVolume` per symbol |
| Status | **VERIFIED**, already fetched every cycle and stored as `Quote.volume_24h_usd` |
| Aggregate? | Per symbol; platform total is the sum over 31 symbols. Aggregate, no indexing |
| Auth | None |
| Frequency | Rolling 24h window, refreshed each poll (60s) |
| Over time | Strike offers no history from this endpoint. Our own snapshots build the series going forward. Backfill: UNKNOWN (DefiLlama lists 30d perp volume for "Strike Finance Perpetuals" per search snippet, REPORTED, daily granularity if it has a volume adapter) |

Example (from `tests/fixtures/strike_ticker24hr.json`, one element, trimmed):

```json
{"symbol":"WTI-USD","lastPrice":"97.143","volume":"766.39","quoteVolume":"75343.24018","closeTime":1792596522008,"count":0}
```

Summing `quoteVolume` over the 31 fixture symbols gives about 7.60M USD. Caveat: `count` is 0 in the fixture, so confirm `quoteVolume` is real traded USD notional and not derived (fixture `lastQty` is also 0).

### b. SLP vault TVL (total and per vault)

| Item | Detail |
|---|---|
| Repo | Not used anywhere |
| Strike public API/docs | UNKNOWN (blocked). The `/stat/v1/...` namespace already hosts `stats/coin/history/funding`, so a sibling stats route for vaults is plausible, but unconfirmed |
| Third party | DefiLlama lists "Strike Finance Perpetuals" at about 1.27M USD TVL on Cardano (REPORTED, snippet). Earlier snapshots differ widely (15.1M to 15.6M, including staking). It is not stated whether DefiLlama's number includes SLP vault deposits; it appears to be the perps product |
| Press | Vault sizes of ~0.2M to >1M USD quoted in news (REPORTED, dated and inconsistent, not usable as a data source) |
| On-chain | Cardano vault contract UTxOs / receipt token supply. Needs script addresses and datum layout from Strike (UNKNOWN) |
| Aggregate? | A vault is one script, so total vault holdings is a small number of address queries if vault state is held in a few UTxOs. Per-user deposits are not needed for TVL |
| Auth | Cardano indexers (Blockfrost, Koios, Kupo) free tier or key required |

### c. SLP vault APR (current and historical)

| Item | Detail |
|---|---|
| Strike public API | UNKNOWN (blocked) |
| UI | Strike app shows APRs; press quotes ~37% to ~49% for various vaults (REPORTED, promotional, variable) |
| Third party | A DefiLlama Yields pool page exists for a Strike pool (REPORTED in search; not fetched, so not confirmed to be an SLP vault). The Yields API (`yields.llama.fi/chart/<pool>`) would give daily APY history if the pool is the right one. UNKNOWN |
| On-chain | Derivable from vault share-price growth over time (needs historical datum/UTxO reads) |
| Aggregate? | Aggregate per vault |
| Frequency | Unknown; Yields data is typically hourly/daily |

### a. Total outstanding Borrow & Earn debt (aggregate USD borrowed, by collateral)

| Item | Detail |
|---|---|
| Repo | Not used |
| Strike public API | UNKNOWN (blocked). This is the critical metric and no public source for it was found in any reachable material |
| DefiLlama | A borrowed-TVL series exists as a concept in their schema, but nothing found shows Strike reporting it. UNKNOWN |
| On-chain, Cardano | Each loan is likely its own position UTxO at the loan script (datum: collateral, debt). Aggregate debt then means summing every open position UTxO, i.e. a position-level scan of one script address. If the protocol also keeps a global state/accumulator UTxO, one read suffices (UNKNOWN) |
| On-chain, Ethereum | enzoBTC collateral is held on Ethereum. Collateral balance in Strike's custody/bridge contract is one `balanceOf` call, but that gives collateral, not debt. Debt is recorded on Cardano |
| Aggregate? | Unknown. Worst case requires indexing individual positions |
| Auth | Indexer key (Blockfrost/Koios); Ethereum RPC for collateral |

## Blockers and what is needed to proceed

1. Allow `api.strikefinance.org`, `docs.strikefinance.org`, `v2.strikefinance.org`, `defillama.com`, `api.llama.fi`, `yields.llama.fi` in the session network settings, or paste Strike docs / HAR output from the Strike app (borrow, earn and vault pages).
2. Ask Strike whether a stats endpoint for Borrow & Earn debt and SLP TVL/APR exists. The explicit 0% interest model and the motive for this feature make an official aggregate plausible.
3. Cardano script addresses for the SLP vault and loan contracts, and the Ethereum custody contract address (from Strike docs or an explorer).

## Effort estimate if debt is only available per position

Assumes a Cardano indexer account and Strike script addresses and datum schemas are available.

| Piece | Estimate |
|---|---|
| Cardano loan script scan (list UTxOs at the script address, decode datum, sum debt by collateral) | 2 to 4 days; a single cycle read is cheap if the open position count is in the low thousands |
| SLP vault TVL read (vault UTxO / share supply) | 0.5 to 1 day |
| Ethereum enzoBTC collateral cross-check (optional) | 0.5 to 1 day |
| Price conversion to USD for non-stable collateral (only if datum stores debt in collateral units) | 0.5 day |
| Caching, rate limits, failure isolation in the existing adapter style, fixtures and tests | 1 to 2 days |
| History | Positions have no history through current state. Building a time series means snapshotting from now on, or replaying chain history (add 3 to 5 days) |

Total: about 1 to 2 weeks for a Cardano-only indexer, +1 day for the Ethereum cross-check. Debt and TVL need only Cardano reads; Ethereum is not required for `borrowed_share` itself.

If Strike or DefiLlama exposes aggregates, effort drops to about 1 day for an adapter plus the Phase 2 work.

## Recommendation

Do not build an indexer yet. Next step: unblock network access or supply Strike's docs / app network calls, then re-run discovery. Volume is ready now; vault TVL, APR and debt are the open items.
