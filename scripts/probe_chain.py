"""Live check of the chain pipeline: quota cost, delta band coverage, greek sanity.

Run:  TZ=UTC .venv/bin/python scripts/probe_chain.py [TICKER ...]
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.metrics.trend import compute_trend
from app.providers import rates
from app.providers.chain import apply_quotes, build_chain
from app.providers.longport_client import LongportClient, us_symbol
from app.providers.quota import get_governor


async def one(client: LongportClient, ticker: str) -> None:
    before = get_governor().spent()

    spot_q = await client.aquote([ticker])
    spot = spot_q[ticker].last
    bars = await client.acandles(ticker, settings.candle_lookback)
    trend = compute_trend(ticker, [b.close for b in bars], last=spot)
    r = rates.risk_free_rate()

    res = await build_chain(client, ticker, spot, r, rv20=trend.rv20.rv)
    chain_cost = get_governor().spent() - before

    print(f"\n=== {ticker}  spot {spot:.2f}  r {r * 100:.4f}%  rv20 {trend.rv20.rv and trend.rv20.rv * 100:.2f}%")
    print(f"    quota: reported {res.quota_spent}, governor observed {chain_cost}")
    for w in res.warnings:
        print(f"    WARN {w}")

    if not res.slices:
        return
    sl = res.slices[0]
    print(f"    expiry {sl.expiry} dte {sl.dte}  fwd {sl.forward and sl.forward.forward:.2f}"
          f"  atm_iv {sl.atm_iv and sl.atm_iv * 100:.2f}%  30d {res.atm_iv_30d and res.atm_iv_30d * 100:.2f}%")
    if res.iv_rv:
        print(f"    IV/RV {res.iv_rv.ratio and f'{res.iv_rv.ratio:.2f}'}  spread {res.iv_rv.spread_points and f'{res.iv_rv.spread_points:+.2f}'}pts")

    # Seed quotes from REST depth (1 unit each) so the match can be selected.
    syms = [c.symbol for c in res.all_candidates]
    books = {}
    for s in syms:
        try:
            books[s] = await client.adepth(s)
        except Exception as exc:  # noqa: BLE001
            print(f"    depth failed {s}: {exc}")
    quoted = apply_quotes(res, books)

    print(f"    {'strike':>7} {'moneyness':>9} {'iv':>7} {'delta':>6} {'api':>6} {'vega/ct':>8}"
          f" {'theta/ct':>9} {'oi':>7} {'bid':>6} {'ask':>6} {'rel%':>6} {'ann%':>6} liq")
    for c in sorted(quoted.all_candidates, key=lambda x: -x.strike):
        mark = " <<<" if quoted.match and c.symbol == quoted.match.candidate.symbol else ""
        print(
            f"    {c.strike:7.1f} {c.moneyness_pct:8.2f}% {c.iv and c.iv * 100:6.2f}%"
            f" {c.delta:6.3f} {c.api_delta if c.api_delta is not None else float('nan'):6.3f}"
            f" {c.vega_contract:8.2f} {c.theta_day_contract:9.3f} {c.open_interest or 0:7d}"
            f" {c.spread.bid if c.spread.bid is not None else float('nan'):6.2f}"
            f" {c.spread.ask if c.spread.ask is not None else float('nan'):6.2f}"
            f" {c.spread.rel_spread_pct if c.spread.rel_spread_pct is not None else float('nan'):6.2f}"
            f" {c.premium.annualized_pct if c.premium.annualized_pct is not None else float('nan'):6.2f}"
            f" {'Y' if c.spread.liquid else 'n'}{mark}"
        )

    deltas = [c.delta for c in quoted.all_candidates if c.delta is not None]
    if deltas:
        print(f"    delta span {min(deltas):.3f}..{max(deltas):.3f}"
              f"  in 0.15-0.20: {sum(1 for d in deltas if 0.15 <= d <= 0.20)}")
    m = quoted.match
    if m and m.candidate:
        print(f"    MATCH K={m.candidate.strike} delta {m.candidate.delta:.3f}"
              f" off_target {m.off_target:.4f} off_band {m.off_band}")
    print(f"    stream symbols {len(quoted.all_symbols)}  total quota spent {get_governor().spent()}")


async def main() -> None:
    tickers = [us_symbol(t) for t in sys.argv[1:]] or ["SPY.US"]
    rates.refresh()
    client = LongportClient()
    for t in tickers:
        await one(client, t)


if __name__ == "__main__":
    asyncio.run(main())
