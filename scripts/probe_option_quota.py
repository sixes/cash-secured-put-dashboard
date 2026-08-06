"""Characterize the undocumented per-minute option-symbol quota (error 301607).

Not part of the app. Run manually; it deliberately burns quota.

Discovered while building the chain pipeline: calc_indexes on ~300 distinct option
symbols fails with
    301607 "Too many option securities request within one minute"
This message appears nowhere in the official docs or the rate-limit table.

Two questions decide the whole refresh architecture:
  A) Is the quota counted in DISTINCT symbols, or in total symbol-requests?
     If repeats are free, polling a fixed chain every 30s costs nothing after the
     first call. If not, the poll interval is quota-bound.
  B) What is the ceiling?
"""

from __future__ import annotations

import datetime as dt
import sys
import time

from longport.openapi import CalcIndex, Config, QuoteContext

INDEXES = [CalcIndex.ImpliedVolatility, CalcIndex.Delta, CalcIndex.OpenInterest]
COOLDOWN = 70


def chain_symbols(ctx: QuoteContext, underlying: str = "SPY.US") -> list[str]:
    """Collect option symbols WITHOUT spending option-quote quota.

    option_chain_expiry_date_list and option_chain_info_by_date return symbol
    metadata only, so they appear not to count against this quota.
    """
    expiries = sorted(ctx.option_chain_expiry_date_list(underlying))
    today = dt.date.today()
    future = [e for e in expiries if (e - today).days >= 0]

    symbols: list[str] = []
    for expiry in future:
        for row in ctx.option_chain_info_by_date(underlying, expiry):
            if row.put_symbol:
                symbols.append(row.put_symbol)
            if row.call_symbol:
                symbols.append(row.call_symbol)
        if len(symbols) > 2000:
            break
    return symbols


def is_quota_error(exc: Exception) -> bool:
    return "301607" in str(exc)


def experiment_repeats(ctx: QuoteContext, symbols: list[str], batch: int = 20) -> None:
    """Same symbols over and over. Does repetition consume quota?"""
    print(f"\n=== A: repeat the SAME {batch} symbols until failure ===")
    fixed = symbols[:batch]
    for i in range(1, 41):
        try:
            ctx.calc_indexes(fixed, INDEXES)
        except Exception as exc:
            kind = "QUOTA" if is_quota_error(exc) else "OTHER"
            print(f"  call {i:>3}: FAIL [{kind}] {exc}")
            print(f"  -> repeats DO consume quota; died after {i - 1} calls "
                  f"({(i - 1) * batch} symbol-requests, {batch} distinct)")
            return
        print(f"  call {i:>3}: ok  ({i * batch} symbol-requests, {batch} distinct)")
        time.sleep(0.3)
    print(f"  -> 40 calls x {batch} symbols = 800 symbol-requests on {batch} distinct "
          f"symbols, no failure. Quota is DISTINCT-symbol based; repolling is cheap.")


def experiment_distinct(ctx: QuoteContext, symbols: list[str], batch: int = 20) -> None:
    """Fresh symbols every call. Where is the distinct-symbol ceiling?"""
    print(f"\n=== B: fresh {batch} distinct symbols per call until failure ===")
    used = 0
    for i in range(1, 61):
        chunk = symbols[used : used + batch]
        if len(chunk) < batch:
            print(f"  ran out of symbols at {used} distinct without failing")
            return
        try:
            ctx.calc_indexes(chunk, INDEXES)
        except Exception as exc:
            kind = "QUOTA" if is_quota_error(exc) else "OTHER"
            print(f"  call {i:>3}: FAIL [{kind}] at cumulative {used + batch} distinct")
            print(f"  -> distinct-symbol ceiling is between {used} and {used + batch}")
            return
        used += batch
        print(f"  call {i:>3}: ok  (cumulative {used} distinct)")
        time.sleep(0.3)


def experiment_single_large(ctx: QuoteContext, symbols: list[str], size: int) -> bool:
    """Can one call carry `size` distinct symbols from a clean window?"""
    try:
        out = ctx.calc_indexes(symbols[:size], INDEXES)
        print(f"  single call with {size} distinct: ok ({len(out)} rows)")
        return True
    except Exception as exc:
        kind = "QUOTA" if is_quota_error(exc) else "OTHER"
        print(f"  single call with {size} distinct: FAIL [{kind}] {exc}")
        return False


def main() -> None:
    ctx = QuoteContext(Config.from_env())
    symbols = chain_symbols(ctx)
    print(f"collected {len(symbols)} option symbols for testing")

    which = sys.argv[1] if len(sys.argv) > 1 else "all"

    if which in ("all", "repeats"):
        print(f"\ncooling down {COOLDOWN}s to clear the window...")
        time.sleep(COOLDOWN)
        experiment_repeats(ctx, symbols)

    if which in ("all", "distinct"):
        print(f"\ncooling down {COOLDOWN}s to clear the window...")
        time.sleep(COOLDOWN)
        experiment_distinct(ctx, symbols)

    if which in ("all", "sizes"):
        for size in (100, 200, 300):
            print(f"\ncooling down {COOLDOWN}s to clear the window...")
            time.sleep(COOLDOWN)
            experiment_single_large(ctx, symbols, size)


if __name__ == "__main__":
    main()
