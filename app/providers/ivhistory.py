"""IV history and IV Rank, assembled from three sources that never mix.

No free source covers arbitrary tickers, so this is a bootstrap plus own-recorder
design:

* index proxies (VIX/VXN/RVX/VXD from CBOE, FRED as fallback) for SPY/QQQ/IWM/DIA
* DoltHub `post-no-preference/options` for any other ticker on day one
* our own nightly ATM-IV recording, which supersedes both after a full year

Two correctness rules govern the whole module:

1. VIX is a whole-strip variance measure, not ATM IV — its level runs ~1.2x SPY's
   ATM IV. Rank and percentile are invariant under a positive affine map, so *rank*
   transfers but the *level* must never be shown as the ticker's IV.
2. Never concatenate raw IV levels across sources; a changeover date would inject a
   fake vol jump. Every row carries its `source` and rank is computed within one.

Network access lives in `refresh_ticker`, which is gated to once per day and is meant
to be called from a background thread. `iv_rank` reads cache only, so rendering a page
never waits on a third party — DoltHub in particular can take over a minute to answer.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from app.config import settings
from app.providers.longport_client import display_ticker
from app.store import Store, get_store

log = logging.getLogger(__name__)

# A full year of sessions. Below this the range is not a "52-week" range at all.
IV_RANK_WINDOW = 252

CBOE_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{index}_History.csv"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
DOLT_SQL = "https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master"

# DoltHub answers a cold query slowly; this runs off the request path.
DOLT_TIMEOUT = 120.0

# ETFs whose implied vol has a published index. The index is a rank proxy only.
PROXY_INDEX = {"SPY": "VIX", "QQQ": "VXN", "IWM": "RVX", "DIA": "VXD"}
FRED_SERIES = {"VIX": "VIXCLS", "VXN": "VXNCLS", "RVX": "RVXCLS", "VXD": "VXDCLS"}

SOURCE_LOCAL = "local"
SOURCE_DOLT = "dolthub"

# DoltHub takes the query as a URL parameter, so the symbol is interpolated into SQL.
# Only shapes that are actually US tickers are allowed through.
_SYMBOL_OK = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")

_MISSING = {"", ".", "NA", "null", "n/a"}

# Logged as today's detail when a source answered correctly but simply does not carry
# the symbol. Distinguishes a permanent gap from a network blip worth retrying.
NO_COVERAGE = "not covered"


class IVSourceError(RuntimeError):
    """A source could not be reached or answered unusably. Worth retrying."""


@dataclass(frozen=True)
class IVRank:
    """Where the current IV sits in its trailing range, plus how we know."""

    ticker: str
    source: str
    rank: float | None
    percentile: float | None
    n: int
    low: float | None
    high: float | None
    observed: float | None
    as_of: str | None
    proxy_of: str | None = None
    window: int = IV_RANK_WINDOW
    # False when the range comes from a publisher's asserted high/low rather than
    # from observations we hold. `n` is then 0 and is not a sample size.
    series_backed: bool = True

    @property
    def sufficient(self) -> bool:
        """False when the sample is too thin to present un-caveated.

        A bounds-only source asserts a full 52-week range, so it is not thin — it is
        simply unverifiable, which `series_backed` reports instead.
        """
        if not self.series_backed:
            return True
        return self.n >= settings.min_iv_history_days

    @property
    def level_displayable(self) -> bool:
        """A proxy index level is not this ticker's IV and must not be shown as it."""
        return self.proxy_of is None

    @property
    def label(self) -> str:
        if self.proxy_of:
            return f"{self.proxy_of} proxy"
        if self.source == SOURCE_DOLT:
            return "DoltHub 52w range"
        return "own recording"


# --- rank math (pure) ---------------------------------------------------------


def rank_within(value: float, low: float, high: float) -> float | None:
    """Position of `value` in [low, high] as 0-100.

    Clamped: a fresh vol spike legitimately prints outside its own trailing range,
    and 103 would read as an error rather than as "at the top".
    """
    if high is None or low is None or high <= low:
        return None
    return max(0.0, min(100.0, 100.0 * (value - low) / (high - low)))


def percentile_within(value: float, series: list[float]) -> float | None:
    """Share of observations at or below `value`, 0-100."""
    if not series:
        return None
    return 100.0 * sum(1 for x in series if x <= value) / len(series)


# --- source fetchers ----------------------------------------------------------


def _parse_two_column(text: str, date_col: int, value_col: int, fmt: str | None) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if i == 0 or len(row) <= max(date_col, value_col):
            continue
        raw_day, raw_val = row[date_col].strip(), row[value_col].strip()
        if raw_val.lower() in _MISSING:
            continue
        try:
            day = datetime.strptime(raw_day, fmt).date().isoformat() if fmt else raw_day
            rows.append((day, float(raw_val)))
        except ValueError:
            continue
    return rows


def fetch_index_series(index: str) -> list[tuple[str, float]]:
    """Daily closes of a CBOE vol index as IV ratios, oldest-first.

    CBOE publishes vol points (15.99); everything downstream of this module speaks
    ratios, so divide by 100 at the boundary exactly as we do for the API's IV.
    """
    try:
        resp = httpx.get(CBOE_CSV.format(index=index), timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        rows = _parse_two_column(resp.text, 0, 4, "%m/%d/%Y")
        if rows:
            return [(d, v / 100.0) for d, v in rows]
        log.warning("CBOE %s returned no parseable rows", index)
    except Exception as exc:  # noqa: BLE001
        log.warning("CBOE %s fetch failed: %s", index, exc)

    series = FRED_SERIES.get(index)
    if not series:
        return []
    try:
        resp = httpx.get(FRED_CSV.format(series=series), timeout=30.0)
        resp.raise_for_status()
        return [(d, v / 100.0) for d, v in _parse_two_column(resp.text, 0, 1, None)]
    except Exception as exc:  # noqa: BLE001
        log.warning("FRED %s fetch failed: %s", series, exc)
        return []


def fetch_dolthub(ticker: str) -> dict[str, float | str] | None:
    """Latest DoltHub `volatility_history` row: current IV plus its 52-week range.

    Returns None only when the query succeeded and the dataset has no such symbol —
    QQQ, IWM, GLD and TLT among many others, which is why the index-proxy leg exists.
    Raises IVSourceError on a transport or query failure, so the caller can retry a
    transient problem without retrying a permanent absence.

    Only single-row lookups are usable: DoltHub applies a server-side deadline and any
    range scan of this table dies with "context deadline exceeded" partway through.
    """
    if not _SYMBOL_OK.match(ticker):
        raise IVSourceError(f"implausible symbol {ticker!r}")

    query = (
        "SELECT date, iv_current, iv_year_high, iv_year_low "
        "FROM volatility_history "
        f"WHERE act_symbol = '{ticker}' AND iv_current IS NOT NULL "
        "ORDER BY date DESC LIMIT 1"
    )
    try:
        resp = httpx.get(
            f"{DOLT_SQL}?q={urllib.parse.quote(query)}", timeout=DOLT_TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise IVSourceError(f"DoltHub request failed: {exc}") from exc

    if body.get("query_execution_status") != "Success":
        raise IVSourceError(f"DoltHub query failed: {body.get('query_execution_message')}")

    rows = body.get("rows") or []
    if not rows:
        return None

    row = rows[0]
    try:
        return {
            "date": str(row["date"])[:10],
            "atm_iv": float(row["iv_current"]),
            "high": float(row["iv_year_high"]),
            "low": float(row["iv_year_low"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise IVSourceError(f"DoltHub row unusable: {exc}") from exc


# --- writes -------------------------------------------------------------------


def record_local(ticker: str, atm_iv: float, on: date | None = None, store: Store | None = None) -> bool:
    """Store one day's own-measured 30-day constant-maturity ATM IV.

    Costs nothing: the value is already computed for the page. Returns False when
    today is already recorded, so calling this on every chain rebuild is safe.
    """
    if atm_iv is None or atm_iv <= 0:
        return False
    store = store or get_store()
    name = display_ticker(ticker)
    day = (on or date.today()).isoformat()
    key = f"iv:{SOURCE_LOCAL}:{name}"
    if store.fetched_today(key, on):
        return False
    store.upsert_iv_history([(name, day, atm_iv, SOURCE_LOCAL)])
    store.mark_fetched(key, True, f"{atm_iv:.4f}", on)
    return True


def refresh_ticker(ticker: str, store: Store | None = None, on: date | None = None) -> str | None:
    """Populate whichever external source covers this ticker. Once per day.

    Blocking. Call from a worker thread — DoltHub is slow enough to stall a page.
    Returns the source that was written, or None.
    """
    store = store or get_store()
    name = display_ticker(ticker)

    index = PROXY_INDEX.get(name)
    if index:
        source = f"cboe:{index}"
        key = f"iv:{source}"
        if store.fetched_today(key, on):
            return source
        series = fetch_index_series(index)
        if not series:
            store.mark_fetched(key, False, "no rows", on)
            return None
        # Keep a margin over the rank window so a few holidays cannot shorten it.
        cutoff = ((on or date.today()) - timedelta(days=500)).isoformat()
        rows = [(index, d, v, source) for d, v in series if d >= cutoff]
        store.upsert_iv_history(rows)
        store.mark_fetched(key, True, f"{len(rows)} rows, latest {series[-1][0]}", on)
        return source

    key = f"iv:{SOURCE_DOLT}:{name}"
    if store.fetched_today(key, on):
        return SOURCE_DOLT
    if store.fetch_detail(key, on) == NO_COVERAGE:
        # The dataset simply lacks this symbol. Retrying every poll would hammer a
        # free service for an answer that cannot change today.
        return None
    try:
        row = fetch_dolthub(name)
    except IVSourceError as exc:
        # Transient: no NO_COVERAGE marker, so the next poll tries again.
        log.warning("%s", exc)
        store.mark_fetched(key, False, str(exc)[:200], on)
        return None
    if row is None:
        store.mark_fetched(key, False, NO_COVERAGE, on)
        return None
    store.upsert_iv_bounds(
        name, str(row["date"]), SOURCE_DOLT, float(row["atm_iv"]), float(row["high"]), float(row["low"])
    )
    store.upsert_iv_history([(name, str(row["date"]), float(row["atm_iv"]), SOURCE_DOLT)])
    store.mark_fetched(key, True, f"as of {row['date']}", on)
    return SOURCE_DOLT


# --- read ---------------------------------------------------------------------


def iv_rank(ticker: str, current_iv: float | None = None, store: Store | None = None) -> IVRank | None:
    """IV Rank from cache only; never touches the network.

    Source preference: our own recording once it spans a year, then the index proxy,
    then DoltHub's precomputed range, then a short own recording flagged insufficient.
    """
    store = store or get_store()
    name = display_ticker(ticker)

    own = store.iv_series(name, SOURCE_LOCAL, IV_RANK_WINDOW)
    if len(own) >= IV_RANK_WINDOW:
        return _from_series(name, SOURCE_LOCAL, own, current_iv)

    index = PROXY_INDEX.get(name)
    if index:
        proxy = store.iv_series(index, f"cboe:{index}", IV_RANK_WINDOW)
        if len(proxy) >= settings.min_iv_history_days:
            # Ranked on the index's own latest close: the proxy is only valid as a
            # whole, so mixing our IV level into its range would be meaningless.
            return _from_series(name, f"cboe:{index}", proxy, None, proxy_of=index)

    bounds = store.latest_iv_bounds(name, SOURCE_DOLT)
    if bounds is not None and bounds["iv_year_high"] is not None:
        observed = current_iv if current_iv is not None else bounds["atm_iv"]
        return IVRank(
            ticker=name,
            source=SOURCE_DOLT,
            rank=rank_within(observed, bounds["iv_year_low"], bounds["iv_year_high"]),
            # A single bounds row carries no distribution, only its extremes, so there
            # is no percentile and no observation count to report.
            percentile=None,
            n=0,
            series_backed=False,
            low=bounds["iv_year_low"],
            high=bounds["iv_year_high"],
            observed=observed,
            as_of=bounds["date"],
        )

    if own:
        return _from_series(name, SOURCE_LOCAL, own, current_iv)
    return None


def _from_series(
    ticker: str,
    source: str,
    series: list[tuple[str, float]],
    current_iv: float | None,
    proxy_of: str | None = None,
) -> IVRank:
    values = [v for _, v in series]
    observed = values[-1] if current_iv is None else current_iv
    return IVRank(
        ticker=ticker,
        source=source,
        rank=rank_within(observed, min(values), max(values)),
        percentile=percentile_within(observed, values),
        n=len(values),
        low=min(values),
        high=max(values),
        observed=observed,
        as_of=series[-1][0],
        proxy_of=proxy_of,
    )
