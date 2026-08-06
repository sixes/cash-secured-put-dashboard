"""Local option pricing and greeks (Black-76 on a parity-implied forward).

Why local: `calc_indexes.theta` from Longbridge is unusable. Its sign flips across
moneyness and it disagrees with Black-Scholes by up to 70x — a put and a call at the
same ATM strike came back +17.38 and -21.65 when both must be strongly negative.
See scripts/calibrate_greeks.py. The API's IV, delta, gamma and vega do check out,
so we take IV from the API and derive every greek here.

Why a forward rather than spot + dividend yield: the forward comes straight out of
put-call parity, F = K + e^(rT)(C_mid - P_mid), which requires no dividend forecast.
The discount factor e^(-qT) = (F/S)e^(-rT) falls out of it, so spot-based greeks are
still available without ever guessing q.

Units, all per share:
  delta  dimensionless, signed (negative for puts)
  gamma  per 1.00 move in spot
  vega   per 1.00 of sigma; divide by 100 for "per IV point"
  theta  per CALENDAR day (year / 365)
Multiply by contract_size (100 for standard US equity options) for per-contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist, median

SQRT_2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT_2)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float  # per calendar day
    rho: float  # per 1.00 of r
    d1: float
    d2: float


@dataclass(frozen=True)
class Forward:
    forward: float
    source: str  # "parity" | "carry"
    n_strikes: int
    div_yield: float | None


def implied_forward(
    quotes: list[tuple[float, float | None, float | None]],
    r: float,
    t: float,
    spot: float | None = None,
    atm_window: float = 0.10,
) -> Forward | None:
    """Forward from put-call parity, F = K + e^(rT)(C - P).

    `quotes` is (strike, call_mid, put_mid). Uses the median across strikes near the
    money — a single strike is hostage to one wide spread, and deep wings carry the
    least parity information.
    """
    if t <= 0:
        return None

    grow = math.exp(r * t)
    candidates: list[float] = []
    for strike, call_mid, put_mid in quotes:
        if strike <= 0 or call_mid is None or put_mid is None:
            continue
        if call_mid <= 0 or put_mid <= 0:
            continue
        if spot is not None and abs(strike / spot - 1.0) > atm_window:
            continue
        candidates.append(strike + grow * (call_mid - put_mid))

    if not candidates:
        return None

    fwd = median(candidates)
    if not math.isfinite(fwd) or fwd <= 0:
        return None

    q = _div_yield(fwd, spot, r, t)
    return Forward(forward=fwd, source="parity", n_strikes=len(candidates), div_yield=q)


def carry_forward(spot: float, r: float, t: float, div_yield: float = 0.0) -> Forward:
    """Fallback when no strike has two-sided quotes on both legs."""
    return Forward(
        forward=spot * math.exp((r - div_yield) * t),
        source="carry",
        n_strikes=0,
        div_yield=div_yield,
    )


def _div_yield(forward: float, spot: float | None, r: float, t: float) -> float | None:
    if spot is None or spot <= 0 or forward <= 0 or t <= 0:
        return None
    return r - math.log(forward / spot) / t


def black76_greeks(
    forward: float,
    strike: float,
    t: float,
    r: float,
    sigma: float,
    is_put: bool = True,
    spot: float | None = None,
) -> Greeks | None:
    """Greeks for an option on `forward`, expressed per share.

    delta/gamma/vega/theta are reported with respect to SPOT when `spot` is given,
    which is what a seller sizing a cash-secured put cares about. Without `spot`
    they are with respect to the forward.
    """
    if forward <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        return None

    sqrt_t = math.sqrt(t)
    vol = sigma * sqrt_t
    d1 = (math.log(forward / strike) + 0.5 * vol * vol) / vol
    d2 = d1 - vol

    disc_r = math.exp(-r * t)
    pdf_d1 = norm_pdf(d1)

    # e^(-qT), the spot->forward bridge. Falls out of the forward itself.
    if spot is not None and spot > 0:
        disc_q = (forward / spot) * disc_r
        underlying = spot
        q = _div_yield(forward, spot, r, t) or 0.0
    else:
        # Pure forward space: Black-76 is BSM with q = r, since a forward has no
        # cost of carry under the forward measure. Theta here holds F fixed.
        disc_q = disc_r
        underlying = forward
        q = r

    if is_put:
        price = disc_r * (strike * norm_cdf(-d2) - forward * norm_cdf(-d1))
        delta = -disc_q * norm_cdf(-d1)
        rho = -strike * t * disc_r * norm_cdf(-d2)
        theta_year = (
            -(underlying * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            + r * strike * disc_r * norm_cdf(-d2)
            - q * underlying * disc_q * norm_cdf(-d1)
        )
    else:
        price = disc_r * (forward * norm_cdf(d1) - strike * norm_cdf(d2))
        delta = disc_q * norm_cdf(d1)
        rho = strike * t * disc_r * norm_cdf(d2)
        theta_year = (
            -(underlying * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            - r * strike * disc_r * norm_cdf(d2)
            + q * underlying * disc_q * norm_cdf(d1)
        )

    gamma = disc_q * pdf_d1 / (underlying * vol)
    vega = underlying * disc_q * pdf_d1 * sqrt_t

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta_year / DAYS_PER_YEAR,
        rho=rho,
        d1=d1,
        d2=d2,
    )


def strike_for_put_delta(
    target_delta: float,
    forward: float,
    t: float,
    r: float,
    sigma: float,
    spot: float | None = None,
) -> float | None:
    """Strike whose put delta magnitude is `target_delta`. Inverse of black76_greeks.

    Lets the chain pipeline request only the strikes that can plausibly land in the
    delta band, instead of a full strike window. That matters because option quote
    requests are capped at 500 symbols per rolling minute.
    """
    if not (0.0 < target_delta < 1.0) or forward <= 0 or t <= 0 or sigma <= 0:
        return None

    disc_r = math.exp(-r * t)
    disc_q = (forward / spot) * disc_r if spot and spot > 0 else disc_r

    p = target_delta / disc_q
    if not (0.0 < p < 1.0):
        return None

    # |delta| = disc_q * N(-d1)  =>  d1 = -Phi^-1(|delta| / disc_q)
    d1 = -NormalDist().inv_cdf(p)
    vol = sigma * math.sqrt(t)
    return forward * math.exp(0.5 * vol * vol - d1 * vol)


def implied_vol_put(
    price: float,
    forward: float,
    strike: float,
    t: float,
    r: float,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> float | None:
    """Bisection on put price. Only needed to cross-check the API's IV."""
    if price <= 0 or forward <= 0 or strike <= 0 or t <= 0:
        return None

    intrinsic = math.exp(-r * t) * max(strike - forward, 0.0)
    if price < intrinsic - tol:
        return None

    lo, hi = 1e-6, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        g = black76_greeks(forward, strike, t, r, mid, is_put=True)
        if g is None:
            return None
        if abs(g.price - price) < tol:
            return mid
        if g.price > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
