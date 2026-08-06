import math

import pytest

from app.metrics.trend import (
    compute_trend,
    drawdown_from_high,
    realized_vol,
    sma_distance,
)


def test_sma_on_synthetic_ramp():
    # closes 1..200 -> mean is 100.5, price is 200
    closes = [float(i) for i in range(1, 201)]
    out = sma_distance(closes, window=200)
    assert out.sufficient
    assert out.sma == pytest.approx(100.5)
    assert out.distance_pct == pytest.approx((200 / 100.5 - 1) * 100)


def test_sma_uses_only_the_last_window():
    closes = [1.0] * 50 + [float(i) for i in range(1, 201)]
    assert sma_distance(closes, window=200).sma == pytest.approx(100.5)


def test_sma_insufficient_returns_none_not_partial_mean():
    out = sma_distance([float(i) for i in range(1, 138)], window=200)
    assert out.sma is None
    assert out.distance_pct is None
    assert out.n == 137
    assert not out.sufficient


def test_sma_ignores_nonpositive_closes():
    closes = [0.0, -5.0] + [100.0] * 200
    out = sma_distance(closes, window=200)
    assert out.n == 200
    assert out.sma == pytest.approx(100.0)


def test_rv_on_constant_return_series_is_not_zero_under_zero_mean():
    # A constant +1%/day drift has zero *sample* variance but nonzero mean square.
    # The zero-mean estimator must report the drift as vol; that is the intended
    # variance-swap-consistent behaviour, not a bug.
    closes = [100.0 * (1.01**i) for i in range(21)]
    out = realized_vol(closes, window=20)
    assert out.rv == pytest.approx(math.log(1.01) * math.sqrt(252), rel=1e-9)


def test_rv_on_flat_series_is_zero():
    out = realized_vol([100.0] * 21, window=20)
    assert out.rv == pytest.approx(0.0)
    assert out.n_returns == 20


def test_rv_needs_window_plus_one_closes():
    assert realized_vol([100.0] * 20, window=20).rv is None
    assert realized_vol([100.0] * 21, window=20).rv is not None


def test_rv_alternating_returns_hand_computed():
    # r alternates +ln(1.02), -ln(1.02); mean(r^2) = ln(1.02)^2
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.02 if i % 2 == 0 else 1 / 1.02))
    out = realized_vol(closes, window=20)
    assert out.rv == pytest.approx(math.log(1.02) * math.sqrt(252), rel=1e-9)


def test_drawdown_on_known_peak_and_trough():
    closes = [100.0, 120.0, 150.0, 90.0]
    out = drawdown_from_high(closes)
    assert out.high == pytest.approx(150.0)
    assert out.drawdown_pct == pytest.approx(-40.0)


def test_drawdown_at_ath_is_zero():
    out = drawdown_from_high([100.0, 120.0, 150.0])
    assert out.drawdown_pct == pytest.approx(0.0)


def test_drawdown_uses_expanding_max_not_rolling_window():
    # Peak of 500 sits 300 sessions back, outside any 252-day window.
    closes = [500.0] + [100.0] * 300
    out = drawdown_from_high(closes)
    assert out.high == pytest.approx(500.0)
    assert out.drawdown_pct == pytest.approx(-80.0)
    # The 52-week figure must NOT see that old peak.
    assert out.high_52w == pytest.approx(100.0)
    assert out.drawdown_52w_pct == pytest.approx(0.0)


def test_drawdown_reports_its_own_lookback_for_honest_labelling():
    out = drawdown_from_high([100.0] * 640, first_date="2023-01-03")
    assert out.high_n == 640
    assert out.high_since == "2023-01-03"


def test_drawdown_without_first_date_leaves_provenance_none():
    assert drawdown_from_high([100.0, 90.0]).high_since is None


def test_drawdown_intraday_price_can_exceed_history():
    out = drawdown_from_high([100.0, 120.0], last=130.0)
    assert out.high == pytest.approx(130.0)
    assert out.drawdown_pct == pytest.approx(0.0)


def test_drawdown_empty_series():
    assert drawdown_from_high([]) is None


def test_compute_trend_wires_everything():
    closes = [float(i) for i in range(1, 301)]
    out = compute_trend("SPY", closes, last=300.0)
    assert out.symbol == "SPY"
    assert out.n_bars == 300
    assert out.last == pytest.approx(300.0)
    assert out.sma200.sufficient
    assert out.sma200.sma == pytest.approx(sum(range(101, 301)) / 200)
    assert out.rv20.sufficient
    assert out.drawdown.drawdown_pct == pytest.approx(0.0)


def test_compute_trend_short_history_degrades_without_raising():
    out = compute_trend("NEW", [10.0, 11.0, 12.0])
    assert out.sma200.sma is None
    assert out.rv20.rv is None
    assert out.drawdown is not None
