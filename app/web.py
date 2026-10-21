"""HTML pages: / (dashboard) and /report. Server-rendered; JS only enhances."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import calc, report
from app.api import Context, ctx_of, iso
from app.config import ALL_VENUES, PRESETS_HOURS, STRIKE
from app.engine import Leg, OpportunityRow

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
router = APIRouter()

VENUE_NAMES = {"strike": "Strike", "hyperliquid": "Hyperliquid", "okx": "OKX"}
REPO_URL = "https://github.com/topunix/strike-fast"
RESTRICTED_URL = "https://www.strikefinance.org/restricted"
TABS = ("opps", "all", "unranked", "hold", "only")
PRESET_LABELS = {"1d": "1 day", "1w": "1 week", "1m": "1 month"}


# --- formatting helpers (exposed to templates) --------------------------------------------
def money(x: float | None, signed: bool = True) -> str:
    if x is None:
        return "n/a"
    sign = ("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")
    a = abs(x)
    return f"{sign}${a:,.2f}" if a < 1000 else f"{sign}${a:,.0f}"


def pct(x: float | None, d: int = 0) -> str:
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def rate_pct(x: float | None, d: int = 5) -> str:
    """Funding fraction as a percent string (0.0001 -> 0.01000%)."""
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def num(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{x:,.{d}f}"


def ago(ms: int | None, now_ms: int) -> str:
    if ms is None:
        return "no data"
    s = max(0, (now_ms - ms) // 1000)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def stamp(ms: int | None) -> str:
    return iso(ms) or "n/a"


def day(ms: int | None) -> str:
    return "n/a" if ms is None else (iso(ms) or "")[:10]


@dataclass
class Controls:
    notional: float
    hours: float
    fee: str
    side: str
    tab: str
    unverified: bool

    def qs(self, **over) -> str:
        d = {
            "notional": f"{self.notional:g}",
            "hours": f"{self.hours:g}",
            "fee": self.fee,
            "side": self.side,
            "tab": self.tab,
        }
        if self.unverified:
            d["unverified"] = "1"
        d.update({k: v for k, v in over.items() if v is not None})
        if over.get("unverified") == "":
            d.pop("unverified", None)
        return urlencode(d)

    @property
    def preset_key(self) -> str | None:
        for k, h in PRESETS_HOURS.items():
            if abs(h - self.hours) < 1e-9:
                return k
        return None


def parse_controls(request: Request, ctx: Context) -> Controls:
    q = request.query_params

    def f(name: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(q.get(name, default))
        except (TypeError, ValueError):
            return default
        return v if lo < v <= hi else default

    fee = q.get("fee", "taker")
    side = q.get("side", "long")
    tab = q.get("tab", "opps")
    return Controls(
        notional=f("notional", ctx.settings.default_notional, 0, 100_000_000),
        hours=f("hours", ctx.settings.default_hours, 0, 8760),
        fee=fee if fee in ("taker", "maker") else "taker",
        side=side if side in ("long", "short") else "long",
        tab=tab if tab in TABS else "opps",
        unverified=q.get("unverified") == "1",
    )


def funding_view(leg: Leg, role: str, notional: float) -> dict:
    """Funding display for one leg: raw per interval, then hourly, then 24h dollars, APR last.

    usd_24h is from the position's point of view: negative means the position pays.
    """
    f = leg.quote.funding if leg.quote else None
    rate = leg.rate_per_hour
    usd = calc.funding_pnl_usd(role, rate, notional, 24)
    return {
        "venue": leg.venue,
        "role": role,
        "raw": None if f is None else f.rate_per_interval,
        "interval_h": None if f is None else f.interval_hours,
        "hourly": rate,
        "usd_24h": usd,
        "apr": calc.annualized_apr(rate),
        "zero": rate == 0,
    }


def venue_pills(ctx: Context) -> list[dict]:
    now = ctx.now_ms()
    out = []
    for v in ALL_VENUES:
        enabled = v in ctx.settings.enabled_venues
        ts, stale = ctx.engine.venue_freshness(v)
        client = ctx.clients.get(v)
        degraded = bool(client and client.degraded)
        if not enabled:
            continue
        status = "degraded" if degraded else "stale" if stale else "ok"
        out.append(
            {
                "name": VENUE_NAMES.get(v, v),
                "status": status,
                "ago": ago(ts, now),
                "iso": stamp(ts),
                "error": ctx.state.last_error.get(v),
            }
        )
    return out


def common(request: Request, ctx: Context, page: str) -> dict:
    return {
        "request": request,
        "page": page,
        "pills": venue_pills(ctx),
        "repo_url": REPO_URL,
        "restricted_url": RESTRICTED_URL,
        "trade_url": ctx.settings.trade_url(),
        "money": money,
        "pct": pct,
        "rate_pct": rate_pct,
        "num": num,
        "ago": ago,
        "stamp": stamp,
        "day": day,
        "vn": lambda v: VENUE_NAMES.get(v, v),
        "now_ms": ctx.now_ms(),
        "presets": PRESETS_HOURS,
        "preset_labels": PRESET_LABELS,
    }


# --- pages ------------------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request) -> HTMLResponse:
    ctx = ctx_of(request)
    c = parse_controls(request, ctx)
    eng = ctx.engine
    ranked, excluded = eng.opportunities(
        c.notional, c.hours, c.fee, include_unverified=c.unverified
    )
    verified = [r for r in ranked if r.confidence in ("High", "Medium") and not r.stale]
    top = verified[:5]
    rep = ctx.report()
    hold: list[dict] = []
    if c.tab == "hold":
        hold = hold_rows(ctx, c)
    only = eng.strike_only(c.notional) if c.tab == "only" else []
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **common(request, ctx, "home"),
            "c": c,
            "top": top,
            "ranked": ranked,
            "excluded": excluded,
            "best_net": top[0].two_leg.net_usd if top else None,
            "headline": rep.get("headline"),
            "history_loading": bool(ctx.backfiller and ctx.backfiller.progress.loading),
            "hold": hold,
            "only": only,
            "n_only": len(eng.strike_only(c.notional)) if c.tab != "only" else len(only),
            "funding_view": funding_view,
            "line_items": line_items_for,
            "any_data": any(ctx.state.quotes.values()),
            "tabs": [
                ("opps", "Two-leg opportunities"),
                ("hold", "Cheapest to hold"),
                ("only", "Only on Strike"),
            ],  # fmt: skip
        },
    )


def line_items_for(r: OpportunityRow, c: Controls) -> list[dict]:
    from app.api import line_items

    return [i.model_dump() for i in line_items(r, c.notional, c.fee == "maker")]


def hold_rows(ctx: Context, c: Controls) -> list[dict]:
    """View A rows for the chosen direction with 1d/1w/1m net at the best venue."""
    eng = ctx.engine
    rows = []
    for m in eng.markets.values():
        if not eng.competitors(m):
            continue
        res = eng.holding_cost(m, c.side, c.notional, c.hours, c.fee)
        if res.best is None:
            rows.append({"res": res, "best_nets": None, "conf": None})
            continue
        best_v = res.best[0]
        nets = {}
        for k, h in PRESETS_HOURS.items():
            r2 = eng.holding_cost(m, c.side, c.notional, h, c.fee)
            tot = next((v.cost.total_cost_usd for v in r2.venues if v.venue == best_v), None)
            nets[k] = None if tot is None else -tot
        conf = next(v for v in res.venues if v.venue == best_v)
        rows.append({"res": res, "best_nets": nets, "conf": conf})
    rows.sort(key=lambda r: -(r["res"].gap_usd if r["res"].gap_usd is not None else -1e18))
    return rows


FLAGSHIP_ORDER = {"BTC-USD": 0, "ETH-USD": 1, "SOL-USD": 2}


@router.get("/report", response_class=HTMLResponse, include_in_schema=False)
def report_page(request: Request) -> HTMLResponse:
    ctx = ctx_of(request)
    rep = ctx.report()
    markets = sorted(
        rep["markets"],
        key=lambda m: (m["ticker_only"], FLAGSHIP_ORDER.get(m["symbol"], 9), m["symbol"]),
    )
    p = ctx.backfiller.progress if ctx.backfiller else None
    has_data = any(m["pairs"] for m in markets)
    return templates.TemplateResponse(
        request,
        "report.html",
        {
            **common(request, ctx, "report"),
            "rep": rep,
            "markets": markets,
            "loading": bool(p and p.loading),
            "progress": p,
            "has_data": has_data,
            "dir_text": dir_text,
            "chart_symbols": [m["symbol"] for m in markets if m["pairs"]] or ["BTC-USD"],
            "start": rep["window_start_ms"],
            "end": rep["window_end_ms"],
            "LONG_STRIKE": report.LONG_STRIKE,
            "STRIKE": STRIKE,
        },
    )


def dir_text(direction: str, other: str) -> str:
    o = VENUE_NAMES.get(other, other)
    if direction == report.LONG_STRIKE:
        return f"Long Strike / short {o}"
    return f"Long {o} / short Strike"
