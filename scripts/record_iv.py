"""Nightly IV recorder. Run once per session close from cron.

Two jobs, both idempotent:

1. Refresh the external IV-history sources (CBOE vol indices, or DoltHub's published
   52-week range) for the tickers we care about.
2. Record our own 30-day constant-maturity ATM IV from the Longbridge chain, which is
   the only leg that eventually covers every ticker we have ever queried.

Leg 2 spends option quota (~28 units per ticker), so tickers are processed one at a
time and the run stops early rather than tripping the 500-per-minute cap.

    TZ=UTC python scripts/record_iv.py SPY QQQ AAPL
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.metrics.trend import compute_trend
from app.providers import ivhistory, rates
from app.providers.chain import build_chain
from app.providers.longport_client import display_ticker, get_client, us_symbol
from app.providers.quota import get_governor
from app.store import get_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("record_iv")

# A chain rebuild measured 28 units; stop before a partial one can trip the cap.
MIN_QUOTA = 40


async def record_one(client, raw: str) -> bool:
    symbol = us_symbol(raw)
    name = display_ticker(symbol)

    source = await asyncio.to_thread(ivhistory.refresh_ticker, symbol)
    log.info("%s external source: %s", name, source or "none")

    quotes = await client.aquote([symbol])
    if symbol not in quotes or quotes[symbol].last is None:
        log.warning("%s: no quote; skipping own recording", name)
        return False

    spot = quotes[symbol].last
    bars = await client.acandles(symbol, settings.candle_lookback)
    trend = compute_trend(symbol, [b.close for b in bars], last=spot)

    chain = await build_chain(
        client, symbol, spot, rates.risk_free_rate(), rv20=trend.rv20.rv
    )
    if not chain.atm_iv_30d:
        log.warning("%s: no 30d ATM IV (%s)", name, "; ".join(chain.warnings) or "no reason given")
        return False

    written = ivhistory.record_local(symbol, chain.atm_iv_30d)
    log.info(
        "%s atm_iv_30d=%.4f (%d quota units) %s",
        name,
        chain.atm_iv_30d,
        chain.quota_spent,
        "recorded" if written else "already recorded today",
    )
    return written


async def main(tickers: list[str]) -> int:
    store = get_store()
    rates.refresh(store)
    client = get_client()
    governor = get_governor()

    recorded = 0
    for raw in tickers:
        if governor.available() < MIN_QUOTA:
            log.warning(
                "stopping early: %d quota units left, need ~%d", governor.available(), MIN_QUOTA
            )
            break
        try:
            if await record_one(client, raw):
                recorded += 1
        except Exception:
            log.exception("failed on %s", raw)

    for raw in tickers:
        name = display_ticker(us_symbol(raw))
        rank = ivhistory.iv_rank(name, store=store)
        if rank is None:
            log.info("%s: no rank yet", name)
        else:
            log.info(
                "%s rank=%s n=%d source=%s%s",
                name,
                "—" if rank.rank is None else f"{rank.rank:.1f}",
                rank.n,
                rank.source,
                "" if rank.sufficient else " (insufficient)",
            )

    log.info("recorded %d/%d", recorded, len(tickers))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:] or list(settings.default_tickers)
    raise SystemExit(asyncio.run(main(args)))
