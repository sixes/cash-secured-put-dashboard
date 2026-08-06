"""Live engine: owns the three clocks and the quota budget between them.

Prices arrive by push and cost nothing after the one-time subscribe. Greeks come
from `calc_indexes`, which is REST and billed per option symbol per call, so chains
rebuild on a slow timer while bid/ask ticks freely (see providers/quota.py).

Comparing tenors needs a third clock, because the whole DTE window is priced. Only the
best tenor is re-priced every `greeks_poll_seconds`; every other expiry waits
`slow_tenor_poll_seconds`. And the order of operations is inverted against the obvious
one: the chain is built and RANKED with no subscriptions at all, then only
`max_quoted_contracts` are subscribed, so the separate 500-symbol concurrent ceiling
stops scaling with the width of the window.

Only tickers with at least one live viewer are polled. An abandoned browser tab must
not keep spending an account-wide budget that other tickers need.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from app.config import MARKET_TZ, settings
from app.metrics.chart import PriceChart, build_price_chart
from app.metrics.trend import TrendMetrics, compute_trend
from app.params import ScreenParams, chain_key, defaults, trend_key
from app.providers import ivhistory, pricehistory, rates
from app.providers.chain import (
    FAST,
    SLOW,
    ChainResult,
    apply_quotes,
    build_chain,
    rank_for_quotes,
)
from app.providers.ivhistory import IVRank
from app.providers.longport_client import LongportClient, get_client, us_symbol
from app.providers.quota import get_governor
from app.providers.quotehub import QuoteHub
from app.providers.subscriptions import SubscriptionManager

log = logging.getLogger(__name__)

_MARKET_TZ = ZoneInfo(MARKET_TZ)

# Floor under the estimated option-quota cost of a rebuild. Measured 28 for one slice;
# a multi-tenor view estimates from what its own last rebuild actually spent, since the
# cost scales with how many tenors are due on their clocks.
ESTIMATED_REFRESH_COST = 40

# Consecutive refreshes a challenger tenor must win before the fast clock re-points.
# Re-pointing drops one tenor's subscriptions and buys another's, so a tie oscillating
# on a half-cent mid would otherwise churn the whole set every minute.
REPOINT_WINS = 2


def is_regular_session(now: datetime | None = None) -> bool:
    """US regular trading hours, 09:30-16:00 ET on a weekday.

    Market holidays are NOT handled: this only decides whether a quiet feed is
    reported as "stale" or as "market closed", so a holiday shows a spurious
    staleness warning rather than a wrong price.
    """
    t = (now or datetime.now(tz=_MARKET_TZ)).astimezone(_MARKET_TZ)
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


@dataclass
class TickerView:
    """Everything one ticker's page needs, minus the live quotes."""

    ticker: str
    spot: float
    trend: TrendMetrics
    chain: ChainResult
    rate: float
    built_at: float
    params: ScreenParams = field(default_factory=defaults)
    iv_rank: IVRank | None = None
    chart: PriceChart | None = None
    error: str | None = None
    viewers: int = 0
    # Wall time of the last billed rebuild, and the part of it spent blocked on the
    # option-quota window. A parameter change that waits has to say so.
    chain_seconds: float = 0.0
    quota_wait_seconds: float = 0.0
    underlying_seconds: float = 0.0
    # Which expiry owns the fast clock, plus the hysteresis state that keeps it from
    # oscillating. Every other priced expiry runs on slow_tenor_poll_seconds.
    fast_expiry: date | None = None
    challenger: date | None = None
    challenger_wins: int = 0
    # The ranked contracts actually held on the push stream, which is a small subset of
    # every candidate priced.
    quoted_symbols: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def age(self) -> float:
        return time.monotonic() - self.built_at

    @property
    def refresh_cost(self) -> int:
        """What this view's next rebuild is likely to cost, from what the last one did.

        A fixed constant would under-read a 13-tenor window by an order of magnitude and
        let the poller overdraw the account-wide budget.
        """
        return max(ESTIMATED_REFRESH_COST, self.chain.quota_spent)


@dataclass(frozen=True)
class LinkStatus:
    connected: bool
    stale: bool
    session_open: bool
    seconds_since_push: float | None
    quota_spent: int
    quota_available: int
    quota_blocked_for: float = 0.0

    @property
    def label(self) -> str:
        if not self.connected:
            return "disconnected"
        # Ahead of "live": prices are still arriving, but any billed rebuild is stalled,
        # and a browser waiting on one deserves to see that from an already-open tab.
        if self.quota_blocked_for > 0:
            return f"waiting for option quota ({self.quota_blocked_for:.0f}s)"
        if not self.session_open:
            return "market closed"
        return "delayed - reconnecting" if self.stale else "live"


class LiveEngine:
    def __init__(self, client: LongportClient | None = None) -> None:
        self.client = client or get_client()
        self.hub = QuoteHub(self.client)
        self.subs = SubscriptionManager(self.client)
        self._views: dict[str, TickerView] = {}
        self._lock = asyncio.Lock()
        self._poller: asyncio.Task | None = None
        self._governor = get_governor()
        self._connected = True
        self._iv_fetching: set[str] = set()
        self._iv_tasks: set[asyncio.Task] = set()
        self._price_fetching: set[str] = set()
        self._price_tasks: set[asyncio.Task] = set()

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self.hub.attach(asyncio.get_running_loop())
        await asyncio.to_thread(rates.refresh)
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            try:
                await self._poller
            except asyncio.CancelledError:
                pass
            self._poller = None
        await asyncio.to_thread(self.subs.clear)

    # --- reads ----------------------------------------------------------------

    def status(self) -> LinkStatus:
        return LinkStatus(
            connected=self._connected,
            stale=self.hub.is_stale(),
            session_open=is_regular_session(),
            seconds_since_push=self.hub.seconds_since_push(),
            quota_spent=self._governor.spent(),
            quota_available=self._governor.available(),
            quota_blocked_for=self._governor.blocked_for(),
        )

    def quoted(self, ticker: str, params: ScreenParams | None = None) -> ChainResult | None:
        """The chain with live quotes folded in. Free: reads local state only.

        Every FREE parameter is applied right here, which is why a delta or liquidity
        edit needs no rebuild.
        """
        view = self._views.get(ticker)
        if view is None:
            return None
        return apply_quotes(
            view.chain, self.hub.books(view.quoted_symbols), params or view.params
        )

    def view(self, ticker: str) -> TickerView | None:
        return self._views.get(ticker)

    def active(self) -> list[str]:
        return list(self._views)

    # --- viewer accounting ----------------------------------------------------

    def add_viewer(self, ticker: str) -> None:
        view = self._views.get(ticker)
        if view is not None:
            view.viewers += 1

    def remove_viewer(self, ticker: str) -> None:
        view = self._views.get(ticker)
        if view is not None:
            view.viewers = max(0, view.viewers - 1)

    # --- builds ---------------------------------------------------------------

    async def ensure(
        self,
        ticker: str,
        params: ScreenParams | None = None,
        max_age: float | None = None,
    ) -> TickerView:
        """Return a view that answers `params`, doing the least work that requires."""
        symbol = us_symbol(ticker)
        p = params or defaults()
        limit = settings.greeks_poll_seconds if max_age is None else max_age

        async with self._lock:
            view = self._views.get(symbol)
            if view is None:
                view = await self._build(symbol, p)
                self._views[symbol] = view
                return view

        underlying, chain = self._work_for(view, p, limit)
        if underlying or chain:
            await self._refresh(view, p, underlying=underlying, chain=chain)
        else:
            # Only free fields moved. apply_quotes will honour them at render time.
            view.params = p
        return view

    def _work_for(
        self, view: TickerView, params: ScreenParams, limit: float
    ) -> tuple[bool, bool]:
        """(refetch underlying, rebuild chain) needed before `view` can answer `params`."""
        if view.age > limit:
            return True, True
        return (
            trend_key(view.params) != trend_key(params),
            chain_key(view.params) != chain_key(params),
        )

    async def _build(self, symbol: str, params: ScreenParams) -> TickerView:
        started = time.monotonic()
        spot, trend, rate, chart = await self._underlying(symbol, params)
        view = TickerView(
            ticker=symbol,
            spot=spot,
            trend=trend,
            chain=ChainResult(symbol, spot, [], None, None, None, []),
            rate=rate,
            built_at=time.monotonic(),
            params=params,
            chart=chart,
            underlying_seconds=time.monotonic() - started,
        )
        await self._rebuild_chain(view)
        return view

    async def _underlying(
        self, symbol: str, params: ScreenParams
    ) -> tuple[float, TrendMetrics, float, PriceChart | None]:
        quotes = await self.client.aquote([symbol])
        if symbol not in quotes or quotes[symbol].last is None:
            raise UnknownTicker(symbol)

        spot = quotes[symbol].last
        bars = await self.client.acandles(symbol, settings.candle_lookback)
        closes = [b.close for b in bars]
        dates = [b.ts.date().isoformat() for b in bars]
        # The windows must reach the cards, not just the chart: without them the SMA
        # card would keep reading the function default while the chart moved.
        trend = compute_trend(
            symbol,
            closes,
            last=spot,
            sma_window=params.sma_window,
            rv_window=params.rv_window,
            first_date=dates[0] if dates else None,
        )
        chart = await asyncio.to_thread(
            self._build_chart, symbol, dates, closes, params
        )
        return spot, trend, rates.risk_free_rate(), chart

    def _build_chart(
        self,
        symbol: str,
        dates: list[str],
        closes: list[float],
        params: ScreenParams,
    ) -> PriceChart | None:
        """Cache the bars already in hand, then draw from the deepest series cached.

        Blocking (SQLite): call from a worker thread. Never fetches, so a ticker seen
        for the first time charts the Longbridge window and upgrades to Yahoo's full
        history once the background fetch lands.
        """
        try:
            pricehistory.record_longport(symbol, list(zip(dates, closes)))
            cached = pricehistory.series(symbol)
        except Exception as exc:  # noqa: BLE001 - a chart is never worth a 500
            log.warning("price history unavailable for %s: %s", symbol, exc)
            return None
        if cached is None:
            return None
        return build_price_chart(
            list(cached.dates),
            list(cached.closes),
            sma_window=params.sma_window,
            window=params.chart_window_sessions,
            width=settings.chart_width,
            height=settings.chart_height,
            source=cached.source,
            source_label=cached.label,
            all_time=cached.all_time,
        )

    async def _refresh(
        self,
        view: TickerView,
        params: ScreenParams | None = None,
        *,
        underlying: bool = True,
        chain: bool = True,
        only: Sequence[date] | None = None,
    ) -> None:
        p = params or view.params
        if view._lock.locked():
            # Another request is already refreshing this ticker, so wait it out rather
            # than paying for a second rebuild. Its result is only OUR answer if it was
            # built with our parameters, hence the re-check once the lock clears.
            async with view._lock:
                pass
            underlying, chain = self._work_for(view, p, settings.greeks_poll_seconds)
            if not (underlying or chain):
                view.params = p
                return

        async with view._lock:
            view.params = p
            try:
                if underlying:
                    started = time.monotonic()
                    spot, trend, rate, chart = await self._underlying(view.ticker, p)
                    view.underlying_seconds = time.monotonic() - started
                    view.spot, view.trend, view.rate = spot, trend, rate
                    view.chart = chart
                if chain:
                    await self._rebuild_chain(view, only)
                view.error = None
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh failed for %s: %s", view.ticker, exc)
                view.error = str(exc)

    async def _rebuild_chain(
        self, view: TickerView, only: Sequence[date] | None = None
    ) -> None:
        waited_before = self._governor.waited()
        started = time.monotonic()
        chain = await build_chain(
            self.client,
            view.ticker,
            view.spot,
            view.rate,
            rv20=view.trend.rv20.rv,
            params=view.params,
            previous=view.chain,
            only=only,
        )
        view.chain_seconds = time.monotonic() - started
        view.quota_wait_seconds = self._governor.waited() - waited_before
        view.chain = self._point_clocks(view, chain)
        view.built_at = time.monotonic()

        # The funnel. build_chain subscribed nothing, so the whole window has been priced
        # and ranked before anything reaches the concurrent-subscription ceiling; only
        # the ranked contracts get a live quote, and the rest keep their `—`.
        view.quoted_symbols = rank_for_quotes(view.chain.slices, view.params)
        stream = [view.ticker] + self._fast_extras(view) + view.quoted_symbols
        await asyncio.to_thread(self.subs.activate, view.ticker, stream)
        # subscribe() has no first-push flag in 3.0.18, so nothing arrives until the
        # next tick. Without this the table renders empty on a quiet symbol.
        await asyncio.to_thread(self.hub.seed, stream)
        await asyncio.to_thread(self._update_iv_rank, view)
        if view.iv_rank is None or not view.iv_rank.sufficient:
            self._schedule_iv_fetch(view.ticker)
        if view.chart is None or view.chart.source != pricehistory.SOURCE_YAHOO:
            self._schedule_price_fetch(view.ticker)

    def _point_clocks(self, view: TickerView, chain: ChainResult) -> ChainResult:
        """Decide which tenor owns the fast clock, and label every slice with its own.

        The best tenor moves as prices move, but re-pointing drops 24 subscriptions and
        buys 24 more, so a challenger has to win REPOINT_WINS refreshes in a row.
        """
        if not chain.slices:
            view.fast_expiry = None
            return chain

        priced = {sl.expiry for sl in chain.slices}
        # The new chain's ranked set does not exist yet, so this is the PREVIOUS set —
        # the only symbols actually holding a live quote. Feeding it the whole chain would
        # let a symbol dropped from the ranked set decide the clock on a frozen book.
        quoted = apply_quotes(chain, self.hub.books(view.quoted_symbols), view.params)
        best = (
            quoted.match.candidate.expiry
            if quoted.match and quoted.match.candidate
            else None
        )

        current = view.fast_expiry if view.fast_expiry in priced else None
        if current is None:
            # No incumbent to protect: whatever ranks best takes the clock immediately,
            # falling back to the shortest priced tenor before any quote has arrived.
            current = best or chain.slices[0].expiry
            view.challenger, view.challenger_wins = None, 0
        elif best is not None and best != current:
            view.challenger_wins = (
                view.challenger_wins + 1 if view.challenger == best else 1
            )
            view.challenger = best
            if view.challenger_wins >= REPOINT_WINS:
                current, view.challenger, view.challenger_wins = best, None, 0
        else:
            view.challenger, view.challenger_wins = None, 0

        view.fast_expiry = current
        chain.slices = [
            replace(sl, clock=FAST if sl.expiry == current else SLOW)
            for sl in chain.slices
        ]
        return chain

    def _fast_extras(self, view: TickerView) -> list[str]:
        """The fast tenor's ATM probe pair, so its parity forward keeps a live quote."""
        for sl in view.chain.slices:
            if sl.expiry == view.fast_expiry:
                return [s for s in (sl.atm_call_symbol, sl.atm_put_symbol) if s]
        return []

    def _due_expiries(self, view: TickerView) -> Sequence[date] | None:
        """Expiries to re-price now: the fast tenor, plus any slow tenor past its timer.

        None means the whole window, which is what a first build or a parameter change
        needs — a changed DTE window or screen band moves which symbols exist at all.
        """
        if not view.chain.slices:
            return None
        return [
            sl.expiry
            for sl in view.chain.slices
            if sl.clock == FAST or sl.age >= settings.slow_tenor_poll_seconds
        ]

    def _update_iv_rank(self, view: TickerView) -> None:
        """Record today's ATM IV and read the rank. Cache only, so it is fast."""
        if view.chain.atm_iv_30d:
            ivhistory.record_local(view.ticker, view.chain.atm_iv_30d)
        view.iv_rank = ivhistory.iv_rank(view.ticker, view.chain.atm_iv_30d)

    def _schedule_iv_fetch(self, symbol: str) -> None:
        """Fetch external IV history off the request path.

        DoltHub can take over a minute to answer, so this must never be awaited by a
        page render. A ticker seen for the first time shows no rank and gains one on
        the next refresh.
        """
        if symbol in self._iv_fetching:
            return
        self._iv_fetching.add(symbol)

        async def run() -> None:
            try:
                await asyncio.to_thread(ivhistory.refresh_ticker, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("IV history fetch failed for %s: %s", symbol, exc)
            finally:
                self._iv_fetching.discard(symbol)

        task = asyncio.create_task(run())
        # Hold a reference or the loop may garbage-collect the task mid-flight.
        self._iv_tasks.add(task)
        task.add_done_callback(self._iv_tasks.discard)

    def _schedule_price_fetch(self, symbol: str) -> None:
        """Fetch the long close history off the request path.

        yfinance scrapes an undocumented endpoint and takes about a second when it
        works, so a page render must never wait on it. The first paint of a new ticker
        charts the Longbridge window and the next refresh picks up the full history.
        """
        if symbol in self._price_fetching:
            return
        self._price_fetching.add(symbol)

        async def run() -> None:
            try:
                await asyncio.to_thread(pricehistory.refresh_ticker, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("price history fetch failed for %s: %s", symbol, exc)
            finally:
                self._price_fetching.discard(symbol)

        task = asyncio.create_task(run())
        self._price_tasks.add(task)
        task.add_done_callback(self._price_tasks.discard)

    async def release(self, ticker: str) -> None:
        symbol = us_symbol(ticker)
        async with self._lock:
            view = self._views.pop(symbol, None)
        if view is not None:
            await asyncio.to_thread(self.subs.release, symbol)
            self.hub.drop(view.chain.all_symbols)

    # --- poller ---------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.greeks_poll_seconds)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("greeks poller iteration failed")

    async def _poll_once(self) -> None:
        await self._check_link()

        due = [
            v
            for v in list(self._views.values())
            if v.viewers > 0 and v.age >= settings.greeks_poll_seconds
        ]
        # Oldest first, so a starved budget degrades fairly instead of always
        # refreshing whichever ticker happens to be first in the dict.
        due.sort(key=lambda v: v.built_at)

        for view in due:
            if self._governor.available() < view.refresh_cost:
                log.info(
                    "skipping refresh of %s: %d quota units left, need ~%d",
                    view.ticker,
                    self._governor.available(),
                    view.refresh_cost,
                )
                break
            await self._refresh(view, only=self._due_expiries(view))

    async def _check_link(self) -> None:
        """Reconcile subscriptions when the feed has gone quiet mid-session.

        The server drops its subscription set when the long link dies, but our book
        survives, so only reconciliation gets symbols ticking again.
        """
        if not is_regular_session() or not self.hub.is_stale():
            self._connected = True
            return

        log.warning("feed quiet during regular session; reconciling subscriptions")
        try:
            missing, extra = await asyncio.to_thread(self.subs.reconcile)
            self._connected = True
            if missing:
                # Re-seed from REST so the page is not left showing pre-drop prices
                # with no indication they are frozen.
                await asyncio.to_thread(self.hub.seed, missing)
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            log.warning("reconcile failed: %s", exc)


class UnknownTicker(ValueError):
    def __init__(self, symbol: str) -> None:
        super().__init__(f"no quote for {symbol}")
        self.symbol = symbol


_engine: LiveEngine | None = None


def get_engine() -> LiveEngine:
    global _engine
    if _engine is None:
        _engine = LiveEngine()
    return _engine
