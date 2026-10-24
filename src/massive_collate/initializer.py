"""The one-time initializer: every listed symbol of chosen exchanges onto the watchlist, in order of
traded value, so backfills go from the most traded symbols down.

It sends no requests: the directory of listings (refreshed daily) and the market snapshot (the
last three sessions' volume of every US stock) are here already. Symbols go on every feed that
collects stocks, at minute resolution where a feed has ticks too (official daily bars come with
them; ticks stay with symbols watched one by one: ticks for every listing would take tens of
terabytes). Symbols already watched keep their settings. The ranking is kept in kv
`watchlist:order`, and the sync takes backfills in that order (collector.sync). Running it again
adds new listings and ranks everything afresh.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

EXCHANGES = ("XNYS", "XNAS")  # the New York Stock Exchange and Nasdaq (not NYSE Arca or NYSE American)
ORDER_KEY = "watchlist:order"  # {ticker: rank}: the order backfills go in, 0 first
DONE_KEY = "init:listings"
BULK_KEY = "init:bulk"  # [ticker]: the symbols it added (kept at minute resolution on plan changes)
# Preferred shares' tickers lose their case in the directory (Massive writes them like BACpE; the
# directory keeps BACPE), so they would be asked for under the wrong symbol: left out until the
# directory keeps the case.
SKIP_TYPES = ("PFD",)
# Feeds with ticks take bulk symbols at minute resolution; the others collect what they have.
BULK_RESOLUTION = {"massive": "minute", "alpaca": "minute"}


def load_order(store) -> dict[str, int]:
    try:
        order = json.loads(store.kv_get(ORDER_KEY) or "{}")
    except ValueError:
        return {}
    return order if isinstance(order, dict) else {}


def load_bulk(store) -> list[str]:
    try:
        bulk = json.loads(store.kv_get(BULK_KEY) or "[]")
    except ValueError:
        return []
    return bulk if isinstance(bulk, list) else []


def initialize(store, feeds: list[str], exchanges=EXCHANGES, by: str = "value", source: str = "massive",
               dry_run: bool = False, now: datetime | None = None) -> dict:
    """Watch every active stock listing on `exchanges` on each of `feeds`, most traded first (see
    Store.listings_by_volume), and keep the ranking for the sync. Returns what it did (or, with
    `dry_run`, would do): {listed, ranked, added: {feed: n}, top: [tickers]}."""
    ranked = store.listings_by_volume(source, exchanges, by, skip_types=SKIP_TYPES)
    have = {(w["source"], w["ticker"]) for w in store.watchlist()}
    watched = {t for _, t in have}  # chosen one by one before: not bulk, whichever feeds it gets added to
    added, bulk = {}, set(load_bulk(store))
    for feed in feeds:
        new: dict[str, list[str]] = {}
        for r in ranked:
            if (feed, r["ticker"]) not in have:
                new.setdefault(r["exchange"], []).append(r["ticker"])
        added[feed] = sum(len(t) for t in new.values())
        bulk |= {t for tickers in new.values() for t in tickers if t not in watched}
        if not dry_run:
            for mic, tickers in new.items():  # one transaction per exchange
                store.watch(feed, tickers, BULK_RESOLUTION.get(feed, "auto"), exchange=mic)
    out = {"listed": len(ranked), "ranked": sum(r["measure"] is not None for r in ranked), "added": added,
           "top": [r["ticker"] for r in ranked[:20]], "exchanges": list(exchanges), "by": by}
    if not dry_run:
        store.kv_set(BULK_KEY, json.dumps(sorted(bulk)))
        store.kv_set(ORDER_KEY, json.dumps({r["ticker"]: i for i, r in enumerate(ranked)}))
        store.kv_set(DONE_KEY, json.dumps({**out, "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}))
    return out
