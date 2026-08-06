"""Chain pipeline: every expiry in the DTE window -> targeted strikes -> IV/OI -> greeks.

QUOTA IS THE BINDING CONSTRAINT. Option quote requests are capped at 500 symbols per
rolling minute (undocumented; see app/providers/quota.py). A naive pull of +/-25% of
spot across three expiries costs ~300 symbols per ticker refresh, which exhausts the
whole account budget on a single ticker. Measured: it fails outright.

So the pipeline inverts the problem twice.

First, per expiry: probe two ATM contracts for a real vol and a parity forward,
analytically solve for the strikes that can land in the delta band, request only those.
Cost per slice, measured at 28:
    option_chain_expiry_date_list      0   (metadata, free)
    option_chain_info_by_date          0   (metadata, free)
    ATM probe (1 put + 1 call)         3
    calc_indexes on the targeted band  <=24

Second, across expiries: which tenor to sell IS the decision, so every listed expiry in
the DTE window is priced. That is affordable only because the order of operations is
inverted against the obvious one — build and RANK with no subscriptions at all, then
subscribe just `max_quoted_contracts`. Subscription pressure is therefore flat in the
width of the window rather than 24 symbols per tenor, which is what keeps two tickers
clear of the separate 500-symbol CONCURRENT ceiling.

The per-minute rate is a different problem with a different answer: calc_indexes bills
on every call, so only the best tenor is re-priced every `greeks_poll_seconds`, the rest
every `slow_tenor_poll_seconds` (see live.py). Bid/ask then arrives by push for free.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Sequence

from app.config import settings
from app.metrics.options import (
    DeltaMatch,
    IvVsRv,
    PutCandidate,
    build_candidate,
    constant_maturity_iv,
    dte,
    interp_atm_iv,
    iv_vs_rv,
    pick_expiries,
    select_by_delta,
    with_quote,
)
from app.metrics.pricing import Forward, carry_forward, implied_forward, strike_for_put_delta
from app.params import ScreenParams, defaults
from app.providers.longport_client import Book, ContractCalc, LongportClient

log = logging.getLogger(__name__)

# Extra strikes either side of the analytic delta-band solution. Absorbs skew (wing
# IV exceeds ATM IV, pushing true delta above the ATM-vol estimate) and coarse grids.
STRIKE_PAD = 3

# Hard ceiling on symbols requested per expiry, so one wide chain cannot starve the
# rest of the app of quota.
MAX_STRIKES_PER_EXPIRY = 24

# Vol used to size the strike search before any IV is known.
FALLBACK_SIGMA = 0.25
# Realized vol understates implied; scale it up before using it as a seed.
RV_TO_IV_SEED = 1.25

# Which clock re-priced a slice. A comparison tenor's greeks can be minutes older than
# the best tenor's, and a row that does not say so reads as one coherent snapshot.
FAST = "fast"  # re-priced every greeks_poll_seconds
SLOW = "slow"  # re-priced every slow_tenor_poll_seconds


@dataclass
class ExpirySlice:
    expiry: date
    dte: int
    forward: Forward | None
    atm_iv: float | None
    candidates: list[PutCandidate] = field(default_factory=list)
    atm_call_symbol: str | None = None
    atm_put_symbol: str | None = None
    quota_spent: int = 0
    # When this slice's greeks were paid for, and on which clock. Not compared: an age
    # is metadata about a figure, never part of its identity.
    built_at: float = field(default_factory=time.monotonic, compare=False)
    clock: str = FAST
    # This tenor's own comparable contract. Pooling candidates across expiries and
    # selecting once would let strike-grid rounding decide which tenor "wins".
    match: DeltaMatch | None = None

    @property
    def age(self) -> float:
        return time.monotonic() - self.built_at

    @property
    def put_symbols(self) -> list[str]:
        return [c.symbol for c in self.candidates]

    @property
    def stream_symbols(self) -> list[str]:
        """Everything the streaming layer must subscribe for this expiry."""
        out = self.put_symbols
        for s in (self.atm_call_symbol, self.atm_put_symbol):
            if s and s not in out:
                out = out + [s]
        return out


@dataclass
class ChainResult:
    ticker: str
    spot: float
    slices: list[ExpirySlice]
    atm_iv_30d: float | None
    iv_rv: IvVsRv | None
    match: DeltaMatch | None
    warnings: list[str] = field(default_factory=list)
    quota_spent: int = 0
    # One line naming the criterion behind `match`, so the page and the API state the
    # ranking rather than each keeping its own prose copy of it.
    best_tenor_reason: str = ""

    @property
    def expiries(self) -> list[date]:
        return [s.expiry for s in self.slices]

    @property
    def match_symbols(self) -> list[str]:
        """One comparable contract per tenor. Highlighted, plural — not a single pick."""
        return [
            s.match.candidate.symbol
            for s in self.slices
            if s.match is not None and s.match.candidate is not None
        ]

    @property
    def best_symbol(self) -> str | None:
        if self.match is None or self.match.candidate is None:
            return None
        return self.match.candidate.symbol

    @property
    def all_symbols(self) -> list[str]:
        out: list[str] = []
        for s in self.slices:
            out.extend(s.stream_symbols)
        return out

    @property
    def all_candidates(self) -> list[PutCandidate]:
        out: list[PutCandidate] = []
        for s in self.slices:
            out.extend(s.candidates)
        return out

    @property
    def ranked_candidates(self) -> list[PutCandidate]:
        """Every candidate across every tenor, best first.

        The question is "which contract", so DTE order would bury the answer. In-band
        contracts rank ahead of screen-band context whatever their yield, since only they
        are candidates to sell. Within that the key is the MARKET annualized premium where
        a quote exists and the MODEL one where it does not — two different kinds of number
        in one ordering, which is why the table labels each row's source rather than
        merging the two into a single column.
        """

        def key(c: PutCandidate) -> tuple[bool, float, int]:
            ann = c.premium.annualized_pct
            if ann is None:
                ann = c.model_annualized_pct or 0.0
            return (c.in_delta_band, ann, c.dte)

        return sorted(self.all_candidates, key=key, reverse=True)


def choose_expiry(
    expiries: list[date],
    params: ScreenParams | None = None,
    now: datetime | None = None,
) -> date | None:
    """Single expiry nearest the middle of the DTE window.

    The fallback for an empty window, and the whole answer when only one tenor is
    wanted. See choose_expiries for the window case.
    """
    p = params or defaults()
    in_window = pick_expiries(expiries, p.dte_min, p.dte_max, now=now)
    mid = (p.dte_min + p.dte_max) / 2.0
    if in_window:
        return min(in_window, key=lambda e: abs(dte(e, now) - mid))

    future = [e for e in expiries if dte(e, now) >= 0]
    return min(future, key=lambda e: abs(dte(e, now) - mid)) if future else None


def choose_expiries(
    expiries: list[date],
    params: ScreenParams | None = None,
    now: datetime | None = None,
) -> list[date]:
    """Every listed expiry inside the DTE window, in DTE order. No cap.

    Which tenor to sell is the decision a premium seller is actually making, so the
    window is priced whole rather than sampled at its midpoint. The cost of a wide
    window is absorbed by the ranked subscription set and the slow clock, not by
    truncating the comparison — and when it cannot be absorbed the governor blocks and
    the page says it is waiting.

    An empty window degrades to the single nearest expiry, so today's behaviour is a
    strict subset of this one.
    """
    p = params or defaults()
    in_window = pick_expiries(expiries, p.dte_min, p.dte_max, now=now)
    if in_window:
        return sorted(in_window, key=lambda e: dte(e, now))

    fallback = choose_expiry(expiries, p, now)
    return [fallback] if fallback is not None else []


def target_strikes(
    strikes: list[float],
    forward: float,
    spot: float,
    t: float,
    r: float,
    sigma: float,
    band: tuple[float, float] | None = None,
    pad: int = STRIKE_PAD,
    limit: int = MAX_STRIKES_PER_EXPIRY,
    target: float | None = None,
) -> list[float]:
    """Strikes that can plausibly land in the screen delta band.

    Solves the band edges analytically at `sigma`, then pads outward because put skew
    lifts wing IV above ATM IV, which lifts true delta above this estimate.
    """
    lo_delta, hi_delta = settings.delta_screen_band if band is None else band
    centre_delta = settings.delta_target if target is None else target
    grid = sorted(k for k in strikes if k > 0)
    if not grid:
        return []

    k_low = strike_for_put_delta(lo_delta, forward, t, r, sigma, spot)  # smallest strike
    k_high = strike_for_put_delta(hi_delta, forward, t, r, sigma, spot)  # largest strike
    if k_low is None or k_high is None:
        return grid[:limit]
    if k_low > k_high:
        k_low, k_high = k_high, k_low

    inside = [k for k in grid if k_low <= k <= k_high]
    if not inside:
        # Grid too coarse to contain the band; take the nearest strike to its middle.
        mid = 0.5 * (k_low + k_high)
        inside = [min(grid, key=lambda k: abs(k - mid))]

    first, last = grid.index(inside[0]), grid.index(inside[-1])
    lo_i, hi_i = max(0, first - pad), min(len(grid), last + pad + 1)
    selected = grid[lo_i:hi_i]

    if len(selected) > limit:
        # Centre the window on the delta TARGET, not the top of the range. On a $1
        # strike grid the screen band is ~38 strikes wide, so keeping the highest
        # `limit` would drop the entire 0.10-0.15 region and leave the 0.15-0.20
        # trading band pinned to the edge of the result.
        k_mid = strike_for_put_delta(centre_delta, forward, t, r, sigma, spot)
        if k_mid is None:
            selected = selected[-limit:]
        else:
            centre = min(range(len(selected)), key=lambda i: abs(selected[i] - k_mid))
            start = max(0, min(centre - limit // 2, len(selected) - limit))
            selected = selected[start : start + limit]
    return selected


async def _atm_probe(
    client: LongportClient,
    rows: list,
    spot: float,
    r: float,
    t: float,
) -> tuple[Forward | None, float | None, str | None, str | None, int]:
    """Two symbols: the ATM call and put. Yields the parity forward and a seed IV.

    Deliberately minimal. This runs before we know which strikes matter, so it must
    not cost more than a rounding error against the 500/min budget.
    """
    pairs = [s for s in rows if s.call_symbol and s.put_symbol]
    if not pairs:
        return None, None, None, None, 0

    atm = min(pairs, key=lambda s: abs(s.strike - spot))
    books = await _books(client, [atm.call_symbol, atm.put_symbol])
    call, put = books.get(atm.call_symbol), books.get(atm.put_symbol)

    fwd = None
    if call and put and call.mid and put.mid:
        fwd = implied_forward([(atm.strike, call.mid, put.mid)], r, t, spot=spot)

    seed_iv = None
    calc = await client.acalc_indexes([atm.put_symbol])
    row = calc.get(atm.put_symbol)
    if row and row.iv:
        seed_iv = row.iv

    # 2 depth calls + 1 calc_indexes symbol
    return fwd, seed_iv, atm.call_symbol, atm.put_symbol, 3


async def _books(client: LongportClient, symbols: list[str]) -> dict[str, Book]:
    async def one(sym: str) -> tuple[str, Book | None]:
        try:
            return sym, await client.adepth(sym)
        except Exception as exc:
            log.debug("depth failed for %s: %s", sym, exc)
            return sym, None

    results = await asyncio.gather(*(one(s) for s in symbols))
    return {sym: book for sym, book in results if book is not None}


async def _atm_iv_at(
    client: LongportClient, ticker: str, expiry: date, spot: float, now: datetime | None
) -> tuple[float | None, int, int]:
    """ATM IV for one expiry at a cost of a single quota unit.

    Used only to bracket 30 days so the constant-maturity figure is interpolated
    rather than clamped. One symbol, not a chain.
    """
    rows = await client.astrikes(ticker, expiry)
    puts = [s for s in rows if s.put_symbol]
    if not puts:
        return None, dte(expiry, now), 0

    atm = min(puts, key=lambda s: abs(s.strike - spot))
    calc = await client.acalc_indexes([atm.put_symbol])
    row = calc.get(atm.put_symbol)
    return (row.iv if row else None), dte(expiry, now), 1


async def build_chain(
    client: LongportClient,
    ticker: str,
    spot: float,
    r: float,
    rv20: float | None = None,
    now: datetime | None = None,
    params: ScreenParams | None = None,
    previous: ChainResult | None = None,
    only: Sequence[date] | None = None,
) -> ChainResult:
    """Price every expiry in the DTE window.

    `only` names the expiries due for a re-price; every other expiry in the window
    reuses `previous`'s slice unchanged, carrying its own `built_at` so the page can
    state that row's real age. That is what lets one tenor run on the 60s clock while
    the rest run on a slower one without re-billing the whole window every minute.
    """
    p = params or defaults()
    warnings: list[str] = []

    expiries = await client.aoption_expiries(ticker)
    if not expiries:
        return ChainResult(ticker, spot, [], None, None, None, ["no listed options"])

    wanted = choose_expiries(expiries, p, now)
    if not wanted:
        return ChainResult(ticker, spot, [], None, None, None, ["all expiries in the past"])

    for e in wanted:
        days = dte(e, now)
        if not (p.dte_min <= days <= p.dte_max):
            warnings.append(
                f"no expiry in the {p.dte_min}-{p.dte_max} DTE window; "
                f"using {e} at {days} DTE"
            )

    kept = {sl.expiry: sl for sl in (previous.slices if previous else [])}
    due = set(wanted) if only is None else {e for e in wanted if e in set(only)}

    slices: list[ExpirySlice] = []
    for expiry in wanted:
        if expiry not in due and expiry in kept:
            slices.append(kept[expiry])
            continue
        # Sequential on purpose. Gathering calc_indexes across a wide window would spend
        # the whole per-minute budget in one gulp and stall every other ticker.
        sl = await _build_slice(client, ticker, expiry, spot, r, rv20, now, warnings, p)
        if sl is not None:
            slices.append(sl)

    points = [(float(s.dte), s.atm_iv) for s in slices if s.atm_iv]
    extra_cost = 0
    if points and not _brackets(points):
        # A range that does not straddle 30 days can only be clamped to, never
        # interpolated through. Two or more tenors usually straddle it already, so this
        # unit is bought only when they do not.
        built = {s.expiry for s in slices}
        nearest = int(min(points, key=lambda dv: abs(dv[0] - 30.0))[0])
        other = _bracketing_expiry(
            [e for e in expiries if e not in built], nearest, now
        )
        if other is not None:
            iv, odays, extra_cost = await _atm_iv_at(client, ticker, other, spot, now)
            if iv:
                points.append((float(odays), iv))

    atm_30 = constant_maturity_iv(points, target_days=30.0)
    if atm_30 and not _brackets(points):
        warnings.append(
            "30d IV clamped, not interpolated: no priced expiry pair straddles 30 DTE"
        )

    mismatches = sum(1 for s in slices for c in s.candidates if c.delta_mismatch)
    if mismatches:
        warnings.append(f"{mismatches} contract(s) differ from API delta by >0.02")

    return ChainResult(
        ticker=ticker,
        spot=spot,
        slices=slices,
        atm_iv_30d=atm_30,
        iv_rv=iv_vs_rv(atm_30, rv20),
        # Selection needs a live bid, which does not exist yet. See apply_quotes.
        match=None,
        warnings=warnings,
        quota_spent=sum(s.quota_spent for s in slices if s.expiry in due) + extra_cost,
    )


def _brackets(points: list[tuple[float, float]], target: float = 30.0) -> bool:
    days = [d for d, _ in points]
    return bool(days) and min(days) <= target <= max(days)


def _bracketing_expiry(
    expiries: list[date], have_days: int, now: datetime | None
) -> date | None:
    """Listed expiry on the opposite side of 30 DTE from `have_days`.

    It must straddle 30, not merely differ from `have_days`: a 45-day partner for a
    48-day slice still leaves 30 outside the range, and constant_maturity_iv would
    clamp rather than interpolate.
    """
    if have_days > 30:
        pool = [e for e in expiries if 0 < dte(e, now) <= 30]
    else:
        pool = [e for e in expiries if dte(e, now) >= 30]
    if not pool:
        return None
    return min(pool, key=lambda e: abs(dte(e, now) - 30))


def apply_quotes(
    result: ChainResult, books: dict[str, Book], params: ScreenParams | None = None
) -> ChainResult:
    """Fold bid/ask into an already-built chain and pick the matches.

    Split out from build_chain because greeks cost option quota and quotes do not:
    this runs on every push tick, build_chain runs on a slow timer. That also makes it
    the seam for every FREE parameter — the delta target, the display band and both
    liquidity gates are applied here, so changing them costs nothing.

    Selection is PER SLICE. Pooling every expiry's candidates and selecting once would
    put roughly one strike per tenor near the target, so the single winner would be
    decided by strike-grid rounding — an arbitrary tenor presented as the match.
    """
    p = params or defaults()

    def refresh(c: PutCandidate) -> PutCandidate:
        book = books.get(c.symbol)
        return with_quote(
            c,
            book.bid if book else None,
            book.ask if book else None,
            max_abs=p.max_abs_spread,
            max_rel_pct=p.max_rel_spread_pct,
            band=p.delta_band,
        )

    slices: list[ExpirySlice] = []
    for sl in result.slices:
        cands = [refresh(c) for c in sl.candidates]
        slices.append(
            replace(
                sl,
                candidates=cands,
                match=select_by_delta(cands, p.delta_target, p.delta_band)
                if cands
                else None,
            )
        )

    match, reason, notes = _best_tenor(slices, p)
    warnings = list(result.warnings) + notes
    if match and match.off_band and match.candidate:
        warnings.append(
            f"no contract in the {p.delta_band[0]:.2f}-{p.delta_band[1]:.2f} "
            f"delta band; closest is {match.candidate.delta:.3f}"
        )
    elif match and match.candidate is None:
        warnings.append("no two-sided quotes on any candidate put")

    return replace(
        result,
        slices=slices,
        match=match,
        warnings=warnings,
        best_tenor_reason=reason,
    )


def _annualized(sl: ExpirySlice) -> float | None:
    if sl.match is None or sl.match.candidate is None:
        return None
    return sl.match.candidate.premium.annualized_pct


def _best_tenor(
    slices: list[ExpirySlice], p: ScreenParams
) -> tuple[DeltaMatch | None, str, list[str]]:
    """The best tenor among the per-slice matches, and the criterion that chose it.

    Annualized premium between contracts at the SAME delta target: comparing a 0.30
    delta weekly against a 0.15 delta monthly measures moneyness, not yield. Liquidity
    is a filter rather than a tie-break, because a spread nobody can cross erases an
    annualized edge on the round trip. Ties go to the LONGER tenor — fewer rolls means
    fewer spreads paid for the same nominal yield.
    """
    matched = [sl for sl in slices if sl.match is not None]
    if not matched:
        return None, "", []
    if len(matched) == 1:
        return matched[0].match, "", []

    quoted = [sl for sl in matched if _annualized(sl) is not None]
    if not quoted:
        return matched[0].match, "", []

    notes: list[str] = []
    liquid = [sl for sl in quoted if sl.match.candidate.spread.liquid]
    pool = liquid or quoted
    if not liquid:
        notes.append(
            "best tenor ranked on an unfillable mid: no tenor's match passes the "
            "liquidity gate"
        )

    winner = max(pool, key=lambda sl: (_annualized(sl), sl.dte))
    fast = next((sl for sl in pool if sl.clock == FAST), None)
    if fast is not None and winner is not fast and winner.clock != FAST:
        # The challenger's quote is as old as its own clock, so a win inside the width
        # of the liquidity gate is not evidence of anything. Report both, keep the live
        # tenor, and say the comparison is age-limited.
        edge = 1.0 + p.max_rel_spread_pct / 100.0
        if _annualized(winner) <= _annualized(fast) * edge:
            notes.append(
                f"{winner.expiry} leads {fast.expiry} by less than the "
                f"{p.max_rel_spread_pct:g}% liquidity gate, on quotes of different "
                f"ages ({winner.age:.0f}s vs {fast.age:.0f}s); keeping the live tenor"
            )
            winner = fast

    reason = (
        f"highest annualized premium at comparable delta ({p.delta_target:g}) among "
        f"{len(pool)} of {len(matched)} tenors with a two-sided quote"
        + ("" if liquid else "; none liquid")
    )
    return winner.match, reason, notes


def rank_for_quotes(
    slices: list[ExpirySlice],
    params: ScreenParams | None = None,
    limit: int | None = None,
) -> list[str]:
    """Contracts worth holding on the free push stream, best first.

    Ranked entirely from data build_chain has already paid for, because nothing
    quote-derived exists yet: `ContractCalc` carries no bid or ask, so premium, spread
    and liquidity are simply unavailable at this point. The key is therefore the LOCAL
    Black-76 premium at the API's IV — a model number, never displayed as a market one —
    with open interest as a liquidity prior and only ever a tie-break.

    Each tenor's delta-target match is reserved before rank fills the rest. Without a
    real quote on those contracts the whole term-structure comparison would rest on
    model prices, and a thin tenor could be ranked out of its own row.
    """
    p = params or defaults()
    cap = settings.max_quoted_contracts if limit is None else limit
    if cap <= 0:
        return []

    picked: list[str] = []
    seen: set[str] = set()

    def take(c: PutCandidate) -> None:
        if c.symbol not in seen:
            seen.add(c.symbol)
            picked.append(c.symbol)

    for sl in slices:
        pool = [c for c in sl.candidates if c.delta is not None]
        if pool:
            take(min(pool, key=lambda c: abs(c.delta - p.delta_target)))

    def key(c: PutCandidate) -> tuple[float, int, int]:
        return (c.model_annualized_pct or 0.0, c.open_interest or 0, c.dte)

    everything = [c for sl in slices for c in sl.candidates]
    tradeable = sorted((c for c in everything if c.in_delta_band), key=key, reverse=True)
    context = sorted(
        (c for c in everything if not c.in_delta_band), key=key, reverse=True
    )
    for c in tradeable + context:
        if len(picked) >= cap:
            break
        take(c)
    return picked[:cap]


async def _build_slice(
    client: LongportClient,
    ticker: str,
    expiry: date,
    spot: float,
    r: float,
    rv20: float | None,
    now: datetime | None,
    warnings: list[str],
    params: ScreenParams,
) -> ExpirySlice | None:
    days = dte(expiry, now)
    t = max(days, 1) / 365.0

    rows = await client.astrikes(ticker, expiry)
    if not rows:
        return None

    fwd, seed_iv, atm_call, atm_put, spent = await _atm_probe(client, rows, spot, r, t)
    if fwd is None:
        fwd = carry_forward(spot, r, t)
        warnings.append(
            f"{expiry}: no two-sided ATM quotes; forward from cost-of-carry with q=0"
        )

    sigma = seed_iv or ((rv20 * RV_TO_IV_SEED) if rv20 else FALLBACK_SIGMA)
    if seed_iv is None:
        warnings.append("strike band sized from realized vol; no ATM IV available")

    wanted = target_strikes(
        [s.strike for s in rows],
        fwd.forward,
        spot,
        t,
        r,
        sigma,
        band=params.delta_screen_band,
        target=params.delta_target,
    )
    wanted_set = set(wanted)
    selected = [s for s in rows if s.strike in wanted_set and s.put_symbol]
    if not selected:
        return None

    put_symbols = [s.put_symbol for s in selected]
    calc: dict[str, ContractCalc] = await client.acalc_indexes(put_symbols)
    spent += len(put_symbols)

    by_strike = {s.put_symbol: s.strike for s in selected}
    ivs = [(by_strike[sym], calc[sym].iv) for sym in put_symbols if sym in calc]
    # Prefer the probed ATM contract. The requested band is entirely OTM, so
    # interpolating within it just clamps to its highest strike (~3% OTM), where put
    # skew inflates IV — precisely the bias that would fake a variance risk premium.
    # ATM is the strike nearest SPOT: the probe runs before the forward is known.
    atm_iv = seed_iv or interp_atm_iv(
        [k for k, _ in ivs], [v for _, v in ivs], fwd.forward
    )

    candidates: list[PutCandidate] = []
    lo, hi = params.delta_screen_band
    for sym in put_symbols:
        c = calc.get(sym)
        if c is None:
            continue
        cand = build_candidate(
            symbol=sym,
            strike=by_strike[sym],
            expiry=expiry,
            spot=spot,
            forward=fwd.forward,
            iv=c.iv,
            r=r,
            bid=None,  # filled in by the streaming layer
            ask=None,
            api_delta=c.delta,
            open_interest=c.open_interest,
            now=now,
            band=params.delta_band,
            max_abs=params.max_abs_spread,
            max_rel_pct=params.max_rel_spread_pct,
        )
        if cand is None or cand.delta is None or cand.moneyness_pct >= 0:
            continue
        if lo <= cand.delta <= hi:
            candidates.append(cand)

    candidates.sort(key=lambda c: c.strike, reverse=True)
    return ExpirySlice(
        expiry=expiry,
        dte=days,
        forward=fwd,
        atm_iv=atm_iv,
        candidates=candidates,
        atm_call_symbol=atm_call,
        atm_put_symbol=atm_put,
        quota_spent=spent,
    )
