"""Guards against the chain table and its documentation drifting apart.

The point of `app/columns.py` is that a column cannot be added, removed or re-scaled
without the documentation following. These tests are what makes that true.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.columns import (
    MISSING_NOTE,
    POLL,
    PUSH,
    SLOW,
    STATIC,
    UNQUOTED_NOTE,
    as_json,
    chain_columns,
    row_states,
)
from app.main import _conventions

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "templates" / "_options.html"


def _chain_table() -> str:
    """The candidates table only.

    The panel also renders a term-structure table above it, whose columns are per-tenor
    summaries rather than per-contract cells and are documented in its own <caption> and
    tooltips. Splitting on the first <tbody> would silently check the wrong table.
    """
    html = TEMPLATE.read_text()
    return html.split('<table class="chain">', 1)[1].split("</table>", 1)[0]


def _body_row() -> str:
    """The single <tr> inside <tbody>, where one <td> is one column."""
    return _chain_table().split("<tbody>", 1)[1].split("</tbody>", 1)[0]


class TestCoversTheTable:
    def test_one_column_per_cell_in_the_rendered_row(self):
        cells = re.findall(r"<td\b", _body_row())
        assert len(cells) == len(chain_columns())

    def test_every_live_patched_cell_is_a_known_column(self):
        # A data-f name the reference does not know would be marked with the wrong clock.
        keys = {c.key for c in chain_columns()}
        for name in re.findall(r'data-f="([^"]+)"', _body_row()):
            assert name in keys, name

    def test_every_live_patched_cell_is_documented_as_push(self):
        by_key = {c.key: c for c in chain_columns()}
        for name in re.findall(r'data-f="([^"]+)"', _body_row()):
            assert by_key[name].updates == PUSH, name

    def test_the_header_row_is_rendered_from_the_tuple(self):
        head = _chain_table().split("<thead>", 1)[1].split("</thead>", 1)[0]
        assert "chain_columns" in head
        # No literal header text left behind to go stale.
        assert "<th>Strike</th>" not in head


class TestEntriesAreComplete:
    def test_keys_are_unique(self):
        keys = [c.key for c in chain_columns()]
        assert len(set(keys)) == len(keys)

    def test_every_column_states_a_meaning_and_a_calculation(self):
        for c in chain_columns():
            assert c.meaning.strip(), c.key
            assert c.formula.strip(), c.key
            assert c.label.strip(), c.key

    def test_updates_is_one_of_the_known_clocks(self):
        for c in chain_columns():
            assert c.updates in {PUSH, POLL, SLOW, STATIC}, c.key

    def test_the_age_cell_is_the_one_on_the_slow_clock(self):
        # A comparison tenor's greeks are minutes old; the cell that says so cannot itself
        # be documented as if it refreshed with the best tenor.
        slow = [c.key for c in chain_columns() if c.updates == SLOW]
        assert slow == ["age"]

    def test_only_the_flags_column_has_an_empty_header(self):
        blank = [c.key for c in chain_columns() if not c.header]
        assert blank == ["flags"]

    def test_thresholds_come_from_the_parameters_not_literals(self):
        from dataclasses import replace

        from app.params import defaults

        p = replace(defaults(), max_abs_spread=0.07, max_rel_spread_pct=0.5)
        flags = next(c for c in chain_columns(p) if c.key == "flags")
        assert "0.07" in flags.formula
        assert "0.5" in flags.formula
        # The startup values must be gone, or a tooltip would describe a gate this
        # request is not using.
        baseline = next(c for c in chain_columns() if c.key == "flags")
        assert baseline.formula != flags.formula

    def test_the_delta_band_follows_the_parameters(self):
        from dataclasses import replace

        from app.params import defaults

        p = replace(defaults(), delta_target=0.25, delta_band=(0.2, 0.3))
        delta = next(c for c in chain_columns(p) if c.key == "delta")
        assert "0.25" in delta.meaning
        assert "0.2-0.3" in delta.meaning


class TestApiSurface:
    def test_conventions_exposes_every_column(self):
        conv = _conventions()
        assert [c["key"] for c in conv["columns"]] == [c.key for c in chain_columns()]

    def test_concept_level_conventions_survive(self):
        # Cross-column rules that no single column owns must not be displaced.
        conv = _conventions()
        for key in (
            "greeks",
            "iv_vs_rv",
            "iv_rank",
            "liquidity",
            "dte",
            "chart",
            "tenor_comparison",
            "best_tenor",
            "annualized",
            "gamma_per_premium",
            "clocks",
            "model_vs_market",
        ):
            assert conv[key]

    def test_row_states_and_missing_note_are_exposed(self):
        conv = _conventions()
        assert set(conv["row_states"]) == {name for name, _ in row_states()}
        assert conv["missing_values"] == MISSING_NOTE
        assert conv["unquoted_rows"] == UNQUOTED_NOTE

    def test_keys_join_to_the_candidate_payload(self):
        # These keys must match _chain_json's candidate fields, or the documentation
        # cannot be joined to the data it describes.
        keys = {c["key"] for c in as_json()}
        for field in (
            "expiry",
            "strike",
            "iv",
            "delta",
            "moneyness_pct",
            "open_interest",
            "vega_contract",
            "theta_day_contract",
            "bid",
            "ask",
            "mid",
            "rel_spread_pct",
            "premium_pct",
            "annualized_pct",
            "model_annualized_pct",
            "gamma_per_premium",
        ):
            assert field in keys, field
