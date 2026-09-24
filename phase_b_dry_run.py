#!/usr/bin/env python3
"""
Delta-Neutral Funding Bot — Phase B (DRY-RUN decision logging)
==============================================================

Builds on the Phase A read-only layer. Every tick it:
  1. reads REAL exchange state (never trusts a local variable),
  2. runs startup / per-tick reconciliation,
  3. computes the survivable move `s` and the zone,
  4. computes the top-up / reduce action it WOULD take, and LOGS it.

It sends NOTHING. Structural guarantee of that safety property:
this file only ever constructs the hyperliquid `Info` client (reads).
It never imports or instantiates `Exchange`, so there is no code path
that can sign or submit an order. No agent key is even required to run
Phase B — reads are by address. (Phase C is where `Exchange` enters.)

Spec source: "Delta-Neutral Funding Bot — Transmission Doc v2" (20 Jul 2026),
sections 6 (frozen config), 8 (exact formulas), 9 (unverified constants),
10 (phases), 11 (safety rules).

Usage:
    export HL_ACCOUNT_ADDRESS=0x....        # the master account being watched
    # HL_AGENT_KEY is NOT needed for Phase B (no signing happens)
    pip install hyperliquid-python-sdk
    python phase_b_dry_run.py            # loop every POLL_SECONDS
    python phase_b_dry_run.py --once     # single tick then exit
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# --- hyperliquid SDK: READ-ONLY client only -------------------------------
# NOTE we import ONLY Info. Not importing Exchange is deliberate — it makes
# "Phase B cannot trade" a property of the file, not of my discipline.
try:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants as hl_constants
except ImportError:
    sys.exit("Install the SDK first:  pip install hyperliquid-python-sdk")


# ==========================================================================
# CONFIG
# ==========================================================================

@dataclass(frozen=True)
class Config:
    # --- FROZEN config (Transmission Doc §6) ------------------------------
    target: float = 0.22          # 🟢 green target survivable move
    floor: float = 0.18           # 🟠 top-up trigger (the overnight-blind-window floor)
    hard_red: float = 0.14        # 🔴 forced-reduce threshold
    delta_band: float = 0.02      # rebalance if |ubtc_value - N| > band * N  (§8)

    # --- UNVERIFIED CONSTANTS (Transmission Doc §9) -----------------------
    # These MUST be verified before mainnet. Phase B only LOGS, so wrong
    # values here just make the logged numbers wrong, not dangerous — but
    # verifying now means Phase B logs real numbers.
    mm: float = 0.01              # VERIFY  maintenance margin fraction (placeholder; tiered with size)
    min_order_usd: float = 10.0   # VERIFY  min order size (guess) — feeds the reduce guard
    lot_size: float = 1e-5        # VERIFY  BTC lot/step size for quantity rounding (guess)

    # --- symbols ----------------------------------------------------------
    perp_coin: str = "BTC"        # perp market name on HL
    spot_coin: str = "UBTC"       # VERIFY  exact spot ticker for the long leg (§9)
    stable: str = "USDC"          # reserve currency in the spot wallet

    # --- runtime ----------------------------------------------------------
    poll_seconds: int = 60
    mainnet: bool = True          # read-only, so mainnet is safe here (§10 Phase A)


CFG = Config()


class Zone(str, Enum):
    GREEN = "🟢 GREEN"
    YELLOW = "🟡 YELLOW"
    ORANGE = "🟠 ORANGE"
    RED = "🔴 RED"
    FLAT = "⚪ FLAT"        # no position open (Phase A test step 2)


# ==========================================================================
# STATE  (Phase A read layer)
# ==========================================================================

@dataclass
class State:
    """A snapshot of the three pockets (§2), read fresh every tick."""
    mark: float               # BTC perp mark price
    short_qty: float          # |szi| of the BTC short, in BTC (0 if flat)
    N: float                  # short notional in USDC = short_qty * mark
    M: float                  # equity/margin backing the perp short (USDC)  -> pocket 2
    R: float                  # idle USDC in the spot wallet                 -> pocket 3
    ubtc_qty: float           # uBTC held in the spot wallet
    ubtc_value: float         # ubtc_qty * mark (long-leg value)             -> pocket 1

    @property
    def is_flat(self) -> bool:
        return self.short_qty <= 0 and self.ubtc_qty <= 0

    @property
    def s(self) -> float:
        """Survivable move: s = M/N - mm  (§6). The health indicator."""
        if self.N <= 0:
            return float("inf")
        return self.M / self.N - CFG.mm


# --- SDK field access -------------------------------------------------------
# §10 step 4 / §9: field names shift between SDK releases. They are read in
# ONE place so verifying/patching them is a single edit. Verify each against
# your installed version by printing the raw dicts once.

def read_state(info: Info, address: str) -> State:
    perp = info.user_state(address)          # perp wallet
    spot = info.spot_user_state(address)     # spot wallet
    mids = info.all_mids()                   # {coin: mid_price_str}

    mark = float(mids[CFG.perp_coin])        # VERIFY key ("BTC")

    # --- perp short leg ---
    short_qty = 0.0
    for ap in perp.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == CFG.perp_coin:
            szi = float(pos.get("szi", 0.0))     # signed size; short => negative
            short_qty = abs(szi) if szi < 0 else 0.0
            break
    N = short_qty * mark

    # M = equity backing the short. For a perp wallet holding only USDC margin
    # + this one short, accountValue IS that equity (posted margin ± uPnL).
    M = float(perp.get("marginSummary", {}).get("accountValue", 0.0))

    # --- spot wallet: uBTC long leg + USDC reserve ---
    ubtc_qty = 0.0
    R = 0.0
    for bal in spot.get("balances", []):
        coin = bal.get("coin")
        total = float(bal.get("total", 0.0))
        if coin == CFG.spot_coin:
            ubtc_qty = total
        elif coin == CFG.stable:
            R = total

    return State(
        mark=mark, short_qty=short_qty, N=N, M=M, R=R,
        ubtc_qty=ubtc_qty, ubtc_value=ubtc_qty * mark,
    )


# ==========================================================================
# RECONCILIATION  (§11 — refuse to act on a divergent / degenerate state)
# ==========================================================================

def reconcile(st: State, log: logging.Logger) -> bool:
    """Return True if the state is coherent enough to reason about.
    In Phase B nothing is sent regardless; this is the hook Phase C hardens."""
    if st.is_flat:
        log.info("FLAT — no legs open. Reconcile trivially OK (nothing to manage).")
        return True
    if st.short_qty <= 0:
        log.warning("DIVERGENCE: long uBTC present but NO short leg → naked long. Would NOT trade.")
        return False
    if st.ubtc_qty <= 0:
        log.warning("DIVERGENCE: short present but NO uBTC long → naked short. Would NOT trade.")
        return False
    # legs should be roughly equal-notional (neutrality). This is informational
    # here; the delta loop handles small drift.
    skew = abs(st.ubtc_value - st.N) / st.N
    if skew > 0.25:
        log.warning("DIVERGENCE: legs skewed %.1f%% (long $%.2f vs short $%.2f). Would NOT trade.",
                    skew * 100, st.ubtc_value, st.N)
        return False
    return True


# ==========================================================================
# DECISION LOGIC  (§6 zones + §8 exact formulas) — computes, does NOT send
# ==========================================================================

@dataclass
class Decision:
    zone: Zone
    action: str          # human-readable "what I WOULD do"
    detail: str = ""


def round_lot(qty: float) -> float:
    """Round a BTC quantity down to the venue lot size (§8 min-order guard)."""
    if CFG.lot_size <= 0:
        return qty
    return (qty // CFG.lot_size) * CFG.lot_size


def decide(st: State) -> Decision:
    if st.is_flat:
        return Decision(Zone.FLAT, "no position — nothing to manage")

    s = st.s

    # 🟢 / 🟡 — no action
    if s >= CFG.target:
        return Decision(Zone.GREEN, f"hold, collect funding (s={s:.3f} ≥ target {CFG.target})")
    if s > CFG.floor:
        return Decision(Zone.YELLOW, f"watch, do nothing (floor {CFG.floor} < s={s:.3f} < target {CFG.target})")

    # s ≤ floor → a top-up is due. Can the reserve cover it, and are we above hard-red?
    # §8 top-up:  ΔM = (target + mm) * N − M ;  transfer = min(ΔM, R)
    dM = (CFG.target + CFG.mm) * st.N - st.M
    transfer = min(dM, st.R)
    reserve_covers = transfer >= dM - 1e-9

    if s >= CFG.hard_red and reserve_covers:
        # 🟠 ORANGE — top up: reserve → perp margin (free transfer, no trade, stays neutral)
        return Decision(
            Zone.ORANGE,
            f"TOP-UP ${transfer:.2f}: reserve → perp margin (restore s→{CFG.target})",
            detail=(f"s={s:.3f} ≤ floor {CFG.floor}; ΔM=${dM:.2f}; "
                    f"reserve R=${st.R:.2f} covers it; new reserve ≈ ${st.R - transfer:.2f}. "
                    f"[spot-wallet→perp-wallet USDC transfer — no order]"),
        )

    # 🔴 RED — reserve can't cover, or s < hard_red → reduce BOTH legs (§8 reduce)
    #   N_target = M / (target + mm) ;  f = 1 − N_target/N
    N_target = st.M / (CFG.target + CFG.mm)
    f = 1.0 - N_target / st.N
    f = max(0.0, min(1.0, f))
    leg_usd = f * st.N

    # min-order guard (§8): round the leg up to MIN_ORDER; if the whole
    # position is below MIN_ORDER we can't trade at all.
    guard_note = ""
    per_leg_position_usd = st.N            # short leg notional; long leg ≈ same
    if per_leg_position_usd < CFG.min_order_usd:
        return Decision(
            Zone.RED,
            "CANNOT REDUCE — position below min order size. ALERT the human.",
            detail=(f"s={s:.3f}; position ${per_leg_position_usd:.2f} < min ${CFG.min_order_usd:.2f}. "
                    f"Manual intervention only."),
        )
    if leg_usd < CFG.min_order_usd:
        guard_note = f" (rounded up from ${leg_usd:.2f} to min ${CFG.min_order_usd:.2f})"
        leg_usd = CFG.min_order_usd

    cut_qty = round_lot(leg_usd / st.mark)
    reason = "s < hard-red" if s < CFG.hard_red else "reserve insufficient for top-up"
    return Decision(
        Zone.RED,
        f"REDUCE both legs by f={f:.3f} → cut ≈ ${leg_usd:.2f} / {cut_qty:.6f} BTC per leg{guard_note}",
        detail=(f"s={s:.3f}; trigger: {reason}. "
                f"WOULD: buy-back {cut_qty:.6f} BTC short AND sell {cut_qty:.6f} uBTC, in parallel. "
                f"N_target=${N_target:.2f}."),
    )


def check_delta(st: State) -> Optional[str]:
    """Separate housekeeping loop (§8): flag long/short notional drift."""
    if st.N <= 0:
        return None
    drift = abs(st.ubtc_value - st.N)
    if drift > CFG.delta_band * st.N:
        direction = "trim uBTC" if st.ubtc_value > st.N else "add uBTC"
        return (f"DELTA drift ${drift:.2f} > band ${CFG.delta_band * st.N:.2f} "
                f"(long ${st.ubtc_value:.2f} vs short ${st.N:.2f}) → WOULD {direction}")
    return None


# ==========================================================================
# LOOP
# ==========================================================================

def tick(info: Info, address: str, log: logging.Logger) -> None:
    st = read_state(info, address)                       # §11: real state every tick
    log.info("pockets: uBTC(long)=$%.2f | perp margin M=$%.2f | reserve R=$%.2f | mark=$%.0f | N=$%.2f",
             st.ubtc_value, st.M, st.R, st.mark, st.N)

    if not reconcile(st, log):
        return

    d = decide(st)
    log.info("%s | s=%.3f | WOULD: %s", d.zone.value, (st.s if st.N > 0 else float('nan')), d.action)
    if d.detail:
        log.info("        └─ %s", d.detail)

    delta_msg = check_delta(st)
    if delta_msg:
        log.info("        └─ %s", delta_msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase B — dry-run decision logging (sends nothing).")
    parser.add_argument("--once", action="store_true", help="run one tick and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("phaseB")

    address = os.environ.get("HL_ACCOUNT_ADDRESS")
    if not address:
        sys.exit("Set HL_ACCOUNT_ADDRESS (the master account to watch). No agent key needed for Phase B.")

    base_url = hl_constants.MAINNET_API_URL if CFG.mainnet else hl_constants.TESTNET_API_URL
    info = Info(base_url, skip_ws=True)                  # READ-ONLY client. No Exchange constructed.

    log.info("Phase B DRY-RUN started — reads only, sends nothing. Watching %s", address)
    log.info("Config: target=%.2f floor=%.2f hard_red=%.2f mm=%.2f (⚠ verify §9 constants)",
             CFG.target, CFG.floor, CFG.hard_red, CFG.mm)

    try:
        while True:
            try:
                tick(info, address, log)
            except Exception as e:                       # a read failure must not crash the watch
                log.exception("tick failed: %s", e)
            if args.once:
                break
            time.sleep(CFG.poll_seconds)
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
