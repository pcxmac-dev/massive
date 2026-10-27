"""Massive (massive.com): stocks, indices, currencies, crypto and futures, one plan per asset class."""
from __future__ import annotations

from ...config import MASSIVE_CLASS_TIERS, asset_tier
from ...ratelimit import MachineLedger, RateLimiter
from ...symbols import ASSETS
from ..massive import PAGE_LIMIT, MassiveClient, MassiveSource
from ..registry import KeyVar, Provider, limiter, machine_caps, minute_window


def build(settings, request_budget):
    key = settings.require_api_key()
    client = MassiveClient(
        key,
        base_url=settings.base_url,
        rate_limiter=limiter("massive", settings, key),
        request_budget=request_budget,
    )
    # Asset classes on another plan keep that plan's rate (the free plan: 5 a minute per class),
    # with the same margin as the feed's own window.
    client.class_limiters = {}
    for a, t in settings.tier.by_asset:
        client.class_limiters[a] = RateLimiter(t.requests_per_minute, minute_window(t.requests_per_minute))
        client.class_limiters[a].share(MachineLedger(f"massive:{a}", key))
    return MassiveSource(machine_caps(client, "massive", settings, key))


PLUGIN = Provider(
    name="massive", title="Massive", build=build, assets=frozenset(ASSETS), order=0,
    about="stocks, indices, currencies, crypto and futures: 1-second and minute bars, reference data, news",
    keys=(KeyVar("MASSIVE_API_KEY"),), signup="massive.com: your dashboard's API keys",
    plan_vars=("MASSIVE_TIER", *MASSIVE_CLASS_TIERS.values(), "MASSIVE_MAX_REQUESTS_PER_RUN",
               "MASSIVE_MAX_REQUESTS_PER_DAY"),
    kind="consolidated", venue_notes={"crypto": "consolidated across exchanges"}, bulk_resolution="minute",
    per_request={"day": PAGE_LIMIT, "minute": PAGE_LIMIT, "second": PAGE_LIMIT},
    # 1-second bars on a paid plan of the asset class (the free plan: day and minute bars, 2 years)
    plan_resolutions=lambda tier, asset: ("day", "minute") if asset_tier(tier, asset).name == "free"
    else ("day", "minute", "second"),
    has_keys=lambda s: bool(s.api_key),
    serves=lambda s: frozenset(ASSETS) if s.api_key else None,  # not set up at all without its key
    markets=lambda s: {"stock": "sip", "index": "index", "fx": "otc", "crypto": "consolidated", "future": "cme"},
)
