# Liquidity quality: data sources (Phase 1 findings)

Status: discovery only. No feature code written. All endpoints below were called live on 2026-10-29 (HTTP 200, no auth, no API key). Documentation: https://docs.strikefinance.org/llms.txt (pages: Platform Stats, Vaults, Credit, Borrow and Earn, SLP Vault).

## Summary

All four metrics are available as aggregates from Strike's own public API. No on-chain indexing of individual positions is needed, on either chain.

| Metric | Endpoint | Aggregate? | History? |
|---|---|---|---|
| Borrow & Earn debt, by collateral | `GET /v2/credit/collateral-assets` | Yes | No, current only. Build the series from our own snapshots |
| SLP TVL | `GET /v2/vaults?type=protocol` (`equity`) or `GET /v2/vault/{id}/portfolio` (`total_value_locked`) | Yes | Yes, hourly since 2026-04-05 |
| SLP APR | same two endpoints (`apr`) | Yes | Derivable from the hourly share price series |
| Perps volume 24h | `GET /price/v2/ticker/24hr` (already fetched) | Yes | Daily history by symbol from `/stat/v1/dashboard/volumes` |

Base URL for all: `https://api.strikefinance.org` (same host as the existing adapter). SLP vault id: `3fd3ab0b-318b-4f99-9ad6-66e9e917bea8` (also the only entry in `approved_vaults` from `/v2/credit/settings`, and the only vault of `type: "protocol"`).

Current reading (2026-10-29 ~12:40 UTC): debt 406,314 USD, SLP equity 930,512 USD, so borrowed share is about 43.7%. Cross-check: shares encumbered by credit lines (352,045) over SLP total shares (798,333) is 44.1%, which agrees. SLP APR 25.15% (30d), 24h volume about 10.0M USD, so volume_to_tvl is about 10.8.

## How the repo fetches Strike data today

- `app/adapters/strike.py`: `StrikeAdapter` over `UpstreamClient` (token bucket, retries, degraded tracking), base `https://api.strikefinance.org`, public, no auth. Endpoints in use: `/price/v2/premiumIndex`, `/price/v2/ticker/24hr`, `/price/v2/exchangeInfo`, `/price/v2/depth`, `/stat/v1/stats/coin/history/funding`.
- Poller (`app/poller.py`): 60 second cycle, per-venue failure isolation, one compressed snapshot per cycle in SQLite `snapshots` (7 day retention, `app/db.py`). The payload holds quotes and ranked opportunities only.
- Output: JSON API in `app/api.py`, schemas in `app/schemas.py`, dashboard in `app/web.py` and templates, MCP tools in `app/mcp/__init__.py` (`get_opportunities`, `get_holding_cost`, `get_report`, `get_switch_estimate`). `get_liquidity_quality` would follow the same `_call(api.<fn>_data, ctx, ...)` pattern.
- No existing code reads Cardano or Ethereum, and nothing in the repo mentions SLP, credit, TVL or vaults.
- Storage gap: snapshots are kept 7 days, so a 30 day series needs its own table with longer retention.

## a. Borrow & Earn debt, by collateral

- Source: `GET /v2/credit/collateral-assets` (docs: api/credit/collateral).
- Aggregate: yes. Docs: "protocol-wide totals across all open Credit Lines", refreshed about every 5 seconds. `total_debt_usd` per collateral asset, plus `open_lines`, `total_collateral_units`, `price`, `price_fresh`, and `encumbered_shares_by_vault`.
- Auth: none. Update frequency: about 5 seconds server side. History: none; current state only.
- Collateral ids: `ADA`, `sUSDrf`, `LP_SUNDAE_USDCx_USDM`, `SCALED_AAVE_V3_ETH_aUSDC`, `enzoBTC`.
- Related: `GET /v2/credit/settings` (risk parameters, `custody_asset_mappings` for cardano and ethereum, `approved_vaults`).

Example (trimmed, live):

```json
{"effective_seq":1629710552,"assets":{
 "ADA":{"total_debt_usd":"64997.61922917","open_lines":42,"total_collateral_units":"315440.467841","price":"0.2009530965555555556","price_fresh":true,"encumbered_shares_by_vault":{"3fd3ab0b-318b-4f99-9ad6-66e9e917bea8":"56964.86142424"}},
 "sUSDrf":{"total_debt_usd":"304546.70983749","open_lines":39},
 "LP_SUNDAE_USDCx_USDM":{"total_debt_usd":"34042.30700271640533","open_lines":5},
 "SCALED_AAVE_V3_ETH_aUSDC":{"total_debt_usd":"2627.35521577","open_lines":3},
 "enzoBTC":{"total_debt_usd":"100","open_lines":1,"total_collateral_units":"0.00125337"}}}
```

Live total: 406,313.99 USD across 90 open lines. Notes:

- `enzoBTC` is currently 1 line and 100 USD of debt, under 0.03% of the total. The signal today is driven by sUSDrf (75%) and ADA (16%).
- sUSDrf (RealFi staked USDrf) is by far the largest collateral. This matters for the fragility story: it is a Cardano-native yield token, not an external-chain asset.
- Not documented: whether `total_debt_usd` is principal only or includes accrued amounts. Borrowing is 0% interest, so principal is the reasonable reading; confirm with Strike.
- Because there is no history endpoint, `borrowed_share` has no backfill. The 7d trend arrow and 7d/30d series only exist after we have run for 7 and 30 days. Phase 2 must show "n/a" for trend until then.

## b. SLP vault TVL

- Source A: `GET /v2/vaults?type=protocol` returns the SLP vault with `equity` (USD, 930,511.84), `wallet_balance`, `total_shares`, `share_price`, `depositors`, `apr`, `return_24h`.
- Source B: `GET /v2/vault/{id}/portfolio?period=...` returns `total_value_locked`, `apr`, `volume` (period volume), `all_time_volume`, `depositors`, and `history`.
- History row format: `[ts_ms, tvl, pnl, fees_earned, depositors, share_price]` (docs list five columns; the live response has a sixth, share price). Granularity depends on `period`: `7d` and `30d` are hourly (with occasional gaps of a few hours), `6m` is daily at UTC midnight, `all` mixes both (818 rows back to 2026-04-05). Period enum: `24h, 7d, 30d, 6m, 1y, all`.
- Aggregate: yes. Auth: none. Per-vault: yes (`/v2/vaults` lists all vaults; only SLP is `type: "protocol"` and credit-eligible).
- Note: `equity` (/v2/vaults) and `total_value_locked` (portfolio) differ slightly at the same moment (930,512 vs 930,321) because they are computed at different times. Pick one source and use it consistently. Recommendation: use the portfolio `total_value_locked`, since it is the one with a history series.
- Do not use DefiLlama for this: its "Strike Finance Perpetuals" TVL is a different number (about 1.9M, includes treasury and other assets) and has no SLP breakdown.

Example (trimmed, live, `/v2/vaults?type=protocol`):

```json
{"vault_id":"3fd3ab0b-318b-4f99-9ad6-66e9e917bea8","type":"protocol","name":"Strike Liquidity Provider (SLP)","total_shares":"798333.00790044","share_price":"1.165568537348413469","equity":"930511.8363354752653","apr":"25.1531","return_24h":"1.0744","depositors":243,"leader_commission_bps":0}
```

## c. SLP vault APR

- Source: `apr` field on `/v2/vaults` and `/v2/vault/{id}/portfolio`. Annualised percentage rate in percent (25.15 means 25.15%). Docs: "Annualised percentage rate (%)".
- Window: governed by the `period` parameter (default 30d, with `period_start_share_price` and `period_start_timestamp` returned). Live check on `/v2/vaults?type=protocol`: `period=7d` and `period=30d` both returned `apr` 25.5049 while `period_return` differed (1.42% vs 2.64%), so the `apr` field does not appear to change with the period and should not be assumed to be period-specific. (The default call a few minutes earlier showed 25.1531, so it also moves over time.) Check this with Strike before relying on `apr` for a 7d vs 30d comparison.
- Historical: derive from the `share_price` column in the portfolio history (return between two timestamps, annualised). This avoids depending on the `apr` field's definition and gives a series back to April.
- The displayed `vault_apr` is Strike's published `apr` (sampled each cycle, so its history starts at first run). Our own figure is exposed as the secondary `derived_apr`: simple annualization over a 30 day window (`LQ_APR_WINDOW_DAYS`) of the share price series, which also exists for the backfilled period. Cross-check on 2026-10-29: derived 37.5% against Strike's published 25.5%. Strike's formula is not documented and cannot be reproduced from `period_return` (2.73% over 30 days is about 33% simple). The hourly share price also differs slightly from `period_start_share_price` (1.1323 against 1.1354 at the period start), so single-endpoint noise of a few tenths of a percent moves a 30 day APR by several points. A 7 day window gave about 61%, which is why 30 days is the default.
- Aggregate, no auth, same cadence as TVL.
- Other APRs in the API (`yield_apr` per collateral on `/v2/credit/collateral-assets`) are the collateral's own yield, not the vault APR.

## d. Strike perps volume

- 24h, now: `GET /price/v2/ticker/24hr` `quoteVolume` per symbol, summed. Already fetched each cycle as `Quote.volume_24h_usd` (`app/adapters/strike.py`). Live: 34 symbols, 10.04M USD.
- Over time: `GET /stat/v1/dashboard/volumes` returns `{"total_volume_daily": [[day_start_ms, "BTC-USD", usd], ...]}`, daily by symbol, from 2026-03-20, single-sided (each match counted once). Today's value is partial and updates every 10 minutes. Cached server side 3 minutes. Live check: 2026-10-28 total 9.05M, consistent with the 24h ticker.
- `GET /stat/v1/dashboard/summary`: all-time totals (`total_volume_usd` 809.3M, `total_users` 6412). `open_interest_usd` is documented as always "0"; do not use it.
- Aggregate, no auth.
- Consistency caution: the vault portfolio `volume` field (30d: 191.6M for the SLP vault's own trading, `all_time_volume` 664.3M) is the vault's volume, not platform volume. Use the ticker or dashboard volume for `volume_to_tvl`, and document the choice.
- Open question: `ticker/24hr` is a rolling 24h. The dashboard series is per UTC day. Use the ticker for the live ratio and the dashboard series only for backfill.

Example (live, trimmed):

```json
{"total_volume_daily":[[1773964800000,"BTC-USD",25274.505618],[1774051200000,"BTC-USD",40224.329879]]}
```

## Other observations

- `GET /stat/v1/dashboard/treasury` lists treasury holdings by asset (USDC, USDM, ADA, sUSDrf, Sundae LP, ...). It is the protocol treasury, not vault TVL, and not needed here.
- Docs state borrowed credit goes directly into SLP and buys SLP shares, and SLP is the only vault eligible for credit draws. So debt over SLP TVL is a well-defined share of the vault, and the shares cross-check above independently confirms it.
- Rate limits: `exchangeInfo` publishes 2400 weight per minute. Weights for the `/v2` and `/stat` endpoints are not published. The existing assumed-weight approach in `app/adapters/strike.py` applies. These calls need only be made once per refresh cycle (or less often; see below).

## Recommended Phase 2 sampling

The existing poller runs every 60 seconds. Debt, TVL and APR move slowly, so one fetch per 5 to 15 minutes would be enough and cheaper, but the request said to follow the existing refresh schedule. Proposed: add the three calls (`collateral-assets`, `vaults?type=protocol`, plus the existing ticker) to the poll cycle with failure isolation like the quote fetch, and store one row per cycle in a new `liquidity_quality` table with its own retention (at least 35 days).

Historical bootstrapping for Phase 2 (optional): TVL and share price back to April via `portfolio?period=all` and daily volume via `dashboard/volumes`. Debt cannot be backfilled, so any 7d/30d `borrowed_share` series starts from first run.

## Risks and open questions for Strike

1. Is `total_debt_usd` principal only, and is it USD at borrow time or marked to market?
2. Is `apr` on the vault endpoints period-specific? The live values suggest not.
3. Are `/v2/credit/*` and `/v2/vaults` stable and intended for third-party use (rate limits, SLA)? The docs list them as public, but they sit in the v2 API, not the `price/v2` market data set the repo uses today.
4. Debt is concentrated in two collateral types (sUSDrf and ADA). Collateral-specific liquidation risk would be a separate signal and is out of scope.

## Effort

Because aggregates exist, no indexer is required. Estimated Phase 2: adapter and parser (about 0.5 day), storage and sampling (0.5 day), flag logic and config (0.5 day), API, MCP tool and dashboard (1 day), tests with fixtures (1 day). About 3 to 4 days in total.

## Phase 2 implementation notes

- Sampling: each 60 second poll cycle stores one `liquidity_samples` row (SLP equity as TVL, share price, debt per collateral asset, summed Strike 24h ticker volume). A failed half leaves NULLs, never a partial ratio. Rows are kept 75 days; reads thin rows older than 7 days to one per hour.
- Backfill (startup, then every 6 hours when it last succeeded): hourly `portfolio?period=30d` and daily `period=6m` for TVL and share price, and `dashboard/volumes` for daily volume, 70 days. Daily volume for day D is stored on the UTC midnight that ends D (the trailing 24h at that moment). Backfill only fills NULL columns and never overwrites live samples. Debt is not backfilled.
- Definitions: TVL is vault `equity` (equal to `total_shares` times `share_price`). `volume_to_tvl` uses the ticker volume live and the dashboard daily volume in the backfilled region.
- Persistence caveat: debt history exists only from first run. On a host whose disk resets on restart (the Render free tier in `render.yaml`), the 7 day debt window can never complete unless the service stays up and its disk persists for 7 days. The debt trend and the flag stay n/a until it does. A persistent disk or external database is needed for the flag to work in production.
