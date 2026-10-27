"""Data sources. Each provider is a plugin (registry.py): the built-in ones are in plugins/, one
module each, and installed packages add theirs under the `massive_collate.providers` entry points."""
from __future__ import annotations

from ..config import Settings
from ..coverage import FeedProfile
from .base import RESOLUTIONS, DataSource
from .alpaca import AlpacaClient, AlpacaSource
from .massive import MassiveClient, MassiveSource
from .registry import KeyVar, Provider, minute_window, providers, rate_window


def provider(name: str) -> Provider:
    p = providers().get(name)
    if p is None:
        raise ValueError(f"unknown source {name!r}; available: {', '.join(providers())}")
    return p


def is_archive(name: str) -> bool:
    """A closed feed (public archive): each symbol is filled once, then never asked again."""
    p = providers().get(name)
    return bool(p and p.archival)


def supports(feed: str, spec) -> bool:
    """Whether a feed can hold a symbol at all (its asset class, and its own list where it has one).
    Watching a symbol puts it on every feed that can; the coverage ledger then keeps them from
    downloading the same span."""
    p = providers().get(feed)
    return bool(p and p.can_carry(spec))


def feed_profiles(settings: Settings) -> dict[str, FeedProfile]:
    """Every feed configured on this machine and what it can serve (see coverage.py)."""
    known = providers()
    order = list(settings.source_priority) + sorted(set(known) - set(settings.source_priority))
    out = {}
    for i, name in enumerate(n for n in order if n in known):
        p = known[name]
        assets = p.served(settings)
        if assets is None:  # not set up here at all
            continue
        out[name] = FeedProfile(name, settings.for_source(name).tier, assets, i, tick_days=settings.tick_history_days,
                                second_days=settings.second_history_days, venues=p.market_of(settings))
    return out


def markets(feed: str, settings: Settings) -> dict[str, str]:
    """Which market each asset class of a feed is: feeds of the same market are copies (one
    download per span between them); different markets are combined by the composite model."""
    p = providers().get(feed)
    return p.market_of(settings) if p else {}


def build_source(name: str, settings: Settings, request_budget: int | None = None) -> DataSource:
    return provider(name).build(settings, request_budget)


__all__ = ["RESOLUTIONS", "DataSource", "MassiveClient", "MassiveSource", "AlpacaClient", "AlpacaSource",
           "KeyVar", "Provider", "providers", "provider", "is_archive", "build_source", "feed_profiles", "supports",
           "markets", "rate_window", "minute_window"]
