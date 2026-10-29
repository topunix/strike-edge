"""MCP server: read-only tools over the same logic as the /api/v1 endpoints.

Remote: mounted at /mcp on the FastAPI app (Streamable HTTP, stateless JSON).
Local:  python -m app.mcp  (stdio, runs its own live poller).
No auth and no write actions. The inbound rate limit applies to /mcp like any other path.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.exceptions import HTTPException

from app import api
from app.config import TAGLINE
from app.schemas import (
    HoldingCostResponse,
    LiquidityQualityResponse,
    OpportunitiesResponse,
    ReportResponse,
    SwitchResponse,
)

RESTRICTED_URL = "https://www.strikefinance.org/restricted"

# Appended to every tool description so an agent always sees units, caveats and the notice.
NOTICE = (
    "UNITS: *_usd is US dollars, *_bps is basis points, *_rate_per_hour is a fraction per "
    "hour (0.0001 = 0.01% per hour), *_hours is hours, *_ms is UTC epoch milliseconds, *_iso "
    "is ISO 8601 UTC. Funding: a positive rate means longs pay shorts. null means missing "
    "data, never 0. "
    "ESTIMATES ONLY: figures are modeled after fees and slippage and assume current funding "
    "persists; they are not profit, not guaranteed and not financial advice. Funding, basis "
    "and liquidity change; past funding does not predict future funding. "
    "FRESHNESS: every venue has source timestamps in the response; data older than 5 minutes "
    "is stale (stale=true) and is excluded from rankings; poll cycle is 60 seconds. "
    f"Strike is not available to persons in restricted jurisdictions. See {RESTRICTED_URL}"
)

NotionalArg = Annotated[
    float | None,
    Field(gt=0, le=100_000_000, description="Position size in USD (default 10000)."),
]
HoursArg = Annotated[
    float | None,
    Field(
        gt=0,
        le=8760,
        description="Holding period in hours (default 24; 168 = 1 week, 720 = 1 month).",
    ),
]
FeeModeArg = Annotated[
    Literal["taker", "maker"],
    Field(description="taker (market orders, default) or maker ('if filled as maker')."),
]
SymbolArg = Annotated[str, Field(description="Strike market, e.g. ETH-USD or ETH.")]

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _call(fn, *args):
    """Run a shared v1 function, turning its HTTP errors into tool errors with the same text.

    ToolError is the SDK's "anticipated failure": its message reaches the client (any other
    exception is treated as a crash and reported generically).
    """
    try:
        return fn(*args)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc


def build_mcp_server(ctx: api.Context) -> MCPServer:
    server = MCPServer(
        "strike-edge",
        title="Strike Edge",
        description=(
            f"{TAGLINE} Funding, fees and slippage compared across Strike, Hyperliquid and OKX."
        ),
        instructions=(
            "Strike Edge estimates what perpetual futures positions cost or earn on Strike "
            "versus Hyperliquid and OKX, after fees, funding and slippage. All tools are "
            "read-only. " + NOTICE
        ),
        version="1.0.0",
    )

    @server.tool(
        name="get_opportunities",
        title="Two-leg funding opportunities",
        annotations=READ_ONLY,
        description=(
            "Rank two-leg funding trades (long one venue, short another, equal notional; one "
            "leg is always Strike) by estimated net USD after funding on both legs, fees on "
            "four fills and slippage on four fills. Never ranked by raw spread. Each row has "
            "net_usd over `hours`, net_usd_by_period (1d, 1w, 1m), break_even_hours, basis_bps, "
            "a confidence level with reasons, stale flags and cost line items. Rows with "
            "insufficient liquidity, stale data or ticker-only matches are in `unranked` "
            "(ticker-only matches are ranked only with include_unverified=true). " + NOTICE
        ),
    )
    def get_opportunities(
        min_net_usd: Annotated[
            float | None, Field(description="Only ranked rows with net_usd >= this (USD).")
        ] = None,
        notional: NotionalArg = None,
        hours: HoursArg = None,
        fee_mode: FeeModeArg = "taker",
        direction: Annotated[
            Literal["long_strike_short_other", "long_other_short_strike"] | None,
            Field(description="Restrict to one direction. Omit for both."),
        ] = None,
        symbol: Annotated[
            str | None, Field(description="Restrict to one market, e.g. BTC-USD or BTC.")
        ] = None,
        include_unverified: Annotated[
            bool, Field(description="Also rank ticker-only matches (always Low confidence).")
        ] = False,
    ) -> OpportunitiesResponse:
        return _call(
            api.opportunities_data,
            ctx,
            min_net_usd,
            notional,
            hours,
            fee_mode,
            direction,
            symbol,
            include_unverified,
        )

    @server.tool(
        name="get_holding_cost",
        title="Cheapest venue to hold one position",
        annotations=READ_ONLY,
        description=(
            "Where is it cheapest to hold ONE long or short position? Per venue: entry and "
            "exit fees, slippage from walking the live order book at your size, and funding "
            "over the holding period, with the best venue, the worst venue and the USD gap "
            "(best_vs_worst_gap_usd). Negative total_cost_usd means the position is paid. " + NOTICE
        ),
    )
    def get_holding_cost(
        symbol: SymbolArg,
        side: Annotated[Literal["long", "short"], Field(description="long or short.")],
        notional: NotionalArg = None,
        hours: HoursArg = None,
        fee_mode: FeeModeArg = "taker",
    ) -> HoldingCostResponse:
        return _call(api.holding_cost_data, ctx, symbol, side, notional, hours, fee_mode)

    @server.tool(
        name="get_report",
        title="30-day historical report",
        annotations=READ_ONLY,
        description=(
            "30-day funding report on a $10,000 position with taker fees: generated key "
            "findings and a headline, per-market share of hours the two-leg carry was "
            "positive, days to cover round-trip fees, cumulative estimated net over 7 and 30 "
            "days, longest favorable stretch, and the share of hours Strike was the cheaper "
            "venue for longs and shorts. Pass `symbol` to return one market's table (summary "
            "and findings stay global). history_loading=true means the cache is still "
            "filling after a restart (about 3 minutes). " + NOTICE
        ),
    )
    def get_report(
        symbol: Annotated[
            str | None, Field(description="Optional market, e.g. ETH-USD or ETH.")
        ] = None,
    ) -> ReportResponse:
        return _call(api.report_data, ctx, symbol)

    @server.tool(
        name="get_switch_estimate",
        title="Should I move my position to Strike?",
        annotations=READ_ONLY,
        description=(
            "You already hold a long or short on Hyperliquid or OKX. Compare KEEPING it there "
            "with CLOSING it and reopening on Strike, over `hours` (default 336 = 2 weeks). "
            "Stay = funding on your current venue plus its exit fee and slippage at the end "
            "(no entry fee, the position is open). Switch = close on the current venue now, "
            "open on Strike now, funding on Strike, close on Strike at the end. Returns "
            "stay_cost_usd, switch_cost_usd, estimated_saving_usd (stay minus switch; negative "
            "means staying is cheaper), switching_cost_upfront_usd, break_even_hours (only when "
            "Strike's hourly funding advantage is positive), a plain-language summary, "
            "confidence with reasons, stale flags, line items, and "
            "strike_cheaper_share_30d (fraction of the last 30 days' hours Strike was cheaper "
            "for this side, funding only). " + NOTICE
        ),
    )
    def get_switch_estimate(
        symbol: SymbolArg,
        side: Annotated[
            Literal["long", "short"], Field(description="The side you hold: long or short.")
        ],
        from_venue: Annotated[
            Literal["hyperliquid", "okx"],
            Field(description="Venue the position is on today: hyperliquid or okx."),
        ],
        notional: NotionalArg = None,
        hours: Annotated[
            float | None,
            Field(
                gt=0,
                le=8760,
                description="How long you would keep it, in hours (default 336 = 2 weeks).",
            ),
        ] = None,
        fee_mode: FeeModeArg = "taker",
    ) -> SwitchResponse:
        return _call(api.switch_data, ctx, symbol, side, notional, from_venue, hours, fee_mode)

    @server.tool(
        name="get_liquidity_quality",
        title="Strike liquidity quality",
        annotations=READ_ONLY,
        description=(
            "How much of Strike's SLP vault liquidity is borrowed capital (Borrow & Earn credit "
            "at 0% interest) that can leave quickly. Returns current borrowed_share (debt / SLP "
            "TVL, a fraction: 0.44 = 44%), vault_apr (SLP share price growth over 30 days, "
            "annualized, a fraction) and volume_to_tvl (24h perps volume / SLP TVL), their 7 day "
            "trends, the 7 day (hourly) and 30 day (daily) series, and a flag: fragile (share "
            "high and rising while volume_to_tvl is flat or falling), ok, or insufficient_data. "
            "Any unavailable input is null (n/a) and no ratio is computed from partial data; "
            "debt history only exists since debt_history_started_ms, so debt trends are null "
            "for about the first 7 days. Thresholds are returned in flag.thresholds. " + NOTICE
        ),
    )
    def get_liquidity_quality() -> LiquidityQualityResponse:
        return _call(api.liquidity_quality_data, ctx)

    return server
