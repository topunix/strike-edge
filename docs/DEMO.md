# Strike Edge: 2 minute 20 second demo script

Tagline: **Find the better trade. Strike fast.**

Goal: a judge understands the tool in 10 seconds and trusts it by the end. The run is about 2 minutes 20 seconds, under the 2.5 minute cap. The honest framing is the selling point: estimates after fees, funding and slippage.

## Before you start (5 minutes ahead)

- Open the live site **at least 5 minutes before** the demo. On Render's free tier the service sleeps after about 15 idle minutes and its disk resets, so a cold start needs a few poll cycles and about 3 minutes to re-backfill 30 days of history.
- Check `/healthz`: all three venues should say `ok` and `history_loading` should be `false`. If `/report` still says "history loading", give it a minute and refresh.
- Have three tabs ready: the home page, `/report`, and `/docs`. Optionally a terminal with one `curl` command ready (below).
- **AI assistant (for the last 20 seconds), set up beforehand.** Connect an MCP-capable assistant to `https://strike-edge.onrender.com/mcp` (no login, read-only). For example, in Claude Code: `claude mcp add --transport http strike-edge https://strike-edge.onrender.com/mcp`; in a desktop or web assistant, add a custom connector or remote MCP server with that URL. Ask it one throwaway question first so the connection is warm, then keep the window ready. Run both demo questions once beforehand so you know roughly what the tools return that day (the numbers change; do not memorize them).
- Desktop for the main run, a phone (or a 375px browser window) for the 10 second mobile moment.

## The script

**0:00 to 0:15, the question (home page)**

Say: "After funding, fees, spread and slippage, is there a real reason to trade perps on Strike instead of Hyperliquid or OKX? Strike Edge measures that: honest estimates after fees, funding and slippage."

Point at: the three venue pills under the title. "These are live, refreshed every 60 seconds. If a venue goes stale for 5 minutes it greys out and drops out of the rankings."

**0:15 to 0:30, the headline**

Point at the blue headline box and read it. "This line is generated from 30 days of real funding data, not written by hand." Then read the line under it: "Two-leg trades pay fees up front and recover them through funding over time, so short holds rarely cover costs." This sets up why the period presets matter.

**0:30 to 1:00, the top 5**

Say: "These are the top five verified opportunities for a 1 week hold on $10,000. Long one venue, short another, equal size. Ranked by estimated net after fees and slippage, never by raw spread, and only High and Medium confidence."

Point at, left to right: the strategy, then the 1 day, 1 week and 1 month estimates. "At 1 day most of these are negative because fees are paid up front. At 1 week and 1 month the funding gap pays for them. Break-even tells you how many hours that takes." Point at the confidence badge: "Confidence drops for stale data, thin books, closed stock markets, and anything we could not verify."

**1:00 to 1:15, one expanded cost breakdown**

Click "Details and assumptions" on the first row. Walk down it: "Funding on each leg, shown as the raw rate per interval, then per hour, then dollars over 24 hours on your size. Then every cost line: funding on both legs, fees on four fills, slippage from walking the real order book. Basis is shown, with the caveat that basis and funding can change." Click **Trade on Strike** only if asked (it opens Strike with the referral code if one is configured).

**1:15 to 1:30, direction toggle**

Click **Cheapest to hold**, then toggle **Long** and **Short**. "If I just want to hold one position, where is it cheapest? Same cost model. The best venue can change with the direction and from hour to hour, so this is the current hour. The 30 day pattern is in the report: at the last check (2026-10-22) Strike was cheaper for ETH longs in most hours and costlier for ETH shorts, but look at `/report` and say what it shows today. Each row shows best venue, worst venue and the estimated difference."

**1:30 to 1:50, the report**

Open `/report`. Read the first key finding: "Strike's ETH, SOL, XRP and PUMP perps traded below their index price in most hours, so longs were cheaper to hold on Strike and shorts were more expensive." Then: "And it is balanced for BTC, and reversed for ADA. This box is generated from the data." Scroll to the charts, pick **ETH**: "Top chart is hourly funding on all three venues. Bottom chart is cumulative estimated net on $10,000 for each direction, starting below zero by the cost of four fills, with the zero line marked." Point at the caveat: "A discount to index can change quickly, and past funding does not predict future funding."

**1:50 to 2:00, bots and agents**

Switch to `/docs`: "Everything on this page is also a versioned JSON API with explicit units, source timestamps, stale flags and the assumptions used." Open `/llms.txt`: "And this file lets an AI agent discover and use the API, with real example responses."

Optional terminal line:

```bash
curl -s "https://strike-edge.onrender.com/api/v1/opportunities?hours=168&min_net_usd=25" | head -c 600
```

**2:00 to 2:20, an AI assistant over MCP**

*Main beat, about 12 seconds (2:00 to 2:12), answered by `get_report`.* Switch to the connected AI assistant and ask: "Over the last 30 days, was Strike cheaper than Hyperliquid and OKX for holding ETH longs?" Say: "It is calling the `get_report` tool on our `/mcp` endpoint: the same logic and the same numbers as the report page and the API, with units, freshness and the estimates-only notice attached to every tool."

Narrate around the **percentage of hours**, and read what the tool returns instead of a number from memory. The answer is a share: out of the hours in the last 30 days, the percentage in which Strike was the cheaper venue to hold an ETH long, once against Hyperliquid and once against OKX (the report's summary line reads "Strike was the cheaper venue for X% of hours for ETH longs vs Hyperliquid, Y% vs OKX (funding only)"). Read out the two percentages the assistant reports. Then say, based on what you just read, whether each share is above half ("so for most of the month Strike was the cheaper place to hold an ETH long") or below half ("so for fewer than half of the hours"). Finish with: "That is funding only, over 30 days, and past funding does not predict future funding." If the tool says `history_loading` is true, say the history is still filling after a restart and use the fallback, or ask again in a minute.

*Optional second beat, about 8 seconds (2:12 to 2:20); skip it if you are running long.* Ask: "Where is ETH cheapest to hold long for 1 week at $10k, after fees?" It calls `get_holding_cost`. Say: "Live results change hourly; read out whatever the tool returns, including when another venue is cheaper this hour." Then read the best venue, its estimated cost and the gap to the worst venue as returned. This is one hour's snapshot; the 30 day pattern is the report answer above.

Fallback if the connector is unavailable (assistant not connected, server waking up, or no network): say "The same question is one call" and run this for the main beat, which prints each competitor with the fraction of hours Strike was cheaper for an ETH long (multiply by 100 for the percentage):

```bash
curl -s "https://strike-edge.onrender.com/api/v1/report" | python3 -c "import sys,json; r=json.load(sys.stdin); m=[x for x in r['markets'] if x['symbol']=='ETH-USD'][0]; print([(v['other_venue'], v['long_strike_cheaper_pct']) for v in m['view_a']])"
```

And this for the optional second beat, then point at `best_venue` and `best_vs_worst_gap_usd`:

```bash
curl -s "https://strike-edge.onrender.com/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168&notional=10000" | head -c 700
```

Close (last few seconds): "Estimates only, not financial advice. The point is that Strike's competitiveness is now measurable. Find the better trade. Strike fast."

## Mobile moment (10 seconds)

On a phone, open the home page. Each row is a card: market, strategy, 1 day, 1 week and 1 month estimates, break-even, confidence, and a big **Trade on Strike** button. Everything else is inside "Details and assumptions".

## Likely questions

- **Is the MCP endpoint safe to expose?** It is read-only (three tools, no write actions), needs no login, and is rate limited like the API. It serves the same estimates and carries the same disclaimers.

- **Why does the live answer differ from the 30 day report?** The live tools price one position at this hour with the current funding rate; the report counts how often Strike was cheaper across the last 30 days of hourly funding. A single hour can go the other way without changing the 30 day pattern, and the pattern can change too.

- **Why are Strike longs cheaper on ETH and SOL (over the last 30 days)?** Strike's funding follows the gap between its perp price and its index price. Its perp traded below index in most hours, so its funding was negative: longs were paid or paid less. Hyperliquid's rate sits near its +0.00125% per hour floor. This can reverse, and the discount also affects entry and exit prices.
- **Is this profit or arbitrage?** No. They are estimates after modeled costs, assuming current funding persists.
- **Where do fees come from?** Strike 0.050% taker and -0.005% maker rebate (base tier, from Strike's fee docs); Hyperliquid 0.045% and 0.015% from its docs; OKX 0.050% and 0.020% for the regular tier. They are configurable.
- **What does "HL fee unverified" mean?** Hyperliquid's HIP-3 markets (stocks, gold, WTI) carry a deployer fee its API does not expose. We use the base fee and lower the confidence one level.
- **What are the "ticker match only" markets?** CXMT, DRAM, MINIMAX, SPCX, UNITREE and ZHIPU share a ticker across venues but we cannot verify the contracts are the same, so they are Low confidence and not ranked by default.
- **Why is a row greyed or missing?** Stale data (older than 5 minutes), a book too thin for the size, or missing funding data. Missing is never shown as zero.
- **What if the site just woke up?** Free hosting sleeps. History needs about 3 minutes to reload; the live numbers work immediately.
