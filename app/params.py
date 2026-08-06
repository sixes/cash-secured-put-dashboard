"""The screening parameters, as a value that is threaded rather than a global.

`app.config.settings` is a frozen dataclass bound by name into eight modules, so
`replace()` plus rebinding would be invisible to the code that reads it. Per-request
parameters therefore travel as an argument: this object.

The parameters split into three cost classes, and the split is the whole point —
without it every tweak would spend option quota:

  FREE     delta target, delta band, both liquidity gates. These are only read
           downstream of `apply_quotes`, which is pure and already re-runs on every
           render and every SSE frame. Zero option quota, no rebuild.
  REBUILD  the DTE window and the screening delta band. These decide which option
           symbols are requested, so a change is a full `build_chain`: ~28 of the 450
           working units per rolling minute, and it can block until the window clears.
  TREND    the SMA / RV / chart windows. No option quota, but they re-run the
           underlying fetch (a quote, 1000 daily bars, a SQLite chart rebuild).

Nothing here raises on bad input. A rejected value keeps the inherited one and
produces a message the page shows, because a 500 on `?dte_min=abc` would be a worse
answer than an unchanged window with an explanation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping
from urllib.parse import urlencode

from app.config import settings

FREE = "free"
REBUILD = "rebuild"
TREND = "trend"

COST_LABELS = {
    FREE: "free — applies on the next render",
    REBUILD: "billed — rebuilds the chain",
    TREND: "slow — refetches the price history",
}

COST_NOTES = {
    FREE: (
        "Read only when quotes are folded in, which happens on every render and every "
        "push frame anyway. Costs no option quota and takes effect immediately."
    ),
    REBUILD: (
        "Changes which option symbols are requested, so the chain is rebuilt: about 28 "
        "of the 450 option-quota units available per rolling minute. When the window is "
        "full the request waits — up to a minute — rather than showing stale numbers."
    ),
    TREND: (
        "Costs no option quota but re-fetches the daily bars and redraws the chart, so "
        "the page takes a beat longer. Card and chart always move together."
    ),
}


@dataclass(frozen=True)
class ScreenParams:
    dte_min: int
    dte_max: int
    delta_target: float
    delta_band: tuple[float, float]
    delta_screen_band: tuple[float, float]
    max_abs_spread: float
    max_rel_spread_pct: float
    sma_window: int
    rv_window: int
    chart_window_sessions: int


@dataclass(frozen=True)
class Field:
    name: str  # query-string key, and the JSON key when saved
    label: str
    attr: str  # ScreenParams attribute
    index: int | None  # tuple element, or None for a scalar
    kind: str  # "int" | "float"
    lo: float  # inclusive
    hi: float  # inclusive
    step: float
    cost: str


# The maximum window the daily-bar fetch can supply. An SMA longer than the series is
# not an error (the card says n=137/200), but a window past the API's ceiling could
# never be satisfied by any amount of waiting.
_MAX_BARS = settings.candle_lookback

FIELDS: tuple[Field, ...] = (
    Field("dte_min", "DTE min", "dte_min", None, "int", 0, 730, 1, REBUILD),
    Field("dte_max", "DTE max", "dte_max", None, "int", 1, 730, 1, REBUILD),
    Field("delta_screen_lo", "Screen Δ from", "delta_screen_band", 0, "float", 0.01, 0.99, 0.01, REBUILD),
    Field("delta_screen_hi", "Screen Δ to", "delta_screen_band", 1, "float", 0.01, 0.99, 0.01, REBUILD),
    Field("delta_target", "Δ target", "delta_target", None, "float", 0.01, 0.99, 0.005, FREE),
    Field("delta_lo", "Δ band from", "delta_band", 0, "float", 0.01, 0.99, 0.01, FREE),
    Field("delta_hi", "Δ band to", "delta_band", 1, "float", 0.01, 0.99, 0.01, FREE),
    Field("max_abs_spread", "Max spread, absolute", "max_abs_spread", None, "float", 0.0, 50.0, 0.01, FREE),
    Field("max_rel_spread_pct", "Max spread, % of mid", "max_rel_spread_pct", None, "float", 0.0, 100.0, 0.1, FREE),
    Field("sma_window", "SMA window", "sma_window", None, "int", 2, _MAX_BARS, 1, TREND),
    # An n-day realized vol needs n+1 closes, so it can never reach the bar ceiling.
    Field("rv_window", "RV window", "rv_window", None, "int", 2, _MAX_BARS - 1, 1, TREND),
    Field("chart_window_sessions", "Chart sessions", "chart_window_sessions", None, "int", 20, 20000, 1, TREND),
)

_BY_NAME = {f.name: f for f in FIELDS}


def defaults() -> ScreenParams:
    """The code baseline. `settings` stays the single source of the starting values."""
    return ScreenParams(
        dte_min=settings.dte_min,
        dte_max=settings.dte_max,
        delta_target=settings.delta_target,
        delta_band=tuple(settings.delta_band),  # type: ignore[arg-type]
        delta_screen_band=tuple(settings.delta_screen_band),  # type: ignore[arg-type]
        max_abs_spread=settings.max_abs_spread,
        max_rel_spread_pct=settings.max_rel_spread_pct,
        sma_window=settings.sma_window,
        rv_window=settings.rv_window,
        chart_window_sessions=settings.chart_window_sessions,
    )


def value_of(params: ScreenParams, f: Field) -> float:
    v = getattr(params, f.attr)
    return v if f.index is None else v[f.index]


def to_dict(params: ScreenParams) -> dict[str, float | int]:
    """Every field under its query-string key. Also the persisted JSON shape."""
    return {f.name: value_of(params, f) for f in FIELDS}


def to_query(params: ScreenParams, base: ScreenParams | None = None) -> str:
    """Only the fields that differ from `base`, so a default view has a bare URL."""
    base = defaults() if base is None else base
    pairs = [
        (f.name, _fmt(f, value_of(params, f)))
        for f in FIELDS
        if value_of(params, f) != value_of(base, f)
    ]
    return urlencode(pairs)


def from_query(
    q: Mapping[str, object], base: ScreenParams | None = None
) -> tuple[ScreenParams, list[str]]:
    """Overlay `q` on `base`. Never raises; every rejection is explained."""
    base = defaults() if base is None else base
    errors: list[str] = []
    params = base

    for f in FIELDS:
        raw = q.get(f.name)
        if raw is None or str(raw).strip() == "":
            continue
        value = _coerce(f, str(raw).strip(), errors)
        if value is None:
            continue
        params = _with(params, f, value)

    return _cross_check(params, base, errors)


def chain_key(params: ScreenParams) -> tuple:
    """The parameters a chain was BUILT with. A change here costs option quota."""
    return (params.dte_min, params.dte_max, params.delta_screen_band)


def trend_key(params: ScreenParams) -> tuple:
    """Free of option quota, but a change re-runs the underlying fetch."""
    return (params.sma_window, params.rv_window, params.chart_window_sessions)


def origin(f: Field, params: ScreenParams, url: Mapping[str, object], saved: Mapping[str, object] | None) -> str:
    """Which layer the effective value came from: url, saved, or code.

    Compared by value rather than by presence, so a rejected URL value reports the
    layer that actually supplied the number instead of claiming credit for it.
    """
    effective = value_of(params, f)
    for label, layer in (("url", url), ("saved", saved or {})):
        raw = layer.get(f.name)
        if raw is None or str(raw).strip() == "":
            continue
        if _coerce(f, str(raw).strip(), []) == effective:
            return label
    return "code"


def field_rows(
    params: ScreenParams,
    url: Mapping[str, object] | None = None,
    saved: Mapping[str, object] | None = None,
) -> list[dict]:
    """Everything the form needs per input, so the template holds no policy."""
    url = url or {}
    return [
        {
            "name": f.name,
            "label": f.label,
            "value": _fmt(f, value_of(params, f)),
            "kind": f.kind,
            "min": _fmt(f, f.lo),
            "max": _fmt(f, f.hi),
            "step": f.step,
            "cost": f.cost,
            "origin": origin(f, params, url, saved),
        }
        for f in FIELDS
    ]


def groups(
    params: ScreenParams,
    url: Mapping[str, object] | None = None,
    saved: Mapping[str, object] | None = None,
) -> list[dict]:
    rows = field_rows(params, url, saved)
    return [
        {
            "cost": cost,
            "label": COST_LABELS[cost],
            "note": COST_NOTES[cost],
            "fields": [r for r in rows if r["cost"] == cost],
        }
        for cost in (FREE, REBUILD, TREND)
    ]


# --- internals ----------------------------------------------------------------


def _fmt(f: Field, v: float) -> str:
    return str(int(v)) if f.kind == "int" else f"{v:g}"


def _coerce(f: Field, raw: str, errors: list[str]) -> float | int | None:
    try:
        value = int(raw) if f.kind == "int" else float(raw)
    except ValueError:
        errors.append(f"{f.label}: '{raw}' is not a number — ignored")
        return None
    if not (f.lo <= value <= f.hi):
        errors.append(
            f"{f.label}: {raw} is outside {_fmt(f, f.lo)}–{_fmt(f, f.hi)} — ignored"
        )
        return None
    return value


def _with(params: ScreenParams, f: Field, value: float | int) -> ScreenParams:
    if f.index is None:
        return replace(params, **{f.attr: value})
    pair = list(getattr(params, f.attr))
    pair[f.index] = value
    return replace(params, **{f.attr: tuple(pair)})


def _cross_check(
    params: ScreenParams, base: ScreenParams, errors: list[str]
) -> tuple[ScreenParams, list[str]]:
    """Reject combinations that are individually in range but jointly meaningless.

    Reverting to `base` is enough: the baseline is either the code defaults or a saved
    row that came through this same function.
    """
    if params.dte_min > params.dte_max:
        errors.append(
            f"DTE window {params.dte_min}–{params.dte_max} is inverted — "
            f"kept {base.dte_min}–{base.dte_max}"
        )
        params = replace(params, dte_min=base.dte_min, dte_max=base.dte_max)

    if params.delta_screen_band[0] >= params.delta_screen_band[1]:
        errors.append(
            f"screening Δ band {params.delta_screen_band[0]:g}–{params.delta_screen_band[1]:g} "
            f"is inverted — kept {base.delta_screen_band[0]:g}–{base.delta_screen_band[1]:g}"
        )
        params = replace(params, delta_screen_band=base.delta_screen_band)

    if params.delta_band[0] >= params.delta_band[1]:
        errors.append(
            f"Δ band {params.delta_band[0]:g}–{params.delta_band[1]:g} is inverted — "
            f"kept {base.delta_band[0]:g}–{base.delta_band[1]:g}"
        )
        params = replace(params, delta_band=base.delta_band)

    # The table only contains contracts inside the screening band, so a trading band
    # outside it could never be filled.
    lo, hi = params.delta_screen_band
    if not (lo <= params.delta_band[0] and params.delta_band[1] <= hi):
        errors.append(
            f"Δ band {params.delta_band[0]:g}–{params.delta_band[1]:g} falls outside the "
            f"screening band {lo:g}–{hi:g}, so nothing could match it — widened the "
            f"screening band to cover it"
        )
        params = replace(
            params,
            delta_screen_band=(
                min(lo, params.delta_band[0]),
                max(hi, params.delta_band[1]),
            ),
        )

    # Clamp rather than revert: the baseline target can itself sit outside a band the
    # user has deliberately moved, and a message that changed nothing would be a lie.
    blo, bhi = params.delta_band
    if not (blo <= params.delta_target <= bhi):
        clamped = min(max(params.delta_target, blo), bhi)
        errors.append(
            f"Δ target {params.delta_target:g} is outside its band {blo:g}–{bhi:g} — "
            f"moved to {clamped:g}"
        )
        params = replace(params, delta_target=clamped)

    return params, errors
