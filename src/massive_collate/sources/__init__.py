"""Data sources. Add a provider by subclassing DataSource and registering a builder here."""
from __future__ import annotations

from typing import Callable

from ..config import Settings
from ..ratelimit import RateLimiter
from .base import RESOLUTIONS, DataSource
from .massive import MassiveClient, MassiveSource


def _build_massive(settings: Settings, request_budget: int | None) -> DataSource:
    client = MassiveClient(
        settings.require_api_key(),
        base_url=settings.base_url,
        rate_limiter=RateLimiter(settings.tier.requests_per_minute),
        request_budget=request_budget,
    )
    return MassiveSource(client)


BUILDERS: dict[str, Callable[[Settings, int | None], DataSource]] = {
    "massive": _build_massive,
}


def build_source(name: str, settings: Settings, request_budget: int | None = None) -> DataSource:
    try:
        return BUILDERS[name](settings, request_budget)
    except KeyError:
        raise ValueError(f"unknown source {name!r}; available: {', '.join(BUILDERS)}") from None


__all__ = ["RESOLUTIONS", "DataSource", "MassiveClient", "MassiveSource", "build_source", "BUILDERS"]
