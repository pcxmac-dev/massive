"""Tiingo (tiingo.com): US stock daily bars from 1962 with a free account key; a closed archive.

Its scan (`capabilities`, one request each) asks what else the account's plan gives: IEX's minute
bars (one exchange), forex and crypto bars, and Blue Ocean's overnight bars (a paid add-on,
2026-10-27: $9 a month; Alpaca's free plan has them: boats.py, and from earlier: Tiingo had none
before 2024-09-25, asked 2026-10-28). The stocks' daily bars are collected here, the forex
minutes on a feed of their own on this key's limits (tiingofx.py).

Its plan is found in part: Tiingo's API has no account endpoint and sends no rate-limit headers
(2026-10-27), but its News API is the paid plans' (Power, Commercial) and not Starter's (the pricing
table, 2026-10-27), so the scan's News request tells Starter from a paid plan. Power and Commercial
reach the same data at other limits: a paid key is taken for Power, the lower limits, unless
TIINGO_PLAN says commercial (starter, the default; power; commercial: the key script and the Plugins
view offer them). It also asks for the fundamentals add-on, of a stock outside the Dow 30 (those
are free to evaluate)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from ...config import PUBLIC_FEEDS
from ...errors import SourceError
from ..public import PublicClient, TiingoSource
from ..registry import KeyVar, Provider, Scanned, limiter, machine_caps

TIER, PER_RUN, PER_DAY = PUBLIC_FEEDS["tiingo"]
BARS = "open,high,low,close,volume"

# Tiingo's plans (tiingo.com/about/pricing, both tables: "I am an Individual" and "I am a Business or
# Organization", 2026-10-27), each kept a little inside: (plan, requests an hour (a run's cap too: the
# sync runs hourly), a day, different symbols a month).
#   Starter (free): 50 an hour, 1,000 a day, 500 symbols, 1 GB a month.
#   Power (individuals, $30 a month or $300 a year): 10,000 an hour, 100,000 a day, every symbol
#     (110,397), 40 GB a month.
#   Commercial (a business's internal use, up to two developers, $50 a month or $499 a year): 20,000 an
#     hour, 150,000 a day, every symbol, 100 GB a month.
# Enterprise and redistribution are priced by Tiingo's sales, with no published limits: not offered.
PLANS = {
    "starter": (TIER, PER_RUN, PER_DAY, 450),
    "power": (replace(TIER, name="tiingo-power"), 9_500, 95_000, None),
    "commercial": (replace(TIER, name="tiingo-commercial"), 19_000, 142_500, None),
}
ALIASES = {"free": "starter", "business": "commercial"}
PLAN_NOTE = ("Tiingo's API does not say which plan a key is on (no account endpoint, no rate-limit headers). "
             "Its scan tells Starter from a paid plan by the News API, which only Power and Commercial have; "
             "it cannot tell Power from Commercial (the same data, other limits), so a paid key gets Power's "
             "limits unless you set TIINGO_PLAN=commercial")
NEWS = "news (the paid plans: Power and Commercial, not Starter)"
FUNDAMENTALS = "fundamentals (an add-on; asked for ROKU: the Dow 30 are free to evaluate)"


def plan_of(settings) -> str:
    """TIINGO_PLAN's plan (an unknown value: Starter, the free plan's limits, never more)."""
    name = (settings.env.get("TIINGO_PLAN") or "starter").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in PLANS else "starter"


def view(base):
    tier, per_run, per_day, _ = PLANS[plan_of(base)]
    return replace(base, tier=tier, max_requests_per_run=per_run, max_requests_per_day=per_day, origin=base)


def reporter(report, settings):
    if settings is None:
        return report
    return replace(report, limits=replace(report.limits, symbols_a_month=PLANS[plan_of(settings)][3]))


def build(settings, request_budget):
    key = settings.tiingo_api_key
    headers = {"Authorization": f"Token {key}"} if key else {}
    client = PublicClient("https://api.tiingo.com", headers=headers, request_budget=request_budget,
                          rate_limiter=limiter("tiingo", settings, key))
    source = TiingoSource(machine_caps(client, "tiingo", settings, key), has_key=bool(key))
    return monthly_symbols(source, settings, key)


def monthly_symbols(source, settings, key):
    """The plan's different symbols a month on a Tiingo source (collector._monthly_symbol), counted
    for the key on the whole machine: its stocks and its forex (tiingofx) take the same places."""
    from ...ratelimit import MachineLedger

    source.symbols_per_month = PLANS[plan_of(settings)][3]
    source.symbol_ledger = MachineLedger("tiingo:symbols", key)
    return source


def probes(today: date) -> list[tuple[str, str, dict]]:
    """(what, path, params) of the scan: a recent weekday of a busy symbol at each endpoint."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    d = day.isoformat()
    return [
        ("stock daily bars, from 1962", "/tiingo/daily/AAPL/prices", {"startDate": d, "endDate": d}),
        ("stock 1-minute bars from IEX (one exchange)", "/iex/AAPL/prices",
         {"startDate": d, "endDate": d, "resampleFreq": "1min", "afterHours": "true", "columns": BARS}),
        ("stock overnight bars from Blue Ocean (20:00-04:00)", "/boats/AAPL/prices",
         {"startDate": d, "endDate": d, "resampleFreq": "1min", "columns": BARS}),
        ("forex 1-minute bars", "/tiingo/fx/prices", {"tickers": "eurusd", "startDate": d, "endDate": d,
                                                      "resampleFreq": "1min"}),
        ("crypto bars", "/tiingo/crypto/prices", {"tickers": "btcusd", "startDate": d, "endDate": d,
                                                  "resampleFreq": "1min"}),
        (NEWS, "/tiingo/news", {"tickers": "aapl", "limit": 1, "startDate": d}),
        (FUNDAMENTALS, "/tiingo/fundamentals/ROKU/daily", {"startDate": d, "endDate": d, "columns": "marketCap"}),
    ]


def rows_of(answer) -> int:
    """Bars in an answer: a list of bars, or crypto's [{priceData: [...]}]."""
    if isinstance(answer, list) and answer and isinstance(answer[0], dict) and "priceData" in answer[0]:
        return sum(len(a.get("priceData") or []) for a in answer)
    return len(answer) if isinstance(answer, list) else 0


def scan(source, today: date | None = None) -> Scanned:
    """Each probe once; the plan from the News request: news served (articles), a paid plan
    (Power's limits, the lower: Commercial's answers are the same); refused by the plan (401, 403),
    Starter; anything else (a timeout, a server error, an answer without articles: an error said
    in a 200, an empty list), no plan: a paid plan's far higher limits are never taken on less."""
    out, plan, alike = [], {}, {}
    for what, path, params in probes(today or date.today()):
        try:
            answer = source.client.get(path, params)
            n = rows_of(answer)
        except SourceError as e:  # NotEntitled included: a refusal names the add-on it needs
            out.append((what, False, str(e)[:200]))
            if what == NEWS and e.status in (401, 403):
                plan["TIINGO_PLAN"] = "starter"
            continue
        if what == NEWS:
            articles = isinstance(answer, list) and any(isinstance(a, dict) and (a.get("title") or a.get("id"))
                                                        for a in answer)
            if not articles:
                out.append((what, False, f"answered without articles ({str(answer)[:120]}): the plan is not told"))
                continue
            plan["TIINGO_PLAN"], alike["TIINGO_PLAN"] = "power", ("commercial",)
            out.append((what, True, "served: a paid plan, Power or Commercial (the same data)"))
        elif what == FUNDAMENTALS:
            out.append((what, True, f"served: the add-on ({n:,} rows)"))
        else:
            out.append((what, True, f"{n:,} bars for {params['startDate']}"))
    return Scanned(tuple(out), plan, alike)


PLUGIN = Provider(
    name="tiingo", title="Tiingo", build=build, assets=frozenset({"stock"}), order=60,
    about="US stock daily bars from 1962 (a free account key)",
    keys=(KeyVar("TIINGO_API_KEY"),), signup="tiingo.com: a free account's API token",
    archival=True, tier=TIER, per_run=PER_RUN, per_day=PER_DAY,
    limits="Starter (free): 50 requests an hour, 1,000 a day, 500 symbols and 1 GB a month; "
           "Power ($30 a month): 10,000 an hour, 100,000 a day, 40 GB a month; "
           "Commercial ($50 a month, a business): 20,000 an hour, 150,000 a day, 100 GB a month",
    symbols_per_month=450,  # Starter's 500 different symbols a month, a little inside (Power: PLANS)
    plan_vars=("TIINGO_PLAN",), plan_choices={"TIINGO_PLAN": tuple(PLANS)}, plan_note=PLAN_NOTE,
    view=view, reporter=reporter,
    provides={"stock": ("day",)}, per_request={"day": None},  # one request: a symbol's whole history
    has_keys=lambda s: bool(s.tiingo_api_key),
    rate=lambda settings: (PLANS[plan_of(settings)][1], 3600.0),  # Starter: 50 an hour
    markets=lambda settings: {"stock": "sip"},
    scan=scan,
)
