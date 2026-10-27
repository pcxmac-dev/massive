"""Coinbase Exchange (coinbase.com): crypto minute and day bars from 2015, no key; the reference
exchange for crypto (composite.py)."""
from __future__ import annotations

from dataclasses import replace

from ...config import PUBLIC_FEEDS
from ..public import CRYPTO_PAIRS, CoinbaseSource
from ..registry import Provider, public_client


def venue_view(name: str):
    """A crypto exchange feed's settings: all the history it holds, unless CRYPTO_VENUE_DAYS limits it."""
    tier, per_run, per_day = PUBLIC_FEEDS[name]

    def view(base):
        t = replace(tier, history_days=base.crypto_venue_days) if base.crypto_venue_days else tier
        return replace(base, tier=t, max_requests_per_run=per_run, max_requests_per_day=per_day, origin=base)

    return view


PLUGIN = Provider(
    name="coinbase", title="Coinbase",
    build=lambda settings, budget: CoinbaseSource(
        public_client("coinbase", "https://api.exchange.coinbase.com", settings, budget)),
    assets=frozenset({"crypto"}), order=30, about="crypto minute and day bars from 2015 (no key); live trades",
    tier=PUBLIC_FEEDS["coinbase"][0], limits="10 requests per second per IP (public endpoints)",
    kind="exchange", reference_for=frozenset({"crypto"}),
    per_request={"day": CoinbaseSource.PAGE, "minute": CoinbaseSource.PAGE},
    streams={"crypto": "real time, public trades (no key), coins open on a chart"},
    view=venue_view("coinbase"),
    rate=lambda settings: (8, 1.0),  # 10 a second per address: a minute's window let 300 go in a burst
    carries=lambda spec: spec.ticker.upper() in CRYPTO_PAIRS,
)
