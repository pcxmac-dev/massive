"""Bitstamp (bitstamp.net): crypto minute bars from 2011, no key; an exchange of its own."""
from __future__ import annotations

from ...config import PUBLIC_FEEDS
from ..public import BITSTAMP_PAIRS, BitstampSource
from ..registry import Provider, public_client

TIER, PER_RUN, PER_DAY = PUBLIC_FEEDS["bitstamp"]

PLUGIN = Provider(
    name="bitstamp", title="Bitstamp",
    build=lambda settings, budget: BitstampSource(
        public_client("bitstamp", "https://www.bitstamp.net/api/v2", settings, budget)),
    assets=frozenset({"crypto"}), order=20, about="crypto minute bars from 2011 (no key)",
    tier=TIER, per_run=PER_RUN, per_day=PER_DAY, limits="8,000 requests per 10 minutes", kind="exchange",
    per_request={"day": BitstampSource.PAGE, "minute": BitstampSource.PAGE},
    streams={"crypto": "real time, public trades (no key), coins open on a chart"},
    carries=lambda spec: spec.ticker.upper() in BITSTAMP_PAIRS,
)
