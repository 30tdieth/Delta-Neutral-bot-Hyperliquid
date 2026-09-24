#!/usr/bin/env python3
"""
Paper simulator — runs the REAL Phase C control loop against a fake exchange.
=============================================================================

Nothing here talks to Hyperliquid (except `--live`, a read-only price fetch).
`PaperHL` impersonates the handful of Info + Exchange methods Phase C calls,
so `read_state`, `reconcile`, `decide`, `Executor` and `manage_tick` are the
exact code that will run on mainnet — not a copy.

Each scenario scripts a price path, runs the bot tick by tick, then checks
that it did the right thing. Reproducible: same input, same output, seconds.

MODEL (the mainnet target, not the testnet stand-in):
  * perp BTC short + spot UBTC long, 5 size decimals each (read from mainnet)
  * two separate wallets (no portfolio margin) — the doc's 3-pocket model
  * cross margin: M = perp collateral + unrealized PnL
  * taker fees  perp 0.045%, spot 0.070%  (HL base tier — verify current)
  * fills at mid ± 0.05%
  * hourly funding, 1 tick = 1 hour, positive rate = shorts get paid
  * liquidation when M <= SIM_MM x N, perp margin lost, spot leg untouched
  * SIM_MM = 1.25% = half the initial margin at BTC's 40x max leverage (HL
    rule) — same value the bot reads from the venue.
  * "agent" scenarios reject usdClassTransfer exactly like mainnet does for an
    agent key; the other scenarios model a key that CAN transfer (design
    reference only — not what runs live).

USAGE:
  python paper_sim.py               # all scenarios
  python paper_sim.py topup reduce  # some scenarios
  python paper_sim.py --verbose     # also print the bot's own log lines
  python paper_sim.py --live        # start from the live mainnet BTC mark
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import sys
import time
import types
from typing import Callable

import phase_c_testnet as pc

# --- point the real bot code at the mainnet target, and make it instant ----
pc.CFG = dataclasses.replace(pc.CFG, perp_coin="BTC", spot_base="UBTC")
pc.time = types.SimpleNamespace(time=time.time, sleep=lambda _s: None)

ADDR = "0xPAPER"
VENUE = pc.Venue(perp_sz_decimals=5, spot_name="@142", spot_sz_decimals=5)

PERP_TAKER = 0.00045
SPOT_TAKER = 0.00070
FILL_SLIP = 0.0005
FUNDING_H = 0.0000125
SIM_MM = 0.0125
P0_DEFAULT = 77_000.0


# ==========================================================================
# FAKE EXCHANGE
# ==========================================================================

class PaperHL:
    """Implements only what Phase C calls: user_state, spot_user_state,
    meta_and_asset_ctxs, all_mids (Info) and market_open, market_close,
    usd_class_transfer, update_leverage, schedule_cancel (Exchange)."""

    def __init__(self, usdc: float, px: float, perp: float = 0.0, agent: bool = False):
        self.account_address = ADDR
        self.agent = agent             # agent key → transfers rejected
        self.spot_usdc = usdc - perp   # pocket 3: reserve
        self.spot_qty = 0.0            # pocket 1: long leg
        self.collateral = perp         # pocket 2: perp cash
        self.szi = 0.0                 # signed perp size, < 0 = short
        self.entry = 0.0
        self.mark = self.spot_px = px
        self.fail = {"perp": 0, "spot": 0, "transfer": 0}
        self.events: list[dict] = []
        self.tick: object = "E"
        self.liquidated = False
        self.fees = 0.0
        self.funding = 0.0

    # ---- market ----------------------------------------------------------
    def set_prices(self, mark: float, spot: float) -> None:
        self.mark, self.spot_px = mark, spot

    def account_value(self) -> float:
        return self.collateral + self.szi * (self.mark - self.entry)

    def accrue_funding(self) -> None:
        if self.szi < 0:
            f = -self.szi * self.mark * FUNDING_H
            self.collateral += f
            self.funding += f

    def check_liquidation(self) -> None:
        N = -self.szi * self.mark
        if self.szi < 0 and self.account_value() <= SIM_MM * N:
            lost = self.collateral
            self._ev("liq", "", 0, False,
                     f"💥 LIQUIDÉ — marge perp perdue ${lost:.2f}")
            self.szi = self.entry = self.collateral = 0.0
            self.liquidated = True

    def _ev(self, kind, side, sz, ok, text) -> None:
        self.events.append(dict(tick=self.tick, kind=kind, side=side,
                                sz=sz, ok=ok, text=text))

    # ---- Info API --------------------------------------------------------
    def user_state(self, _addr):
        pos = [] if self.szi == 0 else [{"position": {
            "coin": pc.CFG.perp_coin, "szi": str(self.szi), "entryPx": str(self.entry)}}]
        return {"assetPositions": pos,
                "marginSummary": {"accountValue": str(self.account_value())}}

    def spot_user_state(self, _addr):
        return {"balances": [
            {"coin": "USDC", "total": str(self.spot_usdc), "hold": "0"},
            {"coin": pc.CFG.spot_base, "total": str(self.spot_qty), "hold": "0"}]}

    def meta_and_asset_ctxs(self):
        return [{"universe": [{"name": pc.CFG.perp_coin}]}, [{"markPx": str(self.mark)}]]

    def all_mids(self):
        return {pc.CFG.perp_coin: str(self.mark), VENUE.spot_name: str(self.spot_px)}

    # ---- Exchange API ----------------------------------------------------
    @staticmethod
    def _filled(sz, px):
        return {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"filled": {"totalSz": str(sz), "avgPx": str(px), "oid": 0}}]}}}

    @staticmethod
    def _err(msg):
        return {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"error": msg}]}}}

    def _take_fail(self, leg) -> bool:
        if self.fail[leg] > 0:
            self.fail[leg] -= 1
            return True
        return False

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None):
        if name == pc.CFG.perp_coin:
            return self._perp(is_buy, sz)
        if name == VENUE.spot_name:
            return self._spot(is_buy, sz)
        return self._err(f"unknown asset {name}")

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None):
        if self.szi == 0:
            return self._err("no open position")
        return self._perp(self.szi < 0, abs(self.szi) if sz is None else sz)

    def _perp(self, is_buy, sz):
        side = "BUY" if is_buy else "SELL"
        if self._take_fail("perp"):
            self._ev("perp", side, sz, False, f"perp {side} {sz:g} ✗ rejeté (panne injectée)")
            return self._err("injected failure")
        px = self.mark * ((1 + FILL_SLIP) if is_buy else (1 - FILL_SLIP))
        fee = sz * px * PERP_TAKER
        new = round(self.szi + (sz if is_buy else -sz), 10)
        if not is_buy and self.szi <= 0:                       # open / grow short
            if self.account_value() - fee < -new * self.mark / pc.CFG.leverage:
                self._ev("perp", side, sz, False, f"perp SELL {sz:g} ✗ marge insuffisante")
                return self._err("Insufficient margin to place order.")
            self.entry = (self.entry * -self.szi + px * sz) / (-self.szi + sz)
        elif is_buy and self.szi < 0:                          # reduce short
            self.collateral += min(sz, -self.szi) * (self.entry - px)
            if new > 0:
                self.entry = px
        else:
            self.entry = px
        self.szi = 0.0 if abs(new) < 1e-12 else new
        if self.szi == 0:
            self.entry = 0.0
        self.collateral -= fee
        self.fees += fee
        self._ev("perp", side, sz, True, f"perp {side} {sz:g} @ {px:,.0f}")
        return self._filled(sz, px)

    def _spot(self, is_buy, sz):
        side = "BUY" if is_buy else "SELL"
        if self._take_fail("spot"):
            self._ev("spot", side, sz, False, f"spot {side} {sz:g} ✗ rejeté (panne injectée)")
            return self._err("injected failure")
        px = self.spot_px * ((1 + FILL_SLIP) if is_buy else (1 - FILL_SLIP))
        fee = sz * px * SPOT_TAKER
        if is_buy:
            if sz * px + fee > self.spot_usdc + 1e-9:
                self._ev("spot", side, sz, False, f"spot BUY {sz:g} ✗ USDC insuffisant")
                return self._err("Insufficient balance")
            self.spot_usdc -= sz * px + fee
            self.spot_qty += sz
        else:
            if sz > self.spot_qty + 1e-12:
                self._ev("spot", side, sz, False, f"spot SELL {sz:g} ✗ solde insuffisant")
                return self._err("Insufficient balance")
            self.spot_qty = round(self.spot_qty - sz, 10)
            self.spot_usdc += sz * px - fee
        self.fees += fee
        self._ev("spot", side, sz, True, f"spot {side} {sz:g} @ {px:,.0f}")
        return self._filled(sz, px)

    def usd_class_transfer(self, amount, to_perp):
        way = "réserve→perp" if to_perp else "perp→réserve"
        if self.agent:
            self._ev("transfer", way, amount, False, f"transfert ${amount:.2f} ✗ refusé (clé d'agent)")
            return {"status": "err", "response": "Must deposit before performing actions. User: 0xAGENT"}
        if self._take_fail("transfer"):
            self._ev("transfer", way, amount, False, f"transfert ${amount:.2f} ✗ (panne injectée)")
            return {"status": "err", "response": "injected failure"}
        if to_perp:
            if amount > self.spot_usdc + 1e-9:
                self._ev("transfer", way, amount, False, f"transfert ${amount:.2f} ✗ réserve insuffisante")
                return {"status": "err", "response": "Insufficient balance"}
            self.spot_usdc -= amount
            self.collateral += amount
        else:
            free = self.account_value() - abs(self.szi) * self.mark / pc.CFG.leverage
            if amount > free + 1e-9:
                return {"status": "err", "response": "Insufficient withdrawable"}
            self.collateral -= amount
            self.spot_usdc += amount
        self._ev("transfer", way, amount, True, f"transfert ${amount:.2f} {way}")
        return {"status": "ok"}

    def update_leverage(self, *_a, **_k):
        return {"status": "ok"}

    def schedule_cancel(self, _t):
        return {"status": "ok"}


# ==========================================================================
# HARNESS
# ==========================================================================

class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@dataclasses.dataclass
class Row:
    tick: object
    perp: float
    spot: float
    s_before: float
    zone: str
    s_after: float
    actions: list
    notes: list


@dataclasses.dataclass
class Run:
    paper: PaperHL
    ex: pc.Executor
    rows: list
    entry: pc.State
    final: pc.State
    entry_fees: float

    def events(self, kind=None, side=None, ok=None, after_entry=True):
        return [e for e in self.paper.events
                if (not after_entry or e["tick"] != "E")
                and (kind is None or e["kind"] == kind)
                and (side is None or e["side"] == side)
                and (ok is None or e["ok"] == ok)]


@dataclasses.dataclass
class Scenario:
    key: str
    title: str
    question: str
    capital: float
    enter_usd: float
    path: list                        # [(perp_mult, spot_mult), ...] vs P0
    checks: Callable[[Run], list]
    fail_on_entry: dict = dataclasses.field(default_factory=dict)
    fail_after_entry: dict = dataclasses.field(default_factory=dict)
    perp_funding: float = 0.0         # USDC already in the perp wallet at start
    transfers: bool = True            # False = agent key (mainnet reality)


_quiet = logging.getLogger("sim.quiet")
_quiet.addHandler(logging.NullHandler())
_quiet.propagate = False


def classify(st: pc.State, ex: pc.Executor) -> str:
    if ex.paused:
        return "⏸ PAUSE"
    if not pc.reconcile(st, _quiet):
        return "⛔ BLOQUÉ"
    return pc.decide(st).zone.value


def run(sc: Scenario, p0: float, verbose: bool) -> Run:
    botlog = logging.getLogger(f"bot.{sc.key}")
    botlog.handlers.clear()
    botlog.propagate = False
    botlog.setLevel(logging.INFO)
    cap = ListHandler()
    botlog.addHandler(cap)
    if verbose:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("        bot │ %(levelname)-7s %(message)s"))
        botlog.addHandler(h)

    pc.CFG = dataclasses.replace(pc.CFG, transfers_enabled=sc.transfers)
    paper = PaperHL(sc.capital, p0, sc.perp_funding, agent=not sc.transfers)
    ex = pc.Executor(paper, paper, VENUE, True, botlog)
    rows: list[Row] = []

    def tick_notes():
        out = [r.getMessage() for r in cap.records if r.levelno >= logging.WARNING]
        cap.records.clear()
        return out

    def tick_actions(t):
        return [e["text"] for e in paper.events if e["tick"] == t]

    # entry
    paper.fail.update(sc.fail_on_entry)
    st0 = pc.read_state(paper, ADDR, VENUE)
    ex.open_position(sc.enter_usd, st0)
    entry = pc.read_state(paper, ADDR, VENUE)
    rows.append(Row("E", p0, p0, st0.s, "entrée", entry.s, tick_actions("E"), tick_notes()))
    entry_fees = paper.fees
    paper.fail.update(sc.fail_after_entry)

    for t, (pm, sm) in enumerate(sc.path, 1):
        paper.tick = t
        paper.set_prices(p0 * pm, p0 * sm)
        paper.accrue_funding()
        paper.check_liquidation()
        st = pc.read_state(paper, ADDR, VENUE)
        zone = classify(st, ex)
        try:
            pc.manage_tick(paper, ADDR, ex, botlog)
        except Exception as e:                       # a crash is a finding
            botlog.error("EXCEPTION in manage_tick: %r", e)
        after = pc.read_state(paper, ADDR, VENUE)
        rows.append(Row(t, paper.mark, paper.spot_px, st.s, zone, after.s,
                        tick_actions(t), tick_notes()))

    return Run(paper, ex, rows, entry, pc.read_state(paper, ADDR, VENUE), entry_fees)


# ==========================================================================
# REPORT
# ==========================================================================

def fmt_s(x: float) -> str:
    return "   —  " if math.isinf(x) else f"{x * 100:5.1f}%"


def print_run(sc: Scenario, r: Run, p0: float) -> list:
    print(f"\n{'═' * 96}\n▶ {sc.key.upper()} — {sc.title}\n  {sc.question}")
    mode = "clé d'agent, sans virement" if not sc.transfers else "virements autorisés (référence)"
    print(f"  capital ${sc.capital:,.0f} (dont ${sc.perp_funding:,.0f} côté perp) · entrée "
          f"${sc.enter_usd:,.0f}/jambe · BTC départ ${p0:,.0f} · {mode}")
    print(f"{'─' * 96}")
    print(f"  {'t':>2} │ {'BTC perp':>11} │ {'uBTC spot':>11} │ {'s avant':>7} │ {'zone':<11} │ {'s après':>7} │ action")
    print(f"  {'─' * 2}─┼─{'─' * 11}─┼─{'─' * 11}─┼─{'─' * 7}─┼─{'─' * 11}─┼─{'─' * 7}─┼─{'─' * 28}")
    for row in r.rows:
        mv = lambda px: f"{px:>11,.0f}" if row.tick == "E" else f"{(px / p0 - 1) * 100:+10.1f}%"
        acts = row.actions or ["—"]
        print(f"  {str(row.tick):>2} │ {mv(row.perp)} │ {mv(row.spot)} │ {fmt_s(row.s_before):>7} │ "
              f"{row.zone:<11} │ {fmt_s(row.s_after):>7} │ {acts[0]}")
        for a in acts[1:]:
            print(f"  {'':>2} │ {'':>11} │ {'':>11} │ {'':>7} │ {'':<11} │ {'':>7} │ {a}")
        for n in row.notes:
            print(f"  {'':>2}   ↳ {n[:88]}")
    results = sc.checks(r)
    print(f"{'─' * 96}")
    for status, label in results:
        icon = {"PASS": "✅", "FAIL": "❌", "NOTE": "ℹ️ "}[status]
        print(f"  {icon} {label}")
    return results


def drift_rows(r: Run) -> list:
    return [row for row in r.rows if any("DELTA DRIFT" in n for n in row.notes)]


def ok(cond: bool, label: str):
    return ("PASS" if cond else "FAIL", label)


def note(label: str):
    return ("NOTE", label)


# ==========================================================================
# SCENARIOS
# ==========================================================================

def chk_calm(r: Run):
    per_day = r.paper.funding / len(r.rows[1:]) * 24
    round_trip = 2 * r.entry_fees
    return [
        ok(not r.events(kind="perp") and not r.events(kind="spot") and not r.events(kind="transfer"),
           "aucun ordre ni transfert après l'entrée (le bot laisse tranquille)"),
        ok(not r.paper.liquidated, "jamais liquidé"),
        ok(not drift_rows(r), "aucune fausse alerte de dérive (frais et arrondis < 2 %)"),
        ok(r.paper.funding > 0, f"funding encaissé : ${r.paper.funding:.4f} sur 24 h"),
        note(f"frais aller-retour ≈ ${round_trip:.3f} → {round_trip / per_day:.0f} jours de funding "
             f"pour les rembourser (les frais tuent en silence à petite taille)"),
    ]


def chk_topup(r: Run):
    tu = r.events(kind="transfer", ok=True)
    tu_ticks = {e["tick"] for e in tu}
    after = [row.s_after for row in r.rows if row.tick in tu_ticks]
    return [
        ok(len(tu) >= 1, f"renflouement déclenché ({len(tu)}×)"),
        ok(not r.events(kind="perp") and not r.events(kind="spot"),
           "aucun trade — un renflouement déplace du cash, il ne touche pas aux positions"),
        ok(bool(after) and min(after) >= 0.215, "s remonté à ~22 % après chaque renflouement"),
        ok(abs(r.final.short_qty - r.entry.short_qty) < 1e-9 and abs(r.final.spot_qty - r.entry.spot_qty) < 1e-9,
           "tailles des deux jambes inchangées"),
    ]


def chk_reduce(r: Run):
    buys = r.events(kind="perp", side="BUY", ok=True)
    sells = r.events(kind="spot", side="SELL", ok=True)
    paired = {e["tick"] for e in buys} == {e["tick"] for e in sells}
    return [
        ok(len(r.events(kind="transfer", ok=True)) >= 1, "renflouement(s) tant que la réserve suffit"),
        ok(len(buys) >= 1, f"réduction déclenchée une fois la réserve à sec ({len(buys)}×)"),
        ok(paired and bool(buys), "chaque réduction coupe les DEUX jambes au même tick"),
        ok(not r.paper.liquidated, "jamais liquidé malgré +35 %"),
        ok(pc.reconcile(r.final, _quiet) and not drift_rows(r),
           "toujours neutre à la fin, aucune dérive > 2 %"),
        ok(r.final.short_qty < r.entry.short_qty,
           f"position rétrécie : short {r.entry.short_qty:g} → {r.final.short_qty:g} BTC"),
    ]


def chk_leg2(r: Run):
    e = [x for x in r.paper.events if x["tick"] == "E"]
    spot_rej = any(x["kind"] == "spot" and not x["ok"] for x in e)
    unwind = any(x["kind"] == "perp" and x["side"] == "BUY" and x["ok"] for x in e)
    return [
        ok(spot_rej, "jambe 2 (spot) rejetée à l'entrée"),
        ok(unwind, "jambe 1 (short) immédiatement débouclée"),
        ok(r.final.is_flat, "compte à plat — aucune exposition nue"),
        note(f"coût de l'entrée ratée : ${r.paper.fees:.3f} de frais"),
    ]


def chk_breaker(r: Run):
    rej = r.events(ok=False)
    t1 = [e["kind"] for e in r.events(ok=False) if e["tick"] == 1]
    return [
        ok(t1 == ["transfer", "perp"], "tick 1 : renflouement échoue → repli sur la réduction (échoue aussi)"),
        ok(r.ex.paused, "coupe-circuit déclenché → trading en pause"),
        ok(len(rej) == pc.CFG.max_consecutive_failures,
           f"arrêt après {pc.CFG.max_consecutive_failures} échecs consécutifs ({len(rej)} vus)"),
        ok(not r.events(kind="spot"), "spot jamais vendu sans le rachat du short → neutralité préservée"),
        ok(not r.paper.liquidated, "pas liquidé pendant la pause"),
    ]


def chk_rescue(r: Run):
    t1 = r.rows[1]
    return [
        ok(t1.zone.startswith("🔴") and t1.s_before < pc.CFG.hard_red,
           f"zone rouge profonde (s = {t1.s_before * 100:.1f} % < 14 %)"),
        ok(any("transfert" in a and "✗" not in a for a in t1.actions),
           "renflouement d'abord — la réserve suffit"),
        ok(t1.s_after >= 0.215, f"s restauré à {t1.s_after * 100:.1f} % sans passer un seul ordre"),
        ok(not r.events(kind="perp") and not r.ex.paused,
           "carnet en panne ignoré : aucun ordre tenté, pas de pause"),
    ]


def chk_depeg(r: Run):
    blocked = [row for row in r.rows if row.zone == "⛔ BLOQUÉ"]
    first = (blocked[0].spot / blocked[0].perp - 1) * 100 if blocked else None
    alerts = drift_rows(r)
    warn = (alerts[0].spot / alerts[0].perp - 1) * 100 if alerts else None
    return [
        ok(bool(alerts) and warn > -6, f"alerte de dérive dès {warn:+.0f} % (seuil 2 %)" if alerts
           else "décrochage jamais signalé"),
        ok(bool(blocked), f"blocage complet au-delà de 25 % (à {first:+.0f} %)" if blocked
           else "jamais bloqué"),
        ok(not r.events(kind="perp") and not r.events(kind="spot"), "aucun trade sur un état incohérent"),
        note("ancienne valorisation (spot au prix du perp) : écart 0 % à chaque tick → jamais détecté"),
    ]


def chk_gap20(r: Run):
    t1 = r.rows[1]
    return [
        ok(not r.paper.liquidated, f"survit à +20 % d'un coup (s d'entrée {r.entry.s * 100:.1f} %)"),
        ok(t1.zone.startswith("🔴") and any("transfert" in a for a in t1.actions)
           and not r.events(kind="perp"),
           "zone rouge, réserve suffisante → renflouement, pas de réduction"),
        ok(t1.s_after >= 0.215, f"s restauré à {t1.s_after * 100:.1f} %"),
        ok(abs(r.final.short_qty - r.entry.short_qty) < 1e-12, "position conservée intacte"),
    ]


def chk_gap27(r: Run):
    liq = [e for e in r.paper.events if e["kind"] == "liq"]
    return [
        ok(r.paper.liquidated, f"liquidé — attendu : +27 % dépasse le mouvement survivable "
                               f"(s d'entrée {r.entry.s * 100:.1f} %)"),
        ok(all(row.zone == "⛔ BLOQUÉ" for row in r.rows[1:]),
           "après liquidation : long nu détecté → bot bloqué"),
        ok(not r.events(kind="perp") and not r.events(kind="spot"), "aucun ordre sur la jambe restante"),
        ok(abs(r.final.spot_qty - r.entry.spot_qty) < 1e-12,
           "jambe spot intacte — la perte est bornée à la marge perp"),
        note(liq[0]["text"] if liq else "—"),
    ]


def chk_agent_nofund(r: Run):
    notes = [n for row in r.rows for n in row.notes]
    return [
        ok(not r.events(kind="transfer", after_entry=False), "aucun virement tenté"),
        ok(not r.events(kind="perp", after_entry=False) and not r.events(kind="spot", after_entry=False),
           "aucun ordre : entrée refusée proprement"),
        ok(any("cannot move USDC" in n for n in notes),
           "message clair : virer la marge côté perp via l'interface"),
        ok(r.final.is_flat, "compte toujours à plat"),
    ]


def chk_agent_live(r: Run):
    orange = [row for row in r.rows if row.zone.startswith("🟠")]
    buys = {e["tick"] for e in r.events(kind="perp", side="BUY", ok=True)}
    sells = {e["tick"] for e in r.events(kind="spot", side="SELL", ok=True)}
    top = max(row.perp for row in r.rows) / r.rows[0].perp - 1
    return [
        ok(r.entry.short_qty > 0 and r.entry.s > 0.35,
           f"entrée sans virement, s de départ {r.entry.s * 100:.1f} % (réserve déjà côté perp)"),
        ok(not r.events(kind="transfer", after_entry=False), "aucun virement tenté, jamais"),
        ok(bool(orange) and all(not row.actions and any("ACTION NEEDED" in n for n in row.notes)
                                for row in orange),
           f"zone orange → alerte « renflouer à la main », aucun ordre ({len(orange)}×)"),
        ok(bool(buys) and buys == sells, f"zone rouge → réduction des deux jambes au même tick ({len(buys)}×)"),
        ok(not r.ex.paused, "coupe-circuit jamais déclenché"),
        ok(not r.paper.liquidated, f"jamais liquidé malgré +{top * 100:.0f} %"),
        ok(pc.reconcile(r.final, _quiet) and not drift_rows(r), "toujours neutre, aucune dérive"),
    ]


def scenarios() -> list[Scenario]:
    flat = lambda xs: [(x, x) for x in xs]
    return [
        Scenario("agent_nofund", "clé d'agent, marge perp non approvisionnée",
                 "Q : réserve restée côté spot — le bot refuse-t-il d'entrer sans rien casser ?",
                 117, 80, flat([1.0]), chk_agent_nofund, transfers=False),
        Scenario("agent_live", "clé d'agent, réserve côté perp, hausse de 40 %",
                 "Q : configuration mainnet réelle — alerte en orange, réduction en rouge, jamais liquidé ?",
                 117, 80, flat([1.10, 1.21, 1.23, 1.26, 1.30, 1.35, 1.40]), chk_agent_live,
                 perp_funding=35, transfers=False),
        Scenario("calm", "marché calme 24 h",
                 "Q : le bot reste-t-il tranquille quand rien ne se passe ?",
                 1000, 100, flat([1 + 0.015 * math.sin(t / 2) for t in range(1, 25)]), chk_calm),
        Scenario("topup", "hausse de 7,5 % → renflouement",
                 "Q : le short souffre — le bot vire-t-il la réserve vers la marge, sans trader ?",
                 1000, 100, flat([1.02, 1.04, 1.055, 1.07, 1.075]), chk_topup),
        Scenario("reduce", "hausse de 35 %, petite réserve → réduction",
                 "Q : réserve épuisée — le bot coupe-t-il les deux jambes ensemble ?",
                 140, 100, flat([1 + 0.025 * t for t in range(1, 15)]), chk_reduce),
        Scenario("leg2_fail", "la jambe spot échoue à l'entrée",
                 "Q : leg 1 passe, leg 2 échoue — le bot débouclé-t-il au lieu de rester nu ?",
                 1000, 100, flat([1.0, 1.0]), chk_leg2, fail_on_entry={"spot": 1}),
        Scenario("breaker", "tout échoue en zone rouge (virements ET ordres)",
                 "Q : renflouement et réduction échouent — le coupe-circuit s'arrête-t-il après 3 échecs ?",
                 1000, 100, flat([1.11] * 5), chk_breaker,
                 fail_after_entry={"perp": 99, "transfer": 99}),
        Scenario("rescue", "zone rouge, carnet en panne, réserve pleine",
                 "Q : les ordres échouent en plein krach — un virement suffit-il à sauver la position ?",
                 1000, 100, flat([1.11] * 3), chk_rescue, fail_after_entry={"perp": 99}),
        Scenario("depeg", "décrochage de l'uBTC (BTC stable)",
                 "Q : la jambe spot perd sa parité — le bot le voit-il ?",
                 1000, 100, [(1.0, m) for m in (0.95, 0.90, 0.85, 0.80, 0.72, 0.65)], chk_depeg),
        Scenario("gap20", "saut de +20 % pendant la nuit",
                 "Q : un gros saut dans la fenêtre aveugle — le tampon suffit-il ?",
                 1000, 100, flat([1.20, 1.20, 1.20]), chk_gap20),
        Scenario("gap27", "saut de +27 % pendant la nuit",
                 "Q : au-delà du tampon — que se passe-t-il, et que fait le bot ensuite ?",
                 1000, 100, flat([1.27, 1.27]), chk_gap27),
    ]


# ==========================================================================
# MAIN
# ==========================================================================

def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    all_sc = scenarios()
    ap = argparse.ArgumentParser(description="Paper simulator for the Phase C control loop.")
    ap.add_argument("only", nargs="*", help="scenario keys: " + ", ".join(s.key for s in all_sc))
    ap.add_argument("--verbose", action="store_true", help="print the bot's own log lines")
    ap.add_argument("--live", action="store_true", help="start from the live mainnet BTC mark (read-only)")
    args = ap.parse_args()

    p0 = P0_DEFAULT
    if args.live:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants as hl_constants
        meta, ctxs = Info(hl_constants.MAINNET_API_URL, skip_ws=True).meta_and_asset_ctxs()
        p0 = float(ctxs[[a["name"] for a in meta["universe"]].index("BTC")]["markPx"])

    chosen = [s for s in all_sc if not args.only or s.key in args.only]
    unknown = set(args.only) - {s.key for s in all_sc}
    if unknown:
        sys.exit(f"unknown scenario(s): {', '.join(sorted(unknown))}")

    summary = []
    for sc in chosen:
        r = run(sc, p0, args.verbose)
        res = print_run(sc, r, p0)
        n_fail = sum(1 for s, _ in res if s == "FAIL")
        n_pass = sum(1 for s, _ in res if s == "PASS")
        summary.append((sc.key, sc.title, n_pass, n_fail))

    print(f"\n{'═' * 96}\nBILAN   (mm simulé {SIM_MM * 100:.2f} % · mm du bot {pc.CFG.mm * 100:.2f} %)")
    for key, title, n_pass, n_fail in summary:
        print(f"  {'✅' if n_fail == 0 else '❌'} {key:<10} {n_pass}/{n_pass + n_fail}  {title}")
    sys.exit(1 if any(f for *_, f in summary) else 0)


if __name__ == "__main__":
    main()
