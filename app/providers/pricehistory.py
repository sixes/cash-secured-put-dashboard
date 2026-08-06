"""Long-history daily closes for the price chart.

Longbridge caps `candlesticks()` at 1000 bars, about four years, so it cannot see a
true all-time high: a ticker that peaked in 2021 would show a drawdown from a local
high inside that window. yfinance reaches a ticker's inception (SPY: 1993-01-29,
8433 sessions), which is the only reason this module exists.

Two rules govern it:

1. **One basis per series.** Yahoo's auto-adjusted close and Longbridge's
   `ForwardAdjust` close are different series. They are stored under different
   `source` values and a chart is drawn wholly from one of them, never stitched. The
   card names the source it drew.
2. **Never on the request path.** yfinance is a scraper over an undocumented endpoint:
   it takes about a second when it works and breaks outright when Yahoo changes its
   response. `refresh_ticker` does the network work from a worker thread, gated to once
   per day; `series()` reads cache only.

`import yfinance` is deliberately deferred into `fetch_yahoo`. At module scope it costs
about a second of startup and drags in its own pandas machinery, and the test suite
must never import it at all.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

from app.providers.longport_client import display_ticker
from app.store import Store, get_store

log = logging.getLogger(__name__)

# Auto-adjusted close (splits and dividends), full history.
SOURCE_YAHOO = "yahoo"
# ForwardAdjust close, capped at `settings.candle_lookback` bars. Fallback only: it is
# what we already hold when a ticker is first viewed and Yahoo has not answered yet.
SOURCE_LONGPORT = "longport"


class PriceSourceError(RuntimeError):
    """A price source could not be reached or answered unusably. Worth retrying."""


@dataclass(frozen=True)
class PriceSeries:
    """A daily close series from exactly one adjustment basis."""

    symbol: str
    source: str
    dates: tuple[str, ...]
    closes: tuple[float, ...]

    @property
    def label(self) -> str:
        if self.source == SOURCE_YAHOO:
            return "Yahoo adj close"
        return "Longbridge fwd-adj"

    @property
    def all_time(self) -> bool:
        """True when the series reaches the ticker's inception.

        Only then may the maximum be called an all-time high. The Longbridge window is
        truncated by an API limit, so its maximum is a window high and nothing more.
        """
        return self.source == SOURCE_YAHOO


def yahoo_symbol(ticker: str) -> str:
    """`BRK.B` is `BRK-B` at Yahoo. The one translation it needs."""
    return display_ticker(ticker).replace(".", "-")


def fetch_yahoo(ticker: str) -> list[tuple[str, float]]:
    """Full daily close history, oldest-first. Blocking, ~1s. Raises on any failure."""
    try:
        import yfinance  # noqa: PLC0415 — deferred: ~1s import, and tests must not load it
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise PriceSourceError(f"yfinance unavailable: {exc}") from exc

    symbol = yahoo_symbol(ticker)
    try:
        frame = yfinance.Ticker(symbol).history(
            period="max", interval="1d", auto_adjust=True, raise_errors=True
        )
    except Exception as exc:  # noqa: BLE001 - a scraper fails in unbounded ways
        raise PriceSourceError(f"yahoo {symbol}: {type(exc).__name__}: {exc}") from exc

    if frame is None or frame.empty or "Close" not in frame:
        raise PriceSourceError(f"yahoo {symbol}: no Close column")

    rows: list[tuple[str, float]] = []
    for stamp, value in frame["Close"].items():
        close = float(value) if value is not None else 0.0
        # A split or listing artefact can leave NaN or zero rows mid-series; they would
        # otherwise become a fake crash to zero on the chart.
        if not math.isfinite(close) or close <= 0:
            continue
        rows.append((stamp.date().isoformat(), close))

    if len(rows) < 2:
        raise PriceSourceError(f"yahoo {symbol}: {len(rows)} usable closes")
    rows.sort()
    return rows


def record_longport(
    ticker: str, bars: list[tuple[str, float]], store: Store | None = None
) -> int:
    """Cache the daily bars we already fetched for the trend metrics.

    Free — the bars are in hand. This is the fallback series a cold ticker charts from
    while Yahoo is still being fetched in the background.
    """
    if not bars:
        return 0
    store = store or get_store()
    name = display_ticker(ticker)
    return store.upsert_price_history(
        [(name, d, c, SOURCE_LONGPORT) for d, c in bars if c > 0]
    )


def refresh_ticker(ticker: str, store: Store | None = None, on: date | None = None) -> str | None:
    """Fetch and cache the full Yahoo history. Once per day.

    Blocking. Call from a worker thread. Returns the source written, or None on
    failure — in which case the caller keeps drawing the Longbridge window.
    """
    store = store or get_store()
    name = display_ticker(ticker)
    key = f"price:{SOURCE_YAHOO}:{name}"
    if store.fetched_today(key, on):
        return SOURCE_YAHOO
    try:
        rows = fetch_yahoo(ticker)
    except PriceSourceError as exc:
        log.warning("%s", exc)
        store.mark_fetched(key, False, str(exc)[:200], on)
        return None
    store.upsert_price_history([(name, d, c, SOURCE_YAHOO) for d, c in rows])
    store.mark_fetched(key, True, f"{len(rows)} closes from {rows[0][0]}", on)
    return SOURCE_YAHOO


def series(ticker: str, store: Store | None = None) -> PriceSeries | None:
    """Best cached series for this ticker. Never touches the network.

    Prefers Yahoo for its depth, falls back to the Longbridge window so a ticker being
    viewed for the first time still charts something — labelled as what it is.
    """
    store = store or get_store()
    name = display_ticker(ticker)
    for source in (SOURCE_YAHOO, SOURCE_LONGPORT):
        rows = store.price_series(name, source)
        if len(rows) >= 2:
            return PriceSeries(
                symbol=name,
                source=source,
                dates=tuple(d for d, _ in rows),
                closes=tuple(c for _, c in rows),
            )
    return None
