# Delta-neutral funding-rate bot — Hyperliquid

*[Version française](README.fr.md)*

An automated trading bot that collects perpetual-futures **funding** while taking **no directional bet on price**. Python, Hyperliquid API, run in live conditions.

> ### About this project
> This is an **educational experiment**, and it is **not meant to be profitable**. The goal was to learn how to build the machinery around money properly: control loop, margin management, state reconciliation, safety gates, validation. Position sizes are deliberately tiny — at that scale fees eat most of the yield, and that trade-off was accepted from the start.
>
> The repository is published as a technical demonstration. It is not investment advice, and not software to run without reading and understanding it first.

---

## 1. The strategy in one minute

On a perpetual futures contract, a mechanism called **funding** periodically moves money between buyers and sellers to keep the contract price anchored to the real one. In a bullish market, short sellers are the ones being paid.

So the idea is to be short and collect that stream — except being short is a bet that price falls. That bet is neutralised by **buying the same quantity on the spot market at the same time**:

| If Bitcoin… | Spot leg | Short leg | Net |
|---|---|---|---|
| rises | gains | loses as much | ≈ 0 |
| falls | loses | gains as much | ≈ 0 |

The price exposure cancels out — that is what **delta-neutral** means. What remains is the funding.

**Where it gets interesting:** neutrality applies to price, not to everything else. Three risks remain, and the entire codebase is built around them.

1. **Liquidation.** The short leg is backed by posted collateral. If price rises sharply that collateral shrinks, and the venue closes the position for you. This is the primary risk.
2. **The peg.** The two legs are not exactly the same asset: one is a token that *represents* Bitcoin. If it de-pegs, the two legs stop cancelling out.
3. **Friction.** Fees and slippage, all the more visible on a small position.

---

## 2. How the bot measures risk

The bot remembers nothing between cycles: it re-reads the real account state and recomputes everything from six quantities.

| Variable | Meaning |
|---|---|
| `mark` | The venue's official perp price — **the one that triggers a liquidation** |
| `spot_px` | The token's price on its own order book — what the long leg would actually sell at |
| `N` | Size of the short position, in dollars |
| `M` | Margin posted on the perp side: the cushion that absorbs losses |
| `R` | Available USDC reserve |
| `mm` | Maintenance margin required by the venue, read at every startup |

The two prices are read **separately**, by design: using a single price for both legs would make the bot structurally blind to a de-peg of the token — precisely risk #2.

### The core indicator

```
s = M / N − mm
```

`s` reads as a percentage: **how far price can rise before the short leg is liquidated**. The larger the number, the more room there is.

| Zone | `s` | Behaviour |
|---|---|---|
| 🟢 Green | ≥ 22 % | Do nothing |
| 🟡 Yellow | 18–22 % | Watch |
| 🟠 Orange | ≤ 18 % | Alert: margin needs topping up |
| 🔴 Red | < 14 % | Cut both legs, automatically and simultaneously |

Two details that matter:

- **The yellow band is a deliberate dead zone.** Without a gap between the trigger threshold and the target it restores, the bot would react to every small price wiggle. Same principle as a thermostat.
- **`s` only looks at the short leg.** An excellent `s` can coexist with badly unbalanced legs. Checking neutrality is therefore a *separate* control — conflating the two is a classic trap.

### Where the thresholds come from

From an analysis of 9 years of hourly Bitcoin prices: over the last two years, no rise greater than 12 % in any 8-hour window. The 8-hour window stands for an unattended night, and an 18 % floor leaves headroom beyond the worst observed case. These thresholds are calibrated on Bitcoin and would not transfer as-is to a more volatile asset.

---

## 3. The control loop

Every 60 seconds, the same cycle:

| # | Step |
|---|---|
| 1 | Read real venue state — position, margin, balances, both prices |
| 2 | Check the circuit breaker has not tripped |
| 3 | **Reconcile**: do both legs exist? is their gap above 25 %? |
| 4 | **Check drift**: is the gap between legs above 2 %? (early de-peg detection) |
| 5 | Compute `s`, derive the zone, act if needed |
| 6 | Re-arm the dead-man's switch |

The guiding rule: **never trust a remembered value**. A bot that reasons about its own internal state eventually drifts away from reality.

---

## 4. Architecture

| File | Role |
|---|---|
| `phase_c_testnet.py` | **The engine** — state reading, decisions, execution, safety gates |
| `phase_d_mainnet.py` | Production launcher: **no logic**, settings only |
| `paper_sim.py` | Simulator: runs the real engine against a fake exchange |
| `phase_a_read_only.py`, `phase_b_dry_run.py` | Early stages: read-only, then decisions without execution |
| `Transmissions/` | Design journal: decisions and verified facts |

**One single copy of the logic.** The production launcher holds no rules, so it cannot drift away from the validated engine. Duplicating code across two variants guarantees that a fix will eventually land in only one of them.

---

## 5. Safety gates

This is the heart of the project — more so than the strategy itself.

| Gate | What it prevents |
|---|---|
| **Unarmed by default** | The bot computes everything and sends nothing. All four functions able to emit an order start with this check, at the lowest level |
| **Typed confirmation** | In production, every armed start requires manual keyboard input |
| **Account-mode check** | Refuses to start if the account configuration would make its measurements wrong |
| **Reconciliation** | Refuses to act if a leg is missing or the gap between legs exceeds 25 % |
| **Drift alert** | Flags any gap above 2 % between the two legs |
| **Pre-entry checks** | Verifies margin and cash *before* opening, instead of failing halfway through |
| **Automatic unwind** | If the second leg fails, the first is closed immediately — never a naked position |
| **Circuit breaker** | After three consecutive send failures, the bot stops acting and only watches |
| **Notional cap** | Refuses to build a position beyond a fixed limit |
| **Dust threshold** | Treats a leg as closed below $1: real trading always leaves rounding dust |
| **Unique order id** | An accidental resend cannot create a duplicate |
| **Dead-man's switch** | Re-armed every cycle: if the bot dies, the venue cancels resting orders by itself |
| **Agent key only** | The key can trade but **cannot withdraw funds**. It lives in an environment variable, never in a file |

---

## 6. Validation

### One new capability at a time

| Phase | What it adds |
|---|---|
| A | Read-only account access |
| B | Decisions computed, nothing sent |
| C | First signed orders, on testnet |
| D | Production |

Each phase adds exactly one capability, so a failure isolates immediately. Phases A and B structurally cannot lose money — the order-sending code is not even loaded.

### The simulator

A testnet cannot produce a crisis on demand, and waiting for a real market to move is neither reproducible nor fast. Hence `paper_sim.py`: it replaces the exchange with a fake one but runs **the real engine** — not a copy. You script a price path and check the behaviour, in seconds.

| Scenario | What it proves |
|---|---|
| Calm market | No action and no false alert over 24 h |
| Moderate rise | The top-up alert fires at the right moment, without placing an order |
| Reserve exhausted | The reduction cuts both legs within the same cycle |
| One leg fails | The other is closed immediately, the account returns flat |
| Repeated send failures | The circuit breaker stops after three failures, never selling one leg alone |
| Token de-peg | Early alert, then a full block beyond the threshold |
| Overnight price gap (+20 %) | The position survives and rebalances |
| Overnight price gap (+27 %) | Liquidation, but the loss is bounded to the posted margin; the spot leg is untouched and the bot blocks instead of acting on an incoherent state |

The simulator models real fees, slippage, hourly funding and the venue's liquidation rule. In production, the liquidation distance reported by the venue matched the simulator's prediction **to within a tenth of a point**.

---

## 7. Venue constraints that shaped the design

Three technical constraints, found by measuring rather than assuming, directly changed the architecture.

**The testnet cannot host this strategy.** It requires the same asset to be tradable on both sides with enough liquidity. A full sweep — 212 perpetual markets and 1,261 spot pairs — shows that no asset there meets both conditions. The testnet was therefore used to validate the signing and sending chain, and the simulator took over for the logic.

**An agent key cannot move funds between wallets.** It can place orders, but internal transfers require the master account's signature — the one you refuse on principle to hand to an automated program. Topping up margin is therefore a manual operation, and the bot alerts instead of attempting a transfer that is bound to fail. An important detail: retrying would trip the circuit breaker, which would also stop reductions — the only automated protection left.

**Displayed price ≠ liquidation price.** Charts show traded prices, while margin and liquidation are computed on a separate aggregated price. The gap is tiny in normal conditions and widens exactly when it matters: during a violent move.

---

## 8. Known limitations

Accepted, not ignored.

- Alerts are log lines: **nobody gets woken up**. This is the most significant gap.
- The bot depends on a machine being on. Switched off, the position survives but is unmonitored.
- It can **shrink** a position, never grow one: after several reductions it stays small until manual intervention.
- Topping up margin is manual (see §7).
- Excess margin is never swept back automatically.
- At small size, a round trip in fees costs several days of funding.
- Peg risk is **flagged**, not hedged.

---

## 9. Install and use

```bash
python -m pip install hyperliquid-python-sdk
```

Credentials live in the terminal, never on disk:

```powershell
$env:HL_ACCOUNT_ADDRESS = "0xYourAddress"
$env:HL_AGENT_KEY = (Get-Clipboard).Trim()
```

```bash
python phase_d_mainnet.py --once                   # read and decide, send nothing
python phase_d_mainnet.py --enter <amount> --arm   # open a neutral position
python phase_d_mainnet.py --arm                    # monitor every 60 s
python phase_d_mainnet.py --flatten --arm          # close both legs
```

`--once` takes exactly the same code path as armed mode but sends nothing: that is how a decision gets reviewed before it is authorised.

The simulator needs neither a key nor a connection:

```bash
python paper_sim.py              # all scenarios
python paper_sim.py depeg        # a single one
python paper_sim.py --verbose    # with the bot's own logs
```

---

## 10. Stack

Python 3 · [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) · `eth-account` for signing · no external computation dependency.

MIT licence.
