"""The saved screening baseline. One row, JSON payload, degrades rather than breaks."""

from __future__ import annotations

import pytest

from app.params import defaults, from_query, to_dict
from app.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


class TestScreenDefaults:
    def test_absent_until_saved(self, store):
        assert store.screen_defaults() is None

    def test_a_saved_set_reads_back_intact(self, store):
        p, _ = from_query({"dte_min": "7", "dte_max": "21", "sma_window": "50"})
        store.save_screen_defaults(to_dict(p))
        saved, saved_at = store.screen_defaults()
        assert from_query(saved)[0] == p
        assert saved_at

    def test_saving_twice_replaces_rather_than_accumulates(self, store):
        store.save_screen_defaults(to_dict(defaults()))
        p, _ = from_query({"dte_min": "7"})
        store.save_screen_defaults(to_dict(p))
        saved, _ = store.screen_defaults()
        assert saved["dte_min"] == 7
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM screen_defaults")
            assert cur.fetchone()["n"] == 1

    def test_clear_returns_to_no_saved_row(self, store):
        store.save_screen_defaults(to_dict(defaults()))
        store.clear_screen_defaults()
        assert store.screen_defaults() is None

    def test_clearing_when_empty_is_not_an_error(self, store):
        store.clear_screen_defaults()
        assert store.screen_defaults() is None

    def test_unknown_keys_are_ignored_on_read(self, store):
        # A field renamed in a later version must not take the page down.
        payload = to_dict(defaults()) | {"delta_target_OLD": 0.9, "gone": 1}
        store.save_screen_defaults(payload)
        saved, _ = store.screen_defaults()
        assert from_query(saved)[0] == defaults()

    def test_corrupt_json_degrades_to_no_saved_row(self, store):
        with store._cursor() as cur:
            cur.execute(
                "INSERT INTO screen_defaults (id, params, saved_at) VALUES (1, ?, ?)",
                ("{not json", "2026-08-01"),
            )
        assert store.screen_defaults() is None

    def test_a_json_scalar_is_not_a_parameter_set(self, store):
        with store._cursor() as cur:
            cur.execute(
                "INSERT INTO screen_defaults (id, params, saved_at) VALUES (1, ?, ?)",
                ("42", "2026-08-01"),
            )
        assert store.screen_defaults() is None

    def test_a_hand_edited_out_of_range_value_degrades_with_a_message(self, store):
        store.save_screen_defaults(to_dict(defaults()) | {"sma_window": 0})
        saved, _ = store.screen_defaults()
        params, errors = from_query(saved)
        assert params.sma_window == defaults().sma_window
        assert errors
