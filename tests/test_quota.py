import threading
import time

import pytest

from app.providers.longport_client import is_option_symbol
from app.providers.quota import (
    MEASURED_LIMIT,
    OptionQuotaGovernor,
    is_option_quota_error,
)


# --- symbol classification ---------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["SPY260911P710000.US", "SPY260911C749000.US", "AAPL260918P300000.US"],
)
def test_option_symbols_recognized(symbol):
    assert is_option_symbol(symbol)


@pytest.mark.parametrize("symbol", ["SPY.US", "QQQ.US", "700.HK", "BRK.B.US"])
def test_equity_symbols_are_not_options(symbol):
    assert not is_option_symbol(symbol)


# --- error classification ----------------------------------------------------


def test_recognizes_the_option_quota_error():
    exc = RuntimeError(
        "OpenApiException: (kind=ErrorKind.OpenApi, code=301607, trace_id=) "
        "Too many option securities request within one minute"
    )
    assert is_option_quota_error(exc)


def test_other_301607_variants_are_not_the_option_quota():
    # Same code, different cause: too many klines in one request.
    assert not is_option_quota_error(RuntimeError("code=301607 request too many klines"))
    assert not is_option_quota_error(RuntimeError("code=429 rate limited"))


# --- budget accounting -------------------------------------------------------


def test_limit_sits_below_the_measured_cliff():
    g = OptionQuotaGovernor()
    assert g.limit < MEASURED_LIMIT


def test_spends_and_reports_budget():
    g = OptionQuotaGovernor(limit=100, window=60.0)
    assert g.available() == 100
    assert g.acquire(30)
    assert g.spent() == 30
    assert g.available() == 70


def test_fills_exactly_to_the_limit():
    g = OptionQuotaGovernor(limit=100, window=60.0)
    assert g.acquire(60)
    assert g.acquire(40)
    assert g.available() == 0


def test_blocks_when_the_window_is_full():
    g = OptionQuotaGovernor(limit=100, window=60.0)
    g.acquire(100)
    assert not g.acquire(1, timeout=0.2)  # would exceed; times out rather than tripping


def test_budget_frees_as_the_window_slides():
    g = OptionQuotaGovernor(limit=50, window=0.5)
    g.acquire(50)
    assert g.available() == 0
    time.sleep(0.6)
    assert g.available() == 50
    assert g.acquire(50)


def test_zero_and_negative_costs_are_free():
    g = OptionQuotaGovernor(limit=10, window=60.0)
    assert g.acquire(0)
    assert g.acquire(-5)
    assert g.spent() == 0


def test_oversized_cost_is_clamped_not_deadlocked():
    g = OptionQuotaGovernor(limit=50, window=60.0)
    assert g.acquire(5000, timeout=1.0)  # clamped to the limit
    assert g.spent() == 50


# --- penalty -----------------------------------------------------------------


def test_penalize_blocks_further_spending():
    g = OptionQuotaGovernor(limit=100, window=60.0)
    g.penalize()
    assert g.blocked_for() > 60.0
    assert not g.acquire(1, timeout=0.2)


def test_penalize_marks_the_window_exhausted():
    g = OptionQuotaGovernor(limit=100, window=60.0)
    g.acquire(10)
    g.penalize()
    assert g.available() == 0


# --- concurrency -------------------------------------------------------------


def test_never_exceeds_the_limit_under_concurrent_load():
    """The real failure mode: parallel chain refreshes racing on the same budget."""
    g = OptionQuotaGovernor(limit=100, window=60.0)
    granted = []
    lock = threading.Lock()

    def worker():
        if g.acquire(10, timeout=0.3):
            with lock:
                granted.append(10)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(granted) == 100  # exactly the budget, no more
    assert g.spent() == 100
