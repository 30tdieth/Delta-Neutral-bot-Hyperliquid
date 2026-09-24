#!/usr/bin/env python3
"""
Delta-Neutral Funding Bot — Phase C (TESTNET EXECUTION)
=======================================================

First code that actually SIGNS and SENDS orders. Hardcoded to Hyperliquid
TESTNET — fake money — so every mistake is free. Reuses the Phase B read +
decide logic and adds an executor.

SAFETY MODEL (read this):
  * TESTNET ONLY. base_url is pinned to testnet; a guard refuses to run
    against mainnet from this file.
  * --arm gate. Without --arm the bot behaves exactly like Phase B: it runs
    every decision through the real code path but LOGS instead of sending.
    Run unarmed first, confirm the decisions, THEN add --arm.
  * Exchange-fetched sizing. Lot/step size, the spot pair name and price
    decimals are read from the venue at startup — the §9 guesses are gone.
  * Two-leg unwind. If leg 2 of an entry fails, leg 1 is immediately
    unwound so you are never left directional.
  * Circuit breaker. N consecutive send failures -> pause trading, keep
    watching, alert.
  * Notional cap. Refuses to build a position larger than NOTIONAL_CAP.
  * Path A dead-man's switch (scheduleCancel), re-armed each tick.

CREDENTIALS:
  HL_ACCOUNT_ADDRESS = your MASTER account address (0x..., public, safe)
  HL_AGENT_KEY       = the private key of an API/agent wallet you generated
                       on the testnet UI. An agent key can TRADE but CANNOT
                       WITHDRAW. NEVER use your main wallet's private key or
                       seed phrase. Set it in the shell session, never commit it.

USAGE (PowerShell):
  $env:HL_ACCOUNT_ADDRESS = "0xYOUR_ACCOUNT_ADDRESS"
  $env:HL_AGENT_KEY       = "0xYourAgentKey"
  python phase_c_testnet.py --once                 # unarmed: log only
  python phase_c_testnet.py --enter 30 --arm --once# open a clean $30/leg neutral position
  python phase_c_testnet.py --arm                  # run the manager loop, armed
  python phase_c_testnet.py --flatten --arm        # close both legs
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional

try:
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants as hl_constants
    from hyperliquid.utils.types import Cloid
except ImportError:
    sys.exit("Install the SDK first:  python -m pip install hyperliquid-python-sdk")


# ==========================================================================
# CONFIG
# ==========================================================================

@dataclass(frozen=True)
class Config:
    # FROZEN health config (Transmission Doc §6)
    target: float = 0.22
    floor: float = 0.18
    hard_red: float = 0.14
    delta_band: float = 0.02

    mm: float = 0.0125            # overwritten at startup from the venue (see load_venue)

    # symbols
    # TESTNET: uBTC and BTC-spot do NOT exist here. HYPE is the stand-in.
    # MAINNET TARGET is perp "BTC" + spot "UBTC" — revert both before Phase D.
    perp_coin: str = "HYPE"
    spot_base: str = "HYPE"       # base token of the spot long leg
    quote: str = "USDC"

    # execution
    leverage: int = 4             # cross, set once at startup
    slippage: float = 0.01        # 1% max slippage on market legs
    flatten_slippage: float = 0.40  # --flatten only: wide on purpose (testnet books are thin)
    min_order_usd: float = 10.0   # HL perp min order value; enforced per leg
    notional_cap_usd: float = 200.0   # hard sanity cap on per-leg notional
    max_consecutive_failures: int = 3 # circuit breaker
    # Agent keys CANNOT sign usdClassTransfer (verified live on mainnet 15 Sep
    # 2026: "Must deposit before performing actions. User: <agent address>").
    # False = margin + reserve must already sit in the perp wallet (the owner
    # moves it in the HL UI); the bot alerts instead of transferring.
    transfers_enabled: bool = False

    # dead-man's switch (Path A) — cancel-all if the bot goes silent
    deadman_horizon_s: int = 120

    poll_seconds: int = 60


CFG = Config()


class Zone(str, Enum):
    GREEN = "🟢 GREEN"
    YELLOW = "🟡 YELLOW"
    ORANGE = "🟠 ORANGE"
    RED = "🔴 RED"
    FLAT = "⚪ FLAT"


# ==========================================================================
# VENUE METADATA — replaces the §9 guesses with real values
# ==========================================================================

@dataclass
class Venue:
    perp_sz_decimals: int         # rounding for the perp size
    spot_name: str                # tradable name for the spot pair
    spot_sz_decimals: int         # rounding for the spot base token
    max_leverage: int = 0         # perp max leverage, from the venue (0 = unknown)

    def round_perp(self, qty: float) -> float:
        return round(qty, self.perp_sz_decimals)

    def round_spot(self, qty: float) -> float:
        return round(qty, self.spot_sz_decimals)

    def floor_spot(self, qty: float) -> float:
        f = 10 ** self.spot_sz_decimals
        return int(qty * f) / f


def load_venue(info: Info, log: logging.Logger) -> Venue:
    # perp size decimals
    perp_sz = max_lev = None
    for a in info.meta()["universe"]:
        if a["name"] == CFG.perp_coin:
            perp_sz, max_lev = a["szDecimals"], a["maxLeverage"]
            break
    if perp_sz is None:
        sys.exit(f"perp market {CFG.perp_coin} not found on this network")

    # spot pair name + base-token size decimals
    sm = info.spot_meta()
    tok_by_index = {t["index"]: t for t in sm["tokens"]}
    spot_name = None
    spot_sz = None
    for pair in sm["universe"]:
        base_i, quote_i = pair["tokens"]
        base, quote = tok_by_index[base_i], tok_by_index[quote_i]
        if base["name"] == CFG.spot_base and quote["name"] == CFG.quote:
            spot_name = pair["name"]        # e.g. "UBTC/USDC" or "@N"
            spot_sz = base["szDecimals"]
            break
    if spot_name is None:
        log.error("⚠ %s/%s spot pair NOT found on this network.",
                  CFG.spot_base, CFG.quote)
        log.error("   Options: (a) test perp-leg mechanics only, or (b) substitute an")
        log.error("   available testnet spot pair by editing CFG.spot_base.")
        sys.exit("no spot leg available — see message above")

    log.info("venue: perp %s szDecimals=%d maxLeverage=%dx | spot pair '%s' szDecimals=%d",
             CFG.perp_coin, perp_sz, max_lev, spot_name, spot_sz)
    return Venue(perp_sz, spot_name, spot_sz, max_lev)


# ==========================================================================
# STATE + DECIDE  (identical logic to Phase B)
# ==========================================================================

@dataclass
class State:
    mark: float                   # PERP mark price — the price that liquidates you
    spot_px: float                # SPOT pair mid — the price the long leg sells at
    short_qty: float
    N: float
    M: float
    R: float
    spot_qty: float
    spot_value: float

    @property
    def is_flat(self) -> bool:
        """Flat = both legs worth less than DUST_USD. Real trading always
        leaves rounding dust, so an exact-zero test can never be satisfied."""
        DUST_USD = 1.0
        return (self.short_qty * self.mark) < DUST_USD and self.spot_value < DUST_USD

    @property
    def s(self) -> float:
        return float("inf") if self.N <= 0 else self.M / self.N - CFG.mm


def read_state(info: Info, address: str, venue: Venue) -> State:
    perp = info.user_state(address)
    spot = info.spot_user_state(address)

    # Each leg is priced on ITS OWN market:
    #  - perp: markPx, the price margin and liquidation are computed on
    #  - spot: the spot pair's own mid, i.e. what the long leg is worth
    # Pricing the spot leg with the perp price would hide a spot de-peg
    # (uBTC vs BTC on mainnet) — the exact risk reconcile() must catch.
    meta, ctxs = info.meta_and_asset_ctxs()
    names = [a["name"] for a in meta["universe"]]
    mark = float(ctxs[names.index(CFG.perp_coin)]["markPx"])
    spot_px = float(info.all_mids()[venue.spot_name])

    short_qty = 0.0
    for ap in perp.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == CFG.perp_coin:
            szi = float(pos.get("szi", 0.0))
            short_qty = abs(szi) if szi < 0 else 0.0
            break
    N = short_qty * mark
    M = float(perp.get("marginSummary", {}).get("accountValue", 0.0))

    spot_qty = R = 0.0
    for bal in spot.get("balances", []):
        if bal.get("coin") == CFG.spot_base:
            spot_qty = float(bal.get("total", 0.0))
        elif bal.get("coin") == CFG.quote:
            # free USDC only: "hold" is locked (open orders, or perp margin
            # on unified accounts) and cannot be used for a top-up
            R = float(bal.get("total", 0.0)) - float(bal.get("hold", 0.0))

    return State(mark, spot_px, short_qty, N, M, R, spot_qty, spot_qty * spot_px)


# ==========================================================================
# RECONCILIATION (§11) — ported from Phase B. Refuse to ACT on a state whose
# two legs do not look like one delta-neutral position. `s` alone is blind to
# leg imbalance: it measures only the short's margin health.
# ==========================================================================

MAX_SKEW = 0.25          # |spot_value - N| / N above this = not neutral


def reconcile(st: State, log: logging.Logger) -> bool:
    """True if the state is coherent enough to trade on."""
    if st.is_flat:
        return True
    if st.short_qty <= 0:
        log.error("DIVERGENCE: spot long $%.2f but NO short leg -> naked long. "
                  "REFUSING to act.", st.spot_value)
        return False
    if st.spot_value < CFG.min_order_usd:
        log.error("DIVERGENCE: short $%.2f but no meaningful spot long -> naked "
                  "short. REFUSING to act.", st.N)
        return False
    skew = abs(st.spot_value - st.N) / st.N
    if skew > MAX_SKEW:
        log.error("DIVERGENCE: legs skewed %.1f%% (long $%.2f vs short $%.2f). "
                  "REFUSING to act.", skew * 100, st.spot_value, st.N)
        return False
    return True


def check_delta(st: State) -> Optional[str]:
    """Early-warning drift check (ported from Phase B). reconcile() only
    catches gross breakage (>25%); this flags any gap above delta_band (2%)
    between the two legs — e.g. a uBTC de-peg while BTC is flat. It ALERTS
    only: protective actions (top-up / reduce) keep running on purpose."""
    if st.N <= 0:
        return None
    drift = abs(st.spot_value - st.N)
    if drift > CFG.delta_band * st.N:
        return (f"DELTA DRIFT {drift / st.N:.1%} > band {CFG.delta_band:.0%} "
                f"(long ${st.spot_value:.2f} vs short ${st.N:.2f}) — legs no longer "
                f"offset. Possible de-peg: check {CFG.spot_base} vs {CFG.perp_coin}.")
    return None


@dataclass
class Decision:
    zone: Zone
    action: str
    kind: str = "none"          # none | topup | reduce | cannot
    amount: float = 0.0         # $ for topup, fraction f for reduce


def decide(st: State) -> Decision:
    if st.is_flat:
        return Decision(Zone.FLAT, "no position — nothing to manage")
    s = st.s
    if s >= CFG.target:
        return Decision(Zone.GREEN, f"hold (s={s:.3f})")
    if s > CFG.floor:
        return Decision(Zone.YELLOW, f"watch (s={s:.3f})")

    # Top up FIRST whenever the reserve covers — even below hard_red. A
    # transfer is free, needs no order book, and adds no price risk (the
    # spot leg gains what the short loses). Reducing is the last resort.
    dM = (CFG.target + CFG.mm) * st.N - st.M
    if not CFG.transfers_enabled:
        # an attempted transfer would fail every tick and trip the circuit
        # breaker, which would also stop reductions — so never try it
        if s >= CFG.hard_red:
            return Decision(Zone.ORANGE,
                            f"manual top-up needed: move ${dM:.2f} spot→perp in the HL UI "
                            f"(agent keys cannot transfer)", kind="alert", amount=dM)
        return reduce_decision(st, "s below hard_red, no automated top-up")
    if st.R >= dM - 1e-9:
        zone = Zone.ORANGE if s >= CFG.hard_red else Zone.RED
        return Decision(zone, f"TOP-UP ${dM:.2f} (restore s→{CFG.target})",
                        kind="topup", amount=dM)
    return reduce_decision(st, "reserve cannot cover")


def reduce_decision(st: State, why: str) -> Decision:
    N_target = st.M / (CFG.target + CFG.mm)
    f = max(0.0, min(1.0, 1.0 - N_target / st.N))
    if st.N < CFG.min_order_usd:
        return Decision(Zone.RED, "CANNOT REDUCE — position below min order size. ALERT.",
                        kind="cannot")
    return Decision(Zone.RED, f"REDUCE both legs by f={f:.3f} ({why})", kind="reduce", amount=f)


# ==========================================================================
# EXECUTOR — the only place that sends. Respects --arm.
# ==========================================================================

class Executor:
    def __init__(self, exch: Exchange, info: Info, venue: Venue,
                 armed: bool, log: logging.Logger):
        self.exch = exch
        self.info = info
        self.venue = venue
        self.armed = armed
        self.log = log
        self._cloid_n = int(time.time())   # monotonic-ish cloid seed (idempotency)
        self.consecutive_failures = 0
        self.paused = False

    def _next_cloid(self) -> Cloid:
        self._cloid_n += 1
        return Cloid.from_int(self._cloid_n)

    def _ok(self, result) -> bool:
        """True if an SDK response reports a fill; logs the error otherwise."""
        try:
            if result.get("status") != "ok":
                self.log.error("send rejected: %s", result)
                return False
            for stt in result["response"]["data"]["statuses"]:
                if "error" in stt:
                    self.log.error("leg error: %s", stt["error"])
                    return False
            return True
        except Exception as e:
            self.log.error("unparseable response %s (%s)", result, e)
            return False

    # ---- primitive legs -------------------------------------------------
    def _market_perp(self, is_buy: bool, qty: float) -> bool:
        qty = self.venue.round_perp(qty)
        if qty <= 0:
            return True
        if not self.armed:
            self.log.info("   [DRY] market perp %s %.6f %s",
                          "BUY" if is_buy else "SELL", qty, CFG.perp_coin)
            return True
        r = self.exch.market_open(CFG.perp_coin, is_buy, qty, None,
                                  CFG.slippage, cloid=self._next_cloid())
        return self._ok(r)

    def _market_spot(self, is_buy: bool, qty: float) -> bool:
        qty = self.venue.round_spot(qty)
        if qty <= 0:
            return True
        if not self.armed:
            self.log.info("   [DRY] market spot %s %.6f %s",
                          "BUY" if is_buy else "SELL", qty, self.venue.spot_name)
            return True
        r = self.exch.market_open(self.venue.spot_name, is_buy, qty, None,
                                  CFG.slippage, cloid=self._next_cloid())
        return self._ok(r)

    # ---- actions --------------------------------------------------------
    def topup(self, amount_usd: float) -> bool:
        if amount_usd <= 0:
            return True
        if not self.armed:
            self.log.info("   [DRY] usd_class_transfer $%.2f spot→perp", amount_usd)
            return True
        r = self.exch.usd_class_transfer(round(amount_usd, 2), True)  # to_perp=True
        if self._ok_transfer(r):
            self.consecutive_failures = 0
            return True
        self._register_failure()
        return False

    def _ok_transfer(self, result) -> bool:
        ok = isinstance(result, dict) and result.get("status") == "ok"
        if not ok:
            self.log.error("transfer rejected: %s", result)
        return ok

    def reduce_both(self, f: float, st: State) -> None:
        # cut EACH leg by fraction f, clamped to what actually exists
        cut_short = min(f * st.short_qty, st.short_qty)
        cut_spot = min(f * st.spot_qty, st.spot_qty)
        # min-order guard on the perp leg (dollar value)
        if cut_short * st.mark < CFG.min_order_usd:
            self.log.warning("reduce leg below $%.0f min — skipping this cycle", CFG.min_order_usd)
            return
        self.log.info("   reducing: buy-back %.6f %s short + sell %.6f %s",
                      cut_short, CFG.perp_coin, cut_spot, CFG.spot_base)
        leg1 = self._market_perp(is_buy=True, qty=cut_short)     # reduce short
        if not leg1:
            self._register_failure()
            return
        leg2 = self._market_spot(is_buy=False, qty=cut_spot)     # reduce long
        if not leg2:
            # leg 1 done, leg 2 failed → re-open the short we just closed to stay neutral
            self.log.error("leg 2 (spot) failed after leg 1 (perp) — UNWINDING leg 1")
            self._market_perp(is_buy=False, qty=cut_short)
            self._register_failure()
            return
        self.consecutive_failures = 0

    def open_position(self, notional_usd: float, st: State) -> None:
        """ENTRY: build a fresh delta-neutral position of `notional_usd` per leg."""
        if notional_usd > CFG.notional_cap_usd:
            self.log.error("requested $%.0f exceeds notional cap $%.0f — refusing",
                           notional_usd, CFG.notional_cap_usd)
            return
        if not st.is_flat:
            self.log.error("position already open — refusing to double up. Close it first.")
            return
        qty = notional_usd / st.mark
        if notional_usd < CFG.min_order_usd:
            self.log.error("requested $%.2f is below the $%.0f min order value — refusing",
                           notional_usd, CFG.min_order_usd)
            return

        # --- POCKET 2 SEEDING -------------------------------------------
        # The short is backed by the perp wallet only (no portfolio margin).
        # Fund it from the reserve so the position starts GREEN, and so the
        # venue's own initial-margin requirement (notional/leverage) is met.
        need_for_green = (CFG.target + CFG.mm) * notional_usd
        need_for_venue = notional_usd / CFG.leverage * 1.05     # 5% headroom
        need_M = max(need_for_green, need_for_venue)
        if st.M < need_M and not CFG.transfers_enabled:
            self.log.error("perp margin $%.2f < $%.2f needed. Agent keys cannot move USDC: "
                           "transfer at least $%.2f spot→perp in the HL UI, then retry.",
                           st.M, need_M, need_M - st.M)
            return
        if st.M < need_M:
            seed = round(need_M - st.M, 2)
            if seed > st.R:
                self.log.error("reserve $%.2f cannot seed perp margin $%.2f — refusing",
                               st.R, seed)
                return
            self.log.info("seeding perp margin: transfer $%.2f reserve→perp "
                          "(M $%.2f → $%.2f, target s=%.2f)",
                          seed, st.M, st.M + seed, CFG.target)
            self.topup(seed)
            if self.armed:
                time.sleep(2)                                   # let it settle
                st = read_state(self.info, self.exch.account_address, self.venue)
                self.log.info("after seeding: M=$%.2f | R=$%.2f", st.M, st.R)
                if st.M < need_M * 0.95:
                    self.log.error("perp margin still $%.2f (< $%.2f) — refusing to enter",
                                   st.M, need_M)
                    return

        # the long leg is paid from spot USDC: check it BEFORE opening the short,
        # otherwise leg 2 fails and leg 1 must be unwound (fees for nothing)
        spot_need = notional_usd * (1 + CFG.slippage) * 1.001
        if st.R < spot_need:
            self.log.error("spot USDC $%.2f < $%.2f needed for the long leg — refusing to enter",
                           st.R, spot_need)
            return

        self.log.info("ENTRY: short %.6f %s + long %.6f %s (~$%.0f/leg)",
                      qty, CFG.perp_coin, qty, CFG.spot_base, notional_usd)
        leg1 = self._market_perp(is_buy=False, qty=qty)          # open short
        if not leg1:
            self.log.error("entry leg 1 failed — nothing opened")
            return
        leg2 = self._market_spot(is_buy=True, qty=qty)           # open long
        if not leg2:
            self.log.error("entry leg 2 failed after leg 1 — UNWINDING the short")
            self._market_perp(is_buy=True, qty=qty)

    def flatten(self, st: State, slippage: float) -> None:
        """Close BOTH legs: buy back the whole short, sell all the spot base.
        Legs are independent here (we WANT to end flat), so a failure on one
        does not unwind the other — it is logged and you re-run."""
        self.log.info("FLATTEN (slippage %.0f%%): short %.4f %s | spot %.4f %s",
                      slippage * 100, st.short_qty, CFG.perp_coin,
                      st.spot_qty, CFG.spot_base)
        if st.short_qty > 0:
            if not self.armed:
                self.log.info("   [DRY] market_close perp %s (buy back %.4f)",
                              CFG.perp_coin, st.short_qty)
            else:
                r = self.exch.market_close(CFG.perp_coin, None, None, slippage,
                                           cloid=self._next_cloid())
                self.log.info("   perp close: %s", "OK" if self._ok(r) else "FAILED")
        sell_qty = self.venue.floor_spot(st.spot_qty)
        if sell_qty > 0:
            if not self.armed:
                self.log.info("   [DRY] market sell spot %.4f %s", sell_qty, self.venue.spot_name)
            else:
                r = self.exch.market_open(self.venue.spot_name, False, sell_qty, None,
                                          slippage, cloid=self._next_cloid())
                self.log.info("   spot sell: %s", "OK" if self._ok(r) else "FAILED")

    def rearm_deadman(self) -> None:
        """Path A: schedule cancel-all in the near future; a live bot keeps
        pushing it out, a dead bot lets it fire. Best-effort."""
        if not self.armed:
            return
        try:
            when = int((time.time() + CFG.deadman_horizon_s) * 1000)
            self.exch.schedule_cancel(when)
        except Exception as e:
            self.log.warning("dead-man's switch re-arm failed (non-fatal): %s", e)

    def _register_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= CFG.max_consecutive_failures:
            self.paused = True
            self.log.error("CIRCUIT BREAKER: %d consecutive failures → trading PAUSED. "
                           "Investigate before restarting.", self.consecutive_failures)


# ==========================================================================
# LOOP
# ==========================================================================

def manage_tick(info, address, ex: Executor, log) -> None:
    st = read_state(info, address, ex.venue)
    log.info("pockets: %s spot=$%.2f | M=$%.2f | R=$%.2f | perp mark=$%.2f | spot px=$%.2f | N=$%.2f | s=%s",
             CFG.spot_base, st.spot_value, st.M, st.R, st.mark, st.spot_px, st.N,
             f"{st.s:.3f}" if st.N > 0 else "n/a")

    if st.N > CFG.notional_cap_usd:
        log.warning("N=$%.2f exceeds cap $%.0f — not adding risk", st.N, CFG.notional_cap_usd)

    if ex.paused:
        log.error("trading paused by circuit breaker — logging only")
        return

    if not reconcile(st, log):
        log.error("state failed reconciliation — no action this tick. "
                  "Fix the legs manually, then restart.")
        ex.rearm_deadman()
        return

    drift = check_delta(st)
    if drift:
        log.error(drift)

    d = decide(st)
    log.info("%s | %s", d.zone.value, d.action)
    if d.kind == "topup":
        if not ex.topup(d.amount) and d.zone == Zone.RED and not ex.paused:
            # transfer failed and we are in real danger → fall back to reducing
            fb = reduce_decision(st, "top-up failed")
            log.error("top-up failed in RED → fallback: %s", fb.action)
            if fb.kind == "reduce":
                ex.reduce_both(fb.amount, st)
    elif d.kind == "reduce":
        ex.reduce_both(d.amount, st)
    elif d.kind == "alert":
        log.error("ACTION NEEDED: %s", d.action)

    ex.rearm_deadman()


def main(network: str = "testnet") -> None:
    """Shared engine. phase_c_testnet.py runs it on testnet; phase_d_mainnet.py
    imports this same file and runs it on mainnet — one copy of the logic, so
    a fix can never be forgotten in one of them (reconcile and delta_band were
    each lost once by copy-pasting Phase B into Phase C)."""
    global CFG
    phase = {"testnet": "C", "mainnet": "D"}[network]
    p = argparse.ArgumentParser(description=f"Phase {phase} — {network.upper()} execution.")
    p.add_argument("--arm", action="store_true", help="actually send orders (default: log only)")
    p.add_argument("--once", action="store_true", help="one tick then exit")
    p.add_argument("--enter", type=float, metavar="USD",
                   help="open a fresh neutral position of USD per leg, then exit")
    p.add_argument("--flatten", action="store_true",
                   help="close both legs (buy back short, sell spot), then exit")
    p.add_argument("--slippage", type=float, default=CFG.flatten_slippage,
                   help="max slippage for --flatten (default %(default)s)")
    args = p.parse_args()

    # Windows consoles default to cp1252 and crash on the zone emojis.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    log = logging.getLogger(f"phase{phase}")

    address = os.environ.get("HL_ACCOUNT_ADDRESS")
    agent_key = os.environ.get("HL_AGENT_KEY")
    if not address or not agent_key:
        sys.exit("Set HL_ACCOUNT_ADDRESS (master) and HL_AGENT_KEY (agent private key).")

    # NETWORK GUARD — testnet unless phase_d_mainnet.py explicitly asks otherwise
    if network == "testnet":
        base_url = hl_constants.TESTNET_API_URL
        assert "testnet" in base_url, "refusing to run: base_url is not testnet"
    elif network == "mainnet":
        base_url = hl_constants.MAINNET_API_URL
    else:
        sys.exit(f"unknown network {network!r}")

    wallet = eth_account.Account.from_key(agent_key)
    info = Info(base_url, skip_ws=True)
    exch = Exchange(wallet, base_url, account_address=address)

    # ACCOUNT MODE GUARD — the whole design (3 pockets, top-up transfers,
    # M read from the perp state) assumes SEPARATE spot and perp balances.
    # In "unifiedAccount" / "portfolioMargin" modes HL reports everything in
    # the spot state and says the perp state is "not meaningful" → M is wrong.
    mode = info.query_user_abstraction_state(address)
    if mode != "disabled":
        msg = (f"account mode is {mode!r}, but this bot needs separate spot/perp "
               f"balances (HL 'Manual/Standard' mode, API value 'disabled').")
        if network == "mainnet":
            sys.exit("REFUSING TO RUN: " + msg + " Switch the account mode in the HL UI first.")
        log.warning(msg + " M and R readings are unreliable here.")

    venue = load_venue(info, log)

    # maintenance margin from the venue: HL defines it as half the initial
    # margin at max leverage (lowest margin tier — valid for small positions)
    if venue.max_leverage:
        CFG = replace(CFG, mm=0.5 / venue.max_leverage)
        log.info("mm = %.2f%% (= half of 1/%dx, from venue)", CFG.mm * 100, venue.max_leverage)

    # MAINNET CONFIRMATION — real money, so a human types it every armed start
    if network == "mainnet" and args.arm:
        log.warning("MAINNET + --arm: this will trade REAL money on %s.", address)
        if input('Type MAINNET to continue: ').strip() != "MAINNET":
            sys.exit("not confirmed — nothing sent.")

    # set cross leverage once — a signed SEND, so it respects --arm like the rest.
    # It does NOT change an already-open position's margin mode.
    if not args.arm:
        log.info("[DRY] update_leverage %dx cross on %s", CFG.leverage, CFG.perp_coin)
    else:
        try:
            r = exch.update_leverage(CFG.leverage, CFG.perp_coin, is_cross=True)
            if isinstance(r, dict) and r.get("status") == "ok":
                log.info("leverage set: %dx cross on %s", CFG.leverage, CFG.perp_coin)
            else:
                log.warning("leverage NOT set (set it in the UI): %s", r)
        except Exception as e:
            log.warning("could not set leverage (set it manually in the UI): %s", e)

    armed = args.arm
    ex = Executor(exch, info, venue, armed, log)
    log.info("Phase %s on %s — %s. Master %s", phase, network.upper(),
             "ARMED (will send)" if armed else "UNARMED (log only)", address)
    if not armed:
        log.info("Running UNARMED: same code path as armed, but nothing is sent. Add --arm to trade.")

    # flatten mode (runs unarmed as a dry run)
    if args.flatten:
        st = read_state(info, address, venue)
        ex.flatten(st, args.slippage)
        if armed:
            time.sleep(3)
            st2 = read_state(info, address, venue)
            log.info("POST-FLATTEN: %s spot=$%.2f (%.4f) | short=%.4f | M=$%.2f | R=$%.2f",
                     CFG.spot_base, st2.spot_value, st2.spot_qty, st2.short_qty, st2.M, st2.R)
            log.info("FLAT ✅" if st2.is_flat else
                     "NOT flat yet — book too thin? re-run --flatten, or raise --slippage")
        return

    # entry mode
    if args.enter is not None:
        if not armed:
            log.error("--enter needs --arm (it places real orders).")
            return
        st = read_state(info, address, venue)
        ex.open_position(args.enter, st)
        time.sleep(2)
        st2 = read_state(info, address, venue)
        log.info("POST-ENTRY: %s spot=$%.2f | M=$%.2f | R=$%.2f | N=$%.2f | s=%s",
                 CFG.spot_base, st2.spot_value, st2.M, st2.R, st2.N,
                 f"{st2.s:.3f}" if st2.N > 0 else "n/a")
        if st2.is_flat:
            log.warning("nothing opened — account is still flat.")
        elif reconcile(st2, log):
            log.info("post-entry reconciliation OK — legs look neutral.")
        else:
            log.error("POST-ENTRY RECONCILIATION FAILED — inspect the account NOW.")
        return

    # manage loop
    try:
        while True:
            try:
                manage_tick(info, address, ex, log)
            except Exception as e:
                log.exception("tick failed: %s", e)
            if args.once:
                break
            time.sleep(CFG.poll_seconds)
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main("testnet")
