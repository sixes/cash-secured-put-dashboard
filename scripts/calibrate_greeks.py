"""Empirically determine the unit scaling of calc_indexes greeks.

The docs claim theta/vega/rho are returned x100 and must be divided by 100 to get
standard per-share values, while delta/gamma carry no such note. Rather than trust
that, this script prices the same contracts with Black-Scholes-Merton in double
precision and reports the ratio api_value / bs_value for each greek.

A ratio of ~1 means the raw value is already the standard per-share greek.
A ratio of ~100 means it needs dividing by 100.

The implied volatility reported by calc_indexes is used as the BS sigma input, so
delta should match almost exactly if our r/q assumptions are close. Deep OTM strikes
suffer catastrophic cancellation, so near-the-money contracts are the reliable signal.

Usage:  TZ=UTC .venv/bin/python scripts/calibrate_greeks.py
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from longport.openapi import (  # noqa: E402
    AdjustType,
    CalcIndex,
    Config,
    Period,
    QuoteContext,
)

UNDERLYING = "SPY.US"
RISK_FREE = 0.040   # ~3M T-bill, continuously compounded
DIV_YIELD = 0.012   # SPY trailing distribution yield

INDEXES = [
    CalcIndex.LastDone,
    CalcIndex.ImpliedVolatility,
    CalcIndex.Delta,
    CalcIndex.Gamma,
    CalcIndex.Theta,
    CalcIndex.Vega,
    CalcIndex.Rho,
    CalcIndex.StrikePrice,
]


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_put_greeks(s: float, k: float, t: float, r: float, q: float, sig: float) -> dict[str, float]:
    """Per-share BSM put greeks. theta is per YEAR, vega per 1.00 of sigma."""
    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sig * sig) * t) / (sig * sqrt_t)
    d2 = d1 - sig * sqrt_t
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    pdf_d1 = norm_pdf(d1)
    return {
        "price": k * disc_r * norm_cdf(-d2) - s * disc_q * norm_cdf(-d1),
        "delta": -disc_q * norm_cdf(-d1),
        "gamma": disc_q * pdf_d1 / (s * sig * sqrt_t),
        "vega": s * disc_q * pdf_d1 * sqrt_t,
        "theta_year": (
            -(s * disc_q * pdf_d1 * sig) / (2.0 * sqrt_t)
            + r * k * disc_r * norm_cdf(-d2)
            - q * s * disc_q * norm_cdf(-d1)
        ),
        "rho": -k * t * disc_r * norm_cdf(-d2),
    }


def main() -> int:
    ctx = QuoteContext(Config.from_env())
    spot = float(ctx.candlesticks(UNDERLYING, Period.Day, 1, AdjustType.ForwardAdjust)[-1].close)

    today = date.today()
    expiries = sorted(e for e in ctx.option_chain_expiry_date_list(UNDERLYING) if 35 <= (e - today).days <= 60)
    expiry = expiries[0]
    dte = (expiry - today).days
    t = dte / 365.0

    strikes = ctx.option_chain_info_by_date(UNDERLYING, expiry)
    # Sample across moneyness: ATM is numerically robust, OTM is what we actually trade.
    wanted = [1.00, 0.97, 0.94, 0.90, 0.85]
    picks = []
    for frac in wanted:
        target = spot * frac
        cands = [s for s in strikes if s.put_symbol]
        best = min(cands, key=lambda s: abs(float(s.price) - target))
        if best.put_symbol not in [p.put_symbol for p in picks]:
            picks.append(best)

    rows = ctx.calc_indexes([p.put_symbol for p in picks], INDEXES)
    by_symbol = {r.symbol: r for r in rows}

    print(f"spot={spot:.2f}  expiry={expiry}  DTE={dte}  T={t:.5f}  r={RISK_FREE}  q={DIV_YIELD}")
    print(f"{'strike':>7} {'K/S':>6} {'greek':>6} {'api_raw':>12} {'bs_per_share':>13} {'ratio':>9}")
    print("-" * 62)

    ratios: dict[str, list[float]] = {"delta": [], "gamma": [], "theta": [], "vega": [], "rho": []}

    for p in picks:
        r_ = by_symbol.get(p.put_symbol)
        if r_ is None or r_.implied_volatility is None or r_.delta is None:
            continue
        k = float(p.price)
        sig = float(r_.implied_volatility) / 100.0  # calc_indexes IV is a percent
        g = bs_put_greeks(spot, k, t, RISK_FREE, DIV_YIELD, sig)

        # BS reference values in standard per-share quoted units.
        ref = {
            "delta": g["delta"],
            "gamma": g["gamma"],
            "theta": g["theta_year"] / 365.0,   # per share per calendar day
            "vega": g["vega"] / 100.0,          # per share per 1 IV percentage point
            "rho": g["rho"] / 100.0,            # per share per 1% rate
        }
        api = {
            "delta": float(r_.delta),
            "gamma": float(r_.gamma),
            "theta": float(r_.theta),
            "vega": float(r_.vega),
            "rho": float(r_.rho),
        }
        for name in ("delta", "gamma", "theta", "vega", "rho"):
            ratio = api[name] / ref[name] if ref[name] else float("nan")
            if math.isfinite(ratio):
                ratios[name].append(ratio)
            print(f"{k:>7.0f} {k / spot:>6.3f} {name:>6} {api[name]:>12.5f} {ref[name]:>13.6f} {ratio:>9.2f}")
        print(f"{'':>7} {'':>6} {'iv':>6} {sig * 100:>11.2f}% {'bs_px':>13} {g['price']:>9.3f}")
        print("-" * 62)

    print("\nMedian ratio api/bs by greek (1 = already per-share, 100 = divide by 100):")
    for name, vals in ratios.items():
        if vals:
            vals.sort()
            med = vals[len(vals) // 2]
            print(f"  {name:>6}: {med:>8.2f}   (samples: {', '.join(f'{v:.1f}' for v in vals)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
