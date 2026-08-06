"""Verification gate for the Longbridge/LongPort quote entitlement.

Run this before building anything else. It answers four questions that the plan depends on
and that the official docs leave ambiguous:

  1. Does this account's quote package return greeks for US options via calc_indexes?
  2. Does it return a populated bid/ask book for US OPTION symbols (REST depth + streaming)?
  3. Are >=250 daily candlesticks available for a US underlying?
  4. How does this host render the SDK's naive datetimes?

Usage:  TZ=UTC .venv/bin/python scripts/probe_longport.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from longport.openapi import (  # noqa: E402
    AdjustType,
    CalcIndex,
    Config,
    Period,
    QuoteContext,
    SubType,
)

UNDERLYING = "SPY.US"
GREEK_INDEXES = [
    CalcIndex.LastDone,
    CalcIndex.ImpliedVolatility,
    CalcIndex.OpenInterest,
    CalcIndex.Delta,
    CalcIndex.Gamma,
    CalcIndex.Theta,
    CalcIndex.Vega,
    CalcIndex.Rho,
    CalcIndex.StrikePrice,
    CalcIndex.ExpiryDate,
    CalcIndex.Premium,
]

results: dict[str, bool | None] = {}


def head(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def check(name: str, ok: bool | None, detail: str = "") -> None:
    results[name] = ok
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    head("0. Environment")
    print(f"  TZ env             : {os.environ.get('TZ', '(unset)')}")
    print(f"  local naive now    : {datetime.now()}")
    print(f"  utc now            : {datetime.now(timezone.utc)}")
    missing = [
        k
        for k in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"\n  ERROR: missing credentials: {', '.join(missing)}")
        print("  Copy .env.example to .env and fill in your keys, then re-run.")
        return 2

    ctx = QuoteContext(Config.from_env())

    head("1. Account entitlement")
    try:
        print(f"  member_id   : {ctx.member_id()}")
        print(f"  quote_level : {ctx.quote_level()}")
        for pkg in ctx.quote_package_details():
            print(f"  package     : {pkg.key} | {pkg.name} | {pkg.start_at} -> {pkg.end_at}")
    except Exception as exc:
        print(f"  could not read entitlement: {exc!r}")

    # ---------------------------------------------------------------- candles
    head("2. Daily candlesticks (need >=250 for SMA200)")
    try:
        bars = ctx.candlesticks(UNDERLYING, Period.Day, 300, AdjustType.ForwardAdjust)
        spot = float(bars[-1].close)
        ts = bars[-1].timestamp
        print(f"  bars={len(bars)}  last_close={spot}  last_ts={ts!r}  tzinfo={ts.tzinfo}")
        check("candlesticks >= 250 daily bars", len(bars) >= 250, f"got {len(bars)}")
    except Exception as exc:
        check("candlesticks >= 250 daily bars", False, repr(exc))
        return 1

    # ------------------------------------------------------------ chain pick
    head("3. Option chain (target 35-60 DTE)")
    today = date.today()
    try:
        expiries = ctx.option_chain_expiry_date_list(UNDERLYING)
    except Exception as exc:
        check("option_chain_expiry_date_list", False, repr(exc))
        return 1

    windowed = sorted(e for e in expiries if 35 <= (e - today).days <= 60)
    print(f"  {len(expiries)} expiries total; {len(windowed)} in the 35-60 DTE window")
    if not windowed:
        check("expiry in 35-60 DTE window", False, "none available")
        return 1
    expiry = windowed[0]
    dte = (expiry - today).days
    check("expiry in 35-60 DTE window", True, f"{expiry} (DTE={dte})")

    strikes = ctx.option_chain_info_by_date(UNDERLYING, expiry)
    # ~15% OTM put is a reasonable stand-in for a 0.15-delta strike.
    target = spot * 0.85
    puts = [s for s in strikes if s.put_symbol and float(s.price) < spot]
    if not puts:
        check("put contracts available", False, "chain returned no OTM puts")
        return 1
    pick = min(puts, key=lambda s: abs(float(s.price) - target))
    opt = pick.put_symbol
    print(f"  spot={spot:.2f}  {len(strikes)} strikes  chose {opt} (K={pick.price}, standard={pick.standard})")

    # ------------------------------------------------------------ 4. greeks
    head("4. Greeks via calc_indexes  [CRITICAL]")
    try:
        ci = ctx.calc_indexes([opt], GREEK_INDEXES)[0]
    except Exception as exc:
        check("calc_indexes returns greeks", False, repr(exc))
        ci = None

    if ci is not None:
        raw = {
            "delta": ci.delta,
            "gamma": ci.gamma,
            "theta": ci.theta,
            "vega": ci.vega,
            "rho": ci.rho,
            "implied_volatility": ci.implied_volatility,
            "open_interest": ci.open_interest,
            "last_done": ci.last_done,
        }
        for k, v in raw.items():
            print(f"  raw {k:<20}= {v}")
        have = [k for k in ("delta", "gamma", "theta", "vega") if raw[k] is not None]
        check(
            "calc_indexes returns delta/gamma/theta/vega",
            len(have) == 4,
            f"populated: {have or 'none'}",
        )
        if raw["theta"] is not None and raw["vega"] is not None:
            print("\n  Scaled per the docs (theta/vega/rho are x100):")
            print(f"    theta/share/day   = {float(raw['theta']) / 100:+.5f}")
            print(f"    theta/contract/day= {float(raw['theta']) / 100 * 100:+.3f}")
            print(f"    vega/share/1ivpt  = {float(raw['vega']) / 100:.5f}")
            print(f"    vega/contract     = {float(raw['vega']) / 100 * 100:.3f}")
            print("  Sanity: per-contract theta $2-6 and vega $10-15 for ATM 30-45 DTE.")

    # --------------------------------------------------- 5. REST depth
    head("5. Bid/ask -- REST depth() on an OPTION symbol  [CRITICAL]")
    rest_book = False
    try:
        d = ctx.depth(opt)
        bid = d.bids[0] if d.bids else None
        ask = d.asks[0] if d.asks else None
        print(f"  bids={len(d.bids)} asks={len(d.asks)}")
        print(f"  best bid={getattr(bid, 'price', None)} x{getattr(bid, 'volume', None)}")
        print(f"  best ask={getattr(ask, 'price', None)} x{getattr(ask, 'volume', None)}")
        rest_book = bool(d.bids and d.asks and bid.price is not None and ask.price is not None)
    except Exception as exc:
        print(f"  depth() raised: {exc!r}")
    check("REST depth() returns a populated option book", rest_book)

    # --------------------------------------------------- 6. streaming
    head("6. Bid/ask -- streaming subscribe() on an OPTION symbol  [CRITICAL]")
    pushes: list[tuple[str, str]] = []
    seen = threading.Event()

    def on_quote(symbol, event):  # noqa: ANN001
        pushes.append(("quote", symbol))
        seen.set()

    def on_depth(symbol, event):  # noqa: ANN001
        pushes.append(("depth", symbol))
        seen.set()

    stream_book = False
    try:
        ctx.set_on_quote(on_quote)
        ctx.set_on_depth(on_depth)
        ctx.subscribe([opt, UNDERLYING], [SubType.Quote, SubType.Depth])
        print(f"  subscribed; server-side subscriptions={len(ctx.subscriptions())}")
        print("  waiting up to 15s for a push (quiet outside US market hours)...")
        seen.wait(timeout=15)
        n_quote = sum(1 for k, _ in pushes if k == "quote")
        n_depth = sum(1 for k, _ in pushes if k == "depth")
        print(f"  pushes received: quote={n_quote} depth={n_depth}")

        rd = ctx.realtime_depth(opt)
        rbid = rd.bids[0].price if rd.bids else None
        rask = rd.asks[0].price if rd.asks else None
        print(f"  realtime_depth({opt}): bid={rbid} ask={rask}")
        stream_book = rbid is not None and rask is not None
        ctx.unsubscribe([opt, UNDERLYING], [SubType.Quote, SubType.Depth])
    except Exception as exc:
        print(f"  streaming raised: {exc!r}")
    check("streaming realtime_depth() gives option bid/ask", stream_book)
    check(
        "received any push within 15s",
        bool(pushes),
        "no pushes -- expected if the US market is closed",
    )

    # --------------------------------------------------- summary
    head("SUMMARY")
    critical = {
        "calc_indexes returns delta/gamma/theta/vega": "greeks",
        "REST depth() returns a populated option book": "bid/ask (REST)",
        "streaming realtime_depth() gives option bid/ask": "bid/ask (stream)",
        "candlesticks >= 250 daily bars": "price history",
    }
    status = {True: "OK", False: "UNAVAILABLE", None: "skipped"}
    for name, label in critical.items():
        print(f"  {label:<20}: {status[results.get(name)]}")

    failed = [n for n, ok in results.items() if ok is False]
    if failed:
        print(f"\n  {len(failed)} check(s) failed:")
        for n in failed:
            print(f"    - {n}")
        print("\n  Note: if only the bid/ask checks failed, the dashboard still works but must")
        print("  hide the spread column and price premium off last_done, labeled as such.")
        return 1
    print("\n  All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
