from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from app.config import settings
from app.metrics.options import build_candidate
from app.metrics.pricing import black76_greeks, carry_forward
from app.params import defaults
from app.providers.chain import (
    _bracketing_expiry,
    FAST,
    MAX_STRIKES_PER_EXPIRY,
    SLOW,
    ChainResult,
    ExpirySlice,
    apply_quotes,
    build_chain,
    choose_expiries,
    choose_expiry,
    rank_for_quotes,
    target_strikes,
)
from app.providers.longport_client import (
    Book,
    ContractCalc,
    StrikeRow,
    display_ticker,
    us_symbol,
)

NOW = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
SPOT = 747.0
R = 0.0375
SIGMA = 0.20


def _expiry(days: int) -> date:
    return date(2026, 8, 1) + __import__("datetime").timedelta(days=days)


class TestChooseExpiry:
    def test_picks_nearest_the_window_midpoint(self):
        # Window is 35-60, midpoint 47.5.
        got = choose_expiry([_expiry(d) for d in (7, 38, 45, 58, 90)], now=NOW)
        assert got == _expiry(45)

    def test_ignores_out_of_window_even_when_nearer_the_midpoint(self):
        # 34 is out of window; 60 is in it. 34 is NOT nearer to 47.5 than 60, but
        # this asserts the window filter runs first regardless.
        got = choose_expiry([_expiry(34), _expiry(60)], now=NOW)
        assert got == _expiry(60)

    def test_falls_back_to_nearest_future_when_window_empty(self):
        got = choose_expiry([_expiry(3), _expiry(20), _expiry(120)], now=NOW)
        assert got == _expiry(20)

    def test_never_returns_a_past_expiry(self):
        got = choose_expiry([_expiry(-30), _expiry(-1)], now=NOW)
        assert got is None

    def test_expiry_today_is_eligible_as_fallback(self):
        assert choose_expiry([_expiry(-5), _expiry(0)], now=NOW) == _expiry(0)

    def test_empty_list(self):
        assert choose_expiry([], now=NOW) is None


class TestChooseExpiries:
    """The window is priced whole. Which tenor to sell is the decision being made, so
    sampling its midpoint answers a question nobody asked."""

    def test_prices_the_whole_window_in_dte_order(self):
        got = choose_expiries([_expiry(d) for d in (60, 38, 45)], now=NOW)
        assert got == [_expiry(38), _expiry(45), _expiry(60)]

    def test_drops_expiries_outside_the_window(self):
        got = choose_expiries([_expiry(d) for d in (7, 34, 41, 60, 90)], now=NOW)
        assert got == [_expiry(41), _expiry(60)]

    def test_there_is_no_cap(self):
        # A wide window is paid for by the slow clock and the ranked subscription set,
        # never by truncating the comparison.
        wide = replace(defaults(), dte_min=7, dte_max=60)
        got = choose_expiries([_expiry(d) for d in range(7, 61, 3)], wide, NOW)
        assert len(got) == 18

    def test_an_empty_window_degrades_to_the_single_nearest(self):
        listed = [_expiry(3), _expiry(20), _expiry(120)]
        assert choose_expiries(listed, now=NOW) == [choose_expiry(listed, now=NOW)]
        assert choose_expiries(listed, now=NOW) == [_expiry(20)]

    def test_one_listed_expiry_in_the_window_reproduces_the_old_answer(self):
        # Today's behaviour has to stay a strict subset, or this is a rewrite rather
        # than a widening.
        listed = [_expiry(7), _expiry(45), _expiry(120)]
        assert choose_expiries(listed, now=NOW) == [choose_expiry(listed, now=NOW)]

    def test_never_returns_a_past_expiry(self):
        assert choose_expiries([_expiry(-30), _expiry(-1)], now=NOW) == []

    def test_empty_list(self):
        assert choose_expiries([], now=NOW) == []


class TestTargetStrikes:
    grid = [float(k) for k in range(600, 800, 5)]

    def _deltas(self, strikes: list[float], t: float) -> dict[float, float]:
        fwd = carry_forward(SPOT, R, t).forward
        return {
            k: abs(black76_greeks(fwd, k, t, R, SIGMA, is_put=True, spot=SPOT).delta)
            for k in strikes
        }

    def test_brackets_the_screen_delta_band(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(self.grid, fwd, SPOT, t, R, SIGMA)
        deltas = self._deltas(got, t)

        lo, hi = settings.delta_screen_band
        inside = [k for k, d in deltas.items() if lo <= d <= hi]
        assert inside, f"no strike in the {lo}-{hi} band; deltas were {deltas}"
        # And the 0.15-0.20 trading band must be reachable too.
        assert any(0.15 <= d <= 0.20 for d in deltas.values()), deltas

    def test_pads_beyond_the_band_on_both_sides(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(self.grid, fwd, SPOT, t, R, SIGMA)
        deltas = self._deltas(got, t)
        lo, hi = settings.delta_screen_band
        assert min(deltas.values()) < lo, "no pad below the band"
        assert max(deltas.values()) > hi, "no pad above the band"

    def test_excludes_far_wings_so_quota_is_not_wasted(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(self.grid, fwd, SPOT, t, R, SIGMA)
        assert len(got) < len(self.grid) / 2
        assert max(got) < SPOT, "band selection must stay OTM for puts"

    def test_respects_the_hard_symbol_ceiling(self):
        dense = [600.0 + 0.5 * i for i in range(400)]
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(dense, fwd, SPOT, t, R, SIGMA)
        assert len(got) <= MAX_STRIKES_PER_EXPIRY

    def test_ceiling_centres_on_the_trading_band(self):
        # A $0.50 grid makes the screen band far wider than the ceiling, so
        # truncation is forced. The 0.15-0.20 band must end up straddled, not
        # clipped to an edge.
        dense = [600.0 + 0.5 * i for i in range(400)]
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(dense, fwd, SPOT, t, R, SIGMA)
        deltas = sorted(self._deltas(got, t).values())
        assert deltas[0] < 0.15, f"nothing below the trading band: {deltas}"
        assert deltas[-1] > 0.20, f"nothing above the trading band: {deltas}"

    def test_higher_vol_widens_the_band_downward(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        low = target_strikes(self.grid, fwd, SPOT, t, R, 0.12)
        high = target_strikes(self.grid, fwd, SPOT, t, R, 0.40)
        assert min(high) < min(low)

    def test_coarse_grid_still_returns_something(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes([500.0, 750.0, 1000.0], fwd, SPOT, t, R, SIGMA)
        assert got

    def test_empty_grid(self):
        t = 45 / 365.0
        assert target_strikes([], 750.0, SPOT, t, R, SIGMA) == []

    def test_drops_nonpositive_strikes(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes([0.0, -5.0] + self.grid, fwd, SPOT, t, R, SIGMA)
        assert all(k > 0 for k in got)

    def test_an_explicit_band_is_honoured(self):
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(self.grid, fwd, SPOT, t, R, SIGMA, band=(0.30, 0.45))
        deltas = self._deltas(got, t)
        assert any(0.30 <= d <= 0.45 for d in deltas.values()), deltas

    def test_the_ceiling_centres_on_an_explicit_target(self):
        # Truncation is centred on the target, not on the singleton's 0.175: otherwise a
        # user asking for 0.40 delta would get a window that never reaches it.
        dense = [400.0 + 0.5 * i for i in range(800)]
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        got = target_strikes(dense, fwd, SPOT, t, R, SIGMA, band=(0.05, 0.60), target=0.40)
        assert len(got) <= MAX_STRIKES_PER_EXPIRY
        deltas = sorted(self._deltas(got, t).values())
        assert deltas[0] < 0.40, f"nothing below the target: {deltas}"
        assert deltas[-1] > 0.40, f"nothing above the target: {deltas}"

    def test_a_different_target_moves_the_window(self):
        dense = [400.0 + 0.5 * i for i in range(800)]
        t = 45 / 365.0
        fwd = carry_forward(SPOT, R, t).forward
        low = target_strikes(dense, fwd, SPOT, t, R, SIGMA, band=(0.05, 0.60), target=0.10)
        high = target_strikes(dense, fwd, SPOT, t, R, SIGMA, band=(0.05, 0.60), target=0.40)
        # A higher delta target sits nearer the money, so the window moves up.
        assert max(high) > max(low)


def _candidate(strike: float, symbol: str) -> object:
    days = 45
    t = days / 365.0
    fwd = carry_forward(SPOT, R, t).forward
    return build_candidate(
        symbol=symbol,
        strike=strike,
        expiry=_expiry(days),
        spot=SPOT,
        forward=fwd,
        iv=SIGMA,
        r=R,
        bid=None,
        ask=None,
        api_delta=None,
        open_interest=1000,
        now=NOW,
    )


def _result(strikes: list[float]) -> ChainResult:
    cands = [_candidate(k, f"SPY.P{int(k)}") for k in strikes]
    sl = ExpirySlice(
        expiry=_expiry(45), dte=45, forward=None, atm_iv=SIGMA, candidates=cands
    )
    return ChainResult("SPY", SPOT, [sl], SIGMA, None, None)


class TestApplyQuotes:
    strikes = [690.0, 700.0, 710.0]

    def test_unquoted_chain_has_no_match(self):
        # The whole point of the split: build_chain cannot select, because
        # select_by_delta requires a live bid.
        res = _result(self.strikes)
        assert res.match is None
        assert all(c.spread.mid is None for c in res.all_candidates)

    def test_quotes_populate_spread_and_premium(self):
        res = _result(self.strikes)
        books = {
            c.symbol: Book(c.symbol, 3.00, 3.05, 10, 10) for c in res.all_candidates
        }
        out = apply_quotes(res, books)
        for c in out.all_candidates:
            assert c.spread.mid == pytest.approx(3.025)
            # 0.05 absolute passes the absolute gate even though 1.65% of mid.
            assert c.spread.liquid is True
            assert c.premium.pct_of_strike == pytest.approx(3.025 / c.strike * 100.0)

    def test_wide_quote_is_flagged_illiquid(self):
        res = _result(self.strikes)
        books = {
            c.symbol: Book(c.symbol, 3.00, 3.30, 10, 10) for c in res.all_candidates
        }
        out = apply_quotes(res, books)
        assert all(c.spread.liquid is False for c in out.all_candidates)

    def test_greeks_survive_the_quote_refresh(self):
        res = _result(self.strikes)
        before = {c.symbol: (c.delta, c.vega, c.theta_day) for c in res.all_candidates}
        out = apply_quotes(res, {c.symbol: Book(c.symbol, 1.0, 1.1, 1, 1) for c in res.all_candidates})
        for c in out.all_candidates:
            assert (c.delta, c.vega, c.theta_day) == before[c.symbol]

    def test_match_is_selected_once_quotes_exist(self):
        res = _result([float(k) for k in range(660, 745, 5)])
        books = {c.symbol: Book(c.symbol, 2.0, 2.05, 5, 5) for c in res.all_candidates}
        out = apply_quotes(res, books)
        assert out.match is not None
        assert out.match.candidate is not None
        lo, hi = settings.delta_band
        assert lo <= out.match.candidate.delta <= hi
        assert out.match.off_band is False

    def test_missing_book_leaves_the_candidate_unquoted(self):
        res = _result(self.strikes)
        out = apply_quotes(res, {})
        assert all(c.spread.mid is None for c in out.all_candidates)
        assert out.match is not None and out.match.candidate is None
        assert any("no two-sided quotes" in w for w in out.warnings)

    def test_zero_bid_is_never_selected(self):
        res = _result(self.strikes)
        books = {c.symbol: Book(c.symbol, 0.0, 0.10, 0, 5) for c in res.all_candidates}
        out = apply_quotes(res, books)
        assert out.match is not None and out.match.candidate is None

    def test_is_pure(self):
        res = _result(self.strikes)
        apply_quotes(res, {c.symbol: Book(c.symbol, 3.0, 3.1, 1, 1) for c in res.all_candidates})
        assert all(c.spread.mid is None for c in res.all_candidates)
        assert res.match is None

    def test_preexisting_warnings_are_preserved(self):
        res = _result(self.strikes)
        res.warnings.append("carried over")
        out = apply_quotes(res, {c.symbol: Book(c.symbol, 3.0, 3.1, 1, 1) for c in res.all_candidates})
        assert "carried over" in out.warnings


class TestApplyQuotesHonoursTheParameters:
    """apply_quotes is the seam every FREE parameter is applied at, so it is where a
    changed target, band or liquidity gate has to bite — at zero option-quota cost."""

    strikes = [float(k) for k in range(660, 745, 5)]

    def _quoted(self, params=None, bid=2.0, ask=2.05):
        res = _result(self.strikes)
        books = {c.symbol: Book(c.symbol, bid, ask, 5, 5) for c in res.all_candidates}
        return apply_quotes(res, books, params)

    def test_the_match_follows_the_delta_target(self):
        base = self._quoted()
        moved = self._quoted(replace(defaults(), delta_target=0.30, delta_band=(0.25, 0.35)))
        assert moved.match.candidate is not None
        assert moved.match.candidate.symbol != base.match.candidate.symbol
        assert 0.25 <= moved.match.candidate.delta <= 0.35

    def test_the_in_band_flag_is_recomputed_not_inherited(self):
        # build_candidate baked the flag in from whatever band was in force then, and
        # with_quote uses replace(), so a stale flag would survive the band change.
        wide = self._quoted(replace(defaults(), delta_band=(0.01, 0.99), delta_target=0.30))
        assert all(c.in_delta_band for c in wide.all_candidates if c.delta is not None)
        narrow = self._quoted(replace(defaults(), delta_band=(0.9, 0.99), delta_target=0.95))
        assert not any(c.in_delta_band for c in narrow.all_candidates)

    def test_a_tighter_liquidity_gate_turns_clean_rows_wide(self):
        assert all(c.spread.liquid for c in self._quoted().all_candidates)
        tight = self._quoted(replace(defaults(), max_abs_spread=0.0, max_rel_spread_pct=0.5))
        assert all(not c.spread.liquid for c in tight.all_candidates)

    def test_an_unreachable_band_names_the_band_it_could_not_fill(self):
        out = self._quoted(replace(defaults(), delta_band=(0.9, 0.99), delta_target=0.95))
        assert out.match is not None and out.match.candidate is not None
        assert out.match.off_band is True
        assert any("0.90-0.99" in w for w in out.warnings), out.warnings

    def test_omitting_the_parameters_keeps_the_code_defaults(self):
        assert self._quoted() == self._quoted(defaults())


class TestSymbolNormalization:
    @pytest.mark.parametrize(
        "raw,want",
        [
            ("SPY", "SPY.US"),
            ("spy", "SPY.US"),
            ("  spy  ", "SPY.US"),
            ("SPY.US", "SPY.US"),
            ("spy.us", "SPY.US"),
            # "B" is not a market, so the whole thing is the ticker.
            ("BRK.B", "BRK.B.US"),
            ("brk.b", "BRK.B.US"),
            ("BRK.B.US", "BRK.B.US"),
            ("700.HK", "700.HK"),
        ],
    )
    def test_us_symbol(self, raw, want):
        assert us_symbol(raw) == want

    def test_option_symbols_pass_through(self):
        opt = "SPY260918P705000.US"
        assert us_symbol(opt) == opt

    def test_empty_input(self):
        assert us_symbol("") == ""
        assert us_symbol("   ") == ""

    @pytest.mark.parametrize(
        "symbol,want",
        [("SPY.US", "SPY"), ("BRK.B.US", "BRK.B"), ("700.HK", "700"), ("SPY", "SPY")],
    )
    def test_display_ticker(self, symbol, want):
        assert display_ticker(symbol) == want

    @pytest.mark.parametrize("raw", ["SPY", "BRK.B", "QQQ"])
    def test_round_trip(self, raw):
        assert display_ticker(us_symbol(raw)) == raw


class TestBracketingExpiry:
    def test_long_slice_gets_a_short_partner(self):
        got = _bracketing_expiry([_expiry(d) for d in (7, 28, 48, 76)], 48, NOW)
        assert got == _expiry(28)

    def test_short_slice_gets_a_long_partner(self):
        got = _bracketing_expiry([_expiry(d) for d in (7, 20, 35, 76)], 20, NOW)
        assert got == _expiry(35)

    def test_partner_must_straddle_thirty_not_merely_differ(self):
        # 45 is closer to 48 than 28 is, but a 45/48 pair leaves 30 outside the
        # range, so constant-maturity IV would clamp instead of interpolate.
        got = _bracketing_expiry([_expiry(28), _expiry(45), _expiry(48)], 48, NOW)
        assert got == _expiry(28)

    def test_picks_the_partner_nearest_thirty(self):
        got = _bracketing_expiry([_expiry(d) for d in (3, 14, 29, 48)], 48, NOW)
        assert got == _expiry(29)

    def test_never_picks_an_expired_contract(self):
        assert _bracketing_expiry([_expiry(-10), _expiry(48)], 48, NOW) is None

    def test_none_when_nothing_straddles(self):
        assert _bracketing_expiry([_expiry(40), _expiry(48)], 48, NOW) is None


# --- multi-tenor helpers ------------------------------------------------------

WIDE = replace(defaults(), dte_min=7, dte_max=60)


def _candidate_at(strike: float, days: int, bid=None, ask=None, oi: int = 1000):
    t = days / 365.0
    fwd = carry_forward(SPOT, R, t).forward
    return build_candidate(
        symbol=f"SPY{days}P{int(strike * 1000)}",
        strike=strike,
        expiry=_expiry(days),
        spot=SPOT,
        forward=fwd,
        iv=SIGMA,
        r=R,
        bid=bid,
        ask=ask,
        api_delta=None,
        open_interest=oi,
        now=NOW,
    )


def _slice(days: int, strikes: list[float], clock: str = FAST) -> ExpirySlice:
    cands = [_candidate_at(k, days) for k in strikes]
    return ExpirySlice(
        expiry=_expiry(days),
        dte=days,
        forward=None,
        atm_iv=SIGMA,
        candidates=[c for c in cands if c is not None],
        clock=clock,
        quota_spent=3 + len(strikes),
    )


def _multi(*slices: ExpirySlice) -> ChainResult:
    return ChainResult("SPY", SPOT, list(slices), SIGMA, None, None)


def _books(chain: ChainResult, bid: float, ask: float) -> dict[str, Book]:
    return {c.symbol: Book(c.symbol, bid, ask, 5, 5) for c in chain.all_candidates}


def _one(days: int, strike: float, bid, ask, clock: str = FAST):
    """A one-candidate slice, so its delta match is decided rather than searched."""
    c = _candidate_at(strike, days)
    sl = ExpirySlice(
        expiry=_expiry(days),
        dte=days,
        forward=None,
        atm_iv=SIGMA,
        candidates=[c],
        clock=clock,
    )
    book = {} if bid is None else {c.symbol: Book(c.symbol, bid, ask, 5, 5)}
    return sl, book


def _ranked(*pairs, params=None) -> ChainResult:
    books: dict[str, Book] = {}
    for _, b in pairs:
        books.update(b)
    return apply_quotes(_multi(*[sl for sl, _ in pairs]), books, params)


class TestPerSliceMatch:
    """The pooling regression.

    apply_quotes used to flatten every slice and call select_by_delta once. With more
    than one tenor in the pool roughly one strike per expiry sits near the target, so
    the single winner was decided by strike-grid rounding — an arbitrary tenor presented
    as *the* match.
    """

    strikes = [660.0 + float(i) for i in range(86)]

    def _quoted(self, *days: int) -> ChainResult:
        chain = _multi(*[_slice(d, self.strikes) for d in days])
        return apply_quotes(chain, _books(chain, 2.0, 2.05))

    def test_every_tenor_gets_its_own_comparable_contract(self):
        out = self._quoted(20, 45, 60)
        assert len(out.slices) == 3
        assert len(out.match_symbols) == 3

    def test_the_matches_are_three_different_contracts(self):
        assert len(set(self._quoted(20, 45, 60).match_symbols)) == 3

    def test_each_match_belongs_to_the_slice_that_owns_it(self):
        for sl in self._quoted(20, 45, 60).slices:
            assert sl.match.candidate.symbol in {c.symbol for c in sl.candidates}
            assert sl.match.candidate.dte == sl.dte

    def test_every_tenor_is_compared_at_the_same_delta(self):
        # Constant delta is the whole comparison: a 0.30 weekly against a 0.15 monthly
        # measures moneyness wearing a yield costume.
        lo, hi = settings.delta_band
        for sl in self._quoted(20, 45, 60).slices:
            assert lo <= sl.match.candidate.delta <= hi, sl.expiry
            assert sl.match.off_band is False

    def test_a_moved_target_moves_every_tenor_not_just_one(self):
        base = self._quoted(20, 45, 60)
        chain = _multi(*[_slice(d, self.strikes) for d in (20, 45, 60)])
        moved = apply_quotes(
            chain,
            _books(chain, 2.0, 2.05),
            replace(defaults(), delta_target=0.28, delta_band=(0.25, 0.30)),
        )
        assert len(moved.match_symbols) == 3
        assert set(moved.match_symbols).isdisjoint(base.match_symbols)


class TestRankForQuotes:
    """Stage 2 of the funnel: rank the whole universe from what stage 1 already paid
    for, so only `max_quoted_contracts` ever reach the concurrent-subscription ceiling.
    Nothing here may need a bid — `ContractCalc` carries none."""

    strikes = [float(k) for k in range(640, 750, 5)]

    def _thirteen(self) -> list[ExpirySlice]:
        return [_slice(d, self.strikes) for d in range(7, 59, 4)]

    def test_ranks_with_no_quote_anywhere(self):
        slices = self._thirteen()
        assert all(c.spread.mid is None for sl in slices for c in sl.candidates)
        assert rank_for_quotes(slices, WIDE)

    def test_subscribes_the_ceiling_rather_than_the_universe(self):
        slices = self._thirteen()
        universe = sum(len(sl.candidates) for sl in slices)
        got = rank_for_quotes(slices, WIDE)
        assert universe > 4 * settings.max_quoted_contracts
        assert len(got) == settings.max_quoted_contracts

    def test_every_tenor_keeps_its_own_comparable_contract(self):
        # Otherwise a thin tenor reaches the term-structure table with no quote at all,
        # and the comparison the page exists for rests on model prices.
        slices = self._thirteen()
        got = set(rank_for_quotes(slices, WIDE))
        for sl in slices:
            nearest = min(
                sl.candidates, key=lambda c: abs(c.delta - WIDE.delta_target)
            )
            assert nearest.symbol in got, sl.expiry

    def test_no_symbol_is_subscribed_twice(self):
        got = rank_for_quotes(self._thirteen(), WIDE)
        assert len(set(got)) == len(got)

    def test_a_ceiling_below_the_tenor_count_still_honours_it(self):
        assert len(rank_for_quotes(self._thirteen(), WIDE, limit=3)) == 3

    def test_a_zero_ceiling_subscribes_nothing(self):
        assert rank_for_quotes(self._thirteen(), WIDE, limit=0) == []

    def test_tradeable_contracts_outrank_context_rows(self):
        # Near-money rows carry the highest model premium, but they are outside the
        # trading band: context, not candidates.
        slices = [_slice(45, self.strikes)]
        by_symbol = {c.symbol: c for c in slices[0].candidates}
        flags = [by_symbol[s].in_delta_band for s in rank_for_quotes(slices, limit=40)]
        assert flags == sorted(flags, reverse=True), "a context row outranked a candidate"

    def test_the_fill_is_ordered_by_the_model_premium(self):
        # There is no market premium to rank on yet. The single reserved slot is exempt:
        # it is held for the tenor's comparable contract whatever its rank.
        slices = [_slice(45, self.strikes)]
        by_symbol = {c.symbol: c for c in slices[0].candidates}
        got = rank_for_quotes(slices, limit=40)
        fill = [
            by_symbol[s].model_annualized_pct
            for s in got[1:]
            if by_symbol[s].in_delta_band
        ]
        assert fill == sorted(fill, reverse=True)

    def test_a_slice_with_no_greeks_is_skipped_not_fatal(self):
        empty = ExpirySlice(
            expiry=_expiry(30), dte=30, forward=None, atm_iv=None, candidates=[]
        )
        got = rank_for_quotes([empty] + self._thirteen(), WIDE)
        assert len(got) == settings.max_quoted_contracts


class TestBestTenor:
    """Which tenor pays best, and every reason the highest number may not be the pick."""

    # A tie that is exact in binary: 1.00 at 20 DTE and 2.00 at 40 DTE on a 512 strike
    # both annualize to 3.564453125. On a 700 strike the two differ in the last bits and
    # the tie-break would never be exercised.
    TIE = 512.0
    TIGHT = 0.015625  # half-spread of 1/64: liquid, and exact in binary

    def _tie_book(self, mid: float):
        return mid - self.TIGHT, mid + self.TIGHT

    def test_the_highest_annualized_at_comparable_delta_wins(self):
        lo, hi = self._tie_book(2.0)
        out = _ranked(_one(20, self.TIE, lo, hi), _one(40, self.TIE, lo, hi))
        assert out.match.candidate.dte == 20

    def test_a_tie_goes_to_the_longer_tenor(self):
        # Same nominal yield, fewer rolls, so fewer spreads paid.
        out = _ranked(
            _one(20, self.TIE, *self._tie_book(1.0)),
            _one(40, self.TIE, *self._tie_book(2.0)),
        )
        anns = sorted(sl.match.candidate.premium.annualized_pct for sl in out.slices)
        assert anns[0] == anns[1], "the tie is not exact; the tie-break is untested"
        assert out.match.candidate.dte == 40

    def test_an_illiquid_leader_loses_to_a_fillable_tenor(self):
        # A spread nobody can cross erases the annualized edge on the round trip, so
        # liquidity is a filter and not a tie-break.
        out = _ranked(_one(20, 700.0, 1.0, 3.0), _one(40, 700.0, 1.98, 2.02))
        assert out.match.candidate.dte == 40

    def test_nothing_liquid_says_the_pick_is_unfillable(self):
        out = _ranked(_one(20, 700.0, 1.0, 3.0), _one(40, 700.0, 1.0, 3.0))
        assert any("unfillable mid" in w for w in out.warnings), out.warnings
        assert "none liquid" in out.best_tenor_reason

    def test_a_model_figure_never_wins_the_ranking(self):
        # An unsubscribed contract can be shown and ranked for subscription, but it has
        # no market mid, so it cannot become the chain's pick.
        dark = _one(20, 745.0, None, None)  # near the money, large model premium
        out = _ranked(dark, _one(40, 700.0, 1.98, 2.02))
        assert out.match.candidate is not None
        assert out.match.candidate.dte == 40
        by_expiry = {sl.expiry: sl for sl in out.slices}
        unquoted = by_expiry[_expiry(20)]
        assert unquoted.match.candidate is None
        assert (
            unquoted.candidates[0].model_annualized_pct
            > out.match.candidate.premium.annualized_pct
        )

    def test_a_stale_challenger_inside_the_gate_keeps_the_live_tenor(self):
        # The two figures are quotes of different ages, so a win narrower than the
        # liquidity gate's own width is not evidence of anything.
        out = _ranked(
            _one(20, self.TIE, *self._tie_book(1.0)),  # 3.5645 annualized
            _one(40, self.TIE, 1.99, 2.03, clock=SLOW),  # 3.5822, +0.5%
        )
        assert out.match.candidate.dte == 20
        assert any("keeping the live tenor" in w for w in out.warnings), out.warnings

    def test_a_challenger_that_clears_the_gate_takes_the_pick(self):
        out = _ranked(
            _one(20, self.TIE, *self._tie_book(1.0)),  # 3.5645 annualized
            _one(40, self.TIE, 2.98, 3.02, clock=SLOW),  # 5.35
        )
        assert out.match.candidate.dte == 40
        assert not any("keeping the live tenor" in w for w in out.warnings)

    def test_one_tenor_states_no_criterion(self):
        out = _ranked(_one(45, 700.0, 1.98, 2.02))
        assert out.match.candidate is not None
        assert out.best_tenor_reason == ""

    def test_the_reason_names_the_convention_it_ranked_on(self):
        out = _ranked(_one(20, 700.0, 1.98, 2.02), _one(40, 700.0, 1.98, 2.02))
        assert "comparable delta" in out.best_tenor_reason
        assert "2 of 2" in out.best_tenor_reason

    def test_no_quotes_at_all_leaves_the_page_a_reason(self):
        out = _ranked(_one(20, 700.0, None, None), _one(40, 700.0, None, None))
        assert out.match.candidate is None
        assert any("no two-sided quotes" in w for w in out.warnings)


class TestRenderOrdering:
    def test_best_symbol_is_the_chains_own_pick(self):
        out = _ranked(_one(20, 700.0, 1.98, 2.02), _one(40, 700.0, 1.98, 2.02))
        assert out.best_symbol == out.match.candidate.symbol
        assert out.best_symbol in out.match_symbols

    def test_an_unquoted_tenor_contributes_no_match_symbol(self):
        out = _ranked(_one(20, 700.0, 1.98, 2.02), _one(40, 700.0, None, None))
        assert len(out.match_symbols) == 1

    def test_a_chain_with_no_pick_names_no_best_symbol(self):
        assert _multi(_slice(45, [700.0])).best_symbol is None

    def test_candidates_are_ordered_by_rank_across_tenors(self):
        # The reader's question is "which contract", and DTE order buries the answer.
        chain = _multi(_slice(20, [700.0, 715.0]), _slice(45, [690.0, 703.0]))
        out = apply_quotes(chain, _books(chain, 2.0, 2.05))
        rows = out.ranked_candidates
        assert len(rows) == 4
        assert len({c.dte for c in rows}) == 2
        flags = [c.in_delta_band for c in rows]
        assert flags == sorted(flags, reverse=True), "context row above a candidate"
        band = [c.premium.annualized_pct for c in rows if c.in_delta_band]
        assert band == sorted(band, reverse=True)

    def test_an_unquoted_row_is_ranked_on_its_model_figure(self):
        # It still appears — the full screen band of every expiry is context — but it is
        # ranked on the only number it has.
        chain = _multi(_slice(45, [690.0, 700.0]))
        rows = chain.ranked_candidates
        assert all(c.premium.annualized_pct is None for c in rows)
        assert [c.strike for c in rows] == [700.0, 690.0]


class FakeChainClient:
    """Enough of LongportClient for build_chain, counting every billed call."""

    def __init__(self, expiries, grid=None, iv: float = SIGMA) -> None:
        self.expiries = list(expiries)
        self.grid = grid or [float(k) for k in range(600, 800, 5)]
        self.iv = iv
        self.strike_calls: list[date] = []
        self.calc_calls: list[list[str]] = []

    async def aoption_expiries(self, ticker):
        return list(self.expiries)

    async def astrikes(self, ticker, expiry):
        self.strike_calls.append(expiry)
        tag = expiry.isoformat()
        return [
            StrikeRow(k, f"{ticker}{tag}C{int(k)}", f"{ticker}{tag}P{int(k)}", True)
            for k in self.grid
        ]

    async def adepth(self, symbol):
        # Two-sided on both ATM legs, so the parity forward exists.
        return Book(symbol, 3.00, 3.10, 5, 5)

    async def acalc_indexes(self, symbols):
        self.calc_calls.append(list(symbols))
        return {
            s: ContractCalc(s, self.iv, None, None, None, None, 1000, None, None, None)
            for s in symbols
        }


class TestBuildChainAcrossExpiries:
    async def test_prices_every_expiry_in_the_window(self):
        c = FakeChainClient([_expiry(d) for d in (7, 20, 41, 48, 60, 90)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        assert chain.expiries == [_expiry(41), _expiry(48), _expiry(60)]
        assert all(sl.candidates for sl in chain.slices)

    async def test_stage_one_subscribes_and_quotes_nothing(self):
        # The whole window is priced and ranked before anything reaches the 500-symbol
        # concurrent ceiling. The fake has no subscribe method at all, so a call would
        # raise rather than pass quietly.
        c = FakeChainClient([_expiry(41), _expiry(48)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        assert chain.match is None
        assert all(cand.spread.mid is None for cand in chain.all_candidates)
        assert all(cand.model_annualized_pct is not None for cand in chain.all_candidates)

    async def test_each_slice_stays_inside_the_per_expiry_symbol_ceiling(self):
        c = FakeChainClient([_expiry(41), _expiry(48)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        for sl in chain.slices:
            # 3 for the ATM probe, then one unit per requested strike.
            assert 4 <= sl.quota_spent <= 3 + MAX_STRIKES_PER_EXPIRY
            assert sl.clock == FAST

    async def test_a_window_that_straddles_thirty_interpolates_for_free(self):
        c = FakeChainClient([_expiry(20), _expiry(45)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW, params=WIDE)
        assert c.strike_calls == [_expiry(20), _expiry(45)]
        assert chain.atm_iv_30d == pytest.approx(SIGMA)
        assert not any("clamped" in w for w in chain.warnings)
        assert chain.quota_spent == sum(sl.quota_spent for sl in chain.slices)

    async def test_a_window_that_misses_thirty_buys_one_unit(self):
        c = FakeChainClient([_expiry(d) for d in (20, 41, 48, 60)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        assert _expiry(20) in c.strike_calls, "no bracketing partner fetched"
        assert not any("clamped" in w for w in chain.warnings)
        assert chain.quota_spent == sum(sl.quota_spent for sl in chain.slices) + 1

    async def test_no_partner_says_the_thirty_day_figure_is_clamped(self):
        c = FakeChainClient([_expiry(41), _expiry(48), _expiry(60)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        assert any("clamped, not interpolated" in w for w in chain.warnings)

    async def test_only_reprices_the_named_expiries(self):
        c = FakeChainClient([_expiry(20), _expiry(45)])
        first = await build_chain(c, "SPY", SPOT, R, now=NOW, params=WIDE)
        c.strike_calls.clear()

        again = await build_chain(
            c, "SPY", SPOT, R, now=NOW, params=WIDE, previous=first, only=[_expiry(45)]
        )
        assert c.strike_calls == [_expiry(45)]
        # The untouched tenor is the SAME object, so it keeps its own built_at and the
        # page can state that row's real age instead of the newest one's.
        assert again.slices[0] is first.slices[0]
        assert again.slices[1] is not first.slices[1]
        assert again.quota_spent == again.slices[1].quota_spent

    async def test_an_empty_due_set_bills_nothing(self):
        c = FakeChainClient([_expiry(20), _expiry(45)])
        first = await build_chain(c, "SPY", SPOT, R, now=NOW, params=WIDE)
        c.strike_calls.clear()
        again = await build_chain(
            c, "SPY", SPOT, R, now=NOW, params=WIDE, previous=first, only=[]
        )
        assert c.strike_calls == []
        assert again.quota_spent == 0
        assert again.expiries == first.expiries

    async def test_thirteen_tenors_rank_down_to_the_subscription_ceiling(self):
        # 13 tenors is what a 7-60 window holds on SPY. Their whole symbol set would
        # exceed MAX_SUBSCRIBED_SYMBOLS for two tickers and evict by ticker on every
        # render; the ranked set is flat in the width of the window instead.
        c = FakeChainClient([_expiry(d) for d in range(7, 59, 4)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW, params=WIDE)
        assert len(chain.slices) == 13
        assert len(chain.all_symbols) > 2 * settings.max_quoted_contracts
        quoted = rank_for_quotes(chain.slices, WIDE)
        assert len(quoted) == settings.max_quoted_contracts

    async def test_an_empty_window_keeps_the_single_expiry_warning(self):
        c = FakeChainClient([_expiry(20)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        p = defaults()
        assert chain.expiries == [_expiry(20)]
        assert (
            f"no expiry in the {p.dte_min}-{p.dte_max} DTE window; "
            f"using {_expiry(20)} at 20 DTE"
        ) in chain.warnings

    async def test_no_listed_options(self):
        chain = await build_chain(FakeChainClient([]), "SPY", SPOT, R, now=NOW)
        assert chain.slices == []
        assert chain.warnings == ["no listed options"]

    async def test_all_expiries_in_the_past(self):
        c = FakeChainClient([_expiry(-10)])
        chain = await build_chain(c, "SPY", SPOT, R, now=NOW)
        assert chain.warnings == ["all expiries in the past"]

