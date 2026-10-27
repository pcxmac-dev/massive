"""Bitfinex (bitfinex.com): crypto minute and day bars from 2013, no key."""
from __future__ import annotations

from ...config import PUBLIC_FEEDS
from ..public import CRYPTO_PAIRS, BitfinexSource
from ..registry import Provider, public_client
from .coinbase import venue_view

PLUGIN = Provider(
    name="bitfinex", title="Bitfinex",
    build=lambda settings, budget: BitfinexSource(
        public_client("bitfinex", "https://api-pub.bitfinex.com/v2", settings, budget)),
    assets=frozenset({"crypto"}), order=40, about="crypto minute and day bars from 2013 (no key)",
    tier=PUBLIC_FEEDS["bitfinex"][0], limits="about 30 candle requests per minute", kind="exchange",
    per_request={"day": BitfinexSource.PAGE, "minute": BitfinexSource.PAGE},
    view=venue_view("bitfinex"),
    carries=lambda spec: spec.ticker.upper() in CRYPTO_PAIRS,
)
