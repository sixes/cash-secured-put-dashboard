"""Settings and process-level environment setup.

Import this module before anything that touches the longport SDK: the SDK returns
naive datetimes in host-local time, so TZ must be pinned to UTC first.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path


def _force_utc() -> None:
    if os.environ.get("TZ") != "UTC":
        os.environ["TZ"] = "UTC"
    time.tzset()


_force_utc()

from dotenv import load_dotenv  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")
_force_utc()  # .env must not be able to override TZ


MARKET_TZ = "America/New_York"

LONGPORT_ENV_VARS = ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")

# Every batch quote endpoint (quote, option_quote, calc_indexes, subscribe) caps here.
MAX_SYMBOLS_PER_REQUEST = 500

# "One account can only create one long link and subscribe to a maximum of 500
# symbols at the same time." A concurrent ceiling, not a per-minute quota.
MAX_SUBSCRIBED_SYMBOLS = 500

# "the number of concurrent requests should not exceed 5". The SDK self-throttles
# the 10-calls-per-second limit, so we only police concurrency.
MAX_CONCURRENT_REQUESTS = 5


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("DASHBOARD_DB", PROJECT_ROOT / "data" / "dashboard.db")
        )
    )
    default_tickers: tuple[str, ...] = (
        "SOXL", "KORU", "TECL", "UPRO", "SPXL", "MAGX",
        "DRAM", "RAM", "USD", "MSFU", "BNO",
        "TQQQ", "ROM", "QLD", "WMT", "QQQ", "QQQM", "SPY",
        "SPYM", "PSI"
    )

    # Daily bars. 1000 is the hard API ceiling (~4 years); 2000 fails with 301607
    # "request too many klines". Needed so the drawdown high covers a real cycle,
    # not just the 300 sessions SMA200 requires.
    candle_lookback: int = 1000

    dte_min: int = 35
    dte_max: int = 60

    delta_target: float = 0.175
    delta_band: tuple[float, float] = (0.15, 0.20)
    # Wider than the display band so the table shows context around the match.
    delta_screen_band: tuple[float, float] = (0.10, 0.30)

    rv_window: int = 20
    sma_window: int = 200

    # Price chart. 756 sessions (~3y) is drawn, but the running high and the SMA are
    # computed over the WHOLE cached series and then windowed, so the staircase enters
    # the frame at the real prior high instead of resetting to the window's own max.
    chart_window_sessions: int = 756
    chart_width: int = 720
    chart_height: int = 180

    # The chart's SMA200 comes from Yahoo's auto-adjusted closes, the card's from
    # Longbridge's forward-adjusted ones. Two bases agreeing to within this much is
    # confirmation; a wider gap is surfaced as a badge, not averaged away.
    sma_cross_source_tolerance_pct: float = 0.5

    # Liquidity gate: absolute OR relative, because percent-of-mid alone unfairly
    # rejects cheap options already sitting at the minimum legal tick.
    max_abs_spread: float = 0.05
    max_rel_spread_pct: float = 2.0

    # Greeks poll interval. Prices arrive by push and are NOT on this timer.
    # Budgeted against the measured 500-option-symbols-per-minute quota: a chain
    # rebuild costs ~28 units, so 12 tickers at 60s is ~336 units/min, inside the
    # 450 working limit. At 30s the same 12 tickers would need ~672 and stall.
    greeks_poll_seconds: float = field(
        default_factory=lambda: _env_float("GREEKS_POLL_SECONDS", 60.0)
    )

    # Comparison tenors run on their own, slower clock. Only the best tenor is re-priced
    # every greeks_poll_seconds; every other expiry in the DTE window waits this long.
    # A 13-expiry window then costs 28 + 12*28/5 = ~95 units/min per ticker instead of
    # 364, which is what makes an uncapped window fit inside the 450 working limit.
    slow_tenor_poll_seconds: float = field(
        default_factory=lambda: _env_float("SLOW_TENOR_POLL_SECONDS", 300.0)
    )

    # Contracts held on the free push stream. The chain is built and ranked with NO
    # subscriptions, then only the top-ranked contracts are subscribed, so pressure on
    # the 500-symbol CONCURRENT ceiling stops scaling with the width of the DTE window:
    # 13 expiries x 24 strikes would be 312 symbols for one ticker.
    max_quoted_contracts: int = field(
        default_factory=lambda: _env_int("MAX_QUOTED_CONTRACTS", 50)
    )
    sse_max_msgs_per_sec: float = field(
        default_factory=lambda: _env_float("SSE_MAX_MSGS_PER_SEC", 2.0)
    )
    max_active_tickers: int = field(default_factory=lambda: _env_int("MAX_ACTIVE_TICKERS", 12))

    # IV Rank below this sample count is too thin to display un-caveated.
    min_iv_history_days: int = 120

    def missing_credentials(self) -> list[str]:
        return [name for name in LONGPORT_ENV_VARS if not os.environ.get(name)]


settings = Settings()
