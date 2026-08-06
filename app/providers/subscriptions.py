"""Subscription budget: 500 concurrent symbols, evicted LRU by TICKER.

The account may hold one long link with at most 500 concurrently subscribed symbols
(documented). Each active ticker occupies its underlying plus its filtered put chain
(~7-26 symbols measured), so the real ceiling is roughly 16-20 tickers.

Eviction is by whole ticker, never by symbol. Half-evicting a ticker would leave a
page rendering some cells live and others frozen, with nothing to distinguish them.

Reconciliation matters because the server-side subscription set does NOT survive a
dropped long link, while our own book does. After a reconnect the two disagree and
only the server's view stops symbols from ticking.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from app.config import MAX_SUBSCRIBED_SYMBOLS, settings
from app.providers.longport_client import LongportClient

log = logging.getLogger(__name__)


@dataclass
class Entry:
    ticker: str
    symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BudgetState:
    tickers: list[str]
    symbols: int
    limit: int

    @property
    def headroom(self) -> int:
        return self.limit - self.symbols


class SubscriptionManager:
    def __init__(
        self,
        client: LongportClient,
        limit: int = MAX_SUBSCRIBED_SYMBOLS,
        max_tickers: int | None = None,
    ) -> None:
        self._client = client
        self._limit = limit
        self._max_tickers = max_tickers or settings.max_active_tickers
        self._lock = threading.RLock()
        # Most-recently-used last, matching OrderedDict.popitem(last=False).
        self._book: OrderedDict[str, Entry] = OrderedDict()

    # --- accounting -----------------------------------------------------------

    def _count(self) -> int:
        return len({s for e in self._book.values() for s in e.symbols})

    def state(self) -> BudgetState:
        with self._lock:
            return BudgetState(list(self._book.keys()), self._count(), self._limit)

    def symbols_for(self, ticker: str) -> list[str]:
        with self._lock:
            entry = self._book.get(ticker)
            return list(entry.symbols) if entry else []

    def active_symbols(self) -> set[str]:
        with self._lock:
            return {s for e in self._book.values() for s in e.symbols}

    # --- mutation -------------------------------------------------------------

    def activate(self, ticker: str, symbols: list[str]) -> list[str]:
        """Subscribe `ticker`'s symbol set, evicting LRU tickers to make room.

        Returns the tickers evicted. Idempotent: re-activating with an unchanged set
        only bumps recency, spending no quota, because subscribe() is billed per
        symbol and re-subscribing an existing symbol would be billed again.
        """
        wanted = _dedupe(symbols)
        evicted: list[str] = []

        with self._lock:
            existing = self._book.get(ticker)
            if existing is not None and set(existing.symbols) == set(wanted):
                self._book.move_to_end(ticker)
                return []

            self._book[ticker] = Entry(ticker, wanted)
            self._book.move_to_end(ticker)

            while self._over_budget() and len(self._book) > 1:
                victim, _ = next(iter(self._book.items()))
                if victim == ticker:
                    break
                evicted.append(victim)
                self._release(victim)

            if self._over_budget():
                # A single ticker larger than the whole budget cannot be honoured.
                del self._book[ticker]
                raise ValueError(
                    f"{ticker} needs {len(wanted)} symbols, over the {self._limit} limit"
                )

            keep = set(existing.symbols) if existing else set()
            to_add = [s for s in wanted if s not in keep]
            to_drop = [s for s in keep if s not in set(wanted)]

        if to_drop:
            self._unsubscribe(to_drop)
        if to_add:
            self._client.subscribe(to_add)
        return evicted

    def _over_budget(self) -> bool:
        return self._count() > self._limit or len(self._book) > self._max_tickers

    def release(self, ticker: str) -> bool:
        with self._lock:
            if ticker not in self._book:
                return False
            self._release(ticker)
        return True

    def _release(self, ticker: str) -> None:
        """Caller holds the lock. Unsubscribes only symbols no other ticker needs."""
        entry = self._book.pop(ticker, None)
        if entry is None:
            return
        still_needed = {s for e in self._book.values() for s in e.symbols}
        orphaned = [s for s in entry.symbols if s not in still_needed]
        if orphaned:
            self._unsubscribe(orphaned)

    def _unsubscribe(self, symbols: list[str]) -> None:
        try:
            self._client.unsubscribe(symbols)
        except Exception as exc:  # noqa: BLE001
            # A failed unsubscribe leaks server-side budget, which reconcile() fixes.
            log.warning("unsubscribe failed for %d symbols: %s", len(symbols), exc)

    def clear(self) -> None:
        with self._lock:
            for ticker in list(self._book):
                self._release(ticker)

    # --- reconnect ------------------------------------------------------------

    def reconcile(self) -> tuple[list[str], list[str]]:
        """Force the server's subscription set to match our book.

        Returns (resubscribed, removed). Run after any reconnect: the server forgets
        its subscriptions when the long link drops, so our book is the only record of
        what the open pages actually need.
        """
        with self._lock:
            ours = self.active_symbols()

        try:
            theirs = set(self._client.subscriptions())
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read server subscriptions: %s", exc)
            return [], []

        missing = sorted(ours - theirs)
        extra = sorted(theirs - ours)

        if missing:
            log.info("reconcile: resubscribing %d symbols", len(missing))
            self._client.subscribe(missing)
        if extra:
            log.info("reconcile: dropping %d stray symbols", len(extra))
            self._unsubscribe(extra)
        return missing, extra


def _dedupe(symbols: list[str]) -> list[str]:
    """Order-preserving, because the first symbols are the nearest-the-money ones."""
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
