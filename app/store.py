"""SQLite persistence: IV history, daily closes, risk-free rates, and fetch bookkeeping.

Rows carry their `source` so a series is only ever compared within one source. Mixing
raw levels across sources injects a fake jump on the changeover date — a vol jump for
IV, an adjustment-basis jump for prices.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_history (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,
    atm_iv  REAL NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (symbol, date, source)
);
CREATE INDEX IF NOT EXISTS idx_iv_history_lookup ON iv_history (symbol, source, date);

CREATE TABLE IF NOT EXISTS iv_bounds (
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    source       TEXT NOT NULL,
    atm_iv       REAL NOT NULL,
    iv_year_high REAL,
    iv_year_low  REAL,
    PRIMARY KEY (symbol, date, source)
);

CREATE TABLE IF NOT EXISTS risk_free_rate (
    date TEXT PRIMARY KEY,
    rate REAL NOT NULL
);

-- Daily closes for the price chart. `source` is part of the key because an
-- auto-adjusted close and a forward-adjusted close are different series: a chart
-- must be drawn from one of them, never stitched from both.
CREATE TABLE IF NOT EXISTS price_history (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    close  REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, date, source)
);
CREATE INDEX IF NOT EXISTS idx_price_history_lookup ON price_history (symbol, source, date);

CREATE TABLE IF NOT EXISTS fetch_log (
    key       TEXT PRIMARY KEY,
    fetched_on TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    detail    TEXT
);

-- The saved screening baseline, one row by construction. JSON rather than a column
-- per parameter so adding or renaming a field is not a migration; unknown keys are
-- ignored on read.
CREATE TABLE IF NOT EXISTS screen_defaults (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    params   TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else settings.db_path
        self._lock = threading.Lock()
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- IV history -----------------------------------------------------------

    def upsert_iv_history(self, rows: Iterable[tuple[str, str, float, str]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO iv_history (symbol, date, atm_iv, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (symbol, date, source) DO UPDATE SET atm_iv = excluded.atm_iv",
                rows,
            )
        return len(rows)

    def iv_series(self, symbol: str, source: str, limit: int = 252) -> list[tuple[str, float]]:
        """Most recent `limit` observations, returned oldest-first."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT date, atm_iv FROM iv_history WHERE symbol = ? AND source = ? "
                "ORDER BY date DESC LIMIT ?",
                (symbol, source, limit),
            )
            rows = cur.fetchall()
        return [(r["date"], r["atm_iv"]) for r in reversed(rows)]

    def iv_sources(self, symbol: str) -> list[tuple[str, int]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT source, COUNT(*) AS n FROM iv_history WHERE symbol = ? "
                "GROUP BY source ORDER BY n DESC",
                (symbol,),
            )
            return [(r["source"], r["n"]) for r in cur.fetchall()]

    def upsert_iv_bounds(
        self,
        symbol: str,
        on: str,
        source: str,
        atm_iv: float,
        year_high: float | None,
        year_low: float | None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO iv_bounds (symbol, date, source, atm_iv, iv_year_high, iv_year_low) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (symbol, date, source) DO UPDATE SET "
                "atm_iv = excluded.atm_iv, iv_year_high = excluded.iv_year_high, "
                "iv_year_low = excluded.iv_year_low",
                (symbol, on, source, atm_iv, year_high, year_low),
            )

    def latest_iv_bounds(self, symbol: str, source: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM iv_bounds WHERE symbol = ? AND source = ? ORDER BY date DESC LIMIT 1",
                (symbol, source),
            )
            return cur.fetchone()

    # --- price history --------------------------------------------------------

    def upsert_price_history(self, rows: Iterable[tuple[str, str, float, str]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO price_history (symbol, date, close, source) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (symbol, date, source) DO UPDATE SET close = excluded.close",
                rows,
            )
        return len(rows)

    def price_series(
        self, symbol: str, source: str, limit: int | None = None
    ) -> list[tuple[str, float]]:
        """Most recent `limit` closes, returned oldest-first. `None` means the whole series.

        The chart needs the full series even when it only draws a window: the running
        high is an expanding max, so a windowed read would reset it to the window's own
        maximum and understate the drawdown.
        """
        sql = (
            "SELECT date, close FROM price_history WHERE symbol = ? AND source = ? "
            "ORDER BY date DESC"
        )
        params: tuple = (symbol, source)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self._cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [(r["date"], r["close"]) for r in reversed(rows)]

    # --- risk-free rate -------------------------------------------------------

    def upsert_rates(self, rows: Sequence[tuple[str, float]]) -> int:
        if not rows:
            return 0
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO risk_free_rate (date, rate) VALUES (?, ?) "
                "ON CONFLICT (date) DO UPDATE SET rate = excluded.rate",
                rows,
            )
        return len(rows)

    def latest_rate(self) -> tuple[str, float] | None:
        with self._cursor() as cur:
            cur.execute("SELECT date, rate FROM risk_free_rate ORDER BY date DESC LIMIT 1")
            row = cur.fetchone()
        return (row["date"], row["rate"]) if row else None

    # --- fetch bookkeeping ----------------------------------------------------

    def fetched_today(self, key: str, on: date | None = None) -> bool:
        today = (on or date.today()).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "SELECT ok FROM fetch_log WHERE key = ? AND fetched_on = ?", (key, today)
            )
            row = cur.fetchone()
        return bool(row and row["ok"])

    def fetch_detail(self, key: str, on: date | None = None) -> str | None:
        """Detail of today's attempt, successful or not.

        Lets a caller tell a permanent outcome ("this dataset has no such symbol")
        from a transient one, so only the latter is retried.
        """
        today = (on or date.today()).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "SELECT detail FROM fetch_log WHERE key = ? AND fetched_on = ?", (key, today)
            )
            row = cur.fetchone()
        return row["detail"] if row else None

    def mark_fetched(
        self, key: str, ok: bool, detail: str | None = None, on: date | None = None
    ) -> None:
        today = (on or date.today()).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO fetch_log (key, fetched_on, ok, detail) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET fetched_on = excluded.fetched_on, "
                "ok = excluded.ok, detail = excluded.detail",
                (key, today, int(ok), detail),
            )

    # --- saved screening defaults ---------------------------------------------

    def save_screen_defaults(self, params: dict) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO screen_defaults (id, params, saved_at) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET params = excluded.params, "
                "saved_at = excluded.saved_at",
                (json.dumps(params, sort_keys=True), datetime.now(tz=timezone.utc).isoformat()),
            )

    def screen_defaults(self) -> tuple[dict, str] | None:
        """The saved baseline and when it was saved, or None if never saved.

        A row that will not parse is treated as absent: a corrupt saved default must
        degrade to the code baseline, not take the page down.
        """
        with self._cursor() as cur:
            cur.execute("SELECT params, saved_at FROM screen_defaults WHERE id = 1")
            row = cur.fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["params"])
        except (TypeError, ValueError):
            return None
        return (parsed, row["saved_at"]) if isinstance(parsed, dict) else None

    def clear_screen_defaults(self) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM screen_defaults WHERE id = 1")


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    global _store
    with _store_lock:
        if _store is None:
            _store = Store()
        return _store
