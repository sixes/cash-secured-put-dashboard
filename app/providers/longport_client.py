"""Longbridge (LongPort) OpenAPI wrapper.

One process, one `QuoteContext` — the account is allowed a single long link. The SDK
is blocking and self-throttles the 10-calls-per-second limit, so callers use the
`a*` coroutine variants which offload to a thread pool behind a 5-permit semaphore
(the documented concurrent-request ceiling).

Normalization applied at this boundary, and nowhere else:
  - Decimal -> float
  - naive host-local datetimes -> timezone-aware UTC (TZ is pinned to UTC in config)
  - calc_indexes vega/rho -> per-share (the API returns them x100)
  - implied_volatility -> ratio (calc_indexes returns percent, OptionQuote returns ratio)

`calc_indexes.theta` is deliberately NOT exposed. Its sign flips across moneyness and
it disagrees with Black-Scholes by up to 70x; see scripts/calibrate_greeks.py. Theta
comes from app.metrics.pricing instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence, TypeVar

from app.config import MAX_CONCURRENT_REQUESTS, MAX_SYMBOLS_PER_REQUEST, settings
from app.providers.quota import get_governor, is_option_quota_error

log = logging.getLogger(__name__)

T = TypeVar("T")

# e.g. SPY260911P710000.US -> underlying, YYMMDD, C/P, strike*1000, market
_OPTION_RE = re.compile(r"^[A-Z.]+\d{6}[CP]\d+\.[A-Z]+$")


def is_option_symbol(symbol: str) -> bool:
    return bool(_OPTION_RE.match(symbol))


# Markets the API recognizes as a symbol suffix. "BRK.B" must NOT be read as market
# "B", so a suffix only counts when it is one of these.
_MARKETS = frozenset({"US", "HK", "SG", "CN", "SH", "SZ"})


def us_symbol(ticker: str) -> str:
    """Bare user input -> API symbol. 'spy' -> 'SPY.US', 'brk.b' -> 'BRK.B.US'."""
    s = ticker.strip().upper()
    if not s:
        return s
    if is_option_symbol(s):
        return s
    head, _, tail = s.rpartition(".")
    if head and tail in _MARKETS:
        return s
    return f"{s}.US"


def display_ticker(symbol: str) -> str:
    """API symbol -> what the user typed. 'BRK.B.US' -> 'BRK.B'."""
    head, _, tail = symbol.rpartition(".")
    return head if head and tail in _MARKETS else symbol


class LongportUnavailable(RuntimeError):
    """Credentials are missing, or the quote context could not be created."""


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def to_utc(value: datetime | None) -> datetime | None:
    """The SDK returns naive datetimes in host-local time; TZ is pinned to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def chunked(items: Sequence[T], size: int = MAX_SYMBOLS_PER_REQUEST) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Spot:
    symbol: str
    last: float | None
    prev_close: float | None
    ts: datetime | None
    trade_status: str | None


@dataclass(frozen=True)
class BookSide:
    price: float | None
    size: float | None


@dataclass(frozen=True)
class Book:
    symbol: str
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class StrikeRow:
    strike: float
    call_symbol: str | None
    put_symbol: str | None
    standard: bool


@dataclass(frozen=True)
class ContractCalc:
    """calc_indexes output, unit-normalized. Theta is intentionally absent."""

    symbol: str
    iv: float | None  # ratio, e.g. 0.2143
    delta: float | None  # signed, per share, unscaled by the API
    gamma: float | None
    vega: float | None  # per share per 1.00 of sigma (API value / 100)
    rho: float | None  # per share (API value / 100)
    open_interest: int | None
    last_done: float | None
    strike: float | None
    expiry: date | None


class LongportClient:
    def __init__(self) -> None:
        self._ctx: Any = None
        self._lock = threading.Lock()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._governor = get_governor()
        self._on_quote: Callable[[str, Any], None] | None = None
        self._on_depth: Callable[[str, Any], None] | None = None

    @property
    def governor(self):
        return self._governor

    # --- context lifecycle ----------------------------------------------------

    @property
    def ctx(self) -> Any:
        with self._lock:
            if self._ctx is None:
                self._ctx = self._build_ctx()
            return self._ctx

    def _build_ctx(self) -> Any:
        missing = settings.missing_credentials()
        if missing:
            raise LongportUnavailable(f"missing env vars: {', '.join(missing)}")
        from longport.openapi import Config, QuoteContext

        # longport 3.0.x exposes from_env(); from_apikey_env() only exists in 4.x.
        ctx = QuoteContext(Config.from_env())
        if self._on_quote is not None:
            ctx.set_on_quote(self._on_quote)
        if self._on_depth is not None:
            ctx.set_on_depth(self._on_depth)
        return ctx

    def register_callbacks(
        self,
        on_quote: Callable[[str, Any], None] | None = None,
        on_depth: Callable[[str, Any], None] | None = None,
    ) -> None:
        """Push callbacks fire on an SDK thread, not the asyncio loop."""
        with self._lock:
            if on_quote is not None:
                self._on_quote = on_quote
                if self._ctx is not None:
                    self._ctx.set_on_quote(on_quote)
            if on_depth is not None:
                self._on_depth = on_depth
                if self._ctx is not None:
                    self._ctx.set_on_depth(on_depth)

    def reset_ctx(self) -> None:
        with self._lock:
            self._ctx = None

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        async with self._sem:
            return await asyncio.to_thread(fn, *args)

    def _spend(self, symbols: Sequence[str]) -> int:
        """Reserve option quota for `symbols`. Non-option symbols are free."""
        cost = sum(1 for s in symbols if is_option_symbol(s))
        if cost:
            self._governor.acquire(cost)
        return cost

    def _guarded(self, symbols: Sequence[str], fn: Callable[[], T]) -> T:
        """Run an option-quote request under the governor, retrying once on 301607."""
        self._spend(symbols)
        try:
            return fn()
        except Exception as exc:
            if not is_option_quota_error(exc):
                raise
            self._governor.penalize()
            self._spend(symbols)
            return fn()

    # --- entitlement ----------------------------------------------------------

    def quote_level(self) -> str:
        return str(self.ctx.quote_level())

    # --- prices ---------------------------------------------------------------

    def quote(self, symbols: Sequence[str]) -> dict[str, Spot]:
        out: dict[str, Spot] = {}
        for batch in chunked(list(symbols)):
            for q in self.ctx.quote(list(batch)):
                out[q.symbol] = Spot(
                    symbol=q.symbol,
                    last=to_float(q.last_done),
                    prev_close=to_float(q.prev_close),
                    ts=to_utc(q.timestamp),
                    trade_status=str(q.trade_status) if q.trade_status is not None else None,
                )
        return out

    async def aquote(self, symbols: Sequence[str]) -> dict[str, Spot]:
        return await self._call(self.quote, symbols)

    def candles(self, symbol: str, count: int | None = None) -> list[Bar]:
        """Daily bars, forward-adjusted.

        Uses candlesticks() rather than history_candlesticks_*, which carries a
        monthly distinct-symbol quota (error 301607) that would silently break
        arbitrary-ticker search.
        """
        from longport.openapi import AdjustType, Period

        n = count or settings.candle_lookback
        raw = self.ctx.candlesticks(symbol, Period.Day, n, AdjustType.ForwardAdjust)
        bars = [
            Bar(
                ts=to_utc(c.timestamp),
                open=to_float(c.open) or 0.0,
                high=to_float(c.high) or 0.0,
                low=to_float(c.low) or 0.0,
                close=to_float(c.close) or 0.0,
                volume=to_float(c.volume) or 0.0,
            )
            for c in raw
        ]
        bars.sort(key=lambda b: b.ts)
        return bars

    async def acandles(self, symbol: str, count: int | None = None) -> list[Bar]:
        return await self._call(self.candles, symbol, count)

    def depth(self, symbol: str) -> Book:
        """REST order book. One symbol per call, and it spends option quota.

        Used only for initial snapshots; ongoing bid/ask comes from the push cache.
        """
        d = self._guarded([symbol], lambda: self.ctx.depth(symbol))
        return _book_from(symbol, d.bids, d.asks)

    async def adepth(self, symbol: str) -> Book:
        return await self._call(self.depth, symbol)

    # --- options --------------------------------------------------------------

    def option_expiries(self, symbol: str) -> list[date]:
        return sorted(self.ctx.option_chain_expiry_date_list(symbol))

    async def aoption_expiries(self, symbol: str) -> list[date]:
        return await self._call(self.option_expiries, symbol)

    def strikes(self, symbol: str, expiry: date) -> list[StrikeRow]:
        rows = [
            StrikeRow(
                strike=to_float(s.price) or 0.0,
                call_symbol=s.call_symbol or None,
                put_symbol=s.put_symbol or None,
                standard=bool(s.standard),
            )
            for s in self.ctx.option_chain_info_by_date(symbol, expiry)
        ]
        rows.sort(key=lambda r: r.strike)
        return rows

    async def astrikes(self, symbol: str, expiry: date) -> list[StrikeRow]:
        return await self._call(self.strikes, symbol, expiry)

    def calc_indexes(self, symbols: Sequence[str]) -> dict[str, ContractCalc]:
        """Batched greeks/IV/OI. 500 symbols per call covers a whole filtered chain."""
        from longport.openapi import CalcIndex

        indexes = [
            CalcIndex.ImpliedVolatility,
            CalcIndex.Delta,
            CalcIndex.Gamma,
            CalcIndex.Vega,
            CalcIndex.Rho,
            CalcIndex.OpenInterest,
            CalcIndex.LastDone,
            CalcIndex.StrikePrice,
            CalcIndex.ExpiryDate,
        ]
        out: dict[str, ContractCalc] = {}
        for batch in chunked(list(symbols)):
            rows = self._guarded(batch, lambda b=batch: self.ctx.calc_indexes(list(b), indexes))
            for c in rows:
                out[c.symbol] = _calc_from(c)
        return out

    async def acalc_indexes(self, symbols: Sequence[str]) -> dict[str, ContractCalc]:
        return await self._call(self.calc_indexes, symbols)

    def option_quotes(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """OptionQuote carries OI and IV but no greeks and no bid/ask.

        Note its implied_volatility is already a ratio, unlike calc_indexes.
        """
        out: dict[str, dict[str, Any]] = {}
        for batch in chunked(list(symbols)):
            rows = self._guarded(batch, lambda b=batch: self.ctx.option_quote(list(b)))
            for q in rows:
                out[q.symbol] = {
                    "iv": to_float(q.implied_volatility),
                    "open_interest": q.open_interest,
                    "last_done": to_float(q.last_done),
                    "strike": to_float(q.strike_price),
                    "expiry": q.expiry_date,
                    "contract_size": to_float(q.contract_size),
                    "underlying": q.underlying_symbol,
                }
        return out

    async def aoption_quotes(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        return await self._call(self.option_quotes, symbols)

    # --- streaming ------------------------------------------------------------

    def subscribe(self, symbols: Sequence[str]) -> None:
        """Subscribe for Quote+Depth pushes.

        Costs one option-quota unit per option symbol, but only ONCE: pushes and
        subsequent realtime_depth reads are free. That asymmetry is exactly why this
        design streams prices instead of polling depth().

        3.0.18's subscribe() has no is_first_push flag, so nothing arrives until the
        next market tick. Callers must seed state from REST quote()/depth() or the
        page renders empty on a quiet symbol.
        """
        from longport.openapi import SubType

        for batch in chunked(list(symbols)):
            self._guarded(
                batch, lambda b=batch: self.ctx.subscribe(list(b), [SubType.Quote, SubType.Depth])
            )

    def unsubscribe(self, symbols: Sequence[str]) -> None:
        from longport.openapi import SubType

        for batch in chunked(list(symbols)):
            self.ctx.unsubscribe(list(batch), [SubType.Quote, SubType.Depth])

    def subscriptions(self) -> dict[str, list[str]]:
        """Server-side subscription book.

        Unsubscribed symbols linger with an empty sub_types list, so drop them —
        otherwise budget accounting and reconnect reconciliation both overcount.
        """
        out: dict[str, list[str]] = {}
        for s in self.ctx.subscriptions():
            types = [str(t) for t in s.sub_types]
            if types:
                out[s.symbol] = types
        return out

    def realtime_depth(self, symbol: str) -> Book:
        """Reads the SDK's local push cache. Zero HTTP cost."""
        d = self.ctx.realtime_depth(symbol)
        return _book_from(symbol, d.bids, d.asks)

    def realtime_quote(self, symbols: Sequence[str]) -> dict[str, Spot]:
        out: dict[str, Spot] = {}
        for q in self.ctx.realtime_quote(list(symbols)):
            out[q.symbol] = Spot(
                symbol=q.symbol,
                last=to_float(q.last_done),
                prev_close=to_float(getattr(q, "prev_close", None)),
                ts=to_utc(q.timestamp),
                trade_status=str(q.trade_status) if q.trade_status is not None else None,
            )
        return out


def _best(levels: Iterable[Any] | None) -> BookSide:
    if not levels:
        return BookSide(None, None)
    top = min(levels, key=lambda d: d.position)
    return BookSide(to_float(top.price), to_float(top.volume))


def _book_from(symbol: str, bids: Any, asks: Any) -> Book:
    b, a = _best(bids), _best(asks)
    return Book(symbol=symbol, bid=b.price, ask=a.price, bid_size=b.size, ask_size=a.size)


def _calc_from(c: Any) -> ContractCalc:
    iv = to_float(c.implied_volatility)
    # calc_indexes reports IV as a percent (43.54); OptionQuote reports a ratio.
    if iv is not None and iv > 1.0:
        iv = iv / 100.0
    vega = to_float(c.vega)
    rho = to_float(c.rho)
    return ContractCalc(
        symbol=c.symbol,
        iv=iv,
        delta=to_float(c.delta),
        gamma=to_float(c.gamma),
        vega=vega / 100.0 if vega is not None else None,
        rho=rho / 100.0 if rho is not None else None,
        open_interest=c.open_interest,
        last_done=to_float(c.last_done),
        strike=to_float(c.strike_price),
        expiry=c.expiry_date,
    )


_client: LongportClient | None = None
_client_lock = threading.Lock()


def get_client() -> LongportClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = LongportClient()
        return _client
