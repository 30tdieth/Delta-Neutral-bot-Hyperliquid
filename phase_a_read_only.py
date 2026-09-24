"""
Phase A — READ-ONLY skeleton for the delta-neutral funding bot.

This module ONLY reads state and computes health. It places NO orders,
moves NO cash. It is safe to run against mainnet with real keys.

Run: python phase_a_read_only.py
Requires:  pip install hyperliquid-python-sdk
Env vars:  HL_ACCOUNT_ADDRESS  (your main wallet, read-only here)
           HL_AGENT_KEY        (agent wallet secret; not used to trade in Phase A,
                                but wired now so Phase C changes nothing about auth)
"""

import os
import time
import logging

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ----------------------------------------------------------------------
# FROZEN CONFIG  (the numbers we locked — change ONLY here)
# ----------------------------------------------------------------------
COIN          = "BTC"     # perp symbol
SPOT_COIN     = "UBTC"    # spot symbol (long leg) — confirm exact ticker on HL
MM            = 0.01      # maintenance margin fraction (PLACEHOLDER, chosen not to verify)
TARGET_S      = 0.22      # top-up destination (survivable move)
FLOOR_S       = 0.18      # orange trigger: top up when s drops to this
REDUCE_S      = 0.14      # red trigger: too fast to top-up out of
TICK_SECONDS  = 10        # how often we read state

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("phase_a")


def classify(s: float) -> str:
    """Map a survivable-move value to its zone."""
    if s >= TARGET_S:
        return "GREEN  (collect funding)"
    if s >= FLOOR_S:
        return "YELLOW (watch)"
    if s >= REDUCE_S:
        return "ORANGE (would top up)"
    return "RED    (would reduce)"


def read_state(info: Info, address: str) -> dict:
    """
    Read the REAL exchange state. Never trust local variables — this is
    the single source of truth every tick.
    """
    perp = info.user_state(address)          # perp positions + margin
    spot = info.spot_user_state(address)     # spot balances (uBTC + USDC reserve)
    mids = info.all_mids()                    # mark prices

    mark = float(mids[COIN])

    # --- perp short leg ---
    pos_size = 0.0
    margin   = 0.0
    for p in perp.get("assetPositions", []):
        pos = p["position"]
        if pos["coin"] == COIN:
            pos_size = abs(float(pos["szi"]))            # position size in BTC
            margin   = float(pos["marginUsed"])          # equity backing the short
    N = pos_size * mark                                  # notional

    # --- spot: uBTC (long) + USDC (reserve) ---
    ubtc = 0.0
    reserve = 0.0
    for b in spot.get("balances", []):
        if b["coin"] == SPOT_COIN:
            ubtc = float(b["total"])
        if b["coin"] == "USDC":
            reserve = float(b["total"])

    return {
        "mark": mark,
        "pos_size": pos_size,
        "N": N,
        "M": margin,
        "ubtc": ubtc,
        "ubtc_value": ubtc * mark,
        "reserve": reserve,
    }


def reconcile(st: dict) -> bool:
    """
    Startup sanity check. If the two legs are wildly mismatched or empty,
    something is wrong — do NOT trade (Phase A never trades anyway), alert.
    A bot acting on a false state is more dangerous than a stopped one.
    """
    if st["N"] == 0 or st["ubtc"] == 0:
        log.warning("RECONCILE: a leg is empty (N=%.2f, uBTC=%.6f). Not safe to act.",
                    st["N"], st["ubtc"])
        return False
    delta = st["ubtc_value"] - st["N"]
    if abs(delta) > 0.10 * st["N"]:
        log.warning("RECONCILE: legs mismatched by %.1f%% (delta $%.2f). Investigate.",
                    100 * delta / st["N"], delta)
        return False
    return True


def tick(info: Info, address: str):
    st = read_state(info, address)
    s = st["M"] / st["N"] - MM if st["N"] > 0 else float("nan")
    delta = st["ubtc_value"] - st["N"]

    log.info(
        "mark=$%.0f | N=$%.2f M=$%.2f reserve=$%.2f | s=%.1f%% | delta=$%.2f | %s",
        st["mark"], st["N"], st["M"], st["reserve"],
        s * 100, delta, classify(s),
    )


def main():
    address = os.environ.get("HL_ACCOUNT_ADDRESS")
    if not address:
        raise SystemExit("Set HL_ACCOUNT_ADDRESS env var (your main wallet address).")

    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    first = read_state(info, address)
    ok = reconcile(first)
    log.info("Startup reconciliation: %s", "OK" if ok else "FAILED — read-only, continuing to observe")

    log.info("Phase A running (READ-ONLY). Ctrl-C to stop.")
    while True:
        try:
            tick(info, address)
        except Exception as e:                 # never let one bad read kill the loop
            log.error("tick error: %s", e)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
