"""The one-time initializer: every listed symbol of chosen exchanges onto the watchlist, in order of
traded value, so backfills go from the most traded symbols down.

It sends no requests: the directory of listings (refreshed daily) and the market snapshot (the
last three sessions' volume of every US stock) are here already. Symbols go on every feed that
collects stocks, at minute resolution where a feed has ticks too (official daily bars come with
them; ticks stay with symbols watched one by one: ticks for every listing would take tens of
terabytes). Symbols already watched keep their settings. The ranking is kept in kv
`watchlist:order`, and the sync takes backfills in that order (collector.sync). Running it again
adds new listings and ranks everything afresh.

CME Group's four exchanges (CME, NYMEX, CBOT, COMEX: the user, 2026-10-27) go alongside NYSE and
Nasdaq: each futures product in the directory is watched as its continuous chart (`ES1!`) on every
feed that collects futures. No volume is known for them before they are fetched, so the best-known
products go first (MAJOR_FUTURES), then by sector, the event and power contracts last. Their order
follows the stocks'; the sync gives futures a capped share of each run (collector.FUTURES_PER_RUN).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

STOCK_EXCHANGES = ("XNYS", "XNAS")  # the New York Stock Exchange and Nasdaq (not NYSE Arca or NYSE American)
CME_GROUP = ("XCME", "XNYM", "XCBT", "XCEC")  # CME, NYMEX, CBOT, COMEX: their futures products
EXCHANGES = STOCK_EXCHANGES + CME_GROUP
ORDER_KEY = "watchlist:order"  # {ticker: rank}: the order backfills go in, 0 first
DONE_KEY = "init:listings"
BULK_KEY = "init:bulk"  # [ticker]: the symbols it added (kept at minute resolution on plan changes)
# Preferred shares' tickers lose their case in the directory (Massive writes them like BACpE; the
# directory keeps BACPE), so they would be asked for under the wrong symbol: left out until the
# directory keeps the case.
SKIP_TYPES = ("PFD",)
# The futures taken first, in this order: equity index, energy, metals, rates, grains, livestock,
# currencies and crypto (CME Group's most traded). The rest by sector (FUTURES_SECTORS), then code.
MAJOR_FUTURES = ("ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K", "CL", "NG", "RB", "HO", "BZ", "MCL", "GC", "SI",
                 "HG", "PL", "PA", "MGC", "ZN", "ZB", "ZF", "ZT", "UB", "TN", "SR3", "ZC", "ZS", "ZW", "ZL", "ZM", "KE",
                 "LE", "HE", "GF", "6E", "6J", "6B", "6A", "6C", "6S", "6M", "BTC", "MBT", "ETH", "MET")
FUTURES_SECTORS = ("us_index", "crude_oil", "nat_gas", "precious", "base", "long_term_gov", "short_term_gov",
                   "short_term_non_gov", "grains_and_oilseeds", "livestock", "majors", "intl_index", "sel_sector_index",
                   "refined_products", "minors", "cross_rates", "softs", "dairy", "us", "europe", "asia")
LAST_SECTORS = ("sports_index", "electricity", "ssf")  # event contracts, power, single-stock futures


def futures_rank(f: dict) -> tuple:
    """Where a futures product goes in the fetch order (MAJOR_FUTURES, then FUTURES_SECTORS, then
    the rest, LAST_SECTORS last)."""
    code, sector = f["ticker"], f.get("type") or ""
    if code in MAJOR_FUTURES:
        return (0, MAJOR_FUTURES.index(code), "")
    if sector in LAST_SECTORS:
        return (3, LAST_SECTORS.index(sector), code)
    return (1, FUTURES_SECTORS.index(sector), code) if sector in FUTURES_SECTORS else (2, 0, code)


def bulk_resolution(feed: str) -> str:
    """Feeds with ticks take bulk symbols at minute resolution (their plugin's `bulk_resolution`);
    the others collect what they have."""
    from .sources import providers

    p = providers().get(feed)
    return p.bulk_resolution if p else "auto"


def load_order(store) -> dict[str, int]:
    return store.kv_json(ORDER_KEY, {})


def load_bulk(store) -> list[str]:
    return store.kv_json(BULK_KEY, [])


def initialize(store, feeds: list[str], exchanges=EXCHANGES, by: str = "value", source: str = "massive",
               dry_run: bool = False, now: datetime | None = None, futures_feeds: list[str] = ()) -> dict:
    """Watch every active stock listing on `exchanges` on each of `feeds`, most traded first (see
    Store.listings_by_volume), and every futures product of the CME Group exchanges among them as
    its continuous chart on each of `futures_feeds` (futures_rank); keep the ranking for
    the sync. Returns what it did (or, with `dry_run`, would do): {listed, ranked, added: {feed: n},
    top: [tickers], futures: {listed, added, top}}."""
    stock_mics = [e for e in exchanges if e not in CME_GROUP]
    future_mics = [e for e in exchanges if e in CME_GROUP]
    ranked = store.listings_by_volume(source, stock_mics, by, skip_types=SKIP_TYPES) if stock_mics else []
    have = {(w["source"], w["ticker"]) for w in store.watchlist()}
    watched = {t for _, t in have}  # chosen one by one before: not bulk, whichever feeds it gets added to
    added, bulk = {}, set(load_bulk(store))
    for feed in feeds if stock_mics else ():
        new: dict[str, list[str]] = {}
        for r in ranked:
            if (feed, r["ticker"]) not in have:
                new.setdefault(r["exchange"], []).append(r["ticker"])
        added[feed] = sum(len(t) for t in new.values())
        bulk |= {t for tickers in new.values() for t in tickers if t not in watched}
        if not dry_run:
            for mic, tickers in new.items():  # one transaction per exchange
                store.watch(feed, tickers, bulk_resolution(feed), exchange=mic)
    futures = _futures(store, future_mics, futures_feeds, source, have, dry_run) if future_mics else None
    order = load_order(store)
    if ranked:  # the stocks ranked afresh; futures and symbols watched one by one keep their places
        order = {t: r for t, r in order.items() if t not in {x["ticker"] for x in ranked}}
        shift = len(ranked)
        order = {**{r["ticker"]: i for i, r in enumerate(ranked)}, **{t: shift + r for t, r in order.items()}}
    if futures:  # after the stocks, most traded first
        start = max(order.values(), default=-1) + 1
        order |= {f"{f['ticker']}1!": start + i for i, f in enumerate(futures.pop("ranked"))}
    out = {"listed": len(ranked), "ranked": sum(r["measure"] is not None for r in ranked), "added": added,
           "top": [r["ticker"] for r in ranked[:20]], "exchanges": list(exchanges), "by": by}
    if futures is not None:
        out["futures"] = futures
    if not dry_run:
        store.kv_set(BULK_KEY, json.dumps(sorted(bulk)))
        store.kv_set(ORDER_KEY, json.dumps(order))
        store.kv_set(DONE_KEY, json.dumps({**out, "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}))
    return out


def _futures(store, mics: list[str], feeds: list[str], source: str, have: set, dry_run: bool) -> dict:
    """Every futures product on `mics` onto `feeds` as its continuous chart (PRODUCT1!), in
    futures_rank's order. A product code the symbol rules cannot read is skipped and counted."""
    from .symbols import resolve

    ranked = sorted(store.futures_products(source, mics), key=futures_rank)
    specs, unreadable = [], []
    for f in ranked:
        try:
            spec = resolve(f"{f['ticker']}1!")
        except ValueError:
            unreadable.append(f["ticker"])
            continue
        if spec.asset == "future":
            specs.append(spec)
        else:
            unreadable.append(f["ticker"])
    added = {}
    for feed in feeds:
        new = [s for s in specs if (feed, s.ticker) not in have]
        added[feed] = len(new)
        if not dry_run:
            for spec in new:
                store.watch_spec(feed, spec, bulk_resolution(feed))
    by_mic: dict[str, int] = {}
    for f in ranked:
        by_mic[f["exchange"]] = by_mic.get(f["exchange"], 0) + 1
    readable = {s.source_ticker for s in specs}
    return {"listed": len(ranked), "byExchange": by_mic, "added": added, "unreadable": unreadable,
            "top": [f"{f['ticker']}1!" for f in ranked[:20] if f["ticker"] in readable],
            "ranked": [f for f in ranked if f["ticker"] in readable]}
