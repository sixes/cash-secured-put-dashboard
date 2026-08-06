"""Risk-free rate from FRED DGS3MO (3-month Treasury constant maturity).

Keyless CSV endpoint, cached once per day in SQLite. DGS3MO is quoted in percent on
a bond-equivalent basis; we store the percent and expose a continuously-compounded
decimal, which is what Black-76 discounting wants.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from datetime import date, datetime, timedelta

import httpx

from app.store import Store, get_store

log = logging.getLogger(__name__)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
SERIES = "DGS3MO"
FETCH_KEY = f"rate:{SERIES}"

# Used only if FRED is unreachable and the cache is empty.
FALLBACK_RATE = 0.04

# FRED marks holidays and non-observations with a bare ".".
_MISSING = {"", ".", "NA", "null"}


def _parse_csv(text: str) -> list[tuple[str, float]]:
    """Parse FRED's two-column CSV.

    The date column has been named both `DATE` and `observation_date`, so read by
    position rather than by header name.
    """
    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[str, float]] = []
    for i, row in enumerate(reader):
        if i == 0 or len(row) < 2:
            continue
        day, raw = row[0].strip(), row[1].strip()
        if raw in _MISSING:
            continue
        try:
            rows.append((day, float(raw)))
        except ValueError:
            continue
    return rows


def refresh(store: Store | None = None, lookback_days: int = 400) -> int:
    """Fetch DGS3MO and cache it. Returns rows written; 0 if already done today."""
    store = store or get_store()
    if store.fetched_today(FETCH_KEY):
        return 0

    try:
        resp = httpx.get(FRED_CSV.format(series=SERIES), timeout=20.0)
        resp.raise_for_status()
        rows = _parse_csv(resp.text)
    except Exception as exc:
        log.warning("FRED %s fetch failed: %s", SERIES, exc)
        store.mark_fetched(FETCH_KEY, False, str(exc))
        return 0

    if not rows:
        store.mark_fetched(FETCH_KEY, False, "no parseable rows")
        return 0

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    recent = [r for r in rows if r[0] >= cutoff]
    written = store.upsert_rates(recent or rows[-1:])
    store.mark_fetched(FETCH_KEY, True, f"{written} rows, latest {rows[-1][0]}")
    return written


def risk_free_rate(store: Store | None = None, refresh_if_stale: bool = True) -> float:
    """Continuously-compounded risk-free rate as a decimal.

    DGS3MO is an annualized bond-equivalent yield; convert with ln(1 + y) so that
    exp(-rT) discounts correctly.
    """
    store = store or get_store()
    if refresh_if_stale:
        refresh(store)

    latest = store.latest_rate()
    if latest is None:
        log.warning("no cached %s; falling back to %.2f%%", SERIES, FALLBACK_RATE * 100)
        return FALLBACK_RATE

    day, pct = latest
    simple = pct / 100.0
    if simple <= -1.0:
        return FALLBACK_RATE
    return math.log1p(simple)


def rate_provenance(store: Store | None = None) -> dict[str, object]:
    store = store or get_store()
    latest = store.latest_rate()
    if latest is None:
        return {"source": "fallback", "series": SERIES, "as_of": None, "pct": FALLBACK_RATE * 100}
    day, pct = latest
    stale_days = (date.today() - datetime.strptime(day, "%Y-%m-%d").date()).days
    return {
        "source": "FRED",
        "series": SERIES,
        "as_of": day,
        "pct": pct,
        "stale_days": stale_days,
    }
