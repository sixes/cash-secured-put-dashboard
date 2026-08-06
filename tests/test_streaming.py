from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from app.providers.quotehub import QuoteHub, STALE_AFTER_SECONDS
from app.providers.subscriptions import SubscriptionManager


@dataclass
class FakeLevel:
    position: int
    price: float
    volume: float


class FakeClient:
    """Stands in for LongportClient. Records every subscribe/unsubscribe call."""

    def __init__(self) -> None:
        self.subscribed: list[list[str]] = []
        self.unsubscribed: list[list[str]] = []
        self.server: set[str] = set()
        self.on_quote = None
        self.on_depth = None
        self.depth_calls: list[str] = []
        self.quote_calls: list[list[str]] = []
        self.books: dict[str, object] = {}
        self.spots: dict[str, object] = {}
        self.cached: dict[str, object] = {}
        self.fail_unsubscribe = False

    def register_callbacks(self, on_quote=None, on_depth=None):
        self.on_quote, self.on_depth = on_quote, on_depth

    def subscribe(self, symbols):
        self.subscribed.append(list(symbols))
        self.server.update(symbols)

    def unsubscribe(self, symbols):
        if self.fail_unsubscribe:
            raise RuntimeError("network down")
        self.unsubscribed.append(list(symbols))
        self.server.difference_update(symbols)

    def subscriptions(self):
        return {s: ["Quote", "Depth"] for s in self.server}

    def depth(self, symbol):
        self.depth_calls.append(symbol)
        if symbol not in self.books:
            raise RuntimeError("no book")
        return self.books[symbol]

    def realtime_depth(self, symbol):
        if symbol not in self.cached:
            raise RuntimeError("not cached")
        return self.cached[symbol]

    def quote(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: self.spots[s] for s in symbols if s in self.spots}


def opt(strike: int) -> str:
    return f"SPY260918P{strike}000.US"


def chain(n: int, base: int = 700) -> list[str]:
    return [opt(base - i) for i in range(n)]


class TestBudget:
    def test_activate_subscribes_once(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        syms = ["SPY.US"] + chain(5)
        m.activate("SPY.US", syms)
        assert sorted(s for b in c.subscribed for s in b) == sorted(syms)

    def test_reactivating_an_unchanged_set_spends_nothing(self):
        # subscribe() is billed per option symbol, so a page refresh must not re-bill.
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        syms = ["SPY.US"] + chain(5)
        m.activate("SPY.US", syms)
        c.subscribed.clear()
        assert m.activate("SPY.US", syms) == []
        assert c.subscribed == []

    def test_changed_set_subscribes_only_the_delta(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US", opt(700), opt(699)])
        c.subscribed.clear()
        c.unsubscribed.clear()
        m.activate("SPY.US", ["SPY.US", opt(699), opt(698)])
        assert [s for b in c.subscribed for s in b] == [opt(698)]
        assert [s for b in c.unsubscribed for s in b] == [opt(700)]

    def test_never_exceeds_the_symbol_ceiling(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=100, max_tickers=99)
        for i in range(40):
            m.activate(f"T{i}.US", [f"T{i}.US"] + chain(20, base=1000 + i * 100))
            assert m.state().symbols <= 100, f"budget blown at ticker {i}"

    def test_evicts_the_least_recently_viewed_ticker(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=1000, max_tickers=3)
        for name in ("A", "B", "C"):
            m.activate(f"{name}.US", [f"{name}.US"])
        m.activate("A.US", ["A.US"])  # A is now the most recent, B the oldest
        evicted = m.activate("D.US", ["D.US"])
        assert evicted == ["B.US"]
        assert m.state().tickers == ["C.US", "A.US", "D.US"]

    def test_eviction_removes_the_whole_symbol_set(self):
        # A half-evicted ticker would render some cells live and some frozen with
        # nothing to tell them apart.
        c = FakeClient()
        m = SubscriptionManager(c, limit=1000, max_tickers=1)
        first = ["A.US"] + chain(6, base=800)
        m.activate("A.US", first)
        c.unsubscribed.clear()
        m.activate("B.US", ["B.US"] + chain(6, base=900))
        assert sorted(s for b in c.unsubscribed for s in b) == sorted(first)

    def test_shared_symbols_are_not_unsubscribed_on_eviction(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=1000, max_tickers=2)
        shared = opt(650)
        m.activate("A.US", ["A.US", shared])
        m.activate("B.US", ["B.US", shared])
        c.unsubscribed.clear()
        m.activate("C.US", ["C.US"])  # evicts A
        dropped = [s for b in c.unsubscribed for s in b]
        assert "A.US" in dropped
        assert shared not in dropped, "still needed by B"

    def test_ticker_larger_than_the_budget_is_refused(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=10)
        with pytest.raises(ValueError, match="over the 10 limit"):
            m.activate("BIG.US", chain(11))
        # And it must not be left half-registered.
        assert m.state().tickers == []

    def test_release_unsubscribes(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US", opt(700)])
        assert m.release("SPY.US") is True
        assert sorted(s for b in c.unsubscribed for s in b) == ["SPY.US", opt(700)]
        assert m.release("SPY.US") is False

    def test_duplicate_symbols_are_collapsed(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US", opt(700), opt(700), "SPY.US"])
        assert m.state().symbols == 2

    def test_failed_unsubscribe_does_not_corrupt_the_book(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US", opt(700)])
        c.fail_unsubscribe = True
        assert m.release("SPY.US") is True
        assert m.state().tickers == []


class TestReconcile:
    def test_resubscribes_after_the_server_forgets(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        syms = ["SPY.US"] + chain(4)
        m.activate("SPY.US", syms)
        c.server.clear()  # the long link dropped
        c.subscribed.clear()

        missing, extra = m.reconcile()
        assert sorted(missing) == sorted(syms)
        assert extra == []
        assert c.server == set(syms)

    def test_server_set_matches_our_book_afterwards(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US"] + chain(3))
        m.activate("QQQ.US", ["QQQ.US"] + chain(3, base=600))
        c.server.clear()
        m.reconcile()
        assert set(c.subscriptions()) == m.active_symbols()

    def test_drops_strays_the_server_still_holds(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US"])
        c.server.add(opt(123))  # a leaked unsubscribe from an earlier session
        missing, extra = m.reconcile()
        assert missing == []
        assert extra == [opt(123)]
        assert opt(123) not in c.server

    def test_noop_when_already_in_sync(self):
        c = FakeClient()
        m = SubscriptionManager(c, limit=500)
        m.activate("SPY.US", ["SPY.US"] + chain(3))
        c.subscribed.clear()
        assert m.reconcile() == ([], [])
        assert c.subscribed == []


@dataclass
class FakeQuoteEvent:
    last_done: float | None = None
    timestamp: object = None


@dataclass
class FakeDepthEvent:
    bids: list = None
    asks: list = None


@dataclass
class FakeBook:
    symbol: str
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None


@dataclass
class FakeSpot:
    symbol: str
    last: float | None
    prev_close: float | None = None
    ts: object = None


class TestQuoteHub:
    def _hub(self) -> tuple[QuoteHub, FakeClient]:
        c = FakeClient()
        return QuoteHub(c), c

    def test_depth_push_populates_the_book(self):
        hub, _ = self._hub()
        hub._on_depth(
            opt(700),
            FakeDepthEvent(bids=[FakeLevel(0, 5.40, 12)], asks=[FakeLevel(0, 5.45, 8)]),
        )
        st = hub.get(opt(700))
        assert (st.bid, st.ask, st.bid_size, st.ask_size) == (5.40, 5.45, 12.0, 8.0)
        assert st.mid == pytest.approx(5.425)

    def test_takes_the_top_of_book_regardless_of_order(self):
        hub, _ = self._hub()
        hub._on_depth(
            opt(700),
            FakeDepthEvent(
                bids=[FakeLevel(2, 5.20, 1), FakeLevel(0, 5.40, 9)],
                asks=[FakeLevel(1, 5.50, 3), FakeLevel(0, 5.45, 4)],
            ),
        )
        st = hub.get(opt(700))
        assert (st.bid, st.ask) == (5.40, 5.45)

    def test_quote_push_does_not_blank_the_book(self):
        # depth and quote pushes each carry only half the state.
        hub, _ = self._hub()
        hub._on_depth(
            opt(700),
            FakeDepthEvent(bids=[FakeLevel(0, 5.40, 1)], asks=[FakeLevel(0, 5.45, 1)]),
        )
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=5.42))
        st = hub.get(opt(700))
        assert (st.bid, st.ask, st.last) == (5.40, 5.45, 5.42)

    def test_depth_push_does_not_blank_last_or_prev_close(self):
        hub, c = self._hub()
        c.spots["SPY.US"] = FakeSpot("SPY.US", last=747.03, prev_close=745.0)
        hub.seed(["SPY.US"])
        hub._on_depth(
            "SPY.US",
            FakeDepthEvent(bids=[FakeLevel(0, 747.0, 1)], asks=[FakeLevel(0, 747.1, 1)]),
        )
        st = hub.get("SPY.US")
        assert st.prev_close == 745.0
        assert st.last == 747.03

    def test_crossed_book_has_no_mid(self):
        hub, _ = self._hub()
        hub._on_depth(
            opt(700),
            FakeDepthEvent(bids=[FakeLevel(0, 5.50, 1)], asks=[FakeLevel(0, 5.40, 1)]),
        )
        assert hub.get(opt(700)).mid is None

    def test_one_sided_book_has_no_mid(self):
        hub, _ = self._hub()
        hub._on_depth(opt(700), FakeDepthEvent(bids=[FakeLevel(0, 5.50, 1)], asks=[]))
        st = hub.get(opt(700))
        assert st.bid == 5.50 and st.ask is None and st.mid is None

    def test_version_advances_on_every_mutation(self):
        hub, _ = self._hub()
        v0 = hub.version()
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=1.0))
        v1 = hub.version()
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=1.1))
        assert v0 < v1 < hub.version()

    def test_books_adapter_shape(self):
        hub, _ = self._hub()
        hub._on_depth(
            opt(700),
            FakeDepthEvent(bids=[FakeLevel(0, 1.0, 2)], asks=[FakeLevel(0, 1.1, 3)]),
        )
        books = hub.books([opt(700), opt(699)])
        assert set(books) == {opt(700)}
        assert (books[opt(700)].bid, books[opt(700)].ask) == (1.0, 1.1)

    def test_unknown_symbols_are_absent_not_blank(self):
        hub, _ = self._hub()
        assert hub.get(opt(700)) is None
        assert hub.snapshot([opt(700)]) == {}

    def test_stale_before_any_push(self):
        hub, _ = self._hub()
        assert hub.seconds_since_push() is None
        assert hub.is_stale() is True

    def test_fresh_right_after_a_push(self):
        hub, _ = self._hub()
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=1.0))
        assert hub.seconds_since_push() < STALE_AFTER_SECONDS
        assert hub.is_stale() is False

    def test_seeding_does_not_count_as_link_traffic(self):
        # Otherwise a REST seed would mask a dead websocket.
        hub, c = self._hub()
        c.books[opt(700)] = FakeBook(opt(700), 5.4, 5.45)
        hub.seed([opt(700)])
        assert hub.get(opt(700)).bid == 5.4
        assert hub.is_stale() is True

    def test_drop_forgets_symbols(self):
        hub, _ = self._hub()
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=1.0))
        hub.drop([opt(700)])
        assert hub.get(opt(700)) is None


class TestSeeding:
    def test_seeds_underlying_from_quote(self):
        hub = QuoteHub(c := FakeClient())
        c.spots["SPY.US"] = FakeSpot("SPY.US", last=747.03, prev_close=745.1)
        spent = hub.seed(["SPY.US"])
        st = hub.get("SPY.US")
        assert (st.last, st.prev_close) == (747.03, 745.1)
        assert spent == 0, "underlying quotes cost no option quota"

    def test_option_seed_costs_one_unit_each(self):
        hub = QuoteHub(c := FakeClient())
        for k in (700, 699):
            c.books[opt(k)] = FakeBook(opt(k), 5.0, 5.1)
        assert hub.seed([opt(700), opt(699)]) == 2

    def test_prefers_the_free_push_cache_over_rest(self):
        hub = QuoteHub(c := FakeClient())
        c.cached[opt(700)] = FakeBook(opt(700), 5.4, 5.45)
        c.books[opt(700)] = FakeBook(opt(700), 9.9, 9.9)
        assert hub.seed([opt(700)]) == 0
        assert c.depth_calls == []
        assert hub.get(opt(700)).bid == 5.4

    def test_skips_symbols_already_pushed(self):
        hub = QuoteHub(c := FakeClient())
        hub._on_depth(
            opt(700),
            FakeDepthEvent(bids=[FakeLevel(0, 5.4, 1)], asks=[FakeLevel(0, 5.45, 1)]),
        )
        c.books[opt(700)] = FakeBook(opt(700), 9.9, 9.9)
        assert hub.seed([opt(700)]) == 0
        assert c.depth_calls == []

    def test_a_failed_depth_costs_nothing_and_raises_nothing(self):
        hub = QuoteHub(FakeClient())
        assert hub.seed([opt(700)]) == 0
        assert hub.get(opt(700)) is None

    def test_empty_cache_falls_through_to_rest(self):
        hub = QuoteHub(c := FakeClient())
        c.cached[opt(700)] = FakeBook(opt(700), None, None)
        c.books[opt(700)] = FakeBook(opt(700), 5.4, 5.45)
        assert hub.seed([opt(700)]) == 1
        assert hub.get(opt(700)).bid == 5.4


class TestListener:
    @pytest.mark.asyncio
    async def test_push_from_sdk_thread_wakes_the_listener(self):
        hub = QuoteHub(FakeClient())
        hub.attach(asyncio.get_running_loop())
        with hub.listener() as lis:
            threading.Thread(
                target=hub._on_quote, args=(opt(700), FakeQuoteEvent(last_done=1.0))
            ).start()
            assert await lis.wait(timeout=2.0) is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false_as_a_heartbeat(self):
        hub = QuoteHub(FakeClient())
        hub.attach(asyncio.get_running_loop())
        with hub.listener() as lis:
            assert await lis.wait(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_bursts_coalesce_into_one_wakeup(self):
        hub = QuoteHub(FakeClient())
        hub.attach(asyncio.get_running_loop())
        with hub.listener() as lis:
            for i in range(50):
                hub._on_quote(opt(700), FakeQuoteEvent(last_done=float(i)))
            assert await lis.wait(timeout=1.0) is True
            # 50 pushes, one pending wakeup: the next wait must time out.
            assert await lis.wait(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_listener_is_removed_on_exit(self):
        hub = QuoteHub(FakeClient())
        hub.attach(asyncio.get_running_loop())
        with hub.listener():
            pass
        assert hub._listeners == set()

    @pytest.mark.asyncio
    async def test_push_before_attach_does_not_raise(self):
        hub = QuoteHub(FakeClient())
        hub._on_quote(opt(700), FakeQuoteEvent(last_done=1.0))
        assert hub.get(opt(700)).last == 1.0
