import math

import pytest

from app.metrics.pricing import (
    black76_greeks,
    carry_forward,
    implied_forward,
    implied_vol_put,
    norm_cdf,
)

F, K, T, R, SIG = 100.0, 100.0, 0.5, 0.04, 0.25


def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_put_call_parity_holds_on_our_own_prices():
    put = black76_greeks(F, K, T, R, SIG, is_put=True)
    call = black76_greeks(F, K, T, R, SIG, is_put=False)
    # C - P = e^(-rT)(F - K)
    assert call.price - put.price == pytest.approx(math.exp(-R * T) * (F - K), abs=1e-10)


def test_put_call_parity_away_from_the_money():
    for strike in (80.0, 90.0, 110.0, 125.0):
        put = black76_greeks(F, strike, T, R, SIG, is_put=True)
        call = black76_greeks(F, strike, T, R, SIG, is_put=False)
        assert call.price - put.price == pytest.approx(
            math.exp(-R * T) * (F - strike), abs=1e-10
        )


def test_atm_forward_put_delta_is_near_half():
    g = black76_greeks(F, K, T, R, SIG, is_put=True)
    assert -0.55 < g.delta < -0.45


def test_put_delta_is_negative_and_monotone_in_strike():
    deltas = [black76_greeks(F, k, T, R, SIG, is_put=True).delta for k in (80, 90, 100, 110, 120)]
    assert all(d < 0 for d in deltas)
    # Higher strike -> deeper ITM put -> delta closer to -1.
    assert deltas == sorted(deltas, reverse=True)


def test_theta_is_negative_for_otm_and_atm_options():
    """The defect that forced local pricing: API theta flipped sign. Ours must not.

    Only asserted OTM/ATM. That covers the entire cash-secured-put use case, and
    deep ITM European options are a separate legitimate positive-theta case below.
    """
    for strike in (70.0, 85.0, 100.0):  # OTM/ATM puts (K <= F)
        g = black76_greeks(F, strike, T, R, SIG, is_put=True)
        assert g.theta < 0, f"put theta positive at K={strike}"

    for strike in (100.0, 115.0, 130.0):  # OTM/ATM calls (K >= F)
        g = black76_greeks(F, strike, T, R, SIG, is_put=False)
        assert g.theta < 0, f"call theta positive at K={strike}"


@pytest.mark.parametrize("is_put,strike", [(True, 200.0), (False, 20.0)])
def test_deep_itm_has_positive_theta_by_construction(is_put, strike):
    """Not a bug. A deep ITM option is worth ~e^(-rT)|F-K|; as T shrinks that
    discount unwinds and the option gains value, so theta is genuinely positive."""
    g = black76_greeks(F, strike, T, R, SIG, is_put=is_put)
    assert g.theta > 0
    expected = -math.exp(-R * T) if is_put else math.exp(-R * T)
    assert g.delta == pytest.approx(expected, abs=1e-3)


def test_theta_negative_at_very_short_dte_both_legs():
    # The exact configuration where the API returned put +17.38 / call -21.65.
    t = 2.0 / 365.0
    put = black76_greeks(747.0, 747.0, t, R, 0.14, is_put=True, spot=747.0)
    call = black76_greeks(747.0, 747.0, t, R, 0.14, is_put=False, spot=747.0)
    assert put.theta < 0 and call.theta < 0
    # Near ATM with q~=r the two should be close in magnitude, not 39 apart.
    assert abs(put.theta - call.theta) < 0.05


def test_gamma_and_vega_are_positive():
    g = black76_greeks(F, K, T, R, SIG, is_put=True)
    assert g.gamma > 0
    assert g.vega > 0


def test_gamma_matches_finite_difference_of_delta():
    h = 1e-4
    spot = 100.0
    base = black76_greeks(F, K, T, R, SIG, is_put=True, spot=spot)
    # Perturb spot, holding the carry relationship so the forward moves with it.
    up = black76_greeks(F * (1 + h), K, T, R, SIG, is_put=True, spot=spot * (1 + h))
    dn = black76_greeks(F * (1 - h), K, T, R, SIG, is_put=True, spot=spot * (1 - h))
    fd = (up.delta - dn.delta) / (2 * h * spot)
    assert base.gamma == pytest.approx(fd, rel=1e-4)


def test_vega_matches_finite_difference_of_price():
    h = 1e-6
    base = black76_greeks(F, K, T, R, SIG, is_put=True)
    up = black76_greeks(F, K, T, R, SIG + h, is_put=True)
    dn = black76_greeks(F, K, T, R, SIG - h, is_put=True)
    assert base.vega == pytest.approx((up.price - dn.price) / (2 * h), rel=1e-5)


def test_theta_matches_finite_difference_of_price():
    h = 1e-6
    base = black76_greeks(F, K, T, R, SIG, is_put=True)
    up = black76_greeks(F, K, T + h, R, SIG, is_put=True)
    dn = black76_greeks(F, K, T - h, R, SIG, is_put=True)
    # dPrice/dT is positive; theta is -dPrice/dT per day.
    d_price_dt = (up.price - dn.price) / (2 * h)
    assert base.theta == pytest.approx(-d_price_dt / 365.0, rel=1e-3)


def test_delta_matches_finite_difference_of_price_in_forward_space():
    h = 1e-6
    base = black76_greeks(F, K, T, R, SIG, is_put=True)
    up = black76_greeks(F + h, K, T, R, SIG, is_put=True)
    dn = black76_greeks(F - h, K, T, R, SIG, is_put=True)
    assert base.delta == pytest.approx((up.price - dn.price) / (2 * h), rel=1e-5)


def test_degenerate_inputs_return_none():
    assert black76_greeks(F, K, 0.0, R, SIG) is None
    assert black76_greeks(F, K, T, R, 0.0) is None
    assert black76_greeks(0.0, K, T, R, SIG) is None
    assert black76_greeks(F, 0.0, T, R, SIG) is None


def test_implied_forward_recovers_a_known_forward():
    true_f = 103.5
    quotes = []
    for k in (95.0, 100.0, 105.0, 110.0):
        c = black76_greeks(true_f, k, T, R, SIG, is_put=False).price
        p = black76_greeks(true_f, k, T, R, SIG, is_put=True).price
        quotes.append((k, c, p))
    out = implied_forward(quotes, R, T, spot=103.0, atm_window=0.10)
    assert out.forward == pytest.approx(true_f, rel=1e-9)
    assert out.source == "parity"


def test_implied_forward_median_survives_one_bad_strike():
    true_f = 100.0
    quotes = []
    for k in (96.0, 98.0, 100.0, 102.0, 104.0):
        c = black76_greeks(true_f, k, T, R, SIG, is_put=False).price
        p = black76_greeks(true_f, k, T, R, SIG, is_put=True).price
        quotes.append((k, c, p))
    quotes[0] = (96.0, quotes[0][1] + 5.0, quotes[0][2])  # one badly marked call
    out = implied_forward(quotes, R, T, spot=100.0)
    assert out.forward == pytest.approx(true_f, rel=1e-6)


def test_implied_forward_recovers_dividend_yield():
    spot, q = 100.0, 0.015
    true_f = spot * math.exp((R - q) * T)
    quotes = []
    for k in (98.0, 100.0, 102.0):
        c = black76_greeks(true_f, k, T, R, SIG, is_put=False).price
        p = black76_greeks(true_f, k, T, R, SIG, is_put=True).price
        quotes.append((k, c, p))
    out = implied_forward(quotes, R, T, spot=spot)
    assert out.div_yield == pytest.approx(q, abs=1e-9)


def test_implied_forward_skips_one_sided_and_zero_quotes():
    assert implied_forward([(100.0, None, 2.0)], R, T) is None
    assert implied_forward([(100.0, 0.0, 2.0)], R, T) is None
    assert implied_forward([], R, T) is None


def test_implied_forward_respects_the_atm_window():
    # Only a deep wing strike is available; the window must exclude it.
    assert implied_forward([(50.0, 51.0, 0.1)], R, T, spot=100.0, atm_window=0.10) is None


def test_carry_forward_fallback():
    out = carry_forward(100.0, R, T, div_yield=0.01)
    assert out.forward == pytest.approx(100.0 * math.exp((R - 0.01) * T))
    assert out.source == "carry"


def test_implied_vol_roundtrips():
    price = black76_greeks(F, K, T, R, SIG, is_put=True).price
    assert implied_vol_put(price, F, K, T, R) == pytest.approx(SIG, abs=1e-6)


def test_implied_vol_rejects_sub_intrinsic_price():
    intrinsic = math.exp(-R * T) * (120.0 - F)
    assert implied_vol_put(intrinsic * 0.5, F, 120.0, T, R) is None


def test_spot_greeks_reduce_to_forward_greeks_when_q_equals_r():
    """With q = 0 the forward is S*e^(rT) and spot delta must equal e^(0)*N(-d1)."""
    spot = 100.0
    fwd = spot * math.exp(R * T)
    g = black76_greeks(fwd, K, T, R, SIG, is_put=True, spot=spot)
    # q = 0 -> disc_q = 1 -> delta = -N(-d1)
    assert g.delta == pytest.approx(-(1.0 - norm_cdf(g.d1)), rel=1e-12)
