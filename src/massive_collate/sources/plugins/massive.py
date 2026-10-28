"""Massive (massive.com): stocks, indices, currencies, crypto and futures, one plan per asset class.

Its scan finds the stocks plan from what the key may ask for (MassiveSource.plan_reach) and sets
MASSIVE_TIER where you have not. The other classes' plans are not probed (PLAN_NOTE).

Its headlines are a publisher of its own (PUBLISHER, read by publications.py every 30 minutes),
on this feed's key and limits."""
from __future__ import annotations

from ...config import MASSIVE_CLASS_TIERS, TIERS, asset_tier, tier_for
from ...ratelimit import MachineLedger, RateLimiter
from ...symbols import ASSETS
from ..massive import PAGE_LIMIT, MassiveClient, MassiveSource
from ..publisher import Feed, Policy, Publisher
from ..registry import KeyVar, Provider, Scanned, limiter, machine_caps, minute_window

# plan_reach's answers, and the plans that have each (config.tier_for)
REACH = {"day_3y": "stock daily bars 3 years back (Starter and up)",
         "day_6y": "stock daily bars 6 years back (Developer and up)",
         "day_11y": "stock daily bars 11 years back (Advanced)",
         "day_21y": "stock daily bars 21 years back (Advanced)",
         "second": "stock 1-second bars (the paid plans)",
         "trades": "stock trades (Developer and up)",
         "quotes": "stock quotes (Advanced)"}
PLAN_NOTE = ("only the stocks plan (MASSIVE_TIER) is probed: the other asset classes' plans "
             f"({', '.join(MASSIVE_CLASS_TIERS.values())}) are what you set, free unless you do. "
             "A free class is held to 5 requests a minute, and a probe that took one for paid would lift that")


def build(settings, request_budget, max_retries: int = 4):
    key = settings.require_api_key()
    client = MassiveClient(
        key,
        base_url=settings.base_url,
        rate_limiter=limiter("massive", settings, key),
        request_budget=request_budget,
        max_retries=max_retries,
    )
    # Asset classes on another plan keep that plan's rate (the free plan: 5 a minute per class),
    # with the same margin as the feed's own window.
    client.class_limiters = {}
    for a, t in settings.tier.by_asset:
        client.class_limiters[a] = RateLimiter(t.requests_per_minute, minute_window(t.requests_per_minute))
        client.class_limiters[a].share(MachineLedger(f"massive:{a}", key))
    return MassiveSource(machine_caps(client, "massive", settings, key))


def scan(source) -> Scanned:
    """The stocks plan the key has (seven small requests, AAPL's): daily bars 3, 6, 11 and 21 years
    back, then the last week's 1-second bars, trades and quotes."""
    reach = source.plan_reach("AAPL")
    found = tier_for(reach)
    rows = [(REACH.get(k, k), ok, "") for k, ok in reach.items()]
    years = TIERS[found].history_years
    rows.append((f"stocks plan: {found}", True, f"{years} years of history" if years else "all of Massive's history"))
    return Scanned(tuple(rows), {"MASSIVE_TIER": found})


PLUGIN = Provider(
    name="massive", title="Massive", build=build, assets=frozenset(ASSETS), order=0,
    about="stocks, indices, currencies, crypto and futures: 1-second and minute bars, reference data, news",
    keys=(KeyVar("MASSIVE_API_KEY"),), signup="massive.com: your dashboard's API keys",
    plan_vars=("MASSIVE_TIER", *MASSIVE_CLASS_TIERS.values(), "MASSIVE_MAX_REQUESTS_PER_RUN",
               "MASSIVE_MAX_REQUESTS_PER_DAY"),
    plan_choices={v: tuple(TIERS) for v in ("MASSIVE_TIER", *MASSIVE_CLASS_TIERS.values())},
    plan_note=PLAN_NOTE, scan=scan,
    kind="consolidated", venue_notes={"crypto": "consolidated across exchanges"}, bulk_resolution="minute",
    per_request={"day": PAGE_LIMIT, "minute": PAGE_LIMIT, "second": PAGE_LIMIT},
    # 1-second bars on a paid plan of the asset class (the free plan: day and minute bars, 2 years)
    plan_resolutions=lambda tier, asset: ("day", "minute") if asset_tier(tier, asset).name == "free"
    else ("day", "minute", "second"),
    has_keys=lambda s: bool(s.api_key),
    serves=lambda s: frozenset(ASSETS) if s.api_key else None,  # not set up at all without its key
    markets=lambda s: {"stock": "sip", "index": "index", "fx": "otc", "crypto": "consolidated", "future": "cme"},
)

NEWS_DAYS = 2  # the first read reaches this far back


def read_headlines(ctx) -> int:
    """Massive's newest headlines (one request, on Massive's limits: its rate window, daily cap and
    kills), into the main database's news for the News view and the globe's ticker. Returns how many.
    Never retried here: a 429 is the account's to answer (RateLimited kills it), anything else the
    reader's to ask again on its schedule (with a budget of one, a retry only failed again)."""
    from datetime import datetime, timedelta, timezone

    source = build(ctx.settings.for_source("massive"), 1, max_retries=0)
    source.client.on_request = lambda n: (ctx.store.count_request("massive", now=ctx.now), ctx.count(1))
    newest = ctx.store.latest_news_ms("massive")
    since = datetime.fromtimestamp(newest / 1000, timezone.utc) if newest else ctx.now - timedelta(days=NEWS_DAYS)
    n = ctx.store.save_news("massive", source.news(since, 100))
    ctx.store.kv_set("news:massive", ctx.now.isoformat())  # the Fetcher's "Headlines" row reads when
    return n


PUBLISHER = Publisher(
    name="massive-news", title="Massive headlines", order=300, kind="vendor", account="massive",
    about="news headlines for the News view and the globe's ticker, on Massive's key and limits",
    feeds=(Feed("massive-news", "Massive", "Headlines", "https://api.massive.com/v2/reference/news", "headlines",
                every_minutes=30, moments=False),),
    policy=Policy(min_minutes=30, per_day=60, terms="your Massive plan's terms; one request a read",
                  terms_url="https://massive.com/terms"),
    read=read_headlines,
)
