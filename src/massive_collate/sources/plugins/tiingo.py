"""Tiingo (tiingo.com): US stock daily bars from 1962 with a free account key; a closed archive."""
from __future__ import annotations

from ...config import PUBLIC_FEEDS
from ..public import PublicClient, TiingoSource
from ..registry import KeyVar, Provider, limiter, machine_caps

TIER, PER_RUN, PER_DAY = PUBLIC_FEEDS["tiingo"]


def build(settings, request_budget):
    key = settings.tiingo_api_key
    headers = {"Authorization": f"Token {key}"} if key else {}
    client = PublicClient("https://api.tiingo.com", headers=headers, request_budget=request_budget,
                          rate_limiter=limiter("tiingo", settings, key))
    return TiingoSource(machine_caps(client, "tiingo", settings, key), has_key=bool(key))


PLUGIN = Provider(
    name="tiingo", title="Tiingo", build=build, assets=frozenset({"stock"}), order=60,
    about="US stock daily bars from 1962 (a free account key)",
    keys=(KeyVar("TIINGO_API_KEY"),), signup="tiingo.com: a free account's API token",
    archival=True, tier=TIER, per_run=PER_RUN, per_day=PER_DAY,
    limits="free plan: 50 requests per hour, 1,000 per day",
    symbols_per_month=450,  # the free plan's 500 different symbols a month, a little inside
    provides={"stock": ("day",)}, per_request={"day": None},  # one request: a symbol's whole history
    has_keys=lambda s: bool(s.tiingo_api_key),
    rate=lambda settings: (45, 3600.0),  # free plan: 50 an hour
    markets=lambda settings: {"stock": "sip"},
)
