import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.metrics.options import (
    build_candidate,
    constant_maturity_iv,
    dte,
    interp_atm_iv,
    iv_vs_rv,
    market_today,
    normalize_api_delta,
    pick_expiries,
    premium_yield,
    select_by_delta,
    spread_metrics,
)

NY = ZoneInfo("America/New_York")


# --- DTE ---------------------------------------------------------------------


def test_dte_expiry_day_is_zero():
    now = datetime(2026, 9, 11, 14, 0, tzinfo=NY)
    assert dte(date(2026, 9, 11), now) == 0


def test_dte_counts_calendar_days_not_business_days():
    now = datetime(2026, 8, 3, 10, 0, tzinfo=NY)  # Monday
    assert dte(date(2026, 8, 10), now) == 7


def test_dte_across_a_year_boundary():
    now = datetime(2026, 12, 20, 10, 0, tzinfo=NY)
    assert dte(date(2027, 1, 15), now) == 26


def test_dte_uses_new_york_not_utc():
    """22:00 New York on the 1st is already the 2nd in UTC. DTE must not lose a day."""
    now = datetime(2026, 8, 1, 22, 0, tzinfo=NY)
    assert market_today(now) == date(2026, 8, 1)
    assert dte(date(2026, 9, 11), now) == 41
    # Same instant expressed in UTC must agree.
    assert dte(date(2026, 9, 11), now.astimezone(timezone.utc)) == 41


def test_dte_negative_when_expired():
    now = datetime(2026, 9, 12, 10, 0, tzinfo=NY)
    assert dte(date(2026, 9, 11), now) == -1


def test_market_today_rejects_naive_datetime():
    with pytest.raises(ValueError):
        market_today(datetime(2026, 8, 1, 12, 0))


def test_pick_expiries_filters_to_the_dte_window():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=NY)
    expiries = [
        date(2026, 8, 7),  # 6
        date(2026, 9, 4),  # 34, just outside
        date(2026, 9, 11),  # 41
        date(2026, 9, 30),  # 60
        date(2026, 10, 1),  # 61, just outside
    ]
    assert pick_expiries(expiries, now=now) == [date(2026, 9, 11), date(2026, 9, 30)]


# --- spread ------------------------------------------------------------------


def test_spread_basic_math():
    s = spread_metrics(4.38, 4.43)
    assert s.mid == pytest.approx(4.405)
    assert s.abs_spread == pytest.approx(0.05)
    assert s.rel_spread_pct == pytest.approx(0.05 / 4.405 * 100)
    assert s.liquid


def test_cheap_option_at_minimum_tick_passes_on_absolute_rule():
    # 0.05 wide on a 0.075 mid is 66% relative, but it is one legal tick.
    s = spread_metrics(0.05, 0.10)
    assert s.rel_spread_pct > 2.0
    assert s.liquid  # rescued by the absolute rule


def test_wide_expensive_option_fails_both_rules():
    s = spread_metrics(10.00, 10.80)
    assert not s.liquid


def test_expensive_option_passes_on_relative_rule():
    s = spread_metrics(10.00, 10.15)  # 1.49% of mid, 0.15 absolute
    assert s.abs_spread > 0.05
    assert s.liquid


def test_zero_bid_is_never_liquid():
    s = spread_metrics(0.0, 0.05)
    assert s.zero_bid
    assert not s.liquid


def test_missing_quote_yields_no_mid():
    assert spread_metrics(None, 4.43).mid is None
    assert spread_metrics(4.38, None).mid is None


def test_crossed_book_is_rejected():
    s = spread_metrics(5.0, 4.0)
    assert s.mid is None
    assert not s.liquid


# --- premium yield -----------------------------------------------------------


def test_premium_yield_hand_computed():
    # mid 4.405 on a 710 strike over 41 days
    p = premium_yield(4.405, 4.38, 710.0, 41)
    assert p.pct_of_strike == pytest.approx(4.405 / 710 * 100)
    assert p.annualized_pct == pytest.approx(4.405 / 710 * 100 * 365 / 41)
    assert p.pct_of_strike_bid == pytest.approx(4.38 / 710 * 100)
    assert p.pct_of_strike_bid < p.pct_of_strike  # bid is the conservative case


def test_premium_yield_annualization_is_simple_not_compounded():
    p = premium_yield(1.0, 1.0, 100.0, 365)
    assert p.annualized_pct == pytest.approx(p.pct_of_strike)


def test_premium_yield_handles_zero_premium_and_zero_days():
    assert premium_yield(0.0, 0.0, 100.0, 30).pct_of_strike is None
    assert premium_yield(1.0, 1.0, 100.0, 0).annualized_pct is None


# --- delta normalization -----------------------------------------------------


def test_normalize_delta_takes_magnitude():
    assert normalize_api_delta(-0.175) == pytest.approx(0.175)


def test_normalize_delta_rescales_percent_style_values():
    assert normalize_api_delta(-17.5) == pytest.approx(0.175)


def test_normalize_delta_rejects_nonsense():
    assert normalize_api_delta(None) is None
    assert normalize_api_delta(500.0) is None


# --- IV interpolation --------------------------------------------------------


def test_interp_atm_iv_between_two_strikes():
    assert interp_atm_iv([740.0, 750.0], [0.14, 0.16], 745.0) == pytest.approx(0.15)


def test_interp_atm_iv_clamps_outside_the_grid():
    assert interp_atm_iv([740.0, 750.0], [0.14, 0.16], 100.0) == pytest.approx(0.14)
    assert interp_atm_iv([740.0, 750.0], [0.14, 0.16], 9999.0) == pytest.approx(0.16)


def test_interp_atm_iv_skips_missing_ivs():
    assert interp_atm_iv([740.0, 745.0, 750.0], [0.14, None, 0.16], 745.0) == pytest.approx(
        0.15
    )


def test_interp_atm_iv_empty():
    assert interp_atm_iv([], [], 100.0) is None


def test_constant_maturity_iv_interpolates_total_variance_not_iv():
    # 20d at 20%, 60d at 30%. Linear-in-IV would give 22.5% at 30 days.
    out = constant_maturity_iv([(20.0, 0.20), (60.0, 0.30)], target_days=30.0)
    t0, t1, tt = 20 / 365, 60 / 365, 30 / 365
    var = 0.20**2 * t0 + 0.25 * (0.30**2 * t1 - 0.20**2 * t0)
    assert out == pytest.approx(math.sqrt(var / tt))
    assert out != pytest.approx(0.225)  # not the naive linear-in-IV answer


def test_constant_maturity_iv_flat_term_structure_is_flat():
    assert constant_maturity_iv([(20.0, 0.25), (60.0, 0.25)], 30.0) == pytest.approx(0.25)


def test_constant_maturity_iv_clamps_rather_than_extrapolating():
    pts = [(40.0, 0.20), (60.0, 0.30)]
    assert constant_maturity_iv(pts, 10.0) == pytest.approx(0.20)
    assert constant_maturity_iv(pts, 900.0) == pytest.approx(0.30)


def test_constant_maturity_iv_single_and_empty():
    assert constant_maturity_iv([(41.0, 0.22)], 30.0) == pytest.approx(0.22)
    assert constant_maturity_iv([], 30.0) is None


# --- IV vs RV ----------------------------------------------------------------


def test_iv_vs_rv_ratio_and_spread():
    out = iv_vs_rv(0.1439, 0.1238)
    assert out.ratio == pytest.approx(0.1439 / 0.1238)
    assert out.spread_points == pytest.approx((0.1439 - 0.1238) * 100)


def test_iv_vs_rv_missing_inputs():
    assert iv_vs_rv(None, 0.12).ratio is None
    assert iv_vs_rv(0.14, None).ratio is None
    assert iv_vs_rv(0.14, 0.0).ratio is None


# --- candidates and selection ------------------------------------------------

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=NY)
EXPIRY = date(2026, 9, 11)  # 41 DTE
SPOT = 747.03
FWD = 749.0
R = 0.0375


def make(strike: float, iv: float, bid: float, ask: float, api_delta=None, oi=100):
    return build_candidate(
        symbol=f"SPY260911P{int(strike * 1000)}.US",
        strike=strike,
        expiry=EXPIRY,
        spot=SPOT,
        forward=FWD,
        iv=iv,
        r=R,
        bid=bid,
        ask=ask,
        api_delta=api_delta,
        open_interest=oi,
        now=NOW,
    )


def test_candidate_populates_greeks_and_units():
    c = make(710.0, 0.1743, 4.38, 4.43)
    assert c.dte == 41
    assert 0.0 < c.delta < 1.0
    # Seller-signed theta: decay is income, so positive.
    assert c.theta_day > 0
    assert c.theta_day_contract == pytest.approx(c.theta_day * 100)
    assert c.vega_contract == pytest.approx(c.vega * 100)
    assert c.moneyness_pct < 0  # OTM put


def test_candidate_theta_magnitude_matches_atm_closed_form():
    """Dollar greeks scale with the underlying, so absolute ranges are meaningless.

    For an ATM option price ~ c*sqrt(T), decay is ~ 0.5*price/T per year. On SPY at
    747 that is ~$15/contract/day, not the ~$2-6 that would hold for a $100 stock.
    """
    c = make(745.0, 0.1439, 12.30, 12.40)
    price_per_contract = 12.35 * 100
    approx = 0.5 * price_per_contract / (41 / 365) / 365
    assert c.theta_day_contract == pytest.approx(approx, rel=0.10)


def test_candidate_vega_magnitude_matches_atm_closed_form():
    # ATM vega per contract ~ S * phi(0) * sqrt(T).
    c = make(745.0, 0.1439, 12.30, 12.40)
    approx = SPOT * (1 / math.sqrt(2 * math.pi)) * math.sqrt(41 / 365)
    assert c.vega_contract == pytest.approx(approx, rel=0.05)


def test_vega_is_per_iv_point_not_per_unit_sigma():
    """Guards the /100 scaling. Missing it inflates vega by exactly 100x."""
    c = make(745.0, 0.1439, 12.30, 12.40)
    assert c.vega == pytest.approx(c.vega_contract / 100)
    # Per-share vega per IV point on a $747 name is order 1, not order 100.
    assert 0.1 < c.vega < 10.0


def test_candidate_flags_delta_mismatch_against_api():
    ok = make(710.0, 0.1743, 4.38, 4.43, api_delta=None)
    assert not ok.delta_mismatch
    bad = make(710.0, 0.1743, 4.38, 4.43, api_delta=-0.90)
    assert bad.delta_mismatch


def test_candidate_agreeing_api_delta_is_not_flagged():
    c = make(710.0, 0.1743, 4.38, 4.43)
    agreeing = make(710.0, 0.1743, 4.38, 4.43, api_delta=-c.delta)
    assert not agreeing.delta_mismatch


def test_candidate_rejects_expired_contract():
    assert (
        build_candidate(
            "X", 700.0, date(2026, 7, 1), SPOT, FWD, 0.2, R, 1.0, 1.1, None, 1, NOW
        )
        is None
    )


def test_candidate_survives_missing_iv():
    c = make(710.0, 0.0, 4.38, 4.43)
    assert c is not None
    assert c.delta is None and c.theta_day is None
    assert c.spread.mid == pytest.approx(4.405)  # spread still computed


def test_select_by_delta_rejects_itm_strikes():
    """A synthetic chain where the closest delta to target is ITM. Must be skipped."""
    itm = make(760.0, 0.13, 20.0, 20.2)  # strike > spot
    otm = make(700.0, 0.19, 3.0, 3.1)
    assert itm.moneyness_pct > 0
    pick = select_by_delta([itm, otm])
    assert pick.candidate.symbol == otm.symbol


def test_select_by_delta_rejects_zero_bid():
    live = make(700.0, 0.19, 3.0, 3.1)
    dead = make(715.0, 0.17, 0.0, 0.05)
    pick = select_by_delta([live, dead])
    assert pick.candidate.symbol == live.symbol


def test_select_by_delta_picks_nearest_to_target():
    # Dense grid so the band is actually populated; at 20% IV / 41 DTE the 705
    # strike sits at ~0.174 delta.
    chain = [make(float(k), 0.20, 1.0, 1.1) for k in range(640, 750, 5)]
    pick = select_by_delta(chain)
    assert pick.candidate is not None
    assert not pick.off_band
    lo, hi = 0.15, 0.20
    assert lo <= pick.candidate.delta <= hi
    in_band = [c for c in chain if lo <= (c.delta or 0) <= hi]
    assert pick.candidate.delta == min(in_band, key=lambda c: abs(c.delta - 0.175)).delta


def test_select_by_delta_ignores_wings_when_the_band_is_populated():
    wings = [make(float(k), 0.20, 0.2, 0.3) for k in (650, 660, 670)]  # all < 0.05
    in_band = make(705.0, 0.20, 1.0, 1.1)  # ~0.174
    pick = select_by_delta(wings + [in_band])
    assert pick.candidate.symbol == in_band.symbol
    assert not pick.off_band
    assert pick.off_target < 0.01


def test_select_by_delta_flags_when_nothing_lands_in_band():
    # Only far wings, all well below 0.15 delta.
    chain = [make(k, 0.30, 0.2, 0.25) for k in (500.0, 520.0, 540.0)]
    pick = select_by_delta(chain)
    assert pick.candidate is not None  # still returns the closest, honestly flagged
    assert pick.off_band
    assert pick.candidate.delta < 0.15


def test_select_by_delta_empty_chain():
    pick = select_by_delta([])
    assert pick.candidate is None
    assert pick.off_band


def test_in_delta_band_flag_matches_selection_band():
    c = make(700.0, 0.19, 3.0, 3.1)
    assert c.in_delta_band == (0.15 <= c.delta <= 0.20)
