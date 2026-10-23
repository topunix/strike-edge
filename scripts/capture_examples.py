"""Capture real API responses (abridged) for /llms.txt and the README.

Usage: python scripts/capture_examples.py BASE_URL
Writes app/examples/*.json. Lists are shortened; every value is real, taken from the
running service at capture time.
"""

import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1].rstrip("/")
OUT = Path(__file__).resolve().parent.parent / "app" / "examples"


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:  # noqa: S310
        return json.load(r)


def save(name: str, obj: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2) + "\n")
    print("wrote", name)


opp = get("/api/v1/opportunities?symbol=ETH-USD&hours=168&notional=10000&fee_mode=taker")
opp["opportunities"] = opp["opportunities"][:1]
opp["unranked"] = []
save("opportunities", opp)

save("holding_cost", get("/api/v1/holding-cost?symbol=ETH-USD&side=long&hours=168&notional=10000"))

save(
    "switch",
    get("/api/v1/switch?symbol=ETH-USD&side=long&from_venue=hyperliquid&notional=50000&hours=336"),
)

snap = get("/api/v1/snapshot")
snap["markets"] = [m for m in snap["markets"] if m["symbol"] == "ETH-USD"]
save("snapshot", snap)

hist = get("/api/v1/history?symbol=ETH-USD&days=1")
hist["venues_history"] = {v: pts[:3] for v, pts in hist["venues_history"].items()}
hist["cumulative_net_usd_on_10k"] = {
    o: {d: pts[:2] for d, pts in dirs.items()}
    for o, dirs in hist["cumulative_net_usd_on_10k"].items()
}
save("history", hist)

rep = get("/api/v1/report")
rep["markets"] = [m for m in rep["markets"] if m["symbol"] == "ETH-USD"]
rep["key_findings"] = rep["key_findings"][:1]
rep["summary"] = rep["summary"][:3]
save("report", rep)

hz = get("/healthz")
save("healthz", hz)
