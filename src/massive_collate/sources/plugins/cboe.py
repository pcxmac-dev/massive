"""Cboe (cboe.com): daily history of Cboe's indices (VIX from 1990), no key; a closed archive."""
from __future__ import annotations

from ...config import PUBLIC_FEEDS
from ..public import CBOE_INDICES, CboeSource
from ..registry import Provider, public_client

TIER, PER_RUN, PER_DAY = PUBLIC_FEEDS["cboe"]

PLUGIN = Provider(
    version="1.0.0", api="1.0",  # its own release, and the plugin contract it was written for
    name="cboe", title="Cboe",
    build=lambda settings, budget: CboeSource(
        public_client("cboe", "https://cdn-api.cboe.com/api/global/us_indices/daily_prices", settings, budget)),
    assets=frozenset({"index"}), order=50, about="daily history of Cboe's indices, VIX from 1990 (no key)",
    archival=True, tier=TIER, per_run=PER_RUN, per_day=PER_DAY, limits="none published (a public file server)",
    kind="official", markets=lambda settings: {"index": "index"},
    provides={"index": ("day",)}, per_request={"day": None},  # one file per index: its whole history
    carries=lambda spec: spec.source_ticker.upper().removeprefix("I:") in CBOE_INDICES,
)
