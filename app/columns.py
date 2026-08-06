"""One definition per chain-table column, rendered in three places.

The `<thead>`, the footer reference table and `/api/ticker/{ticker}`'s conventions block all
read this tuple, so a formula can never be right in one surface and stale in another.

`updates` matters as much as `formula`, because one row mixes cells of three different ages:

  push    quotes arrive free on the websocket, but ONLY for the ranked contracts that were
          subscribed. An unquoted row shows a dash in every push cell — see UNQUOTED_NOTE.
  poll    re-priced on the billed rebuild. For the best tenor that is every
          `greeks_poll_seconds`; for every other expiry in the DTE window it is every
          `slow_tenor_poll_seconds`, so the same column can be seconds old on one row and
          minutes old on the next. The `age` cell states which.
  static  fixed for the life of the contract.

Every threshold is a function of the request's `ScreenParams`, not of import-time settings:
a page loaded with a 0.5% spread gate must not carry tooltips describing the 2.0% one the
process started with.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import MAX_SUBSCRIBED_SYMBOLS, settings
from app.params import ScreenParams, defaults

PUSH = "push"  # patched by SSE as quotes arrive, and only for subscribed contracts
POLL = "poll"  # recomputed on the billed chain rebuild
SLOW = "slow"  # billed too, but on the comparison tenors' own, slower timer
STATIC = "static"  # fixed for the life of the contract


@dataclass(frozen=True)
class Column:
    key: str
    header: str  # exactly the <th> text; empty for the flags column
    label: str
    meaning: str
    formula: str
    updates: str


def chain_columns(params: ScreenParams | None = None) -> tuple[Column, ...]:
    p = params or defaults()
    lo, hi = p.delta_band
    return (
        Column(
            key="expiry",
            header="Expiry",
            label="Expiration date",
            meaning=(
                "Which tenor the contract belongs to. Every expiry listed inside the "
                f"{p.dte_min}-{p.dte_max} DTE window is priced and ranked here, because "
                "choosing the tenor is part of the trade, not a setting. Rows are ordered by "
                "rank, not by date."
            ),
            formula="Listed by the exchange; DTE is calendar days in America/New_York.",
            updates=STATIC,
        ),
        Column(
            key="strike",
            header="Strike",
            label="Strike",
            meaning=(
                "The price you are obliged to buy at if assigned. Cash-securing one contract "
                "requires 100 x strike, so this is the denominator of every yield here."
            ),
            formula="Listed by the exchange; not computed.",
            updates=STATIC,
        ),
        Column(
            key="moneyness_pct",
            header="Moneyness",
            label="Moneyness",
            meaning=(
                "How far the strike sits below spot. Negative means out of the money for a put. "
                "Measured against the spot at the last chain rebuild, NOT the live price in the "
                "panel heading, so it lags a moving quote by up to "
                f"{int(settings.greeks_poll_seconds)}s."
            ),
            formula="(strike / spot - 1) x 100",
            updates=POLL,
        ),
        Column(
            key="iv",
            header="IV",
            label="Implied volatility",
            meaning=(
                "This contract's own implied vol — the wing, not the money. Put skew makes it "
                "run above ATM IV by construction, which is why the IV-vs-RV card refuses to "
                "use it. Annualized, shown as a percent."
            ),
            formula=(
                "The API's implied vol, normalized to a ratio at ingestion (one SDK surface "
                "returns 43.54, the other 0.4354) and displayed x100."
            ),
            updates=POLL,
        ),
        Column(
            key="delta",
            header="Δ",
            label="Delta",
            meaning=(
                "Magnitude of the put's delta, from our own Black-76 rather than the vendor's. "
                "Read as the risk-neutral chance of finishing in the money — a pricing "
                f"quantity, not a forecast. Target {p.delta_target:g}, band {lo:g}-{hi:g}. "
                "The ≠api badge means the vendor's delta differs by more than 0.02."
            ),
            formula="|-e^(-qT) N(-d1)|, d1 = (ln(F/K) + sigma^2 T/2) / (sigma sqrt(T))",
            updates=POLL,
        ),
        Column(
            key="vega_contract",
            header="Vega/ct",
            label="Vega per contract",
            meaning=(
                "Dollars the contract gains per 1 point of implied vol. A put seller is SHORT "
                "vega, so a vol spike is a loss even with spot unchanged."
            ),
            formula=(
                "Black-76 vega S e^(-qT) phi(d1) sqrt(T), divided by 100 for per-IV-point then "
                "x100 for the contract — so the figure shown equals the raw per-share vega."
            ),
            updates=POLL,
        ),
        Column(
            key="theta_day_contract",
            header="Θ/day/ct",
            label="Theta per day per contract",
            meaning=(
                "Dollars of time decay the contract sheds per calendar day, signed for the "
                "SELLER: positive is income to you. The vendor's theta flips sign across "
                "moneyness and is not used anywhere."
            ),
            formula="-(local Black-76 theta per calendar day) x 100",
            updates=POLL,
        ),
        Column(
            key="open_interest",
            header="OI",
            label="Open interest",
            meaning=(
                "Contracts outstanding at the last settlement. A depth hint only — it is not "
                "volume and it does not move intraday."
            ),
            formula="Reported by the chain feed; not computed.",
            updates=POLL,
        ),
        Column(
            key="bid",
            header="Bid",
            label="Bid",
            meaning=(
                "Best price a buyer is showing — what you would actually receive selling into "
                "the market right now. The conservative premium figure is built from this."
            ),
            formula="Top of book, from the free quote push stream.",
            updates=PUSH,
        ),
        Column(
            key="ask",
            header="Ask",
            label="Ask",
            meaning="Best price a seller is showing; what it would cost to close the position.",
            formula="Top of book, from the free quote push stream.",
            updates=PUSH,
        ),
        Column(
            key="mid",
            header="Mid",
            label="Mid",
            meaning=(
                "Midpoint of the quoted spread. A reference price, not a fill — on a wide "
                "market nobody trades here."
            ),
            formula="(bid + ask) / 2; blank unless ask >= bid > 0.",
            updates=PUSH,
        ),
        Column(
            key="rel_spread_pct",
            header="Spread",
            label="Relative spread",
            meaning=(
                "Width of the market as a percent of mid — the round-trip friction. Shown "
                "relative because an absolute width means little without the price level."
            ),
            formula="(ask - bid) / mid x 100",
            updates=PUSH,
        ),
        Column(
            key="premium_pct",
            header="Prem %K",
            label="Premium, percent of strike",
            meaning=(
                "Premium as a percent of the cash you must set aside — the actual period "
                "return on a cash-secured put, quoted at mid."
            ),
            formula="mid / strike x 100",
            updates=PUSH,
        ),
        Column(
            key="annualized_pct",
            header="Ann %",
            label="Annualized premium",
            meaning=(
                "The period yield scaled to a year, simple and uncompounded. Not a return you "
                "can expect to repeat: it assumes the same premium is on offer every cycle, "
                "which is exactly false after a vol crush."
            ),
            formula="Prem %K x 365 / DTE",
            updates=PUSH,
        ),
        Column(
            key="model_annualized_pct",
            header="Model ann %",
            label="Model annualized premium",
            meaning=(
                "The same annualized yield computed from a MODEL price instead of a market "
                "mid, so an unsubscribed contract can still be ranked and compared. It is "
                "never a substitute for the quote columns beside it: a model figure and a "
                "market figure never share a column, and a model figure can never make a "
                "contract the page's pick. Rows with a live quote are ranked on the market "
                "number; this one is for the rest of the screen band."
            ),
            formula=(
                "Black-76 price at the API's implied vol on the parity-implied forward, "
                "/ strike x 100 x 365 / DTE. No bid or ask is involved."
            ),
            updates=POLL,
        ),
        Column(
            key="gamma_per_premium",
            header="Γ/$",
            label="Gamma per premium dollar",
            meaning=(
                "Convexity bought per dollar of premium collected — the price of a high "
                "annualized yield. At the same delta a short tenor almost always annualizes "
                "higher, and this is what it is charging for: gamma per dollar rises sharply "
                "as DTE falls. Computed from the market mid only, so an unquoted row is blank "
                "rather than flattered by a model price."
            ),
            formula=(
                "gamma / mid — both per contract, so the two factors of 100 cancel. As of the "
                "last rebuild; it is not patched live."
            ),
            updates=POLL,
        ),
        Column(
            key="age",
            header="Age",
            label="Age of the priced figures",
            meaning=(
                "How long ago this row's greeks were billed, and on which clock. The best "
                f"tenor is re-priced every {int(settings.greeks_poll_seconds)}s; every other "
                f"expiry every {int(settings.slow_tenor_poll_seconds)}s, because pricing every "
                "tenor on the fast clock would exceed the option quota outright. A comparison "
                "row is therefore legitimately minutes old, and says so rather than borrowing "
                "the live row's credibility."
            ),
            formula="Wall clock since the slice was built; 'live' is the fast tenor.",
            updates=SLOW,
        ),
        Column(
            key="flags",
            header="",
            label="Flags",
            meaning=(
                "Liquidity warnings. 'no bid' means there is no exit at any price, so the "
                "contract is never treated as liquid. 'wide' means it failed both gates."
            ),
            formula=(
                f"liquid when bid > 0 and (ask - bid <= {p.max_abs_spread:g} or relative "
                f"spread <= {p.max_rel_spread_pct:g}%)"
            ),
            updates=PUSH,
        ),
    )


def as_json(params: ScreenParams | None = None) -> list[dict]:
    return [
        {
            "key": c.key,
            "header": c.header,
            "label": c.label,
            "meaning": c.meaning,
            "formula": c.formula,
            "updates": c.updates,
        }
        for c in chain_columns(params)
    ]


def row_states(params: ScreenParams | None = None) -> tuple[tuple[str, str], ...]:
    p = params or defaults()
    lo, hi = p.delta_band
    slo, shi = p.delta_screen_band
    return (
        (
            "best",
            "Boxed: the highest annualized premium among the tenors whose match has a "
            "two-sided, liquid quote. A description of the ranking, not a recommendation — "
            "check the Γ/$ and Age cells before acting on it.",
        ),
        (
            "match",
            f"Highlighted: each expiry's own closest contract to the {p.delta_target:g} delta "
            f"target — one per tenor, so tenors are compared at comparable moneyness. Shown "
            f"even when nothing lands inside {lo:g}-{hi:g}, flagged rather than mislabelled.",
        ),
        (
            "in-band",
            f"Shaded: delta inside the {lo:g}-{hi:g} trading band. Every row in the table is "
            f"already inside the wider {slo:g}-{shi:g} screening band, which is what was "
            f"requested from the feed.",
        ),
        (
            "unquoted",
            "Dimmed: screened and ranked, but not subscribed, so it has no market price. See "
            "the dash note below.",
        ),
    )


MISSING_NOTE = (
    "A dash means the figure is not computable from the data in hand — a one-sided market, "
    "a missing vol, an expired contract. It is never a zero and never a guess."
)

UNQUOTED_NOTE = (
    f"Only the top {settings.max_quoted_contracts} ranked contracts across all tenors are "
    f"subscribed, because the account may hold at most {MAX_SUBSCRIBED_SYMBOLS} live "
    "subscriptions at once and a wide DTE window screens several hundred. Every other row "
    "keeps its full screen-band context, with a dash in the bid/ask/mid/spread/premium "
    "columns and a labelled model figure instead — never a market-looking number that no "
    "market made."
)
