"""Geometry for the price chart: close, expanding running high, SMA200.

Pure. Takes a date/close series and returns SVG coordinate strings, so the whole thing
is unit-testable and the template does no arithmetic.

Conventions, surfaced in the UI footer:
  - The running high is an EXPANDING max over the whole series, then windowed. A
    windowed max would reset at the left edge and understate the drawdown.
  - The SMA is likewise computed over the whole series and then windowed, so the line
    is complete at the left edge instead of starting `sma_window` sessions in. A series
    shorter than the window yields no SMA line at all — never a partial mean drawn as
    an SMA200.
  - The x axis is one step per session, not per calendar day: weekends and holidays are
    not gaps. Standard for a price chart, and it keeps the step count honest.
  - The right edge is the last daily CLOSE. The live intraday price belongs to a
    different series and is shown in the panel header, not here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Room for the y labels on the left and the date labels underneath. The y labels are
# 9px monospace, so six digits need ~40px.
PAD_L = 44
PAD_R = 8
PAD_T = 8
PAD_B = 14

Y_TICKS = 4
X_TICKS = 5


@dataclass(frozen=True)
class Line:
    label: str
    cls: str  # css class on the polyline: close | high | sma
    points: str  # SVG polyline "x,y x,y ..."


@dataclass(frozen=True)
class PriceChart:
    """Everything the template needs to draw one panel's chart."""

    lines: tuple[Line, ...]
    y_ticks: tuple[tuple[float, float], ...]  # (value, y)
    x_ticks: tuple[tuple[str, float], ...]  # (label, x)
    width: int
    height: int
    plot_top: float
    plot_bottom: float
    plot_left: float
    plot_right: float

    # Measured over the WHOLE series, not the drawn window.
    high: float
    high_date: str
    drawdown_pct: float
    last: float
    last_date: str
    first_date: str
    n: int

    sma: float | None
    sma_window: int

    # Which adjustment basis this was drawn from, and whether it reaches inception —
    # only then may `high` be called an all-time high.
    source: str
    source_label: str
    all_time: bool

    shown: int  # sessions actually drawn
    window_first_date: str


def _running_max(values: list[float]) -> list[float]:
    out: list[float] = []
    peak = values[0]
    for v in values:
        if v > peak:
            peak = v
        out.append(peak)
    return out


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    """Simple mean of the trailing `window` values; None until the window is full."""
    out: list[float | None] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    total = sum(values[:window])
    out[window - 1] = total / window
    for i in range(window, len(values)):
        total += values[i] - values[i - window]
        out[i] = total / window
    return out


def _step_points(xs: list[float], ys: list[float]) -> str:
    """A staircase: only the points where the level changes, doubled at each riser.

    An expanding max is flat for long stretches, so emitting every session would be
    hundreds of collinear points. Doubling at the riser keeps the corner square instead
    of drawing a diagonal ramp through a jump that happened in one session.
    """
    if not xs:
        return ""
    parts = [f"{xs[0]:.1f},{ys[0]:.1f}"]
    for i in range(1, len(xs)):
        if ys[i] != ys[i - 1]:
            parts.append(f"{xs[i]:.1f},{ys[i - 1]:.1f}")
            parts.append(f"{xs[i]:.1f},{ys[i]:.1f}")
    last = f"{xs[-1]:.1f},{ys[-1]:.1f}"
    if parts[-1] != last:
        parts.append(last)
    return " ".join(parts)


def build_price_chart(
    dates: list[str],
    closes: list[float],
    *,
    sma_window: int = 200,
    window: int = 756,
    width: int = 720,
    height: int = 180,
    source: str = "",
    source_label: str = "",
    all_time: bool = False,
) -> PriceChart | None:
    """Build the chart, or None when there is not enough to draw honestly."""
    pairs = [
        (d, float(c))
        for d, c in zip(dates, closes)
        if c is not None and math.isfinite(float(c)) and float(c) > 0
    ]
    if len(pairs) < 2:
        return None

    all_dates = [d for d, _ in pairs]
    all_closes = [c for _, c in pairs]
    n = len(all_closes)

    peaks = _running_max(all_closes)
    smas = _rolling_mean(all_closes, sma_window)

    high = peaks[-1]
    high_date = all_dates[all_closes.index(high)]
    last = all_closes[-1]

    start = max(0, n - window)
    w_dates = all_dates[start:]
    w_closes = all_closes[start:]
    w_peaks = peaks[start:]
    w_smas = smas[start:]
    shown = len(w_closes)

    lo = min(min(w_closes), min(w_peaks))
    hi = max(max(w_closes), max(w_peaks))
    sma_vals = [v for v in w_smas if v is not None]
    if sma_vals:
        lo = min(lo, min(sma_vals))
        hi = max(hi, max(sma_vals))
    if hi <= lo:
        # A flat series still deserves a chart; give it an arbitrary but visible range
        # rather than dividing by zero.
        pad = abs(hi) * 0.01 or 1.0
        lo, hi = lo - pad, hi + pad

    left, right = float(PAD_L), float(width - PAD_R)
    top, bottom = float(PAD_T), float(height - PAD_B)
    span = right - left
    tall = bottom - top

    def x_at(i: int) -> float:
        return left if shown == 1 else left + span * i / (shown - 1)

    def y_at(v: float) -> float:
        # SVG y grows downward, so the highest price maps to the smallest y.
        return bottom - tall * (v - lo) / (hi - lo)

    xs = [x_at(i) for i in range(shown)]

    lines = [
        Line(
            label="close",
            cls="close",
            points=" ".join(f"{x:.1f},{y_at(c):.1f}" for x, c in zip(xs, w_closes)),
        ),
        Line(
            label="running high",
            cls="high",
            points=_step_points(xs, [y_at(v) for v in w_peaks]),
        ),
    ]
    if sma_vals:
        lines.append(
            Line(
                label=f"SMA{sma_window}",
                cls="sma",
                points=" ".join(
                    f"{x:.1f},{y_at(v):.1f}" for x, v in zip(xs, w_smas) if v is not None
                ),
            )
        )

    y_ticks = tuple(
        (lo + (hi - lo) * i / (Y_TICKS - 1), y_at(lo + (hi - lo) * i / (Y_TICKS - 1)))
        for i in range(Y_TICKS)
    )

    ticks = min(X_TICKS, shown)
    x_ticks = tuple(
        (
            w_dates[idx][:7],
            xs[idx],
        )
        for idx in (
            [0] if ticks == 1 else [round((shown - 1) * i / (ticks - 1)) for i in range(ticks)]
        )
    )

    return PriceChart(
        lines=tuple(lines),
        y_ticks=y_ticks,
        x_ticks=x_ticks,
        width=width,
        height=height,
        plot_top=top,
        plot_bottom=bottom,
        plot_left=left,
        plot_right=right,
        high=high,
        high_date=high_date,
        drawdown_pct=(last / high - 1.0) * 100.0,
        last=last,
        last_date=all_dates[-1],
        first_date=all_dates[0],
        n=n,
        sma=smas[-1],
        sma_window=sma_window,
        source=source,
        source_label=source_label,
        all_time=all_time,
        shown=shown,
        window_first_date=w_dates[0],
    )
