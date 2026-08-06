"""Price and trend metrics: drawdown, SMA200 distance, 20-day realized volatility.

Conventions here are deliberate and are surfaced in the UI footer:
  - Drawdown is close-based against an EXPANDING max (all history seen), not a
    rolling 252-day max. The 52-week figure is reported separately.
  - SMA200 is the simple mean of the last 200 trading-day closes. Fewer than 200
    valid closes returns None with the actual n, never a partial mean mislabeled.
  - RV20 uses the zero-mean estimator sqrt(mean(r^2)) * sqrt(252). This is the
    variance-swap-consistent form, which is the right thing to compare against IV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TRADING_DAYS = 252
WEEKS52_SESSIONS = 252


@dataclass(frozen=True)
class Drawdown:
    """Drawdown against the highest close in the fetched window.

    Not a true all-time high: the API caps daily history at 1000 bars (~4 years),
    so `high_n` sessions is the honest label. `high_since` is the first date in the
    window when known.
    """

    high: float
    drawdown_pct: float
    high_n: int
    high_since: str | None
    high_52w: float | None
    drawdown_52w_pct: float | None


@dataclass(frozen=True)
class SmaDistance:
    sma: float | None
    distance_pct: float | None
    n: int
    window: int

    @property
    def sufficient(self) -> bool:
        return self.n >= self.window


@dataclass(frozen=True)
class RealizedVol:
    rv: float | None  # annualized, as a ratio (0.1823 == 18.23%)
    n_returns: int
    window: int

    @property
    def sufficient(self) -> bool:
        return self.n_returns >= self.window


@dataclass(frozen=True)
class TrendMetrics:
    symbol: str
    last: float | None
    drawdown: Drawdown | None
    sma200: SmaDistance
    rv20: RealizedVol
    n_bars: int


def _clean(closes: list[float]) -> list[float]:
    return [c for c in closes if c is not None and c > 0 and math.isfinite(c)]


def drawdown_from_high(
    closes: list[float], last: float | None = None, first_date: str | None = None
) -> Drawdown | None:
    """Close-based drawdown against the expanding max of the fetched window.

    `last` lets an intraday price be measured against a history of daily closes.
    """
    series = _clean(closes)
    if not series:
        return None
    price = last if last is not None and last > 0 else series[-1]

    high = max(max(series), price)
    dd = (price / high - 1.0) * 100.0

    window = series[-WEEKS52_SESSIONS:]
    high_52w = max(max(window), price) if window else None
    dd_52w = (price / high_52w - 1.0) * 100.0 if high_52w else None

    return Drawdown(
        high=high,
        drawdown_pct=dd,
        high_n=len(series),
        high_since=first_date,
        high_52w=high_52w,
        drawdown_52w_pct=dd_52w,
    )


def sma_distance(
    closes: list[float], window: int = 200, last: float | None = None
) -> SmaDistance:
    """Distance of price from the simple mean of the last `window` closes."""
    series = _clean(closes)
    if len(series) < window:
        return SmaDistance(sma=None, distance_pct=None, n=len(series), window=window)

    sma = sum(series[-window:]) / window
    price = last if last is not None and last > 0 else series[-1]
    dist = (price / sma - 1.0) * 100.0 if sma > 0 else None
    return SmaDistance(sma=sma, distance_pct=dist, n=len(series), window=window)


def realized_vol(closes: list[float], window: int = 20) -> RealizedVol:
    """Annualized zero-mean realized volatility from log returns.

    Needs window+1 closes to form `window` returns.
    """
    series = _clean(closes)
    if len(series) < window + 1:
        return RealizedVol(rv=None, n_returns=max(0, len(series) - 1), window=window)

    tail = series[-(window + 1) :]
    rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    mean_sq = sum(r * r for r in rets) / len(rets)
    return RealizedVol(
        rv=math.sqrt(mean_sq) * math.sqrt(TRADING_DAYS),
        n_returns=len(rets),
        window=window,
    )


def compute_trend(
    symbol: str,
    closes: list[float],
    last: float | None = None,
    sma_window: int = 200,
    rv_window: int = 20,
    first_date: str | None = None,
) -> TrendMetrics:
    series = _clean(closes)
    return TrendMetrics(
        symbol=symbol,
        last=last if last is not None and last > 0 else (series[-1] if series else None),
        drawdown=drawdown_from_high(series, last, first_date),
        sma200=sma_distance(series, sma_window, last),
        rv20=realized_vol(series, rv_window),
        n_bars=len(series),
    )
