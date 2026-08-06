from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import live as live_mod
from app.config import settings
from app.live import ESTIMATED_REFRESH_COST, LiveEngine, UnknownTicker, is_regular_session
from app.metrics.options import build_candidate
from app.metrics.pricing import carry_forward
from app.params import defaults
from app.providers import pricehistory
from app.providers.chain import FAST, SLOW, ChainResult, ExpirySlice
from app.providers.ivhistory import IVRank
from app.providers.quota import OptionQuotaGovernor

ET = ZoneInfo("America/New_York")
SPOT = 747.0
R = 0.0375


@dataclass
class FakeBar:
    close: float
    ts: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc)


@dataclass
class FakeSpot:
    symbol: str
    last: float | None
    prev_close: float | None = None
    ts: object = None


class FakeClient:
    def __init__(self) -> None:
        self.subscribed: list[list[str]] = []
        self.unsubscribed: list[list[str]] = []
        self.server: set[str] = set()
        self.reconciles = 0
        self.known = {"SPY.US": SPOT, "QQQ.US": 688.0}
        self.depth_calls: list[str] = []

    def register_callbacks(self, on_quote=None, on_depth=None):
        pass

    async def aquote(self, symbols):
        return {s: FakeSpot(s, self.known[s]) for s in symbols if s in self.known}

    async def acandles(self, symbol, count=None):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        return [FakeBar(float(600 + i), start + timedelta(days=i)) for i in range(300)]

    def quote(self, symbols):
        return {s: FakeSpot(s, self.known[s]) for s in symbols if s in self.known}

    def subscribe(self, symbols):
        self.subscribed.append(list(symbols))
        self.server.update(symbols)

    def unsubscribe(self, symbols):
        self.unsubscribed.append(list(symbols))
        self.server.difference_update(symbols)

    def subscriptions(self):
        self.reconciles += 1
        return {s: ["Quote", "Depth"] for s in self.server}

    def depth(self, symbol):
        self.depth_calls.append(symbol)
        raise RuntimeError("no book")

    def realtime_depth(self, symbol):
        raise RuntimeError("not cached")


def _chain(ticker: str, spot: float, n: int = 3) -> ChainResult:
    days = 45
    t = days / 365.0
    fwd = carry_forward(spot, R, t).forward
    expiry = date(2026, 8, 1) + timedelta(days=days)
    cands = [
        build_candidate(
            symbol=f"{ticker[:3]}260915P{int(spot) - 40 - i}000.US",
            strike=spot - 40 - i,
            expiry=expiry,
            spot=spot,
            forward=fwd,
            iv=0.20,
            r=R,
            bid=None,
            ask=None,
            api_delta=None,
            open_interest=500,
            now=datetime(2026, 8, 1, 15, 0, tzinfo=ET),
        )
        for i in range(n)
    ]
    sl = ExpirySlice(expiry=expiry, dte=days, forward=None, atm_iv=0.20, candidates=cands)
    return ChainResult(ticker, spot, [sl], 0.20, None, None, [], quota_spent=28)


def _multi_chain(
    ticker: str,
    spot: float,
    dtes: tuple[int, ...],
    strikes: list[float] | None = None,
    quota: int = 28,
) -> ChainResult:
    """An unquoted chain across several tenors — what stage 1 of the funnel yields."""
    ks = strikes if strikes is not None else [spot - 40.0 - i for i in range(6)]
    slices = []
    for days in dtes:
        t = days / 365.0
        fwd = carry_forward(spot, R, t).forward
        expiry = date(2026, 8, 1) + timedelta(days=days)
        cands = [
            build_candidate(
                symbol=f"{ticker[:3]}{days}P{int(k)}",
                strike=k,
                expiry=expiry,
                spot=spot,
                forward=fwd,
                iv=0.20,
                r=R,
                bid=None,
                ask=None,
                api_delta=None,
                open_interest=500,
                now=datetime(2026, 8, 1, 15, 0, tzinfo=ET),
            )
            for k in ks
        ]
        slices.append(
            ExpirySlice(
                expiry=expiry,
                dte=days,
                forward=None,
                atm_iv=0.20,
                candidates=[c for c in cands if c is not None],
                quota_spent=3 + len(ks),
            )
        )
    return ChainResult(ticker, spot, slices, 0.20, None, None, [], quota_spent=quota)


def _install_multi(engine, monkeypatch, dtes, strikes=None, quota=28) -> None:
    async def build(
        cl, ticker, spot, r, rv20=None, now=None, params=None, previous=None, only=None
    ):
        engine.build_calls.append(ticker)
        engine.built_only.append(None if only is None else list(only))
        return _multi_chain(ticker, spot, dtes, strikes, quota)

    monkeypatch.setattr(live_mod, "build_chain", build)


def _push_book(engine, symbol: str, bid: float, ask: float) -> None:
    level = lambda price, vol: type(  # noqa: E731
        "L", (), {"position": 0, "price": price, "volume": vol}
    )()
    engine.hub._on_depth(
        symbol, type("D", (), {"bids": [level(bid, 5)], "asks": [level(ask, 5)]})()
    )


@pytest.fixture
def engine(monkeypatch):
    client = FakeClient()
    calls: list[str] = []
    built_with: list = []
    built_only: list = []

    async def fake_build_chain(
        cl, ticker, spot, r, rv20=None, now=None, params=None, previous=None, only=None
    ):
        calls.append(ticker)
        built_with.append(params)
        built_only.append(None if only is None else list(only))
        return _chain(ticker, spot)

    monkeypatch.setattr(live_mod, "build_chain", fake_build_chain)
    monkeypatch.setattr(live_mod.rates, "refresh", lambda: None)
    monkeypatch.setattr(live_mod.rates, "risk_free_rate", lambda: R)

    # Without this the engine would write IV history into the real database.
    iv = FakeIVHistory()
    monkeypatch.setattr(live_mod, "ivhistory", iv)

    # Same reason: _build_chart caches the bars, and the background fetch scrapes Yahoo.
    prices = FakePriceHistory()
    monkeypatch.setattr(live_mod, "pricehistory", prices)

    eng = LiveEngine(client)
    # get_governor() is a process-wide singleton; a fresh one keeps the quota tests
    # from leaking spend into each other.
    eng._governor = OptionQuotaGovernor()
    eng.build_calls = calls
    eng.built_with = built_with
    eng.built_only = built_only
    eng.iv = iv
    eng.prices = prices
    return eng


class FakePriceHistory:
    """Stand-in for the pricehistory provider. Serves back whatever was recorded."""

    SOURCE_YAHOO = pricehistory.SOURCE_YAHOO
    SOURCE_LONGPORT = pricehistory.SOURCE_LONGPORT

    def __init__(self, source: str = pricehistory.SOURCE_LONGPORT) -> None:
        self.recorded: list[tuple[str, str, float]] = []
        self.fetched: list[str] = []
        self.source = source

    def record_longport(self, ticker, bars):
        self.recorded.extend((ticker, d, c) for d, c in bars)
        return len(bars)

    def series(self, ticker):
        rows = [(d, c) for t, d, c in self.recorded if t == ticker]
        if len(rows) < 2:
            return None
        return pricehistory.PriceSeries(
            symbol=ticker,
            source=self.source,
            dates=tuple(d for d, _ in rows),
            closes=tuple(c for _, c in rows),
        )

    def refresh_ticker(self, ticker):
        self.fetched.append(ticker)
        return self.SOURCE_YAHOO


class FakeIVHistory:
    """Stand-in for the ivhistory provider, recording what the engine asked for."""

    def __init__(self, rank: object | None = None) -> None:
        self.recorded: list[tuple[str, float]] = []
        self.fetched: list[str] = []
        self.rank = rank

    def record_local(self, ticker, atm_iv):
        self.recorded.append((ticker, atm_iv))
        return True

    def iv_rank(self, ticker, current_iv=None):
        return self.rank

    def refresh_ticker(self, ticker):
        self.fetched.append(ticker)
        return None


class TestSession:
    @pytest.mark.parametrize(
        "stamp,want",
        [
            ("2026-08-03 09:29", False),
            ("2026-08-03 09:30", True),
            ("2026-08-03 12:00", True),
            ("2026-08-03 15:59", True),
            ("2026-08-03 16:00", False),
            ("2026-08-01 12:00", False),  # Saturday
            ("2026-08-02 12:00", False),  # Sunday
        ],
    )
    def test_boundaries(self, stamp, want):
        assert is_regular_session(datetime.fromisoformat(stamp).replace(tzinfo=ET)) is want


class TestEnsure:
    async def test_builds_a_view(self, engine):
        view = await engine.ensure("spy")
        assert view.ticker == "SPY.US"
        assert view.spot == SPOT
        assert view.trend.sma200.sma is not None
        assert len(view.chain.all_candidates) == 3

    async def test_normalizes_the_ticker(self, engine):
        await engine.ensure("spy")
        assert engine.active() == ["SPY.US"]

    async def test_reuses_a_fresh_view(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("SPY")
        assert engine.build_calls == ["SPY.US"]

    async def test_rebuilds_a_stale_view(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("SPY", max_age=-1.0)
        assert engine.build_calls == ["SPY.US", "SPY.US"]

    async def test_unknown_ticker_raises(self, engine):
        with pytest.raises(UnknownTicker):
            await engine.ensure("NOPE")
        assert engine.active() == []

    async def test_subscribes_underlying_and_chain(self, engine):
        view = await engine.ensure("SPY")
        sent = {s for b in engine.client.subscribed for s in b}
        assert "SPY.US" in sent
        assert set(view.chain.all_symbols) <= sent

    async def test_concurrent_requests_build_once(self, engine):
        await asyncio.gather(*(engine.ensure("SPY") for _ in range(5)))
        assert engine.build_calls == ["SPY.US"]


class TestScreenParameters:
    """The free / billed / trend split, which is the whole point of the feature."""

    async def test_the_chain_is_built_with_the_requested_parameters(self, engine):
        p = replace(defaults(), dte_min=7, dte_max=21)
        await engine.ensure("SPY", p)
        assert engine.built_with == [p]

    async def test_a_free_change_costs_no_rebuild(self, engine):
        # delta target, delta band and both liquidity gates live downstream of
        # apply_quotes, so honouring them is a re-render, not a billed request.
        await engine.ensure("SPY")
        await engine.ensure("SPY", replace(defaults(), delta_target=0.19, delta_band=(0.15, 0.25)))
        assert engine.build_calls == ["SPY.US"]

    async def test_a_free_change_is_still_applied(self, engine):
        await engine.ensure("SPY")
        p = replace(defaults(), max_abs_spread=0.0, max_rel_spread_pct=0.0)
        view = await engine.ensure("SPY", p)
        assert view.params == p
        for c in engine.quoted("SPY.US").all_candidates:
            assert not c.spread.liquid

    async def test_a_billed_change_rebuilds_the_chain(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("SPY", replace(defaults(), dte_min=7, dte_max=21))
        assert engine.build_calls == ["SPY.US", "SPY.US"]

    async def test_a_widened_screening_band_rebuilds(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("SPY", replace(defaults(), delta_screen_band=(0.05, 0.4)))
        assert len(engine.build_calls) == 2

    async def test_repeating_a_billed_request_rebuilds_nothing_further(self, engine):
        p = replace(defaults(), dte_min=7, dte_max=21)
        await engine.ensure("SPY", p)
        await engine.ensure("SPY", p)
        assert engine.build_calls == ["SPY.US"]

    async def test_a_trend_change_refetches_the_underlying_but_not_the_chain(self, engine):
        await engine.ensure("SPY")
        view = await engine.ensure("SPY", replace(defaults(), sma_window=50))
        assert engine.build_calls == ["SPY.US"]
        assert view.trend.sma200.window == 50

    async def test_the_windows_reach_the_cards_not_just_the_chart(self, engine):
        # The latent bug this exposed: compute_trend was called without the windows, so
        # the SMA card read the function default while the chart followed the setting.
        baseline = await engine.ensure("SPY")
        wide_sma = baseline.trend.sma200.sma
        view = await engine.ensure("SPY", replace(defaults(), sma_window=50, rv_window=10))
        assert view.trend.sma200.window == 50
        assert view.trend.sma200.sma != wide_sma
        assert view.trend.rv20.window == 10
        assert view.trend.rv20.n_returns == 10

    async def test_the_card_and_the_chart_move_together(self, engine):
        view = await engine.ensure("SPY", replace(defaults(), sma_window=50))
        assert view.chart.sma_window == 50
        assert view.trend.sma200.window == 50

    async def test_the_chart_window_narrows_what_is_drawn(self, engine):
        view = await engine.ensure("SPY", replace(defaults(), chart_window_sessions=60))
        assert view.chart.shown == 60
        # The series behind it is untouched, so the running high does not reset.
        assert view.chart.n == 300

    async def test_quoted_can_be_asked_for_a_different_band(self, engine):
        await engine.ensure("SPY")
        wide = engine.quoted("SPY.US", replace(defaults(), delta_band=(0.01, 0.99)))
        assert all(c.in_delta_band for c in wide.all_candidates if c.delta is not None)
        none = engine.quoted("SPY.US", replace(defaults(), delta_band=(0.95, 0.99)))
        assert not any(c.in_delta_band for c in none.all_candidates)

    async def test_the_quota_wait_is_measured_not_inferred(self, engine):
        # Elapsed time would also count the request itself; only the governor knows how
        # long it made us stand still.
        waits = iter([1.0, 13.4])
        engine._governor.waited = lambda: next(waits)
        await engine.ensure("SPY")
        assert engine.view("SPY.US").quota_wait_seconds == pytest.approx(12.4)

    async def test_a_concurrent_request_with_other_parameters_is_not_given_our_answer(
        self, engine
    ):
        # Sharing an in-flight rebuild saves quota, but only when it was built with the
        # parameters the waiter asked for. Otherwise it is simply a wrong answer.
        await engine.ensure("SPY")
        p = replace(defaults(), dte_min=7, dte_max=21)
        views = await asyncio.gather(engine.ensure("SPY", p), engine.ensure("SPY", p))
        assert all(v.params == p for v in views)
        assert engine.built_with[-1] == p


class TestQuoted:
    async def test_folds_live_quotes_in(self, engine):
        view = await engine.ensure("SPY")
        sym = view.chain.all_candidates[0].symbol
        engine.hub._on_depth(
            sym,
            type("D", (), {"bids": [type("L", (), {"position": 0, "price": 3.0, "volume": 5})()],
                           "asks": [type("L", (), {"position": 0, "price": 3.05, "volume": 5})()]})(),
        )
        out = engine.quoted("SPY.US")
        quoted = {c.symbol: c for c in out.all_candidates}
        assert quoted[sym].spread.mid == pytest.approx(3.025)
        # The unquoted siblings must stay honestly blank, not inherit a mid.
        others = [c for c in out.all_candidates if c.symbol != sym]
        assert all(c.spread.mid is None for c in others)

    async def test_unknown_ticker_returns_none(self, engine):
        assert engine.quoted("ZZZ.US") is None

    async def test_does_not_mutate_the_stored_chain(self, engine):
        view = await engine.ensure("SPY")
        sym = view.chain.all_candidates[0].symbol
        engine.hub._on_depth(
            sym,
            type("D", (), {"bids": [type("L", (), {"position": 0, "price": 3.0, "volume": 5})()],
                           "asks": [type("L", (), {"position": 0, "price": 3.05, "volume": 5})()]})(),
        )
        engine.quoted("SPY.US")
        assert all(c.spread.mid is None for c in view.chain.all_candidates)


class TestPoller:
    async def test_skips_tickers_with_no_viewers(self, engine):
        # An abandoned tab must not keep spending an account-wide budget.
        await engine.ensure("SPY")
        engine.build_calls.clear()
        engine._views["SPY.US"].built_at -= 1000
        await engine._poll_once()
        assert engine.build_calls == []

    async def test_refreshes_a_watched_ticker(self, engine):
        await engine.ensure("SPY")
        engine.add_viewer("SPY.US")
        engine.build_calls.clear()
        engine._views["SPY.US"].built_at -= 1000
        await engine._poll_once()
        assert engine.build_calls == ["SPY.US"]

    async def test_skips_a_view_that_is_still_fresh(self, engine):
        await engine.ensure("SPY")
        engine.add_viewer("SPY.US")
        engine.build_calls.clear()
        await engine._poll_once()
        assert engine.build_calls == []

    async def test_stops_when_quota_is_exhausted(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("QQQ")
        for t in ("SPY.US", "QQQ.US"):
            engine.add_viewer(t)
            engine._views[t].built_at -= 1000
        engine.build_calls.clear()

        engine._governor.acquire(engine._governor.limit - ESTIMATED_REFRESH_COST + 1)
        await engine._poll_once()
        assert engine.build_calls == [], "refreshed with no budget left"

    async def test_refreshes_oldest_first(self, engine):
        await engine.ensure("SPY")
        await engine.ensure("QQQ")
        engine.add_viewer("SPY.US")
        engine.add_viewer("QQQ.US")
        engine._views["SPY.US"].built_at -= 500
        engine._views["QQQ.US"].built_at -= 1000
        engine.build_calls.clear()
        await engine._poll_once()
        assert engine.build_calls == ["QQQ.US", "SPY.US"]

    async def test_viewer_count_never_goes_negative(self, engine):
        await engine.ensure("SPY")
        engine.remove_viewer("SPY.US")
        engine.remove_viewer("SPY.US")
        assert engine._views["SPY.US"].viewers == 0

    async def test_a_failed_refresh_records_the_error_and_keeps_the_view(self, engine, monkeypatch):
        view = await engine.ensure("SPY")
        engine.add_viewer("SPY.US")

        async def boom(*a, **k):
            raise RuntimeError("301607 quota")

        monkeypatch.setattr(live_mod, "build_chain", boom)
        view.built_at -= 1000
        await engine._poll_once()
        assert "301607" in view.error
        # Stale greeks with a visible error beat an empty page.
        assert len(view.chain.all_candidates) == 3


class TestLinkHealth:
    async def test_no_reconcile_outside_the_session(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: False)
        await engine.ensure("SPY")
        engine.client.reconciles = 0
        await engine._check_link()
        assert engine.client.reconciles == 0
        assert engine.status().label == "market closed"

    async def test_reconciles_when_quiet_mid_session(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: True)
        await engine.ensure("SPY")
        engine.client.server.clear()  # the long link dropped
        engine.client.reconciles = 0
        await engine._check_link()
        assert engine.client.reconciles == 1
        assert engine.client.server == engine.subs.active_symbols()

    async def test_no_reconcile_while_ticks_are_arriving(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: True)
        await engine.ensure("SPY")
        engine.hub._on_quote("SPY.US", type("Q", (), {"last_done": 747.0, "timestamp": None})())
        engine.client.reconciles = 0
        await engine._check_link()
        assert engine.client.reconciles == 0

    async def test_status_reports_delayed_when_quiet_mid_session(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: True)
        await engine.ensure("SPY")
        assert engine.status().label == "delayed - reconnecting"

    async def test_status_reports_live_when_ticking(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: True)
        await engine.ensure("SPY")
        engine.hub._on_quote("SPY.US", type("Q", (), {"last_done": 747.0, "timestamp": None})())
        assert engine.status().label == "live"

    async def test_a_stalled_rebuild_outranks_market_closed(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: False)
        await engine.ensure("SPY")
        engine._governor.blocked_for = lambda: 12.4
        assert engine.status().label == "waiting for option quota (12s)"

    async def test_a_stalled_rebuild_outranks_live(self, engine, monkeypatch):
        monkeypatch.setattr(live_mod, "is_regular_session", lambda now=None: True)
        await engine.ensure("SPY")
        engine.hub._on_quote("SPY.US", type("Q", (), {"last_done": 747.0, "timestamp": None})())
        engine._governor.blocked_for = lambda: 3.0
        assert engine.status().label == "waiting for option quota (3s)"


class TestRelease:
    async def test_unsubscribes_and_forgets(self, engine):
        view = await engine.ensure("SPY")
        syms = list(view.chain.all_symbols)
        await engine.release("SPY")
        assert engine.active() == []
        dropped = {s for b in engine.client.unsubscribed for s in b}
        assert set(syms) <= dropped
        assert "SPY.US" in dropped
        assert engine.hub.snapshot(syms) == {}

    async def test_releasing_an_unknown_ticker_is_a_noop(self, engine):
        await engine.release("ZZZ")
        assert engine.client.unsubscribed == []


class TestIVWiring:
    async def test_records_todays_atm_iv_on_every_rebuild(self, engine):
        await engine.ensure("SPY")
        assert engine.iv.recorded == [("SPY.US", 0.20)]

    async def test_attaches_the_cached_rank_to_the_view(self, engine):
        engine.iv.rank = _rank(sufficient=True)
        view = await engine.ensure("SPY")
        assert view.iv_rank is engine.iv.rank

    async def test_a_missing_rank_triggers_a_background_fetch(self, engine):
        view = await engine.ensure("SPY")
        assert view.iv_rank is None
        # The fetch must not block the build; it lands on the next loop pass.
        await asyncio.sleep(0)
        await asyncio.gather(*engine._iv_tasks)
        assert engine.iv.fetched == ["SPY.US"]

    async def test_a_sufficient_rank_needs_no_fetch(self, engine):
        engine.iv.rank = _rank(sufficient=True)
        await engine.ensure("SPY")
        await asyncio.sleep(0)
        assert engine.iv.fetched == []

    async def test_a_thin_rank_still_triggers_a_fetch(self, engine):
        engine.iv.rank = _rank(sufficient=False)
        await engine.ensure("SPY")
        await asyncio.gather(*engine._iv_tasks)
        assert engine.iv.fetched == ["SPY.US"]

    async def test_only_one_fetch_per_ticker_is_in_flight(self, engine):
        await engine.ensure("SPY")
        engine._schedule_iv_fetch("SPY.US")
        engine._schedule_iv_fetch("SPY.US")
        await asyncio.gather(*engine._iv_tasks)
        assert engine.iv.fetched == ["SPY.US"]


class TestChartWiring:
    async def test_the_view_carries_a_chart_drawn_from_the_cached_series(self, engine):
        view = await engine.ensure("SPY")
        assert view.chart is not None
        assert view.chart.source == pricehistory.SOURCE_LONGPORT
        assert view.chart.n == 300
        assert [line.cls for line in view.chart.lines] == ["close", "high", "sma"]

    async def test_the_bars_in_hand_are_cached_for_the_chart(self, engine):
        await engine.ensure("SPY")
        assert len(engine.prices.recorded) == 300
        assert engine.prices.recorded[0] == ("SPY.US", "2025-01-01", 600.0)

    async def test_the_bars_dates_now_reach_the_drawdown_card(self, engine):
        # compute_trend has always accepted first_date; nothing supplied it, so the
        # card's "since" was silently blank on every render.
        view = await engine.ensure("SPY")
        assert view.trend.drawdown.high_since == "2025-01-01"

    async def test_a_longport_chart_triggers_a_background_yahoo_fetch(self, engine):
        view = await engine.ensure("SPY")
        assert view.chart.source == pricehistory.SOURCE_LONGPORT
        # Yahoo is a slow scrape, so it must not be awaited by the build.
        await asyncio.sleep(0)
        await asyncio.gather(*engine._price_tasks)
        assert engine.prices.fetched == ["SPY.US"]

    async def test_a_yahoo_chart_needs_no_fetch(self, engine):
        engine.prices.source = pricehistory.SOURCE_YAHOO
        view = await engine.ensure("SPY")
        assert view.chart.all_time
        await asyncio.sleep(0)
        assert engine.prices.fetched == []

    async def test_only_one_price_fetch_per_ticker_is_in_flight(self, engine):
        await engine.ensure("SPY")
        engine._schedule_price_fetch("SPY.US")
        engine._schedule_price_fetch("SPY.US")
        await asyncio.gather(*engine._price_tasks)
        assert engine.prices.fetched == ["SPY.US"]

    async def test_an_unavailable_store_costs_the_page_nothing(self, engine):
        def boom(ticker, bars):
            raise RuntimeError("database is locked")

        engine.prices.record_longport = boom
        view = await engine.ensure("SPY")
        assert view.chart is None
        assert view.error is None


class TestThreeClocks:
    """One tenor is re-priced on greeks_poll_seconds; every comparison tenor runs on
    slow_tenor_poll_seconds. calc_indexes bills on every call, so a wide window is only
    affordable if the tenors that are not due are reused rather than re-bought."""

    async def test_a_first_build_prices_the_whole_window(self, engine, monkeypatch):
        _install_multi(engine, monkeypatch, (20, 41, 60))
        view = await engine.ensure("SPY")
        assert engine.built_only == [None], "a first build must price every expiry"
        assert len(view.chain.slices) == 3

    async def test_exactly_one_tenor_owns_the_fast_clock(self, engine, monkeypatch):
        _install_multi(engine, monkeypatch, (20, 41, 60))
        view = await engine.ensure("SPY")
        fast = [sl for sl in view.chain.slices if sl.clock == FAST]
        assert len(fast) == 1
        assert view.fast_expiry == fast[0].expiry
        assert all(
            sl.clock == SLOW for sl in view.chain.slices if sl.expiry != view.fast_expiry
        )

    async def test_only_the_fast_tenor_is_due_while_the_rest_are_young(
        self, engine, monkeypatch
    ):
        _install_multi(engine, monkeypatch, (20, 41, 60))
        view = await engine.ensure("SPY")
        assert engine._due_expiries(view) == [view.fast_expiry]

    async def test_a_slow_tenor_falls_due_on_its_own_timer(self, engine, monkeypatch):
        _install_multi(engine, monkeypatch, (20, 41, 60))
        view = await engine.ensure("SPY")
        stale = next(sl for sl in view.chain.slices if sl.clock == SLOW)
        stale.built_at -= settings.slow_tenor_poll_seconds + 1
        assert set(engine._due_expiries(view)) == {view.fast_expiry, stale.expiry}

    async def test_an_empty_chain_reprices_the_whole_window(self, engine, monkeypatch):
        # None means "everything": there is no previous slice to reuse.
        _install_multi(engine, monkeypatch, ())
        view = await engine.ensure("SPY")
        assert engine._due_expiries(view) is None

    async def test_the_poller_reprices_only_the_due_tenors(self, engine, monkeypatch):
        _install_multi(engine, monkeypatch, (20, 41, 60))
        view = await engine.ensure("SPY")
        fast = view.fast_expiry
        engine.add_viewer("SPY.US")
        engine.built_only.clear()
        view.built_at -= 1000
        await engine._poll_once()
        assert engine.built_only == [[fast]]

    async def test_the_fast_clock_needs_two_wins_to_re_point(self, engine, monkeypatch):
        # Re-pointing drops one tenor's subscriptions and buys another's, so a tie
        # oscillating on a half-cent mid must not churn the set every minute.
        _install_multi(engine, monkeypatch, (20, 40))
        view = await engine.ensure("SPY")
        short, longer = (sl.expiry for sl in view.chain.slices)
        assert view.fast_expiry == short

        for c in view.chain.slices[1].candidates:
            _push_book(engine, c.symbol, 3.00, 3.02)

        await engine._refresh(view, underlying=False)
        assert view.fast_expiry == short, "re-pointed on a single win"
        assert (view.challenger, view.challenger_wins) == (longer, 1)

        await engine._refresh(view, underlying=False)
        assert view.fast_expiry == longer
        assert (view.challenger, view.challenger_wins) == (None, 0)

    async def test_the_ranked_set_stays_inside_the_concurrent_ceiling(
        self, engine, monkeypatch
    ):
        # 13 tenors x 24 strikes is 312 symbols for one ticker, and SubscriptionManager
        # evicts BY TICKER, so two tickers at that width would erase each other on every
        # render. The funnel makes the pressure flat in the width of the window.
        strikes = [SPOT - 40.0 - i for i in range(24)]
        _install_multi(engine, monkeypatch, tuple(range(7, 59, 4)), strikes)
        view = await engine.ensure("SPY")

        assert len(view.chain.all_candidates) == 13 * 24
        assert len(view.quoted_symbols) == settings.max_quoted_contracts
        subscribed = {s for batch in engine.client.subscribed for s in batch}
        assert subscribed == {"SPY.US", *view.quoted_symbols}

    async def test_every_tenor_keeps_a_quoted_comparable_contract(
        self, engine, monkeypatch
    ):
        # The term-structure comparison rests on these rows; a thin tenor ranked out of
        # its own row would leave the whole table on model prices.
        strikes = [SPOT - 40.0 - i for i in range(24)]
        _install_multi(engine, monkeypatch, tuple(range(7, 59, 4)), strikes)
        view = await engine.ensure("SPY")
        target = view.params.delta_target
        for sl in view.chain.slices:
            nearest = min(sl.candidates, key=lambda c: abs(c.delta - target))
            assert nearest.symbol in view.quoted_symbols, sl.expiry

    async def test_a_contract_ranked_out_of_the_quoted_set_loses_its_quote(
        self, engine, monkeypatch
    ):
        # The hub remembers the last book of a symbol it no longer subscribes. Folding
        # that in would print a real-looking mid, an annualized % and a liquid flag on a
        # row that can never tick again — and it would outrank live rows in a table
        # sorted by that very number. The widened window must re-price the SAME symbols,
        # which is what happens in production when a DTE window only grows.
        strikes = [SPOT - 40.0 - i for i in range(24)]
        _install_multi(engine, monkeypatch, (43, 47, 51), strikes)
        view = await engine.ensure("SPY")
        for c in view.chain.all_candidates:
            _push_book(engine, c.symbol, 3.00, 3.02)
        before = engine.quoted("SPY.US").all_candidates
        assert any(c.spread.mid is not None for c in before), "the books did not land"
        seeded = {c.symbol for c in before}

        _install_multi(engine, monkeypatch, tuple(range(7, 59, 4)), strikes)
        view = await engine.ensure("SPY", replace(view.params, dte_min=7, dte_max=60))

        held = set(view.quoted_symbols)
        dropped = [
            c
            for c in engine.quoted("SPY.US").all_candidates
            if c.symbol in seeded and c.symbol not in held
        ]
        assert dropped, "the rebuild must have ranked a previously quoted symbol out"
        for c in dropped:
            assert c.spread.mid is None, c.symbol
            assert c.spread.liquid is False
            assert c.premium.annualized_pct is None


class TestPerViewRefreshCost:
    async def test_a_wide_window_estimates_from_its_own_last_rebuild(
        self, engine, monkeypatch
    ):
        # A fixed constant would under-read a 13-tenor window by an order of magnitude
        # and let the poller overdraw the account-wide budget.
        _install_multi(engine, monkeypatch, (20, 41, 60), quota=364)
        view = await engine.ensure("SPY")
        assert view.refresh_cost == 364

    async def test_a_cheap_view_keeps_the_floor(self, engine):
        view = await engine.ensure("SPY")
        assert view.refresh_cost == ESTIMATED_REFRESH_COST

    async def test_the_poller_will_not_start_a_rebuild_it_cannot_pay_for(
        self, engine, monkeypatch
    ):
        _install_multi(engine, monkeypatch, (20, 41, 60), quota=364)
        view = await engine.ensure("SPY")
        engine.add_viewer("SPY.US")
        view.built_at -= 1000
        engine.build_calls.clear()
        # Enough for the 40-unit floor, nowhere near what this view actually costs.
        engine._governor.acquire(engine._governor.limit - 100)
        await engine._poll_once()
        assert engine.build_calls == []


def _rank(sufficient: bool) -> IVRank:
    n = settings.min_iv_history_days if sufficient else 3
    return IVRank(
        ticker="SPY",
        source="local",
        rank=42.0,
        percentile=40.0,
        n=n,
        low=0.10,
        high=0.30,
        observed=0.20,
        as_of="2026-07-31",
    )
