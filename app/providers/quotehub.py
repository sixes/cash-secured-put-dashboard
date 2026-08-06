"""Push-driven market state. The one place live bid/ask/last lives.

Two clocks, one store. Prices arrive by push on an SDK thread and cost nothing;
greeks are polled on a slow timer and cost option quota (see quota.py). This module
owns only the free half.

Threading: `set_on_quote` / `set_on_depth` fire on an SDK-owned thread, never the
asyncio loop. So `_states` is guarded by a plain `threading.Lock`, and listeners are
woken through `loop.call_soon_threadsafe`. Nothing here may await.

Seeding is mandatory, not an optimisation: longport 3.0.18's `subscribe()` has no
first-push flag, so a symbol that does not tick stays invisible forever. Outside
market hours that is every symbol.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.providers.longport_client import Book, LongportClient, is_option_symbol

log = logging.getLogger(__name__)

# A link with no traffic for this long is treated as suspect rather than current.
# Well above the ~10s heartbeat of a liquid book, below a human's patience.
STALE_AFTER_SECONDS = 45.0


@dataclass(frozen=True)
class MarketState:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    prev_close: float | None = None
    ts: datetime | None = None
    # Wall clock of the last local mutation, for staleness. Distinct from `ts`, which
    # is the exchange's own timestamp and can lag or repeat.
    updated_at: float = 0.0
    version: int = 0
    seeded: bool = False

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        if self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def two_sided(self) -> bool:
        return self.mid is not None


class QuoteHub:
    """Symbol -> MarketState, fed by push callbacks."""

    def __init__(self, client: LongportClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._states: dict[str, MarketState] = {}
        self._version = 0
        self._last_push_at = 0.0
        self._listeners: set[asyncio.Event] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._attached = False

    # --- lifecycle ------------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Register push callbacks. Call once, from the asyncio thread."""
        if self._attached:
            return
        self._loop = loop or asyncio.get_event_loop()
        self._client.register_callbacks(on_quote=self._on_quote, on_depth=self._on_depth)
        self._attached = True

    # --- push side (SDK thread) ----------------------------------------------

    def _on_quote(self, symbol: str, event: Any) -> None:
        # PushQuote carries no symbol of its own and no prev_close; the symbol is this
        # argument, and prev_close survives from the REST seed.
        self._apply(
            symbol,
            last=_f(getattr(event, "last_done", None)),
            ts=_utc(getattr(event, "timestamp", None)),
        )

    def _on_depth(self, symbol: str, event: Any) -> None:
        bid = _top(getattr(event, "bids", None))
        ask = _top(getattr(event, "asks", None))
        self._apply(
            symbol,
            bid=bid[0],
            bid_size=bid[1],
            ask=ask[0],
            ask_size=ask[1],
        )

    def _apply(self, symbol: str, **fields: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self._version += 1
            self._last_push_at = now
            cur = self._states.get(symbol) or MarketState(symbol)
            # A push that omits a field must not blank the previously known value:
            # depth and quote pushes each carry only their own half of the state.
            keep = {k: v for k, v in fields.items() if v is not None}
            self._states[symbol] = replace(
                cur, **keep, updated_at=now, version=self._version, seeded=True
            )
        self._wake()

    def _wake(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for ev in list(self._listeners):
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                # Loop already closed; a shutdown race, not an error worth raising.
                pass

    # --- read side (any thread) ----------------------------------------------

    def get(self, symbol: str) -> MarketState | None:
        with self._lock:
            return self._states.get(symbol)

    def snapshot(self, symbols: Iterable[str] | None = None) -> dict[str, MarketState]:
        with self._lock:
            if symbols is None:
                return dict(self._states)
            return {s: self._states[s] for s in symbols if s in self._states}

    def books(self, symbols: Iterable[str]) -> dict[str, Book]:
        """Adapter for chain.apply_quotes, which speaks Book."""
        return {
            s: Book(s, st.bid, st.ask, st.bid_size, st.ask_size)
            for s, st in self.snapshot(symbols).items()
        }

    def version(self) -> int:
        with self._lock:
            return self._version

    def seconds_since_push(self) -> float | None:
        with self._lock:
            if self._last_push_at == 0.0:
                return None
            return time.monotonic() - self._last_push_at

    def is_stale(self, threshold: float = STALE_AFTER_SECONDS) -> bool:
        """True when the link has gone quiet.

        Outside trading hours a quiet book is normal, so callers must combine this
        with market-session state before showing a warning.
        """
        gap = self.seconds_since_push()
        return gap is None or gap > threshold

    def drop(self, symbols: Iterable[str]) -> None:
        with self._lock:
            for s in symbols:
                self._states.pop(s, None)

    # --- seeding --------------------------------------------------------------

    def seed(self, symbols: Sequence[str]) -> int:
        """Fill state for symbols that have not ticked. Returns quota units spent.

        Costs 1 option-quota unit per option symbol, so it runs only for symbols the
        push feed has not already covered. During active trading that is usually none.
        """
        underlyings = [s for s in symbols if not is_option_symbol(s)]
        options = [s for s in symbols if is_option_symbol(s)]
        spent = 0

        if underlyings:
            try:
                for sym, spot in self._client.quote(underlyings).items():
                    self._apply_seed(sym, last=spot.last, prev_close=spot.prev_close, ts=spot.ts)
            except Exception as exc:  # noqa: BLE001
                log.warning("seed quote failed: %s", exc)

        for sym in options:
            if self._has_book(sym):
                continue
            book = self._cached_book(sym)
            if book is None:
                try:
                    book = self._client.depth(sym)
                    spent += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("seed depth failed for %s: %s", sym, exc)
                    continue
            self._apply_seed(
                sym, bid=book.bid, ask=book.ask, bid_size=book.bid_size, ask_size=book.ask_size
            )
        return spent

    def _cached_book(self, symbol: str) -> Book | None:
        """The SDK's own push cache. Free, but empty until the symbol ticks."""
        try:
            book = self._client.realtime_depth(symbol)
        except Exception:  # noqa: BLE001
            return None
        return book if (book.bid is not None or book.ask is not None) else None

    def _has_book(self, symbol: str) -> bool:
        st = self.get(symbol)
        return st is not None and (st.bid is not None or st.ask is not None)

    def _apply_seed(self, symbol: str, **fields: Any) -> None:
        """Like _apply, but does not count as push traffic for staleness purposes."""
        with self._lock:
            self._version += 1
            cur = self._states.get(symbol) or MarketState(symbol)
            keep = {k: v for k, v in fields.items() if v is not None}
            self._states[symbol] = replace(
                cur, **keep, updated_at=time.monotonic(), version=self._version, seeded=True
            )
        self._wake()

    # --- update notification --------------------------------------------------

    def listener(self) -> "HubListener":
        return HubListener(self)

    def _register(self, ev: asyncio.Event) -> None:
        self._listeners.add(ev)

    def _unregister(self, ev: asyncio.Event) -> None:
        self._listeners.discard(ev)


class HubListener:
    """Wakes on any hub mutation. Coalescing is the caller's job."""

    def __init__(self, hub: QuoteHub) -> None:
        self._hub = hub
        self._event = asyncio.Event()

    def __enter__(self) -> "HubListener":
        self._hub._register(self._event)
        return self

    def __exit__(self, *exc: object) -> None:
        self._hub._unregister(self._event)

    async def wait(self, timeout: float | None = None) -> bool:
        """True if woken by an update, False on timeout.

        A timeout is not a failure: it is the heartbeat that lets an SSE stream
        re-emit connection status while the market is quiet.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._event.clear()
        return True


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _top(levels: Any) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    best = min(levels, key=lambda d: getattr(d, "position", 0))
    return _f(getattr(best, "price", None)), _f(getattr(best, "volume", None))
