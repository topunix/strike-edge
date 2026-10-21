# Strike Edge: 2 minute demo script

Tagline: **Find the better trade. Strike fast.**

Goal: a judge understands the tool in 10 seconds and trusts it by the end of 2 minutes. The honest framing is the selling point: estimates after fees, funding and slippage.

## Before you start (5 minutes ahead)

- Open the live site **at least 5 minutes before** the demo. On Render's free tier the service sleeps after about 15 idle minutes and its disk resets, so a cold start needs a few poll cycles and about 3 minutes to re-backfill 30 days of history.
- Check `/healthz`: all three venues should say `ok` and `history_loading` should be `false`. If `/report` still says "history loading", give it a minute and refresh.
- Have three tabs ready: the home page, `/report`, and `/docs`. Optionally a terminal with one `curl` command ready (below).
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

Click **Cheapest to hold**, then toggle **Long** and **Short**. "If I just want to hold one position, where is it cheapest? Same cost model. For longs the best venue is often Strike; flip to shorts and it changes. Each row shows best venue, worst venue and the estimated difference."

**1:30 to 1:50, the report**

Open `/report`. Read the first key finding: "Strike's ETH, SOL, XRP and PUMP perps traded below their index price in most hours, so longs were cheaper to hold on Strike and shorts were more expensive." Then: "And it is balanced for BTC, and reversed for ADA. This box is generated from the data." Scroll to the charts, pick **ETH**: "Top chart is hourly funding on all three venues. Bottom chart is cumulative estimated net on $10,000 for each direction, starting below zero by the cost of four fills, with the zero line marked." Point at the caveat: "A discount to index can change quickly, and past funding does not predict future funding."

**1:50 to 2:00, bots and agents**

Switch to `/docs`: "Everything on this page is also a versioned JSON API with explicit units, source timestamps, stale flags and the assumptions used." Open `/llms.txt`: "And this file lets an AI agent discover and use the API, with real example responses."

Optional terminal line:

```bash
curl -s "https://YOUR-APP.onrender.com/api/v1/opportunities?hours=168&min_net_usd=25" | head -c 600
```

Close: "Estimates only, not financial advice. The point is that Strike's competitiveness is now measurable. Find the better trade. Strike fast."

## Mobile moment (10 seconds)

On a phone, open the home page. Each row is a card: market, strategy, 1 day, 1 week and 1 month estimates, break-even, confidence, and a big **Trade on Strike** button. Everything else is inside "Details and assumptions".

## Likely questions

- **Why are Strike longs cheaper on ETH and SOL?** Strike's funding follows the gap between its perp price and its index price. Its perp traded below index in most hours, so its funding was negative: longs were paid or paid less. Hyperliquid's rate sits near its +0.00125% per hour floor. This can reverse, and the discount also affects entry and exit prices.
- **Is this profit or arbitrage?** No. They are estimates after modeled costs, assuming current funding persists.
- **Where do fees come from?** Strike 0.050% taker and -0.005% maker rebate (base tier, from Strike's fee docs); Hyperliquid 0.045% and 0.015% from its docs; OKX 0.050% and 0.020% for the regular tier. They are configurable.
- **What does "HL fee unverified" mean?** Hyperliquid's HIP-3 markets (stocks, gold, WTI) carry a deployer fee its API does not expose. We use the base fee and lower the confidence one level.
- **What are the "ticker match only" markets?** CXMT, DRAM, MINIMAX, SPCX, UNITREE and ZHIPU share a ticker across venues but we cannot verify the contracts are the same, so they are Low confidence and not ranked by default.
- **Why is a row greyed or missing?** Stale data (older than 5 minutes), a book too thin for the size, or missing funding data. Missing is never shown as zero.
- **What if the site just woke up?** Free hosting sleeps. History needs about 3 minutes to reload; the live numbers work immediately.
