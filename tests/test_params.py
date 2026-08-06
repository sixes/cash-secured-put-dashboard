"""The screening parameters: parsing, bounds, cross-validation and precedence.

Pure functions, so this is the cheapest place to pin down the rule that matters most —
a bad query must degrade to an explained baseline, never to a 500.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import settings
from app.params import (
    FIELDS,
    FREE,
    REBUILD,
    TREND,
    chain_key,
    defaults,
    field_rows,
    from_query,
    groups,
    to_dict,
    to_query,
    trend_key,
)


class TestDefaults:
    def test_the_baseline_is_the_code_settings(self):
        p = defaults()
        assert p.dte_min == settings.dte_min
        assert p.dte_max == settings.dte_max
        assert p.delta_target == settings.delta_target
        assert p.delta_band == tuple(settings.delta_band)
        assert p.delta_screen_band == tuple(settings.delta_screen_band)
        assert p.sma_window == settings.sma_window
        assert p.rv_window == settings.rv_window
        assert p.chart_window_sessions == settings.chart_window_sessions

    def test_an_empty_query_changes_nothing_and_says_nothing(self):
        p, errors = from_query({})
        assert p == defaults()
        assert errors == []

    def test_a_blank_value_is_not_a_change(self):
        # An untouched <input> submits an empty string; that is not an instruction.
        p, errors = from_query({"dte_min": "", "delta_target": "  "})
        assert p == defaults()
        assert errors == []

    def test_every_field_is_covered_by_a_cost_class(self):
        assert {f.cost for f in FIELDS} == {FREE, REBUILD, TREND}


class TestParsing:
    def test_a_scalar_is_applied(self):
        p, errors = from_query({"dte_min": "7", "dte_max": "21"})
        assert (p.dte_min, p.dte_max) == (7, 21)
        assert errors == []

    def test_a_tuple_element_is_applied_without_disturbing_the_other(self):
        p, _ = from_query({"delta_screen_hi": "0.5"})
        assert p.delta_screen_band == (defaults().delta_screen_band[0], 0.5)

    def test_ints_stay_ints(self):
        p, _ = from_query({"sma_window": "50"})
        assert isinstance(p.sma_window, int)


class TestBadInputNeverRaises:
    @pytest.mark.parametrize(
        "query",
        [
            {"dte_min": "abc"},
            {"delta_target": "5"},
            {"sma_window": "0"},
            {"rv_window": "-3"},
            {"max_rel_spread_pct": "nan"},
            {"chart_window_sessions": "1e400"},
            {"delta_lo": "\x00"},
            {"dte_max": "99999999999999999999999"},
        ],
    )
    def test_it_returns_a_message_rather_than_raising(self, query):
        p, errors = from_query(query)
        assert errors, query
        assert isinstance(p.dte_min, int)

    def test_a_rejected_field_keeps_the_inherited_value(self):
        p, errors = from_query({"dte_min": "abc", "dte_max": "40"})
        assert p.dte_min == defaults().dte_min
        assert p.dte_max == 40
        assert len(errors) == 1
        assert "not a number" in errors[0]

    def test_an_out_of_range_value_names_the_bounds_it_missed(self):
        _, errors = from_query({"sma_window": "0"})
        assert "outside" in errors[0]
        assert "SMA window" in errors[0]

    def test_a_number_is_never_silently_clamped_into_range(self):
        # A clamp would show a figure the user did not ask for with no explanation.
        p, errors = from_query({"dte_max": "5000"})
        assert p.dte_max == defaults().dte_max
        assert errors

    def test_an_rv_window_past_the_bar_ceiling_is_refused(self):
        # n returns need n+1 closes, so the ceiling is one below the fetch limit.
        _, errors = from_query({"rv_window": str(settings.candle_lookback)})
        assert errors


class TestCrossValidation:
    def test_an_inverted_dte_window_reverts_the_pair(self):
        p, errors = from_query({"dte_min": "90", "dte_max": "10"})
        assert (p.dte_min, p.dte_max) == (defaults().dte_min, defaults().dte_max)
        assert "inverted" in errors[0]

    def test_an_inverted_screening_band_reverts(self):
        p, errors = from_query({"delta_screen_lo": "0.5", "delta_screen_hi": "0.2"})
        assert p.delta_screen_band == defaults().delta_screen_band
        assert any("screening" in e for e in errors)

    def test_an_inverted_trading_band_reverts(self):
        p, errors = from_query({"delta_lo": "0.4", "delta_hi": "0.2"})
        assert p.delta_band == defaults().delta_band
        assert errors

    def test_a_trading_band_outside_the_screen_widens_the_screen(self):
        # Otherwise the table could not contain a single row the band would shade.
        p, errors = from_query({"delta_lo": "0.4", "delta_hi": "0.5"})
        assert p.delta_band == (0.4, 0.5)
        assert p.delta_screen_band[0] <= 0.4 and p.delta_screen_band[1] >= 0.5
        assert any("widened" in e for e in errors)

    def test_a_target_outside_its_band_is_moved_to_the_nearest_edge(self):
        # The baseline target (0.175) sits below a band the user moved up, so reverting
        # would leave an unreachable target and a message that changed nothing.
        p, errors = from_query({"delta_lo": "0.25", "delta_hi": "0.35"})
        assert p.delta_band == (0.25, 0.35)
        assert p.delta_target == 0.25
        assert any("moved to" in e for e in errors)

    def test_a_valid_widening_needs_no_message(self):
        p, errors = from_query(
            {"delta_lo": "0.25", "delta_hi": "0.3", "delta_target": "0.28",
             "delta_screen_lo": "0.1", "delta_screen_hi": "0.5"}
        )
        assert p.delta_target == 0.28
        assert errors == []

    def test_the_result_always_satisfies_its_own_invariants(self):
        for q in (
            {"dte_min": "90", "dte_max": "10"},
            {"delta_lo": "0.4", "delta_hi": "0.5"},
            {"delta_screen_lo": "0.5", "delta_screen_hi": "0.2"},
            {"delta_target": "0.9", "delta_hi": "0.2"},
        ):
            p, _ = from_query(q)
            assert p.dte_min <= p.dte_max, q
            assert p.delta_band[0] < p.delta_band[1], q
            assert p.delta_screen_band[0] < p.delta_screen_band[1], q
            assert p.delta_band[0] >= p.delta_screen_band[0], q
            assert p.delta_band[1] <= p.delta_screen_band[1], q
            assert p.delta_band[0] <= p.delta_target <= p.delta_band[1], q


class TestQueryRoundTrip:
    def test_the_default_view_has_a_bare_url(self):
        assert to_query(defaults()) == ""

    def test_only_the_changed_fields_appear(self):
        p, _ = from_query({"delta_target": "0.16"})
        q = to_query(p)
        assert q == "delta_target=0.16"

    def test_a_query_survives_a_round_trip(self):
        p, _ = from_query({"dte_min": "7", "dte_max": "21", "sma_window": "50"})
        back, errors = from_query(dict(pair.split("=") for pair in to_query(p).split("&")))
        assert back == p
        assert errors == []

    def test_to_dict_covers_every_field_under_its_query_key(self):
        d = to_dict(defaults())
        assert set(d) == {f.name for f in FIELDS}
        assert from_query(d)[0] == defaults()


class TestPrecedence:
    def test_the_url_beats_the_saved_default(self):
        saved, _ = from_query({"dte_min": "7", "dte_max": "21"})
        p, _ = from_query({"dte_min": "14"}, saved)
        assert (p.dte_min, p.dte_max) == (14, 21)

    def test_the_saved_default_beats_the_code_default(self):
        saved, _ = from_query({"delta_target": "0.25", "delta_hi": "0.3"})
        p, _ = from_query({}, saved)
        assert p.delta_target == 0.25

    def test_a_rejected_url_value_falls_back_to_the_saved_layer(self):
        saved, _ = from_query({"sma_window": "50"})
        p, errors = from_query({"sma_window": "0"}, saved)
        assert p.sma_window == 50
        assert errors


class TestOrigin:
    def test_each_layer_is_named(self):
        saved = to_dict(replace(defaults(), sma_window=50))
        url = {"dte_min": "7"}
        rows = {r["name"]: r for r in field_rows(from_query(url, from_query(saved)[0])[0], url, saved)}
        assert rows["dte_min"]["origin"] == "url"
        assert rows["sma_window"]["origin"] == "saved"

    def test_a_saved_row_is_a_snapshot_not_a_diff(self):
        # Save persists all ten numbers, so every field reads "saved" afterwards even
        # where the value equals the code default. That is the point: a saved baseline
        # is pinned, and does not drift when a code default later changes.
        saved = to_dict(defaults())
        rows = field_rows(from_query({}, from_query(saved)[0])[0], {}, saved)
        assert {r["origin"] for r in rows} == {"saved"}

    def test_without_a_saved_row_everything_reads_code(self):
        rows = field_rows(defaults(), {}, None)
        assert {r["origin"] for r in rows} == {"code"}

    def test_a_rejected_url_value_does_not_claim_credit(self):
        # Compared by value, so the layer that actually supplied the number is named.
        url = {"sma_window": "0"}
        p, _ = from_query(url)
        rows = {r["name"]: r for r in field_rows(p, url, None)}
        assert rows["sma_window"]["origin"] == "code"


class TestCostKeys:
    def test_the_chain_key_is_exactly_the_billed_fields(self):
        base = defaults()
        for free in ("delta_target", "max_abs_spread", "max_rel_spread_pct"):
            assert chain_key(replace(base, **{free: 0.42})) == chain_key(base), free
        assert chain_key(replace(base, delta_band=(0.11, 0.12))) == chain_key(base)

        assert chain_key(replace(base, dte_min=1)) != chain_key(base)
        assert chain_key(replace(base, dte_max=99)) != chain_key(base)
        assert chain_key(replace(base, delta_screen_band=(0.05, 0.4))) != chain_key(base)

    def test_the_trend_key_is_exactly_the_window_fields(self):
        base = defaults()
        assert trend_key(replace(base, dte_min=1)) == trend_key(base)
        assert trend_key(replace(base, delta_target=0.3)) == trend_key(base)
        for window in ("sma_window", "rv_window", "chart_window_sessions"):
            assert trend_key(replace(base, **{window: 33})) != trend_key(base), window

    def test_no_field_is_billed_and_free_at_once(self):
        billed = {"dte_min", "dte_max", "delta_screen_lo", "delta_screen_hi"}
        trend = {"sma_window", "rv_window", "chart_window_sessions"}
        assert {f.name for f in FIELDS if f.cost == REBUILD} == billed
        assert {f.name for f in FIELDS if f.cost == TREND} == trend


class TestFormMetadata:
    def test_every_field_reaches_the_form(self):
        assert {r["name"] for r in field_rows(defaults())} == {f.name for f in FIELDS}

    def test_the_groups_partition_the_fields(self):
        gs = groups(defaults())
        assert [g["cost"] for g in gs] == [FREE, REBUILD, TREND]
        named = [f["name"] for g in gs for f in g["fields"]]
        assert sorted(named) == sorted(f.name for f in FIELDS)

    def test_the_billed_group_states_its_cost(self):
        billed = next(g for g in groups(defaults()) if g["cost"] == REBUILD)
        assert "28" in billed["note"]
        assert "450" in billed["note"]

    def test_the_rendered_value_is_a_valid_input_for_the_same_field(self):
        # The form's own output must parse back, or Apply would move a value it displayed.
        p = replace(defaults(), delta_target=0.175, chart_window_sessions=756)
        submitted = {r["name"]: r["value"] for r in field_rows(p)}
        back, errors = from_query(submitted)
        assert back == p
        assert errors == []
