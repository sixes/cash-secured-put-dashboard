"""Price history provider. Offline: the Yahoo scrape is stubbed at the module attribute.

`fetch_yahoo` imports yfinance lazily, so nothing here loads it. That is deliberate —
the import costs about a second and the suite must stay hermetic.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.providers import pricehistory
from app.providers.pricehistory import (
    SOURCE_LONGPORT,
    SOURCE_YAHOO,
    PriceSourceError,
    record_longport,
    refresh_ticker,
    series,
    yahoo_symbol,
)
from app.store import Store

TODAY = date(2026, 8, 1)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def rows(n: int, start: float = 100.0) -> list[tuple[str, float]]:
    return [(f"2025-01-{i + 1:02d}", start + i) for i in range(n)]


class TestSymbolTranslation:
    def test_market_suffix_is_dropped(self):
        assert yahoo_symbol("SPY.US") == "SPY"

    def test_a_dotted_class_becomes_a_dash(self):
        # Yahoo spells Berkshire's B shares BRK-B, not BRK.B.
        assert yahoo_symbol("BRK.B.US") == "BRK-B"

    def test_a_bare_symbol_passes_through(self):
        assert yahoo_symbol("AMD") == "AMD"


class TestRefresh:
    def test_a_successful_fetch_is_cached_and_marked(self, store, monkeypatch):
        monkeypatch.setattr(pricehistory, "fetch_yahoo", lambda t: rows(5))
        assert refresh_ticker("SPY.US", store, TODAY) == SOURCE_YAHOO
        assert store.price_series("SPY", SOURCE_YAHOO) == rows(5)
        assert store.fetched_today("price:yahoo:SPY", TODAY)

    def test_the_second_call_the_same_day_does_not_refetch(self, store, monkeypatch):
        calls: list[str] = []

        def once(ticker):
            calls.append(ticker)
            return rows(5)

        monkeypatch.setattr(pricehistory, "fetch_yahoo", once)
        refresh_ticker("SPY.US", store, TODAY)
        refresh_ticker("SPY.US", store, TODAY)
        assert calls == ["SPY.US"]

    def test_a_failure_is_logged_not_raised(self, store, monkeypatch):
        def boom(ticker):
            raise PriceSourceError("yahoo SPY: layout changed")

        monkeypatch.setattr(pricehistory, "fetch_yahoo", boom)
        assert refresh_ticker("SPY.US", store, TODAY) is None
        assert store.price_series("SPY", SOURCE_YAHOO) == []
        # Marked as attempted-and-failed, so the detail is visible without pretending
        # the day's fetch succeeded.
        assert not store.fetched_today("price:yahoo:SPY", TODAY)
        assert "layout changed" in store.fetch_detail("price:yahoo:SPY", TODAY)

    def test_a_failure_does_not_block_the_next_days_attempt(self, store, monkeypatch):
        monkeypatch.setattr(
            pricehistory, "fetch_yahoo", lambda t: (_ for _ in ()).throw(PriceSourceError("down"))
        )
        refresh_ticker("SPY.US", store, TODAY)
        monkeypatch.setattr(pricehistory, "fetch_yahoo", lambda t: rows(3))
        assert refresh_ticker("SPY.US", store, date(2026, 8, 2)) == SOURCE_YAHOO


class TestRecordLongport:
    def test_the_bars_in_hand_are_cached_free(self, store):
        assert record_longport("SPY.US", rows(4), store) == 4
        assert store.price_series("SPY", SOURCE_LONGPORT) == rows(4)

    def test_nonpositive_closes_are_dropped(self, store):
        record_longport("SPY.US", [("2025-01-01", 0.0), ("2025-01-02", 100.0)], store)
        assert store.price_series("SPY", SOURCE_LONGPORT) == [("2025-01-02", 100.0)]

    def test_recording_twice_updates_rather_than_duplicates(self, store):
        record_longport("SPY.US", [("2025-01-01", 100.0)], store)
        record_longport("SPY.US", [("2025-01-01", 101.0)], store)
        assert store.price_series("SPY", SOURCE_LONGPORT) == [("2025-01-01", 101.0)]


class TestSeries:
    def test_none_when_nothing_is_cached(self, store):
        assert series("SPY.US", store) is None

    def test_a_single_close_is_not_a_series(self, store):
        record_longport("SPY.US", [("2025-01-01", 100.0)], store)
        assert series("SPY.US", store) is None

    def test_yahoo_wins_over_longport(self, store, monkeypatch):
        record_longport("SPY.US", rows(4), store)
        monkeypatch.setattr(pricehistory, "fetch_yahoo", lambda t: rows(9, start=50.0))
        refresh_ticker("SPY.US", store, TODAY)
        s = series("SPY.US", store)
        assert s.source == SOURCE_YAHOO
        assert len(s.closes) == 9

    def test_longport_is_the_fallback_and_is_labelled_as_a_window(self, store):
        record_longport("SPY.US", rows(4), store)
        s = series("SPY.US", store)
        assert s.source == SOURCE_LONGPORT
        assert s.label == "Longbridge fwd-adj"
        # Truncated by an API limit, so its maximum is a window high, not an ATH.
        assert not s.all_time

    def test_yahoo_reaches_inception_so_its_high_is_an_ath(self, store, monkeypatch):
        monkeypatch.setattr(pricehistory, "fetch_yahoo", lambda t: rows(4))
        refresh_ticker("SPY.US", store, TODAY)
        s = series("SPY.US", store)
        assert s.all_time
        assert s.label == "Yahoo adj close"

    def test_the_series_is_oldest_first(self, store):
        record_longport("SPY.US", rows(6), store)
        s = series("SPY.US", store)
        assert list(s.dates) == sorted(s.dates)
        assert s.closes[0] < s.closes[-1]

    def test_sources_are_not_mixed(self, store, monkeypatch):
        # An auto-adjusted close and a forward-adjusted one are different series; a
        # chart drawn from both would show a step on the changeover date.
        record_longport("SPY.US", rows(4, start=900.0), store)
        monkeypatch.setattr(pricehistory, "fetch_yahoo", lambda t: rows(4, start=100.0))
        refresh_ticker("SPY.US", store, TODAY)
        s = series("SPY.US", store)
        assert max(s.closes) < 200.0
