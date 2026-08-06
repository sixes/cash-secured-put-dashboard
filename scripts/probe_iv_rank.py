"""Verification probe: does a VIX-based IV Rank agree with SPY's own ATM-IV rank?

Rank is invariant under a positive affine map, and VIX runs ~1.2x SPY ATM IV, so the
*rank* should transfer even though the *level* must not be displayed. This measures the
disagreement against DoltHub's independent SPY ATM IV series.

DoltHub applies a server-side query deadline (`context deadline exceeded`) to any range
scan of `volatility_history`, so only a small, recent, descending slice is obtainable.
"""

from __future__ import annotations

import statistics
import sys
import urllib.parse

import httpx

sys.path.insert(0, ".")

from app.providers.ivhistory import DOLT_SQL, fetch_index_series, rank_within

WINDOW = 252
MIN_HISTORY = 60


def dolt_spy(limit: int = 300) -> list[dict]:
    query = (
        "SELECT date, iv_current, iv_year_high, iv_year_low FROM volatility_history "
        f"WHERE act_symbol='SPY' AND iv_current IS NOT NULL ORDER BY date DESC LIMIT {limit}"
    )
    resp = httpx.get(f"{DOLT_SQL}?q={urllib.parse.quote(query)}", timeout=240.0)
    body = resp.json()
    print(f"dolthub status={body.get('query_execution_status')} rows={len(body.get('rows') or [])}")
    msg = body.get("query_execution_message")
    if msg:
        print(f"  message: {msg}")
    return body.get("rows") or []


def trailing_rank(values: list[float]) -> float | None:
    window = values[-WINDOW:]
    return rank_within(window[-1], min(window), max(window))


def main() -> int:
    rows = dolt_spy()
    if not rows:
        print("no DoltHub rows; cannot verify")
        return 1

    print(f"newest {rows[0]['date'][:10]}  oldest {rows[-1]['date'][:10]}")

    spy = sorted((r["date"][:10], float(r["iv_current"])) for r in rows)
    vix = fetch_index_series("VIX")
    print(f"vix rows={len(vix)} latest={vix[-1]}")
    vix_by_day = dict(vix)

    common = [(day, iv, vix_by_day[day]) for day, iv in spy if day in vix_by_day]
    print(f"aligned sessions: {len(common)}")
    if not common:
        return 1

    ratios = [v / iv for _, iv, v in common]
    print(
        f"VIX / SPY ATM IV  mean={statistics.mean(ratios):.3f} "
        f"median={statistics.median(ratios):.3f}"
    )

    atm = [iv for _, iv, _ in common]
    idx = [v for _, _, v in common]
    errors = []
    for i in range(MIN_HISTORY, len(common)):
        own = trailing_rank(atm[: i + 1])
        proxy = trailing_rank(idx[: i + 1])
        if own is not None and proxy is not None:
            errors.append(abs(own - proxy))
    if errors:
        errors.sort()
        p90 = errors[int(len(errors) * 0.9)]
        print(
            f"rank disagreement over {len(errors)} sessions: "
            f"median={errors[len(errors) // 2]:.2f}pp p90={p90:.2f}pp max={errors[-1]:.2f}pp"
        )
    else:
        print(f"only {len(common)} aligned sessions; need >{MIN_HISTORY} for a series check")

    # Single-point check against DoltHub's own published 52-week bounds, which needs no
    # long history and so survives the query deadline.
    newest = rows[0]
    day = newest["date"][:10]
    own_rank = rank_within(
        float(newest["iv_current"]), float(newest["iv_year_low"]), float(newest["iv_year_high"])
    )
    window = [v for d, v in vix if d <= day][-WINDOW:]
    proxy_rank = rank_within(window[-1], min(window), max(window))
    print(
        f"as of {day}: dolthub SPY rank={own_rank:.1f}  VIX rank={proxy_rank:.1f}  "
        f"diff={abs(own_rank - proxy_rank):.1f}pp"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
