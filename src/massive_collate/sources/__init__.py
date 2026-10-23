"""Data sources. Add a provider by subclassing DataSource and registering a builder here."""
from __future__ import annotations

from typing import Callable

from ..config import ConfigError, Settings
from ..coverage import FeedProfile
from ..symbols import ASSETS
from ..ratelimit import RateLimiter
from .base import RESOLUTIONS, DataSource
from .alpaca import TRADING_URLS, AlpacaClient, AlpacaSource
from .massive import MassiveClient, MassiveSource


def _build_massive(settings: Settings, request_budget: int | None) -> DataSource:
    client = MassiveClient(
        settings.require_api_key(),
        base_url=settings.base_url,
        rate_limiter=RateLimiter(settings.tier.requests_per_minute),
        request_budget=request_budget,
    )
    return MassiveSource(client)


def _build_alpaca(settings: Settings, request_budget: int | None) -> DataSource:
    """Builds without keys too: crypto needs none, and stock requests are refused before sending."""
    a = settings.alpaca
    if a is None:
        raise ConfigError("Alpaca settings missing")
    client = AlpacaClient(a.key_id, a.secret_key, trading_url=TRADING_URLS[a.paper],
                          rate_limiter=RateLimiter(a.tier.requests_per_minute), request_budget=request_budget)
    return AlpacaSource(client, feed=a.feed, crypto_loc=a.crypto_loc, realtime=a.tier.name == "alpaca-plus")


BUILDERS: dict[str, Callable[[Settings, int | None], DataSource]] = {
    "massive": _build_massive,
    "alpaca": _build_alpaca,
}


# Asset classes each feed can collect at all (with keys / on some plan). Watching a symbol puts it
# on every feed that can; the coverage ledger then keeps them from downloading the same span.
SUPPORTED_ASSETS: dict[str, frozenset] = {"massive": frozenset(ASSETS), "alpaca": frozenset({"stock", "crypto"})}


def feed_profiles(settings: Settings) -> dict[str, FeedProfile]:
    """Every feed configured on this machine and what it can serve (see coverage.py)."""
    order = list(settings.source_priority) + sorted(set(BUILDERS) - set(settings.source_priority))
    out = {}
    for i, name in enumerate(n for n in order if n in BUILDERS):
        tier = settings.for_source(name).tier
        if name == "massive":
            if not settings.api_key:
                continue
            assets = frozenset(ASSETS)
        elif name == "alpaca":
            a = settings.alpaca
            assets = frozenset({"crypto"} | ({"stock"} if a and a.has_keys else set()))
        else:
            assets = frozenset(ASSETS)
        out[name] = FeedProfile(name, tier, assets, i)
    return out


def build_source(name: str, settings: Settings, request_budget: int | None = None) -> DataSource:
    try:
        return BUILDERS[name](settings, request_budget)
    except KeyError:
        raise ValueError(f"unknown source {name!r}; available: {', '.join(BUILDERS)}") from None


__all__ = ["RESOLUTIONS", "DataSource", "MassiveClient", "MassiveSource", "AlpacaClient", "AlpacaSource",
           "build_source", "feed_profiles", "BUILDERS", "SUPPORTED_ASSETS"]
