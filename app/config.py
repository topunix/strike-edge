"""Configuration: venues, fees, funding conventions and the symbol map.

Funding conventions (all verified against live responses, see tests/fixtures):

Strike    premiumIndex.fundingRate is the HOURLY rate (interval 1h). Positive rate
          means longs pay shorts. Paid hourly. Fees: taker 0.050%, maker -0.005%
          (rebate), as configured below (base tier, Strike team figures).
Hyperliquid  metaAndAssetCtxs.funding is the HOURLY rate. Positive means longs pay
          shorts. Fees (docs, base tier): taker 0.045%, maker 0.015%. Markets on
          HIP-3 builder dexes (xyz:*) carry an extra deployer fee share that is
          not exposed by the public API; HL_HIP3_FEE_MULTIPLIER scales the base
          fees for those markets (default 1.0, unverified, see README).
OKX       funding-rate.fundingRate is the rate for ONE interval. The interval is
          derived per instrument from nextFundingTime - fundingTime (usually 8h,
          4h or 1h). Positive means longs pay shorts. Fees (docs, regular user
          tier 1): taker 0.050%, maker 0.020%. Order book sizes are in contracts;
          base units = contracts * ctVal * ctMult from /public/instruments.
Binance   geo-blocked (HTTP 451) from many regions: opt-in only via
          ENABLED_VENUES, off by default.

Rate limit budgets are 50% of the published limits (Hyperliquid 1200 weight/min per
IP; OKX 10 to 40 requests per 2s depending on endpoint; Strike rateLimits from
/v2/exchangeInfo, read at startup, 2400 weight/min when last checked).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

STRIKE = "strike"
HYPERLIQUID = "hyperliquid"
OKX = "okx"
BINANCE = "binance"
ALL_VENUES = (STRIKE, HYPERLIQUID, OKX, BINANCE)

STRIKE_BASE = "https://api.strikefinance.org"
HL_BASE = "https://api.hyperliquid.xyz"
OKX_BASE = "https://www.okx.com"

# Fee rates are fractions of notional. Negative means rebate.
FEES: dict[str, dict[str, float]] = {
    STRIKE: {"taker": 0.00050, "maker": -0.00005},
    HYPERLIQUID: {"taker": 0.00045, "maker": 0.00015},
    OKX: {"taker": 0.00050, "maker": 0.00020},
    BINANCE: {"taker": 0.00050, "maker": 0.00020},
}

# A venue's data older than this is stale: greyed out and excluded from rankings.
STALE_AFTER_MS = 5 * 60 * 1000
# Confidence "fresh" threshold: all legs younger than this.
FRESH_MS = 2 * 60 * 1000

PRESETS_HOURS = {"1d": 24, "1w": 168, "1m": 720}


@dataclass(frozen=True)
class MarketMap:
    """One Strike market and its exact-underlying counterparts on other venues."""

    strike: str  # e.g. "BTC-USD"
    hl_coin: str | None = None  # "BTC" or "xyz:TSLA" (HIP-3 dex prefix)
    okx_inst: str | None = None  # "BTC-USDT-SWAP"
    note: str = ""  # contract differences shown in the UI and README

    @property
    def base(self) -> str:
        return self.strike.removesuffix("-USD")

    @property
    def hl_dex(self) -> str | None:
        if self.hl_coin and ":" in self.hl_coin:
            return self.hl_coin.split(":", 1)[0]
        return None


# --- Symbol map -----------------------------------------------------------
# Only exact underlying matches are mapped.
# Sources (verified live 2026-10-21):
#   Hyperliquid main dex: crypto majors.
#   Hyperliquid HIP-3 dex "xyz" (the only HIP-3 dex with real volume): equities,
#     gold, silver, WTI crude, S&P 500. Collateral is USDC.
#   OKX USDT-margined linear swaps: crypto plus equity (instCategory 3) and
#     commodity/index (instCategory 4) perps.
# Contract differences: Strike margin asset is USDT, Hyperliquid collateral is
# USDC, OKX is USDT-margined. TradFi markets follow underlying market hours, and
# reference-price methodology differs per venue.

_HL_MAIN = ("ADA", "BNB", "BTC", "ETH", "HYPE", "NEAR", "PUMP", "SOL", "XRP", "ZEC")
_OKX_CRYPTO = (*_HL_MAIN, "NIGHT")
_HL_XYZ_SAME = (
    "AAOI", "COIN", "CRCL", "CXMT", "DRAM", "GOOGL", "MINIMAX", "MU", "NVDA",
    "SNDK", "SPCX", "TSLA", "UNITREE", "ZHIPU", "SP500",
)  # fmt: skip
_HL_XYZ_RENAMED = {"XAU": "GOLD", "XAG": "SILVER", "WTI": "CL"}
_OKX_TRADFI_SAME = (
    "AAOI", "COIN", "CRCL", "CXMT", "DRAM", "GOOGL", "MINIMAX", "MU", "NVDA",
    "SNDK", "SPCX", "TSLA", "UNITREE", "ZHIPU", "XAU", "XAG", "SKHYNIX",
)  # fmt: skip
_OKX_TRADFI_RENAMED = {"WTI": "CL", "SP500": "US500", "NAS100": "US100"}

_NOTES = {
    "WTI": "WTI crude futures reference. xyz:CL and OKX CL track front-month futures; roll "
    "timing and trading hours differ.",
    "SP500": "S&P 500 index reference: Hyperliquid xyz:SP500, OKX US500.",
    "NAS100": "Nasdaq-100 index: OKX US100 only. Hyperliquid xyz:XYZ100 is not mapped "
    "(own index, not an exact match).",
    "SKHYNIX": "OKX SKHYNIX only. Hyperliquid xyz lists SKHX and SKHY, not mapped "
    "(underlying listing ambiguous).",
    "NIGHT": "OKX only. Not listed on Hyperliquid.",
}
_PRIVATE_NAMES = ("CXMT", "DRAM", "MINIMAX", "SPCX", "UNITREE", "ZHIPU")
_PRIVATE_NOTE = (
    "Ticker match across venues; reference-price methodology is not verified and may differ."
)


def _build_symbol_map(strike_symbols: tuple[str, ...]) -> dict[str, MarketMap]:
    out: dict[str, MarketMap] = {}
    for sym in strike_symbols:
        base = sym.removesuffix("-USD")
        hl: str | None = None
        okx: str | None = None
        if base in _HL_MAIN:
            hl = base
        elif base in _HL_XYZ_SAME:
            hl = f"xyz:{base}"
        elif base in _HL_XYZ_RENAMED:
            hl = f"xyz:{_HL_XYZ_RENAMED[base]}"
        if base in _OKX_CRYPTO or base in _OKX_TRADFI_SAME:
            okx = f"{base}-USDT-SWAP"
        elif base in _OKX_TRADFI_RENAMED:
            okx = f"{_OKX_TRADFI_RENAMED[base]}-USDT-SWAP"
        note = _NOTES.get(base, "")
        if base in _PRIVATE_NAMES:
            note = _PRIVATE_NOTE
        out[sym] = MarketMap(sym, hl, okx, note)
    return out


STRIKE_SYMBOLS = (
    "AAOI-USD", "ADA-USD", "BNB-USD", "BTC-USD", "COIN-USD", "CRCL-USD", "CXMT-USD",
    "DRAM-USD", "ETH-USD", "GOOGL-USD", "HYPE-USD", "MINIMAX-USD", "MU-USD",
    "NAS100-USD", "NEAR-USD", "NIGHT-USD", "NVDA-USD", "PUMP-USD", "SKHYNIX-USD",
    "SNDK-USD", "SOL-USD", "SP500-USD", "SPCX-USD", "TSLA-USD", "UNITREE-USD",
    "WTI-USD", "XAG-USD", "XAU-USD", "XRP-USD", "ZEC-USD", "ZHIPU-USD",
)  # fmt: skip

SYMBOL_MAP: dict[str, MarketMap] = _build_symbol_map(STRIKE_SYMBOLS)

# --- TradFi / RWA classification ------------------------------------------
# Default rule (Strike funding docs): interestRate == 0 and interestRateDampener > 0
# on premiumIndex means a TradFi/RWA market. Overrides below always win.
FORCE_TRADFI: set[str] = set()
FORCE_CRYPTO: set[str] = set()


def is_tradfi(
    symbol: str,
    interest_rate: float | None,
    dampener: float | None,
    force_tradfi: set[str] | None = None,
    force_crypto: set[str] | None = None,
) -> bool:
    """Classify a Strike market as TradFi/RWA (stocks, indices, commodities)."""
    ft = FORCE_TRADFI if force_tradfi is None else force_tradfi
    fc = FORCE_CRYPTO if force_crypto is None else force_crypto
    if symbol in ft:
        return True
    if symbol in fc:
        return False
    if interest_rate is None or dampener is None:
        return False
    return interest_rate == 0 and dampener > 0


@dataclass(frozen=True)
class Settings:
    strike_referral_code: str | None = None
    enabled_venues: tuple[str, ...] = (STRIKE, HYPERLIQUID, OKX)
    default_notional: float = 10_000.0
    default_hours: float = 24.0
    hl_hip3_fee_multiplier: float = 1.0
    budget_fraction: float = 0.5
    fees: dict[str, dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in FEES.items()}
    )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        venues = tuple(
            v.strip().lower()
            for v in e.get("ENABLED_VENUES", ",".join((STRIKE, HYPERLIQUID, OKX))).split(",")
            if v.strip().lower() in ALL_VENUES
        )
        return cls(
            strike_referral_code=e.get("STRIKE_REFERRAL_CODE") or None,
            enabled_venues=venues or (STRIKE,),
            default_notional=float(e.get("DEFAULT_NOTIONAL", "10000")),
            default_hours=float(e.get("DEFAULT_HOURS", "24")),
            hl_hip3_fee_multiplier=float(e.get("HL_HIP3_FEE_MULTIPLIER", "1.0")),
        )

    def fee_rate(self, venue: str, mode: str, market: MarketMap | None = None) -> float:
        rate = self.fees[venue][mode]
        if venue == HYPERLIQUID and market is not None and market.hl_dex:
            rate *= self.hl_hip3_fee_multiplier
        return rate

    def trade_url(self) -> str:
        if self.strike_referral_code:
            return f"https://v2.strikefinance.org/referral/accept?code={self.strike_referral_code}"
        return "https://v2.strikefinance.org"
