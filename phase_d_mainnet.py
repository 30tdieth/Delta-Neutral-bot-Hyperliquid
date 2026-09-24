#!/usr/bin/env python3
"""
Delta-Neutral Funding Bot — Phase D (MAINNET, micro size)
=========================================================

REAL MONEY. This file holds no trading logic: it reuses phase_c_testnet.py as
the engine (the exact code validated by paper_sim.py) and only changes the
settings below. One copy of the logic → a fix can never be forgotten here.

What differs from Phase C:
  * network = mainnet
  * perp BTC + spot UBTC (the real target, not the HYPE stand-in)
  * notional cap $150/leg (enter with ~$100)
  * flatten slippage 1% — mainnet BTC books are deep; 40% (testnet) would be
    reckless here
  * refuses to run unless the account is in Manual/Standard mode
  * every armed start asks you to type MAINNET

CREDENTIALS: a MAINNET agent wallet (agents are per network — the testnet one
does not work here). Terminal only, never in a file:
  $env:HL_ACCOUNT_ADDRESS = "0xYOUR_ACCOUNT_ADDRESS"
  $env:HL_AGENT_KEY       = "0xYourMainnetAgentKey"

FUNDING (agent keys cannot move USDC between spot and perp — verified live):
  In the HL UI, transfer margin + reserve spot→perp YOURSELF before entering.
  Size it so the perp wallet covers margin + reserve, and spot keeps enough
  USDC for the long leg. The bot never transfers; in ORANGE it logs "ACTION NEEDED", in RED it reduces.

USAGE (always unarmed first):
  python phase_d_mainnet.py --once                  # read + decide, sends nothing
  python phase_d_mainnet.py --enter 80 --arm        # open ~$80/leg
  python phase_d_mainnet.py --arm                   # manager loop
  python phase_d_mainnet.py --flatten --arm         # close both legs
"""

from dataclasses import replace

import phase_c_testnet as engine

engine.CFG = replace(
    engine.CFG,
    perp_coin="BTC",
    spot_base="UBTC",
    notional_cap_usd=150.0,
    flatten_slippage=0.01,
)

if __name__ == "__main__":
    engine.main(network="mainnet")
