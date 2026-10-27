"""Massive (massive.com): stocks, indices, currencies, crypto and futures, one plan per asset class."""
from __future__ import annotations

from ...config import MASSIVE_CLASS_TIERS
from ...ratelimit import MachineLedger, RateLimiter
from ...symbols import ASSETS
from ..massive import MassiveClient, MassiveSource
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
    has_keys=lambda s: bool(s.api_key),
    serves=lambda s: frozenset(ASSETS) if s.api_key else None,  # not set up at all without its key
    markets=lambda s: {"stock": "sip", "index": "index", "fx": "otc", "crypto": "consolidated", "future": "cme"},
)
