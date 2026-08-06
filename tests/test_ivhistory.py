"""IV history and IV Rank. Offline: every network call is stubbed."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import settings
from app.providers import ivhistory
from app.providers.ivhistory import (
    IVRank,
    IVSourceError,
    NO_COVERAGE,
    SOURCE_DOLT,
    SOURCE_LOCAL,
    iv_rank,
    percentile_within,
    rank_within,
    record_local,
    refresh_ticker,
)
from app.store import Store

CBOE_CSV = """DATE,OPEN,HIGH,LOW,CLOSE
01/02/1990,17.240000,17.240000,17.240000,17.240000
07/29/2026,16.000000,17.000000,15.000000,12.000000
07/30/2026,16.820000,18.700000,15.820000,20.000000
07/31/2026,16.000000,17.000000,15.000000,16.000000
"""


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def series(store, symbol, source, values, start=date(2025, 1, 1)):
    store.upsert_iv_history(
        [(symbol, (start + timedelta(days=i)).isoformat(), v, source) for i, v in enumerate(values)]
    )


class TestRankMath:
    def test_midpoint_is_fifty(self):
        assert rank_within(0.20, 0.10, 0.30) == pytest.approx(50.0)

    def test_at_the_low_is_zero(self):
        assert rank_within(0.10, 0.10, 0.30) == 0.0

    def test_at_the_high_is_one_hundred(self):
        assert rank_within(0.30, 0.10, 0.30) == 100.0

    def test_a_fresh_spike_clamps_rather_than_exceeding_one_hundred(self):
        # A new high legitimately prints outside its own trailing range; 103 would
        # read as a bug rather than as "at the top".
        assert rank_within(0.40, 0.10, 0.30) == 100.0
        assert rank_within(0.05, 0.10, 0.30) == 0.0

    def test_a_flat_series_has_no_range_so_no_rank(self):
        assert rank_within(0.20, 0.20, 0.20) is None

    def test_percentile_counts_observations_at_or_below(self):
        assert percentile_within(0.20, [0.1, 0.2, 0.3, 0.4]) == pytest.approx(50.0)
        assert percentile_within(0.5, [0.1, 0.2, 0.3, 0.4]) == pytest.approx(100.0)

    def test_percentile_of_nothing_is_none(self):
        assert percentile_within(0.2, []) is None

    def test_rank_is_invariant_under_a_positive_affine_map(self):
        # This is exactly why a VIX rank transfers to SPY even though the levels differ.
        raw = [0.10, 0.14, 0.22, 0.30]
        scaled = [1.21 * v + 0.03 for v in raw]
        assert rank_within(raw[2], min(raw), max(raw)) == pytest.approx(
            rank_within(scaled[2], min(scaled), max(scaled))
        )


class TestIndexFetch:
    def test_cboe_closes_are_converted_from_vol_points_to_ratios(self, monkeypatch):
        monkeypatch.setattr(ivhistory.httpx, "get", lambda *a, **k: _resp(CBOE_CSV))
        rows = ivhistory.fetch_index_series("VIX")
        assert rows[0] == ("1990-01-02", pytest.approx(0.1724))
        assert rows[-1] == ("2026-07-31", pytest.approx(0.16))

    def test_falls_back_to_fred_when_cboe_is_empty(self, monkeypatch):
        fred = "observation_date,VIXCLS\n2026-07-30,.\n2026-07-31,16.00\n"
        calls: list[str] = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _resp("" if "cboe" in url else fred)

        monkeypatch.setattr(ivhistory.httpx, "get", fake_get)
        rows = ivhistory.fetch_index_series("VIX")
        assert len(calls) == 2 and "VIXCLS" in calls[1]
        # FRED's "." placeholder rows are dropped, not read as zero.
        assert rows == [("2026-07-31", pytest.approx(0.16))]


class TestDolthub:
    def test_parses_the_latest_row(self, monkeypatch):
        monkeypatch.setattr(
            ivhistory.httpx,
            "get",
            lambda *a, **k: _resp_json(
                {
                    "query_execution_status": "Success",
                    "rows": [
                        {
                            "date": "2026-07-30",
                            "iv_current": "0.2977",
                            "iv_year_high": "0.3290",
                            "iv_year_low": "0.1748",
                        }
                    ],
                }
            ),
        )
        row = ivhistory.fetch_dolthub("AAPL")
        assert row == {
            "date": "2026-07-30",
            "atm_iv": pytest.approx(0.2977),
            "high": pytest.approx(0.3290),
            "low": pytest.approx(0.1748),
        }

    def test_an_uncovered_symbol_returns_none_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            ivhistory.httpx,
            "get",
            lambda *a, **k: _resp_json({"query_execution_status": "Success", "rows": []}),
        )
        assert ivhistory.fetch_dolthub("QQQ") is None

    def test_a_query_failure_raises_so_it_can_be_retried(self, monkeypatch):
        monkeypatch.setattr(
            ivhistory.httpx,
            "get",
            lambda *a, **k: _resp_json(
                {
                    "query_execution_status": "Error",
                    "query_execution_message": "context deadline exceeded",
                }
            ),
        )
        with pytest.raises(IVSourceError, match="deadline"):
            ivhistory.fetch_dolthub("AAPL")

    def test_the_symbol_is_validated_before_it_reaches_sql(self, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("must not issue a request")

        monkeypatch.setattr(ivhistory.httpx, "get", explode)
        with pytest.raises(IVSourceError, match="implausible"):
            ivhistory.fetch_dolthub("A' OR 1=1--")


class TestRefresh:
    def test_a_proxy_ticker_stores_the_index_series_under_its_own_symbol(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(ivhistory.httpx, "get", lambda *a, **k: _resp(CBOE_CSV))
        assert refreshed(store, "SPY.US") == "cboe:VIX"
        # Stored as VIX, not SPY: the rows are the index's, and labelling them SPY
        # would make them look like SPY's own ATM IV.
        assert store.iv_series("VIX", "cboe:VIX") != []
        assert store.iv_series("SPY", "cboe:VIX") == []

    def test_the_index_is_fetched_once_a_day(self, store, monkeypatch):
        calls: list[int] = []

        def fake_get(*a, **k):
            calls.append(1)
            return _resp(CBOE_CSV)

        monkeypatch.setattr(ivhistory.httpx, "get", fake_get)
        refreshed(store, "SPY.US")
        refreshed(store, "SPY.US")
        assert len(calls) == 1

    def test_a_missing_dolthub_symbol_is_not_retried_the_same_day(self, store, monkeypatch):
        calls: list[int] = []

        def fake_get(*a, **k):
            calls.append(1)
            return _resp_json({"query_execution_status": "Success", "rows": []})

        monkeypatch.setattr(ivhistory.httpx, "get", fake_get)
        assert refreshed(store, "GLD.US") is None
        assert refreshed(store, "GLD.US") is None
        assert len(calls) == 1
        assert store.fetch_detail("iv:dolthub:GLD", TODAY) == NO_COVERAGE

    def test_a_transient_dolthub_failure_is_retried(self, store, monkeypatch):
        calls: list[int] = []

        def fake_get(*a, **k):
            calls.append(1)
            return _resp_json(
                {"query_execution_status": "Error", "query_execution_message": "timeout"}
            )

        monkeypatch.setattr(ivhistory.httpx, "get", fake_get)
        assert refreshed(store, "AAPL.US") is None
        assert refreshed(store, "AAPL.US") is None
        assert len(calls) == 2

    def test_a_dolthub_hit_writes_both_bounds_and_a_history_row(self, store, monkeypatch):
        monkeypatch.setattr(
            ivhistory.httpx,
            "get",
            lambda *a, **k: _resp_json(
                {
                    "query_execution_status": "Success",
                    "rows": [
                        {
                            "date": "2026-07-30",
                            "iv_current": "0.2977",
                            "iv_year_high": "0.3290",
                            "iv_year_low": "0.1748",
                        }
                    ],
                }
            ),
        )
        assert refreshed(store, "AAPL.US") == SOURCE_DOLT
        assert store.latest_iv_bounds("AAPL", SOURCE_DOLT)["iv_year_high"] == pytest.approx(0.3290)
        assert store.iv_series("AAPL", SOURCE_DOLT) == [("2026-07-30", pytest.approx(0.2977))]


class TestRecordLocal:
    def test_records_once_per_day(self, store):
        assert record_local("AAPL.US", 0.30, on=TODAY, store=store) is True
        assert record_local("AAPL.US", 0.31, on=TODAY, store=store) is False
        assert store.iv_series("AAPL", SOURCE_LOCAL) == [(TODAY.isoformat(), pytest.approx(0.30))]

    def test_a_missing_iv_is_not_recorded_as_zero(self, store):
        assert record_local("AAPL.US", 0.0, on=TODAY, store=store) is False
        assert store.iv_series("AAPL", SOURCE_LOCAL) == []

    def test_stores_under_the_display_ticker(self, store):
        record_local("BRK.B.US", 0.16, on=TODAY, store=store)
        assert store.iv_series("BRK.B", SOURCE_LOCAL) != []


class TestIVRank:
    def test_no_history_gives_no_rank(self, store):
        assert iv_rank("AAPL.US", 0.30, store=store) is None

    def test_own_recording_wins_once_it_spans_a_year(self, store):
        series(store, "SPY", SOURCE_LOCAL, [0.10 + i * 0.0005 for i in range(260)])
        series(store, "VIX", "cboe:VIX", [0.20] * 260)
        r = iv_rank("SPY.US", 0.15, store=store)
        assert r.source == SOURCE_LOCAL
        assert r.proxy_of is None and r.level_displayable

    def test_the_index_proxy_is_used_before_a_year_of_our_own(self, store):
        series(store, "SPY", SOURCE_LOCAL, [0.12] * 30)
        series(store, "VIX", "cboe:VIX", [0.10 + i * 0.001 for i in range(200)])
        r = iv_rank("SPY.US", 0.15, store=store)
        assert r.source == "cboe:VIX"
        assert r.proxy_of == "VIX"

    def test_a_proxy_ranks_its_own_latest_level_not_ours(self, store):
        # Rising to 0.299, so the rank must be ~100 regardless of the SPY IV passed in.
        # Ranking our 0.15 inside VIX's range would be meaningless.
        series(store, "VIX", "cboe:VIX", [0.10 + i * 0.001 for i in range(200)])
        r = iv_rank("SPY.US", 0.15, store=store)
        assert r.rank == pytest.approx(100.0)
        assert r.observed == pytest.approx(0.299)
        assert not r.level_displayable

    def test_dolthub_bounds_give_a_rank_but_no_percentile(self, store):
        store.upsert_iv_bounds("AAPL", "2026-07-30", SOURCE_DOLT, 0.2977, 0.3290, 0.1748)
        r = iv_rank("AAPL.US", 0.2977, store=store)
        assert r.source == SOURCE_DOLT
        assert r.rank == pytest.approx(79.7, abs=0.2)
        # One bounds row carries the extremes but no distribution.
        assert r.percentile is None

    def test_dolthub_bounds_never_claim_a_sample_size(self, store):
        store.upsert_iv_bounds("AAPL", "2026-07-30", SOURCE_DOLT, 0.2977, 0.3290, 0.1748)
        r = iv_rank("AAPL.US", 0.2977, store=store)
        assert not r.series_backed
        # We hold no observations behind a publisher's asserted range, so reporting
        # n=252 here would be a fabricated sample size.
        assert r.n == 0
        # Not thin, though — the range covers a year by assertion, so it is presented
        # without the too-thin caveat.
        assert r.sufficient

    def test_dolthub_ranks_our_live_iv_when_we_have_one(self, store):
        store.upsert_iv_bounds("AAPL", "2026-07-30", SOURCE_DOLT, 0.2977, 0.3290, 0.1748)
        r = iv_rank("AAPL.US", 0.1748, store=store)
        assert r.rank == pytest.approx(0.0)

    def test_a_thin_own_series_is_returned_but_flagged(self, store):
        series(store, "AAPL", SOURCE_LOCAL, [0.20, 0.30, 0.40])
        r = iv_rank("AAPL.US", 0.30, store=store)
        assert r.source == SOURCE_LOCAL
        assert r.n == 3 and not r.sufficient

    def test_sufficiency_tracks_the_configured_minimum(self, store):
        n = settings.min_iv_history_days
        series(store, "AAPL", SOURCE_LOCAL, [0.10 + i * 0.001 for i in range(n)])
        assert iv_rank("AAPL.US", 0.15, store=store).sufficient
        series(store, "MSFT", SOURCE_LOCAL, [0.10 + i * 0.001 for i in range(n - 1)])
        assert not iv_rank("MSFT.US", 0.15, store=store).sufficient

    def test_sources_are_never_mixed(self, store):
        # A DoltHub level and an own-recorded level on the same ticker must not land in
        # one range: the changeover would inject a fake vol jump.
        series(store, "AAPL", SOURCE_LOCAL, [0.20, 0.21, 0.22])
        series(store, "AAPL", SOURCE_DOLT, [0.60, 0.61, 0.62], start=date(2024, 1, 1))
        r = iv_rank("AAPL.US", 0.21, store=store)
        assert r.source == SOURCE_LOCAL
        assert r.high == pytest.approx(0.22)

    def test_the_window_is_capped_at_a_year(self, store):
        series(store, "AAPL", SOURCE_LOCAL, [0.90] + [0.10 + i * 0.0005 for i in range(300)])
        r = iv_rank("AAPL.US", 0.15, store=store)
        # The 0.90 outlier is older than 252 sessions and must fall out of the range.
        assert r.n == ivhistory.IV_RANK_WINDOW
        assert r.high < 0.5


class TestLabels:
    def test_a_proxy_says_so(self):
        r = IVRank("SPY", "cboe:VIX", 50.0, 50.0, 252, 0.1, 0.3, 0.2, "2026-07-31", proxy_of="VIX")
        assert r.label == "VIX proxy"
        assert not r.level_displayable

    def test_own_recording_says_so(self):
        r = IVRank("SPY", SOURCE_LOCAL, 50.0, 50.0, 252, 0.1, 0.3, 0.2, "2026-07-31")
        assert r.label == "own recording"
        assert r.level_displayable


TODAY = date(2026, 8, 1)


def refreshed(store, ticker):
    return refresh_ticker(ticker, store=store, on=TODAY)


class _Resp:
    def __init__(self, text: str = "", payload: dict | None = None) -> None:
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload or {}


def _resp(text: str) -> _Resp:
    return _Resp(text=text)


def _resp_json(payload: dict) -> _Resp:
    return _Resp(payload=payload)
