"""Options metrics for cash-secured put screening.

Conventions, all surfaced in the UI footer:
  - DTE is CALENDAR days in America/New_York, with expiry day = 0. T = DTE/365.
  - ATM IV is interpolated across strikes at the forward, then across expiries in
    TOTAL VARIANCE (sigma^2 * T), which is the only interpolation that keeps a
    constant-maturity IV arbitrage-free.
  - IV vs RV uses ATM IV, never the sold put's own IV: skew biases the wing IV high
    by construction, which would fake a variance risk premium.
  - Premium yield is quoted at mid, with the bid-based figure alongside as the
    conservative case. Annualization is simple (x365/DTE), not compounded.
  - Liquidity gate is absolute OR relative, because percent-of-mid alone unfairly
    rejects cheap options already sitting at the minimum legal tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import MARKET_TZ, settings
from app.metrics.pricing import Greeks, black76_greeks

CONTRACT_SIZE = 100.0
DAYS_PER_YEAR = 365.0

_MARKET_TZ = ZoneInfo(MARKET_TZ)


def market_today(now: datetime | None = None) -> date:
    """Today in exchange-local terms. A UTC evening is already tomorrow in UTC."""
    if now is None:
        now = datetime.now(tz=_MARKET_TZ)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(_MARKET_TZ).date()


def dte(expiry: date, now: datetime | None = None) -> int:
    """Calendar days to expiry, expiry day = 0. Can be negative if already past."""
    return (expiry - market_today(now)).days


def years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    return max(dte(expiry, now), 0) / DAYS_PER_YEAR


@dataclass(frozen=True)
class SpreadMetrics:
    bid: float | None
    ask: float | None
    mid: float | None
    abs_spread: float | None
    rel_spread_pct: float | None
    liquid: bool
    zero_bid: bool


def spread_metrics(
    bid: float | None,
    ask: float | None,
    max_abs: float | None = None,
    max_rel_pct: float | None = None,
) -> SpreadMetrics:
    max_abs = settings.max_abs_spread if max_abs is None else max_abs
    max_rel_pct = settings.max_rel_spread_pct if max_rel_pct is None else max_rel_pct

    zero_bid = bid is None or bid <= 0
    if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
        return SpreadMetrics(bid, ask, None, None, None, False, zero_bid)

    mid = (bid + ask) / 2.0
    abs_spread = ask - bid
    rel = (abs_spread / mid) * 100.0 if mid > 0 else None

    # A zero bid means there is no exit; never call it liquid.
    liquid = (not zero_bid) and (
        abs_spread <= max_abs or (rel is not None and rel <= max_rel_pct)
    )
    return SpreadMetrics(bid, ask, mid, abs_spread, rel, liquid, zero_bid)


@dataclass(frozen=True)
class PremiumYield:
    pct_of_strike: float | None
    annualized_pct: float | None
    pct_of_strike_bid: float | None
    annualized_pct_bid: float | None


def premium_yield(
    mid: float | None, bid: float | None, strike: float, days: int
) -> PremiumYield:
    def one(premium: float | None) -> tuple[float | None, float | None]:
        if premium is None or premium <= 0 or strike <= 0:
            return None, None
        pct = premium / strike * 100.0
        ann = pct * DAYS_PER_YEAR / days if days > 0 else None
        return pct, ann

    pct, ann = one(mid)
    pct_bid, ann_bid = one(bid)
    return PremiumYield(pct, ann, pct_bid, ann_bid)


def normalize_api_delta(raw: float | None) -> float | None:
    """API delta as a magnitude in [0, 1].

    Defensive against a percent-scaled feed: values above 1 are divided by 100.
    """
    if raw is None:
        return None
    d = abs(float(raw))
    if d > 1.0:
        d = d / 100.0
    return d if 0.0 <= d <= 1.0 else None


def interp_atm_iv(
    strikes: list[float], ivs: list[float | None], at: float
) -> float | None:
    """Linear IV interpolation across strikes, evaluated at `at` (the forward).

    Linear in strike is fine over the one-or-two-strike gap around the money; total
    variance interpolation is what matters across EXPIRIES, not across strikes.
    """
    pairs = sorted(
        (k, v) for k, v in zip(strikes, ivs) if v is not None and v > 0 and k > 0
    )
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][1]

    if at <= pairs[0][0]:
        return pairs[0][1]
    if at >= pairs[-1][0]:
        return pairs[-1][1]

    for (k0, v0), (k1, v1) in zip(pairs, pairs[1:]):
        if k0 <= at <= k1:
            if k1 == k0:
                return v0
            w = (at - k0) / (k1 - k0)
            return v0 + w * (v1 - v0)
    return pairs[-1][1]


def constant_maturity_iv(
    points: list[tuple[float, float]], target_days: float = 30.0
) -> float | None:
    """Constant-maturity ATM IV from (days, atm_iv) points, via total variance.

    Interpolating IV itself across expiries is not arbitrage-free; interpolating
    sigma^2 * T is. Outside the available range we clamp rather than extrapolate,
    because extrapolated wings of the term structure are pure fiction.
    """
    pts = sorted((d, v) for d, v in points if d > 0 and v is not None and v > 0)
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0][1]

    if target_days <= pts[0][0]:
        return pts[0][1]
    if target_days >= pts[-1][0]:
        return pts[-1][1]

    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target_days <= d1:
            t0, t1, tt = d0 / DAYS_PER_YEAR, d1 / DAYS_PER_YEAR, target_days / DAYS_PER_YEAR
            var0, var1 = v0 * v0 * t0, v1 * v1 * t1
            w = (target_days - d0) / (d1 - d0)
            var = var0 + w * (var1 - var0)
            return math.sqrt(var / tt) if var > 0 and tt > 0 else None
    return pts[-1][1]


@dataclass(frozen=True)
class IvVsRv:
    atm_iv: float | None
    rv: float | None
    ratio: float | None
    spread_points: float | None


def iv_vs_rv(atm_iv: float | None, rv: float | None) -> IvVsRv:
    if atm_iv is None or rv is None:
        return IvVsRv(atm_iv, rv, None, None)
    ratio = atm_iv / rv if rv > 0 else None
    return IvVsRv(atm_iv, rv, ratio, (atm_iv - rv) * 100.0)


@dataclass(frozen=True)
class PutCandidate:
    symbol: str
    strike: float
    expiry: date
    dte: int
    iv: float | None

    delta: float | None  # magnitude, from our Black-76
    api_delta: float | None  # magnitude, health check only
    delta_mismatch: bool

    gamma: float | None
    vega: float | None  # per share per 1 IV POINT (Black-76 vega / 100)
    theta_day: float | None  # per share per day, SHORT sign (positive to a seller)
    theta_day_contract: float | None
    vega_contract: float | None

    open_interest: int | None
    spread: SpreadMetrics
    premium: PremiumYield
    moneyness_pct: float  # (strike/spot - 1) * 100, negative for OTM puts
    in_delta_band: bool

    # Black-76 price at the API's IV, so a contract can be ranked before anything is
    # subscribed. These are MODEL numbers: they are never rendered in a column that
    # otherwise carries a market mid, and never substituted for a missing quote.
    model_premium: float | None = None
    model_annualized_pct: float | None = None

    @property
    def gamma_per_premium(self) -> float | None:
        """Gamma per dollar of premium collected. The cost of a high annualized yield.

        Both legs are per-contract, so the two factors of 100 cancel. Market mid only:
        dividing a real gamma by a model premium would flatter a contract nobody quotes.
        """
        mid = self.spread.mid
        if self.gamma is None or mid is None or mid <= 0:
            return None
        return self.gamma / mid


def build_candidate(
    symbol: str,
    strike: float,
    expiry: date,
    spot: float,
    forward: float,
    iv: float | None,
    r: float,
    bid: float | None,
    ask: float | None,
    api_delta: float | None,
    open_interest: int | None,
    now: datetime | None = None,
    band: tuple[float, float] | None = None,
    max_abs: float | None = None,
    max_rel_pct: float | None = None,
) -> PutCandidate | None:
    days = dte(expiry, now)
    if days < 0:
        return None

    t = days / DAYS_PER_YEAR
    g: Greeks | None = None
    if iv is not None and iv > 0 and t > 0:
        g = black76_greeks(forward, strike, t, r, iv, is_put=True, spot=spot)

    sp = spread_metrics(bid, ask, max_abs, max_rel_pct)
    prem = premium_yield(sp.mid, sp.bid, strike, days)

    local_delta = abs(g.delta) if g else None
    api_d = normalize_api_delta(api_delta)
    mismatch = (
        local_delta is not None and api_d is not None and abs(local_delta - api_d) > 0.02
    )

    lo, hi = settings.delta_band if band is None else band
    model = premium_yield(g.price if g else None, None, strike, days)
    return PutCandidate(
        symbol=symbol,
        strike=strike,
        expiry=expiry,
        dte=days,
        iv=iv,
        delta=local_delta,
        api_delta=api_d,
        delta_mismatch=mismatch,
        gamma=g.gamma if g else None,
        # Black-76 vega is per 1.00 of sigma; traders read per IV point.
        vega=g.vega / 100.0 if g else None,
        vega_contract=g.vega / 100.0 * CONTRACT_SIZE if g else None,
        # Flip sign: the screener is for SELLERS, for whom decay is income.
        theta_day=-g.theta if g else None,
        theta_day_contract=-g.theta * CONTRACT_SIZE if g else None,
        open_interest=open_interest,
        spread=sp,
        premium=prem,
        moneyness_pct=(strike / spot - 1.0) * 100.0 if spot > 0 else 0.0,
        in_delta_band=local_delta is not None and lo <= local_delta <= hi,
        model_premium=g.price if g else None,
        model_annualized_pct=model.annualized_pct,
    )


def with_quote(
    candidate: PutCandidate,
    bid: float | None,
    ask: float | None,
    max_abs: float | None = None,
    max_rel_pct: float | None = None,
    band: tuple[float, float] | None = None,
) -> PutCandidate:
    """Re-derive the quote-dependent fields, and the band flag. Greeks are untouched.

    Chain construction spends option quota; quotes arrive later by push for free, so
    the two must be refreshable independently. `in_delta_band` is re-derived here
    because it was baked in from whichever band was in force at build time, and
    `replace()` would otherwise carry that stale flag through a band change.
    """
    sp = spread_metrics(bid, ask, max_abs, max_rel_pct)
    lo, hi = settings.delta_band if band is None else band
    return replace(
        candidate,
        spread=sp,
        premium=premium_yield(sp.mid, sp.bid, candidate.strike, candidate.dte),
        in_delta_band=candidate.delta is not None and lo <= candidate.delta <= hi,
    )


@dataclass(frozen=True)
class DeltaMatch:
    candidate: PutCandidate | None
    off_target: float | None
    off_band: bool  # True when nothing landed inside the configured band


def select_by_delta(
    candidates: list[PutCandidate],
    target: float | None = None,
    band: tuple[float, float] | None = None,
) -> DeltaMatch:
    """Pick the OTM, two-sided put whose delta is closest to `target`.

    Requires strike < spot and a live bid. If nothing lands in the band we still
    return the closest contract, flagged — showing an honest 0.27 delta beats
    labeling it as a 0.175.
    """
    target = settings.delta_target if target is None else target
    lo, hi = settings.delta_band if band is None else band

    eligible = [
        c
        for c in candidates
        if c.delta is not None and c.moneyness_pct < 0 and not c.spread.zero_bid
    ]
    if not eligible:
        return DeltaMatch(None, None, True)

    in_band = [c for c in eligible if lo <= c.delta <= hi]
    pool = in_band or eligible
    best = min(pool, key=lambda c: abs(c.delta - target))
    return DeltaMatch(best, abs(best.delta - target), not in_band)


def pick_expiries(
    expiries: list[date],
    dte_min: int | None = None,
    dte_max: int | None = None,
    now: datetime | None = None,
) -> list[date]:
    lo = settings.dte_min if dte_min is None else dte_min
    hi = settings.dte_max if dte_max is None else dte_max
    return [e for e in expiries if lo <= dte(e, now) <= hi]
