# investing-dashboard — project instructions

A cash-secured-put screener. FastAPI + Jinja2 + SSE, Longbridge (LongPort) OpenAPI for market data,
SQLite for anything that must survive a restart. Single Python service, no Node build step.

## Role: CFA charterholder

Work on this repository as a CFA charterholder would, not as a generic coder. That means:

- **State the convention, not just the number.** Every metric has a definition that changes its value
  (expanding vs rolling high, zero-mean vs sample-mean volatility, calendar vs trading DTE). The
  convention belongs in the code and on the page.
- **Never present a number more precisely or more confidently than the data supports.** A partial
  window, a proxy source, or a publisher's asserted range must be labelled as such. Prefer a visible
  `—` with a reason over a plausible-looking wrong figure.
- **Provenance is a first-class output.** Every displayed figure must be traceable to a source and an
  as-of date. Do not silently mix sources.
- **Units are a correctness concern.** An unscaled greek is a wrong answer, not a cosmetic issue. See
  the unit-traps table.
- **Prefer the estimator that matches the comparison being made.** RV20 is compared against implied
  vol, so it uses the variance-swap-consistent zero-mean form.
- **Sanity-check magnitudes against closed form** before believing a vendor field. This repo found a
  broken vendor greek that way.

## Metric conventions

The numbers below are the **code defaults**. The DTE window, the delta target and both bands, both
spread gates and the SMA/RV/chart windows are per-request parameters — a page may legitimately read
`SMA50` or a 0.25 target. See **Screening parameters**. The *conventions* are invariant; the windows
are not.

| Metric | Convention |
|---|---|
| `drawdown_from_high` | Close-based, **expanding** max (`cummax`, never `rolling(252).max()`). Named `_from_high`, not `_from_ath`, because the API caps history at 1000 bars (~4y). Reports `high_n` / `high_since` so the UI states the real lookback. Also reports % off the 52-week high. |
| Price chart | Close · expanding running high · SMA200, server-rendered inline SVG. Drawn wholly from **one** source (`app/metrics/chart.py` is pure geometry; the series comes from `providers/pricehistory.py`). High and SMA are computed over the **whole** series and *then* windowed to the drawn `chart_window_sessions` (756), so neither resets at the left edge. A series shorter than 200 draws no SMA line at all. Right edge is the last daily **close** — the live spot is a different series and stays in the panel heading. Only the staircase's step points are emitted, not 756 collinear ones. |
| ATH vs window high | The chart's `high` is a true all-time high **only** when the series reaches inception (Yahoo). On the Longbridge fallback it is labelled "window high, not an ATH". Measured: PYPL is −81.3% from its real 2021 ATH of 305.88 but only −43.5% from the 1000-session high — both are on the page, each with its own source label, and neither is presented as the other. |
| Adjustment basis | `AdjustType.ForwardAdjust` for **both** Longbridge history and live spot — one basis throughout the five metric cards. The chart may instead use Yahoo's auto-adjusted close; that is a **second basis** and is never mixed with the first (see the provenance rules). |
| SMA200 | Simple mean of the last 200 trading-day closes. If fewer than 200 valid closes, return `None` and render `n=137/200 insufficient` — never a partial mean labelled SMA200. |
| RV20 | Log returns, **zero-mean** estimator `sqrt(mean(r²)) · sqrt(252)`. 20 returns require 21 closes. |
| DTE | Calendar days, expiry day = 0, computed in `America/New_York`. `T = DTE/365`. |
| Put delta | `abs(delta)`, and `/100` if `>1`. Candidates restricted to OTM (`K < spot`) with `bid > 0`. Pick nearest 0.175 inside 0.15–0.20; if the closest available is >0.05 off target, show the actual delta and flag it rather than mislabelling. |
| theta/day | Local Black-76, per share per day, `×100` for per contract. Displayed with **short-position sign** (positive for a seller) and the column labelled accordingly. |
| vega | Local Black-76, per share per 1 IV point, `×100` for per contract. |
| Spread | `mid=(bid+ask)/2`, `rel=(ask-bid)/mid·100`. Liquidity gate `abs<=0.05 OR rel<=2.0` — percent-of-mid alone unfairly rejects cheap options already at the minimum legal tick. Zero-bid contracts excluded. |
| Premium % of strike | At **mid**: `premium/strike·100`; annualized `·365/DTE` (simple, not compounded). Bid-based value shown alongside as the conservative case. |
| Tenor comparison | Annualized premium at **constant delta**, never constant strike: each expiry gets its own `select_by_delta` match at the same `delta_target`, and tenors are ranked between those comparable contracts. Comparing a 0.30-delta weekly against a 0.15-delta monthly measures moneyness wearing a yield costume. Annualization is **simple** (`·365/DTE`) and assumes the same terms on every roll, which are not available — IV mean-reverts and assignment interrupts the sequence — so it is a **rate**, never an expected annual return; per-roll costs and pin risk are not in it. Ranked among **liquid, quoted** matches; ties go to the **longer** DTE (fewer rolls, fewer spreads paid). A challenger from the slow clock only displaces the live tenor when it wins by more than the liquidity gate's own width, because the two figures are of different ages. The best row is labelled "highest annualized at comparable delta", never "recommended". |
| Γ per premium dollar | `gamma / mid`, per contract on both legs so the two factors of 100 cancel. The **offset** to a high annualized yield: the same delta at 10 DTE carries far more gamma per dollar collected than at 50 DTE, which is why the shortest tenor usually wins the yield column. Market mid only — dividing a real gamma by a model premium would flatter a contract nobody quotes. |
| Model premium | Local Black-76 at the API's IV, on the parity-implied forward. Exists because `ContractCalc` has no bid/ask, so a contract must be rankable before it is subscribed. It is a **model** number: it lives in its own `Model ann %` column, is never rendered in a column that otherwise carries a market mid, is never substituted for a missing quote, and can never win the best-tenor pick. |
| IV vs RV | Ratio `IV_atm/RV20` plus spread `IV−RV` in vol points. Uses **ATM IV**, never the sold put's own IV — skew biases that high by construction. |
| ATM IV | The **probed ATM contract's** IV wins. Strike interpolation is a fallback only: it clamps to the highest requested strike (~3% OTM) where put skew inflates it (SPY read 17.09% vs a true 15.18%). |
| `atm_iv_30d` | Constant maturity, interpolated between two expiries that **bracket** 30 DTE. Two expiries that merely differ is not enough — clamping is not interpolation. With several tenors priced the built slices usually bracket 30 already, so the extra bracketing expiry (`_bracketing_expiry`, 1 unit) is bought **only** when they do not; a partner must straddle 30, not merely differ, or `constant_maturity_iv` clamps anyway. |
| IV Rank | Window 252 sessions, `100·(iv−low)/(high−low)`, clamped to 0–100 (a fresh spike legitimately prints outside its own trailing range). Source preference: own recording ≥252 sessions → vol-index proxy ≥120 → DoltHub published 52w bounds → short own series, flagged insufficient. |

## Data-provenance rules

- **Never concatenate raw IV levels across sources.** That injects a fake ~20% vol jump on the
  changeover date. Store `source` per row; compute rank **within a single source**.
- **The same rule applies to prices.** Yahoo's auto-adjusted close and Longbridge's
  `ForwardAdjust` close are different series, so `price_history` keys on `source` and a chart is
  drawn **wholly from one of them**, never stitched — the step on the changeover date would be an
  artefact, not a move. The card names the series and its span. ATH and drawdown-from-ATH are
  computed *inside* the charted series so that pair is self-consistent, and they live on the chart
  card, not on the Longbridge drawdown card: one card must not show two bases.
- **A model price and a market price are two sources, so they never share a column.** The screener ranks
  ~262 unsubscribed contracts on a local Black-76 premium; those rows print `—` for bid, ask, mid and
  premium and carry their figure in a separately-headed `Model ann %` column. The two are never averaged,
  never substituted for one another, and a model figure can never win the best-tenor pick — same rule as
  Yahoo vs Longbridge closes, applied to prices we computed ourselves.
- **Cross-source agreement is a check, not a chance to pick a number.** The chart computes its own
  SMA200 from Yahoo, the card computes one from Longbridge; a gap wider than
  `sma_cross_source_tolerance_pct` (0.5) shows a `≠SMA200` badge, the same idiom as the `≠api`
  local-vs-vendor delta check. Measured agreement is far inside that: SPY 0.001%, BRK.B 3e-8%.
  Never average the two or silently prefer whichever rendered last.
- **A vol index is not the ticker's IV.** VIX is a whole-strip variance measure; measured
  VIX ÷ SPY ATM IV = **1.192** (82 aligned sessions). Rank and percentile are invariant under a
  positive affine map, so *rank* transfers (measured median error 1.33pp, p90 3.15pp vs DoltHub's
  independent SPY ATM IV) — the *level* must never be displayed as the ticker's IV.
- **Index rows are stored under the index's own symbol** (`VIX`, source `cboe:VIX`), never relabelled
  as `SPY`.
- **Do not claim a sample size we do not hold.** DoltHub supplies an asserted 52-week high/low, not
  the 252 observations behind it, so that card prints "publisher's range" and shows **no percentile**
  — a percentile needs the distribution.
- Below `min_iv_history_days` (120) the rank card is greyed and marked too thin to trust.

## Unit traps (all confirmed live)

| Trap | Rule |
|---|---|
| `calc_indexes` theta/vega/rho are **×100** | Divide by 100 for per-share. Delta and gamma are **not** scaled. |
| `calc_indexes.implied_volatility` is a **percent** (`43.54`); `OptionQuote.implied_volatility` is a **ratio** (`0.4354`) | Normalize at ingestion. |
| SDK datetimes are **naive host-local**, but naive inputs are read as **UTC** | `app/config.py` pins `TZ=UTC` before the SDK is imported and attaches `timezone.utc` to every returned datetime. Import `app.config` first. |
| Decimals | Converted to `float` at the client boundary, never deeper in. |
| Per-contract vs per-share | For SPY at $747 / 48 DTE, ATM vega ≈ **$99/contract**, theta ≈ **$15/day/contract**. If theta reads ~0.15, the `×100` was missed. Assert against `S·φ(0)·√T`, not a fixed dollar band — these scale with the underlying. |

## Hard platform constraints

**`longport` is pinned to `3.0.18`.** This host is glibc 2.34; all 4.x wheels are tagged
`manylinux_2_39` with no sdist. Python 3.11. 3.0.x uses `Config.from_env()` — `from_apikey_env()`
arrived in 4.0.0, so current docs do not apply.

**`yfinance` is a scraper, not a supported API.** It exists here only because `candlesticks()` caps
at 1000 bars and therefore cannot see a true all-time high; Yahoo reaches inception (SPY 1993-01-29,
8433 closes; PYPL 2015; BRK-B 1996). Consequences that are all load-bearing:
- `import yfinance` lives **inside** `fetch_yahoo`, never at module scope — it costs ~1s and drags in
  its own pandas machinery, and `pytest` must not load it at all.
- It **never runs on the request path**: `refresh_ticker` is called from `asyncio.to_thread` and gated
  to once a day via `fetch_log`. Reads (`series()`) are cache-only.
- Every failure mode is funnelled into `PriceSourceError`, logged with `mark_fetched(ok=False)`, and
  degrades to the Longbridge window — never a 500. A delisted symbol surfaces as
  `YFTzMissingError: possibly delisted`, not an HTTP error, so catch broadly.
- Yahoo needs `BRK-B`, not `BRK.B`: `yahoo_symbol()` is the one symbol translation.
- Zero option quota. Daily bars and Yahoo are both free of the 500-symbol/min ceiling.

**`calc_indexes.theta` is unusable.** Measured against closed form it flips sign across moneyness
(−72.6, −22.2, −1.1, +12.4, +20.9) and grows *more* negative with DTE, which is backwards. A field-order
bug is ruled out: delta 0.96, gamma 1.00, vega 99.49, rho 99.00 all map correctly. **All greeks are
therefore local Black-76** (`app/metrics/pricing.py`) from the API's IV (which is sound — textbook skew),
with a parity-implied forward and `r` from FRED DGS3MO. The API's delta is kept only as a health check
(local runs 0.010–0.015 above it, inside the 0.02 tolerance).

**Undocumented: option quote requests are capped at 500 option symbols per rolling minute** (error
`301607`, *"Too many option securities request within one minute"*). This appears nowhere in the official
docs, which describe only the 500-symbol *concurrent-subscription* ceiling. It is the binding constraint
on the whole design.

**Those two 500s are different in kind and need different answers.** The undocumented quota is a
**rate**, so an over-budget request can *wait* and the page can say so. `MAX_SUBSCRIBED_SYMBOLS = 500`
is a **concurrency** ceiling with nothing to wait for, and `SubscriptionManager` evicts **by ticker** —
so the multi-tenor design had to keep per-ticker symbol count flat (hence the top-50 funnel) or SPY and
QQQ would erase each other on every render. Measured: SPY lists 39 expiries and QQQ 38; the default
35–60 DTE window holds **3** (41, 48, 60) and a 7–60 window **13**. A slice costs **28 units** (≤24
strikes through `calc_indexes` plus ~4 for the ATM probe), and it is re-paid on every refresh.

| Operation | Option-quota cost |
|---|---|
| `calc_indexes(n symbols)` | n, **every call** — repeats are not free |
| `subscribe(n symbols)` | n, but **only once** |
| `depth(symbol)` REST | 1 |
| `realtime_depth` / `realtime_quote` cache reads | **free** |
| `option_chain_expiry_date_list`, `option_chain_info_by_date` | **free** |

`app/providers/quota.py` gates every billed call through `OptionQuotaGovernor` (sliding window, working
limit 450 of the measured 500, 65s backoff). A chain rebuild costs **28 units** measured **per tenor**, so
`greeks_poll_seconds=60` keeps 12 single-tenor tickers at ~336 units/min. Never lower it to 30 without
redoing that arithmetic. A multi-tenor view pays `28/min` for its fast slice plus
`28×(n−1)/slow_tenor_poll_seconds` for the rest — ~95/min at 13 tenors — which is why the poller budgets
each view against its **own** `chain.quota_spent` rather than one global estimate.

Other SDK facts worth not rediscovering:
- **`calc_indexes` returns no bid or ask.** `ContractCalc` is
  `symbol, iv, delta, gamma, vega, rho, open_interest, last_done, strike, expiry` — so premium, spread and
  liquidity simply do not exist until something is subscribed or a `depth()` unit is spent. Any ranking
  that happens before subscription is therefore model-based by necessity, not by preference.
- `candlesticks()` caps the bar count: 1000 works, **2000 fails** with `301607 "request too many klines"`.
  Prefer it over `history_candlesticks_*`, which carries a monthly distinct-symbol quota (100 baseline).
- **3.0.18 `subscribe()` takes only `(symbols, sub_types)`** — no `is_first_push` in any spelling. Callers
  **must** seed state from REST `quote()`/`depth()`, or a quiet symbol renders permanently empty.
- `subscriptions()` keeps returning unsubscribed symbols with an empty `sub_types` list; filter those or
  the budget overcounts and reconnect reconciliation breaks.
- `PushQuote` has no `symbol` field (it is the callback's first argument) and no `prev_close`; seed
  `prev_close` once from `quote()`.
- The server drops its subscription set when the long link dies while our book survives — only
  reconciliation gets symbols ticking again.
- **DoltHub applies a server-side query deadline.** Any range scan of `volatility_history` dies partway
  with `context deadline exceeded`. Only single-row `ORDER BY date DESC LIMIT 1` lookups are usable, and
  those take 40–120s, so DoltHub is a **bounds** source, never a series source, and must never be called
  on the request path. Rows may carry NULL `iv_current`; filter them. A missing symbol (permanent, cache
  `NO_COVERAGE` for the day) and a transport failure (transient, retry) need different retry policies.

## Architecture

Three clocks, one page: **prices push (free), the best tenor polls (billed), the comparison tenors
crawl (billed, amortised)**.

```
Longbridge WS ──push──> QuoteHub ──┐
 (Quote+Depth, top 50)             │  symbol -> {bid, ask, last, ts}
calc_indexes ─poll  (60s, best) ───┤  symbol -> {delta, gamma, theta, vega, oi, iv}
calc_indexes ─slow (300s, others) ─┤
                                   └──> SSE /api/stream/{ticker} ──> browser patches cells
```

### The funnel: rank first, subscribe second

Every expiry inside the DTE window is priced (no cap — `choose_expiries`), which is 13 tenors × 24
strikes = **312 contracts** on a 7–60 window. The naive order — subscribe an expiry's strikes, then
decide which one matters — cannot survive that, so it is inverted:

| Stage | What | Cost |
|---|---|---|
| **build** | every expiry in the window, full screen band, greeks via `calc_indexes`. **No subscriptions, no depth, no seed.** | 28/tenor, billed |
| **rank** | score all 312 candidates from data build already paid for (`rank_for_quotes`) | free |
| **subscribe** | the top `max_quoted_contracts` (50) only, Quote+Depth | 50 units **once**, then free pushes |
| **re-rank** | `apply_quotes` folds real bid/ask in and re-ranks on market figures | free |

Subscription pressure is therefore **flat in the width of the window** — 50/ticker whatever the tenor
count, so ten tickers fit under the concurrent ceiling where the naive design wanted 312 for one.

The funnel fixes concurrency; it does **not** fix rate, because `calc_indexes` charges n units *every
call* and so is re-paid on every refresh. That is what the slow clock is for: 13 tenors at 60s is
364 units/min/ticker, but `28 + 12×28/5min ≈ 95` fits. When a window is still too wide the governor
blocks, the build waits, and the panel says so — never a silent stale number.

- **`ContractCalc` carries no bid or ask**, so premium, spread and `liquid` do not exist at rank time.
  The pre-quote key is therefore a **local Black-76 model premium** at the API's IV plus
  `open_interest` as the only available liquidity prior, with `|delta − delta_target|` reserving each
  tenor's comparable contract a slot before rank fills the rest — otherwise a thin tenor reaches the
  term-structure table with no quote and the whole comparison rests on model prices.
- **A model figure can never become `ChainResult.match`.** The best tenor is chosen only among matches
  that hold a real two-sided quote.
- `build_chain` deliberately does **not** select the delta match — `select_by_delta` needs a live bid.
  `apply_quotes()` folds free quote books in and selects **per slice**, so the two clocks stay
  independent. Pooling every slice's candidates and selecting once was a real bug: roughly one strike
  per tenor sits near the target, so strike-grid rounding decided which tenor was presented as *the*
  match. `tests/test_chain.py::TestPerSliceMatch` pins it.
- **Re-pointing the fast clock needs hysteresis.** A challenger tenor must win `REPOINT_WINS` (2)
  consecutive refreshes before the 60s clock moves, or a tie oscillating on a half-cent mid re-prices
  two slices a minute forever.
- **The quoted set has no demotion hysteresis, deliberately.** Retaining a demoted incumbent either
  breaks the ≤50 cap or needs slot arbitration against the per-tenor reservations; and unlike the fast
  clock, the ranking only moves on a billed rebuild, where a marginal flip costs 1 subscribe unit.
- `ESTIMATED_REFRESH_COST` (40) is only a floor: the poller budgets each view against **its own** last
  `chain.quota_spent` (`TickerView.refresh_cost`), because a 13-tenor view costs an order of magnitude
  more than a 3-tenor one and a single global estimate would either starve the wide view or overdraw
  the narrow one.
- The symbol window is **centred on the delta target**, not truncated from the top: truncating dropped
  SPY's whole 0.10–0.15 delta region and pinned the match to the edge.
- A depth push and a quote push each carry only their half of the state, so `_apply` merges only
  non-`None` fields — otherwise every depth tick blanks `last`.
- REST seeding goes through a separate path from pushes, so a seed does not count as link traffic; a
  successful seed must not mask a dead websocket.
- `SubscriptionManager` runs LRU eviction **by ticker**, not by symbol — a ticker occupies its underlying
  plus its chain, so evicting half a set leaves a broken page.
- Only tickers with `viewers > 0` are polled, oldest-first, breaking once the governor has less than that
  view's `refresh_cost` left. An abandoned tab must not spend an account-wide budget.
- SSE is rate-capped with a 10s heartbeat so a client can tell a quiet market from a dead connection.
  `X-Accel-Buffering: no` is required or nginx buffers the stream into uselessness.
- The **first render is a complete server-side snapshot**; SSE only patches `data-f` cells afterwards. The
  page must stay fully readable with JavaScript disabled.
- **Chain-column documentation lives only in `app/columns.py`.** The table's `<thead>` (with its `title`
  tooltips), the footer "Chain columns" reference and `_conventions()["columns"]` all render from
  `chain_columns(params)`; thresholds interpolate the **request's** `ScreenParams` rather than repeating
  literals or startup values. Do not write a fourth prose copy.
  `tests/test_columns.py` fails if a `<td>` is added without an entry, or if a `data-f` cell is documented on
  the wrong clock. Each entry carries `updates` (`push`/`poll`/`slow`/`static`) because a row mixes cells
  of **three** different ages — a `poll` cell can be a full `greeks_poll_seconds` older than the bid beside
  it, and a comparison tenor's greeks a full `slow_tenor_poll_seconds`. Every row states its own age.
- **An unsubscribed row shows `—`, never a plausible number.** Only the top-50 quoted contracts have a
  bid, ask, mid, premium or liquidity flag; the other ~262 rows are visible context and render `—` in
  every quote column, with their model figure in its own labelled `Model ann %` column. SSE patches
  quoted rows only.
- If the link drops, show "delayed — reconnecting" rather than silently serving stale numbers.

## Screening parameters

The ten numbers that decide what the screener shows are editable per request (`app/params.py`). The
values in `app/config.py` are now the **code baseline**, not the only possible values.

**`ScreenParams` is threaded as an argument, never installed as a global.** `settings` is a
`frozen=True` dataclass instantiated once and imported *by value* — eight modules hold the object, and
there is no `get_settings()` seam — so `dataclasses.replace()` plus rebinding would be invisible to
`chain.py`, `options.py`, `live.py` and `columns.py`. Do not add a request-scoped mutable singleton
for this; pass the frozen object down. `build_chain`, `choose_expiries`, `target_strikes(…, target=)`,
`apply_quotes`, `build_candidate`, `with_quote`, `chain_columns`, `row_states` and every `LiveEngine`
entry point take it.

**Platform ceilings are not screening parameters.** `greeks_poll_seconds`, `slow_tenor_poll_seconds` and
`max_quoted_contracts` stay in `settings` and **out** of `ScreenParams`: they are how much of a shared
account-wide budget one viewer may spend, not what the screener shows, and a URL that could raise them
would let any unauthenticated visitor starve every other tab.

**Three cost classes, all measured live.** The split is the whole point of the design, and it is not
guessable from the UI, so `params.FIELDS` carries each field's class and the form labels it:

| Class | Fields | Cost |
|---|---|---|
| **free** | `delta_target`, `delta_band`, `max_abs_spread`, `max_rel_spread_pct` | **0 option quota.** Applied entirely inside `apply_quotes`, which is pure and already re-runs on every render and every SSE frame. Measured: the match moved 705 → 720 → 705 with `quota_spent` pinned at 103. |
| **rebuild** | `dte_min`, `dte_max`, `delta_screen_band` | Changes which option symbols are requested, so a full `build_chain`: **~28 units** of the 450 working limit, plus `hub.seed` at 1/unseen symbol and subscribe churn. Measured 103 → 143; an immediate repeat of the same URL added nothing. |
| **trend** | `sma_window`, `rv_window`, `chart_window_sessions` | 0 option quota but re-runs `_underlying` — a quote, 1000 daily bars and a SQLite chart rebuild. Measured: three successive changes all left `quota_spent` at 94. |

`chain_key` and `trend_key` in `app/params.py` are the two comparisons that decide this. Keep them in
step with the table above; misfiling a field either bills the user for a free edit or serves a stale chain.

**Precedence: URL parameter → saved default → code default**, compared **by value** so each field's
rendered `origin` names the layer that actually supplied the number — a rejected URL value reads
`saved` or `code`, not `url`. The saved row (`screen_defaults`, one row, `store.py`) is a **snapshot,
not a diff**: `to_dict()` writes all ten fields, so after Save every field reads `saved` and a later
change to a code default cannot silently drift a pinned baseline. Unknown keys, corrupt JSON and
hand-edited out-of-range values all degrade to the baseline with a message.

**Rebuild policy on the cached view.** One `TickerView` per symbol, now remembering the `params` that
produced it. `chain_key` differs → billed rebuild; `trend_key` differs → re-run `_underlying` only;
only free fields differ → **no rebuild at all**, store the new params and let `apply_quotes` re-derive
at render time. Two browsers on different chain keys make each other's views rebuild; that is the price
of not multiplying the cache, and it is why `_refresh`'s "another caller holds the lock" early return
must **re-check `view.params` after the lock releases** — otherwise the waiter is handed a chain built
for someone else's parameters.

A billed rebuild **waits** on the governor rather than refusing, so the wait is reported in three
places: the field's cost label before the click, `TickerView.quota_wait_seconds` in the panel after it
(measured live at 16.2s under a primed window), and `LinkStatus.label` → `waiting for option quota (Ns)`
from `governor.blocked_for()`, which ranks **ahead of** `market closed` and `live` so an already-open
tab sees the stall. A *new* SSE connection cannot show it — `api_stream` awaits `ensure` first.

**`with_quote` uses `dataclasses.replace()`**, so `in_delta_band` must be **recomputed** in
`apply_quotes`' refresh closure; inherited, a stale flag would survive a band change.

**The SSE stream must carry the parameters.** `dashboard.html` emits
`data-stream="/api/stream/SPY.US?<effective query>"` and `app.js` prefers it. Without this the stream
recomputes `match` and the liquidity flags from the code defaults and overwrites a correctly-rendered
page — verified: with `delta_target=0.25` the frame reports the 720 strike, not the default's 705.

**`from_query` never raises and never 500s.** A bad value keeps the baseline for that field and appends
a message the page renders inline. Cross-checks *repair* rather than reject: an inverted pair reverts,
a trading band outside the screening band **widens the screening band**, a target outside its own band
**clamps to the nearest edge** — and each says which value was ignored and why.

**The SMA/RV cards used to ignore the windows.** `live.py` called `compute_trend()` without
`sma_window`/`rv_window`, so the cards used the function defaults (200, 20) while only the chart read
`settings.sma_window`. They agreed by coincidence. Both now receive the request's windows; verified at
`sma_window=50`, card 744.21 vs chart 744.22 (the 0.01 gap is the two adjustment bases, not the fix).

**Starlette's `request.form()` asserts `python-multipart` is installed**, even for
`application/x-www-form-urlencoded`, and this repo does not depend on it. `main.py:_form` decodes the
body with `parse_qsl` instead. Do not reach for `Form(...)` here.

## Working here

- `./run.sh {start|stop|restart|status|log}` manages the server as a background process (pidfile in `data/run/`,
  log in `data/logs/app.log`, `PORT`/`HOST` overridable, default `0.0.0.0:9288` — 9188 belongs to another service
  on this host). It exports `TZ=UTC` itself. For foreground work with autoreload,
  `source .venv/bin/activate && TZ=UTC uvicorn app.main:app --reload`.
- **The default bind is every interface and there is no authentication.** On this host `hostname -I` is a public
  address, so anyone who can reach the port can load arbitrary tickers and spend the account-wide 500-symbol/min
  option quota that every ticker shares. Editable parameters widen this: a billed rebuild is now a lever any
  caller can pull on demand, and `POST /settings/save` / `POST /settings/reset` change the saved baseline for
  everyone. Validated bounds and one saved row keep the surface small, but the mitigation is
  `HOST=127.0.0.1` for loopback-only, or authentication.
- `pytest` must stay **hermetic** — no network, no writes to `data/dashboard.db`. Stub `httpx.get`, and
  monkeypatch `live_mod.ivhistory` **and `live_mod.pricehistory`** when exercising the engine (a fixture
  once wrote synthetic IV into the production database; `_build_chart` would likewise cache real closes
  into it, and `_schedule_price_fetch` would scrape Yahoo). Stub `pricehistory.fetch_yahoo` at the module
  attribute, never the yfinance object itself.
- Assertions that need a live or slow external source live in `scripts/probe_*.py`, not in the test suite.
- No `innerHTML` in `app/static/app.js`; build nodes and set `textContent`.
