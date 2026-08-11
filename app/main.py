"""FastAPI routes. Server-rendered snapshot first, SSE patches second.

The initial render is a complete HTML page, so the dashboard is fully readable with
JavaScript disabled. SSE only overwrites values that have since moved.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.columns import MISSING_NOTE, UNQUOTED_NOTE, chain_columns, row_states
from app.columns import as_json as columns_json
from app.config import MAX_SUBSCRIBED_SYMBOLS, settings
from app.live import LiveEngine, UnknownTicker, get_engine
from app.params import ScreenParams, defaults, from_query, groups, to_dict, to_query
from app.providers import pricehistory, rates
from app.providers.chain import FAST
from app.providers.longport_client import LongportUnavailable, display_ticker, us_symbol
from app.store import get_store

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# The chain <thead>, the footer reference and the API's conventions block all render from
# one function of the request's parameters, so a formula cannot go stale in a single
# surface — and cannot describe the values the process happened to start with.
templates.env.globals["missing_note"] = MISSING_NOTE
templates.env.globals["unquoted_note"] = UNQUOTED_NOTE

# Emit a status frame this often even when nothing moves, so a client can tell a
# quiet market from a dead connection.
SSE_HEARTBEAT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    try:
        await engine.start()
    except LongportUnavailable as exc:
        log.error("Longbridge unavailable: %s", exc)
    yield
    await engine.stop()


app = FastAPI(title="CSP Screener", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, ticker: str | None = None):
    engine = get_engine()
    resolved = _resolve_params(request)
    params = resolved.params
    wanted = [ticker] if ticker else list(settings.default_tickers)

    panels, errors = [], list(resolved.errors)
    for raw in wanted:
        try:
            panels.append(await _panel(engine, raw, params))
        except UnknownTicker:
            errors.append(f"{raw.upper()}: no quote — check the symbol")
        except LongportUnavailable as exc:
            errors.append(f"market data unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("panel failed for %s", raw)
            errors.append(f"{raw.upper()}: {exc}")

    param_groups = groups(params, request.query_params, resolved.saved)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "panels": panels,
            "errors": errors,
            "query": ticker or "",
            "status": engine.status(),
            "settings": settings,
            "rate": rates.rate_provenance(),
            "params": params,
            "param_groups": param_groups,
            "params_changed": sum(
                1 for g in param_groups for f in g["fields"] if f["origin"] != "code"
            ),
            "param_query": to_query(params),
            "params_open": "params_open" in request.query_params,
            "saved_at": resolved.saved_at,
            "chain_columns": chain_columns(params),
            "row_states": row_states(params),
        },
    )


@dataclass(frozen=True)
class _Resolved:
    params: ScreenParams
    saved: dict | None
    saved_at: str | None
    errors: list[str]


def _resolve_params(request: Request) -> _Resolved:
    """URL parameter, else saved default, else code default.

    The saved row is overlaid on the code baseline through the same validator as the
    query, so a row written by an older field set — or hand-edited — degrades to the
    baseline with a message rather than taking the page down.
    """
    stored = get_store().screen_defaults()
    saved, saved_at, errors = None, None, []
    base = defaults()
    if stored is not None:
        saved, saved_at = stored
        base, errors = from_query(saved, base)
        errors = [f"saved default — {e}" for e in errors]

    params, query_errors = from_query(request.query_params, base)
    return _Resolved(params, saved, saved_at, errors + query_errors)


@app.post("/settings/save")
async def settings_save(request: Request):
    """Persist the submitted set as the baseline, then redirect to a bare URL.

    The form posts every field, so what is saved is exactly what the page showed. The
    redirect drops the query on purpose: those values are now the base, and leaving them
    in the URL would mark every field "url" and defeat the next Reset.
    """
    form = await _form(request)
    params, _ = from_query(form, defaults())
    get_store().save_screen_defaults(to_dict(params))
    return RedirectResponse(
        _ticker_url(form.get("ticker"), form.get("params_open")), status_code=303
    )


@app.post("/settings/reset")
async def settings_reset(request: Request):
    form = await _form(request)
    get_store().clear_screen_defaults()
    return RedirectResponse(
        _ticker_url(form.get("ticker"), form.get("params_open")), status_code=303
    )


async def _form(request: Request) -> dict[str, str]:
    """Decode an urlencoded POST body.

    Starlette's `request.form()` asserts `python-multipart` is installed even for
    `application/x-www-form-urlencoded`, and both settings forms post nothing else.
    """
    body = await request.body()
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def _ticker_url(ticker: object, params_open: object = None) -> str:
    pairs = {}
    raw = str(ticker or "").strip()
    if raw:
        pairs["ticker"] = raw
    if params_open:
        pairs["params_open"] = "1"
    return f"/?{urlencode(pairs)}" if pairs else "/"


async def _panel(engine: LiveEngine, raw: str, params: ScreenParams) -> dict:
    view = await engine.ensure(raw, params)
    chain = engine.quoted(view.ticker, params)
    return {
        "ticker": display_ticker(view.ticker),
        "symbol": view.ticker,
        "view": view,
        "trend": view.trend,
        "chart": view.chart,
        "sma_gap_pct": _sma_cross_check(view),
        "chain": chain,
        "slices": chain.slices if chain else [],
        "candidates": chain.ranked_candidates if chain else [],
        # Plural: one comparable contract per tenor. `best` is the ranked pick among them.
        "matches": set(chain.match_symbols) if chain else set(),
        "best": chain.best_symbol if chain else None,
        # Only these carry a market price; every other row renders dashes.
        "quoted": set(view.quoted_symbols),
        # Each row states its own age, which belongs to its tenor rather than to it.
        "tenor_of": {sl.expiry: sl for sl in (chain.slices if chain else [])},
        "cost": _standing_cost(engine, chain),
    }


def _standing_cost(engine: LiveEngine, chain) -> dict:
    """What this window costs per minute, stated before the user widens it further.

    A wide window is affordable only because one tenor runs on the fast clock and the
    rest on the slow one. Spelling the arithmetic out on the page is the only way a
    reader can tell a slow refresh from a broken one.
    """
    slices = chain.slices if chain else []
    per_slice = max((sl.quota_spent for sl in slices), default=0)
    fast = sum(1 for sl in slices if sl.clock == FAST)
    slow = len(slices) - fast
    fast_rate = fast * per_slice * 60.0 / max(settings.greeks_poll_seconds, 1.0)
    slow_rate = slow * per_slice * 60.0 / max(settings.slow_tenor_poll_seconds, 1.0)
    status = engine.status()
    return {
        "tenors": len(slices),
        "fast": fast,
        "slow": slow,
        "contracts": sum(len(sl.candidates) for sl in slices),
        "quoted": settings.max_quoted_contracts,
        "units_per_min": round(fast_rate + slow_rate),
        "limit": status.quota_spent + status.quota_available,
        "slow_seconds": int(settings.slow_tenor_poll_seconds),
        "fast_seconds": int(settings.greeks_poll_seconds),
        # What the rebuild the reader just waited for actually billed. Not a constant:
        # a one-tenor window costs ~28 and a nineteen-tenor one an order of magnitude more.
        "last_rebuild": chain.quota_spent if chain else 0,
    }


def _sma_cross_check(view) -> float | None:
    """Signed % gap between the chart's SMA200 and the cards'.

    Only meaningful across adjustment bases: the chart draws Yahoo's auto-adjusted
    closes while the cards use Longbridge's forward-adjusted ones, so agreement is a
    genuine cross-source confirmation. Returns None when both sides came from
    Longbridge, since comparing a series against itself proves nothing.
    """
    chart = view.chart
    card_sma = view.trend.sma200.sma
    if chart is None or chart.sma is None or not card_sma:
        return None
    if chart.source == pricehistory.SOURCE_LONGPORT:
        return None
    return (chart.sma / card_sma - 1.0) * 100.0


@app.get("/api/ticker/{ticker}")
async def api_ticker(ticker: str, request: Request):
    engine = get_engine()
    params = _resolve_params(request).params
    try:
        view = await engine.ensure(ticker, params)
    except UnknownTicker:
        raise HTTPException(status_code=404, detail=f"no quote for {ticker}")
    except LongportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    chain = engine.quoted(view.ticker, params)
    return JSONResponse(
        {
            "ticker": display_ticker(view.ticker),
            "spot": view.spot,
            "trend": _trend_json(view.trend),
            "chart": _chart_json(view.chart),
            "sma_cross_source_gap_pct": _sma_cross_check(view),
            "iv_rank": _iv_rank_json(view.iv_rank),
            "options": _chain_json(chain, view.quoted_symbols),
            "status": _status_json(engine),
            "params": to_dict(params),
            "conventions": _conventions(params),
        }
    )


@app.get("/api/stream/{ticker}")
async def api_stream(ticker: str, request: Request):
    engine = get_engine()
    symbol = us_symbol(ticker)
    # The stream must carry the page's parameters. Recomputing the match and the
    # liquidity flags from defaults would overwrite a correctly-rendered page.
    params = _resolve_params(request).params
    try:
        await engine.ensure(ticker, params)
    except UnknownTicker:
        raise HTTPException(status_code=404, detail=f"no quote for {ticker}")
    except LongportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return StreamingResponse(
        _stream(engine, symbol, params, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, nginx buffers the stream into uselessness.
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(engine: LiveEngine, symbol: str, params: ScreenParams, request: Request):
    interval = 1.0 / max(settings.sse_max_msgs_per_sec, 0.1)
    engine.add_viewer(symbol)
    try:
        with engine.hub.listener() as listener:
            yield _frame(engine, symbol, params)
            while not await request.is_disconnected():
                await listener.wait(timeout=SSE_HEARTBEAT_SECONDS)
                yield _frame(engine, symbol, params)
                # Rate cap. Pushes that land during this sleep collapse into the
                # next frame, so a fast-moving chain cannot flood the browser.
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    finally:
        engine.remove_viewer(symbol)


def _frame(engine: LiveEngine, symbol: str, params: ScreenParams) -> str:
    chain = engine.quoted(symbol, params)
    view = engine.view(symbol)
    # Only subscribed contracts can have moved, and patching an unquoted row would
    # replace its honest dashes with a stale-looking blank.
    quoted = set(view.quoted_symbols) if view else set()
    payload = {
        "spot": _spot_of(engine, symbol, view),
        "status": _status_json(engine),
        # One match per tenor; `best` is the ranked pick among them.
        "matches": chain.match_symbols if chain else [],
        "best": chain.best_symbol if chain else None,
        "rows": [
            _row_json(c) for c in chain.all_candidates if c.symbol in quoted
        ]
        if chain
        else [],
    }
    return f"event: patch\ndata: {json.dumps(payload)}\n\n"


def _spot_of(engine: LiveEngine, symbol: str, view) -> float | None:
    """Live last trade, falling back to the price the panel was built at."""
    state = engine.hub.get(symbol)
    if state is not None and state.last is not None:
        return state.last
    return view.spot if view else None


def _row_json(c) -> dict:
    return {
        "symbol": c.symbol,
        "bid": c.spread.bid,
        "ask": c.spread.ask,
        "mid": c.spread.mid,
        "rel_spread_pct": c.spread.rel_spread_pct,
        "liquid": c.spread.liquid,
        "premium_pct": c.premium.pct_of_strike,
        "annualized_pct": c.premium.annualized_pct,
        "premium_pct_bid": c.premium.pct_of_strike_bid,
    }


def _status_json(engine: LiveEngine) -> dict:
    s = engine.status()
    return {
        "label": s.label,
        "connected": s.connected,
        "stale": s.stale,
        "session_open": s.session_open,
        "seconds_since_push": s.seconds_since_push,
        "quota_spent": s.quota_spent,
        "quota_available": s.quota_available,
        # Non-zero while the governor is standing down after a 301607, so an already-open
        # tab can see that a billed rebuild is stalled rather than merely slow.
        "quota_blocked_for": s.quota_blocked_for,
    }


def _trend_json(t) -> dict:
    dd = t.drawdown
    return {
        "last": t.last,
        "drawdown": None
        if dd is None
        else {
            "high": dd.high,
            "pct": dd.drawdown_pct,
            "lookback_sessions": dd.high_n,
            "high_since": dd.high_since,
            "high_52w": dd.high_52w,
            "pct_52w": dd.drawdown_52w_pct,
        },
        "sma200": {
            "value": t.sma200.sma,
            "distance_pct": t.sma200.distance_pct,
            "n": t.sma200.n,
            "window": t.sma200.window,
            "sufficient": t.sma200.sufficient,
        },
        "rv20": {
            "value": t.rv20.rv,
            "n_returns": t.rv20.n_returns,
            "window": t.rv20.window,
            "sufficient": t.rv20.sufficient,
        },
        "n_bars": t.n_bars,
    }


def _chart_json(c) -> dict | None:
    """Provenance and figures for the price chart.

    The polyline point strings are deliberately absent: they are presentation geometry
    for one SVG viewBox, not data anyone could reuse.
    """
    if c is None:
        return None
    return {
        "source": c.source,
        "source_label": c.source_label,
        # False when the series is truncated by an API limit, in which case `high` is a
        # window high and must not be called an all-time high.
        "all_time": c.all_time,
        "first_date": c.first_date,
        "last_date": c.last_date,
        "n": c.n,
        "window_first_date": c.window_first_date,
        "sessions_drawn": c.shown,
        "last": c.last,
        "high": c.high,
        "high_date": c.high_date,
        "drawdown_pct": c.drawdown_pct,
        "sma": c.sma,
        "sma_window": c.sma_window,
    }


def _iv_rank_json(r) -> dict | None:
    if r is None:
        return None
    return {
        "rank": r.rank,
        "percentile": r.percentile,
        "low": r.low,
        "high": r.high,
        "observed": r.observed,
        "n": r.n,
        "window": r.window,
        "sufficient": r.sufficient,
        # False when the range is a publisher's asserted 52-week high/low rather than
        # observations we hold; `n` is then 0 and is not a sample size.
        "series_backed": r.series_backed,
        "source": r.source,
        "label": r.label,
        "as_of": r.as_of,
        # False when the ranked value is a vol index, whose level is ~1.19x this
        # ticker's ATM IV and must never be presented as the ticker's own.
        "level_displayable": r.level_displayable,
        "proxy_of": r.proxy_of,
    }


def _chain_json(chain, quoted: list[str] | None = None) -> dict | None:
    if chain is None:
        return None
    return {
        "atm_iv_30d": chain.atm_iv_30d,
        "iv_vs_rv": None
        if chain.iv_rv is None
        else {
            "atm_iv": chain.iv_rv.atm_iv,
            "rv": chain.iv_rv.rv,
            "ratio": chain.iv_rv.ratio,
            "spread_points": chain.iv_rv.spread_points,
        },
        # Every listed expiry in the DTE window, in DTE order. Each carries its own age
        # and clock: only the best tenor is on the fast one.
        "slices": [_slice_json(sl) for sl in chain.slices],
        "matches": chain.match_symbols,
        "best": chain.best_symbol,
        "best_reason": chain.best_tenor_reason,
        # Subscribed contracts. Everything else is screened and ranked but unpriced.
        "quoted": list(quoted or []),
        "warnings": chain.warnings,
        "quota_spent": chain.quota_spent,
        "candidates": [
            {
                "symbol": c.symbol,
                "strike": c.strike,
                "expiry": str(c.expiry),
                "dte": c.dte,
                "iv": c.iv,
                "delta": c.delta,
                "api_delta": c.api_delta,
                "delta_mismatch": c.delta_mismatch,
                "gamma": c.gamma,
                "gamma_per_premium": c.gamma_per_premium,
                "vega_per_iv_point": c.vega,
                "vega_contract": c.vega_contract,
                "theta_day": c.theta_day,
                "theta_day_contract": c.theta_day_contract,
                "open_interest": c.open_interest,
                "moneyness_pct": c.moneyness_pct,
                "in_delta_band": c.in_delta_band,
                # Model, not market. Never a substitute for `mid` above.
                "model_premium": c.model_premium,
                "model_annualized_pct": c.model_annualized_pct,
                **_row_json(c),
            }
            for c in chain.ranked_candidates
        ],
    }


def _slice_json(sl) -> dict:
    c = sl.match.candidate if sl.match else None
    return {
        "expiry": str(sl.expiry),
        "dte": sl.dte,
        "atm_iv": sl.atm_iv,
        "forward": sl.forward.forward if sl.forward else None,
        # "fast" is re-priced every greeks_poll_seconds; "slow" on the comparison clock.
        "clock": sl.clock,
        "age_seconds": sl.age,
        "match": None
        if c is None
        else {
            "symbol": c.symbol,
            "strike": c.strike,
            "delta": c.delta,
            "mid": c.spread.mid,
            "liquid": c.spread.liquid,
            "premium_pct": c.premium.pct_of_strike,
            "annualized_pct": c.premium.annualized_pct,
            "theta_day_contract": c.theta_day_contract,
            "gamma_per_premium": c.gamma_per_premium,
        },
        "off_band": sl.match.off_band if sl.match else None,
    }


def _conventions(params: ScreenParams | None = None) -> dict:
    p = params or defaults()
    return {
        "drawdown": "close-based against the expanding max of the fetched window (<=1000 daily bars, ~4y), not a true all-time high",
        "chart": f"one adjustment basis per chart, never stitched: Yahoo auto-adjusted closes when cached (full history, so its high IS an all-time high), else the <=1000-bar Longbridge forward-adjusted window (a window high). Running high is an expanding max and SMA{p.sma_window} a full-series mean, both computed over the whole series then windowed to the last {p.chart_window_sessions} sessions drawn. Right edge is the last daily close, not the live spot",
        "sma200": f"simple mean of the last {p.sma_window} trading-day closes; null when n < {p.sma_window}",
        "rv20": f"zero-mean estimator sqrt(mean(r^2))*sqrt(252) over {p.rv_window} log returns (variance-swap consistent)",
        "dte": f"calendar days in America/New_York, expiry day = 0, T = DTE/365; expiries screened in the {p.dte_min}-{p.dte_max} DTE window",
        "greeks": "Black-76 computed locally from the API's implied vol; the API's theta is unusable (sign flips across moneyness) and is not used",
        "forward": "implied by put-call parity from ATM mid prices, so no dividend-yield assumption is needed",
        "rate": "FRED DGS3MO, converted from bond-equivalent yield to continuously compounded",
        "theta_sign": "shown positive for a put SELLER (decay is income)",
        "vega": "per share per 1 IV point; the per-contract column is x100",
        "premium": "at mid, as a percent of strike; annualized simple (x365/DTE), not compounded",
        "iv_vs_rv": "ATM IV vs RV20, never the sold put's own IV (skew biases the wing high)",
        "iv_rank": "position of 30d ATM IV in its trailing 252-session range, computed within a single source (own recording once it spans a year, else the VIX/VXN/RVX/VXD proxy, else DoltHub's published 52-week range); a proxy index level is ~1.19x this ticker's ATM IV so only its rank transfers, never its level",
        "delta": f"target {p.delta_target:g} inside the {p.delta_band[0]:g}-{p.delta_band[1]:g} trading band; rows are requested across the wider {p.delta_screen_band[0]:g}-{p.delta_screen_band[1]:g} screening band",
        "liquidity": f"liquid when absolute spread <= {p.max_abs_spread:g} OR relative <= {p.max_rel_spread_pct:g}%; zero-bid is never liquid",
        "tenor_comparison": f"every listed expiry in the {p.dte_min}-{p.dte_max} DTE window is priced, and each one gets its OWN match nearest {p.delta_target:g} delta, so tenors are compared at constant delta rather than constant strike — a 0.30 delta weekly against a 0.15 delta monthly measures moneyness, not yield",
        "best_tenor": "the highest annualized premium among the tenors whose match has a two-sided, LIQUID quote; ties go to the longer tenor (fewer rolls, fewer spreads paid). A model-priced contract can never be the pick, and a slow-clock tenor does not displace the live one on a lead narrower than the liquidity gate",
        "annualized": "simple x365/DTE, not compounded. A RATE, never an expected annual return: it assumes the same premium is on offer at every roll, which IV mean-reversion and assignment both break, and it excludes transaction costs and pin risk. At constant delta a shorter tenor almost always annualizes higher because a normal upward-sloping IV term structure is paying for risk, not giving money away",
        "gamma_per_premium": "gamma / mid per contract (the two factors of 100 cancel) — the convexity bought per dollar collected, and the offset to a high annualized yield. It rises sharply as DTE falls",
        "clocks": f"three: quotes push free for the subscribed contracts, the best tenor's greeks are re-priced every {int(settings.greeks_poll_seconds)}s, and every other tenor every {int(settings.slow_tenor_poll_seconds)}s. Each row states its own age, because pricing every tenor on the fast clock would exceed the option-quota rate limit",
        "model_vs_market": f"the chain is built and ranked with NO subscriptions, then only the top {settings.max_quoted_contracts} contracts are subscribed, because the account holds at most {MAX_SUBSCRIBED_SYMBOLS} concurrent subscriptions. The pre-quote rank is therefore a local Black-76 premium at the API's IV — the chain feed carries no bid or ask. Model and market figures never share a column and a model figure is never substituted for a missing quote",
        # Per-column meanings and formulas. The keys match the candidate field names above
        # wherever they overlap, so the documentation joins to the data.
        "columns": columns_json(p),
        "row_states": {name: note for name, note in row_states(p)},
        "missing_values": MISSING_NOTE,
        "unquoted_rows": UNQUOTED_NOTE,
    }
