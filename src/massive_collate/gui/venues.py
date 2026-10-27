"""The Exchanges view (rail icon: a pair of scales; the user, 2026-10-27): every venue the app tracks
symbols on, its products, and what is known about them.

A venue is where a product's data comes from as a market: the stock exchanges (NYSE, Nasdaq, the
other US venues), CME Group's four (CME, NYMEX, CBOT, COMEX: the futures products' exchanges in the
directory), the index calculators, each crypto exchange (each feed's market), forex (OTC) and Blue
Ocean's overnight session. A product is a watched symbol, once per venue whatever the feeds holding
it. Read from the watchlist and the directory only: no bars, no requests.

`summary` gives the left column (each venue's state now, its products by sector, how many are
collected and how freshly, the feeds and the delivery scheme behind it); `products` a venue's
products for the middle; a product's own insight is the symbol panel's (/api/info).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from ..sessions import ET, MarketCalendar
from .markets import _around_the_clock, _iso, us_equities

# id -> (name, group, asset, timezone, hours)
VENUES: dict[str, tuple[str, str, str, str, str]] = {
    "XNYS": ("New York Stock Exchange", "Stock exchanges", "stock", "America/New_York",
             "Pre-market 04:00, regular 09:30-16:00, after-hours to 20:00 ET"),
    "XNAS": ("Nasdaq", "Stock exchanges", "stock", "America/New_York",
             "Pre-market 04:00, regular 09:30-16:00, after-hours to 20:00 ET"),
    "US-OTHER": ("Other US exchanges", "Stock exchanges", "stock", "America/New_York",
                 "Pre-market 04:00, regular 09:30-16:00, after-hours to 20:00 ET"),
    "BOATS": ("Blue Ocean (overnight)", "Stock exchanges", "stock", "America/New_York",
              "Overnight 20:00-04:00 ET, Sunday to Thursday nights"),
    "XCME": ("CME", "CME Group", "future", "America/Chicago", "Globex: Sunday 18:00 to Friday 17:00 ET, a break 17:00-18:00"),
    "XNYM": ("NYMEX", "CME Group", "future", "America/Chicago", "Globex: Sunday 18:00 to Friday 17:00 ET, a break 17:00-18:00"),
    "XCBT": ("CBOT", "CME Group", "future", "America/Chicago", "Globex: Sunday 18:00 to Friday 17:00 ET, a break 17:00-18:00"),
    "XCEC": ("COMEX", "CME Group", "future", "America/Chicago", "Globex: Sunday 18:00 to Friday 17:00 ET, a break 17:00-18:00"),
    "FUT-OTHER": ("Other futures", "CME Group", "future", "America/Chicago", "Globex hours"),
    "INDEX": ("US indices", "Indices", "index", "America/New_York", "Calculated 09:30-16:00 ET"),
    "FX": ("Forex (OTC)", "Forex", "fx", "America/New_York", "Sunday 17:00 to Friday 17:00 ET"),
}
CRYPTO_NAMES = {"coinbase": "Coinbase", "bitstamp": "Bitstamp", "bitfinex": "Bitfinex", "kraken": "Kraken",
                "alpaca": "Alpaca", "consolidated": "Consolidated (Massive)"}
GROUPS = ("Stock exchanges", "CME Group", "Indices", "Crypto", "Forex")
STOCK_MICS = ("XNYS", "XNAS")


def venue_of(row: dict, futures_mic: dict[str, str], markets: dict[str, dict]) -> str:
    """The venue a watchlist row's data is (see the module's docstring)."""
    asset, feed = row["asset"], row["source"]
    if feed == "boats":
        return "BOATS"
    if asset == "stock":
        return row["exchange"] if row.get("exchange") in STOCK_MICS else "US-OTHER"
    if asset == "future":
        mic = futures_mic.get(row["source_ticker"])
        return mic if mic in VENUES else "FUT-OTHER"
    if asset == "index":
        return "INDEX"
    if asset == "fx":
        return "FX"
    if asset == "crypto":
        return f"CRYPTO:{markets.get(feed, {}).get('crypto') or feed}"
    return asset.upper()


def _venue_meta(vid: str) -> tuple[str, str, str, str, str]:
    if vid.startswith("CRYPTO:"):
        market = vid.split(":", 1)[1]
        return (CRYPTO_NAMES.get(market, market.title()), "Crypto", "crypto", "UTC", "Around the clock, every day")
    return VENUES.get(vid, (vid, "Other", "stock", "America/New_York", ""))


def _overnight_state(cal: MarketCalendar, now: datetime) -> dict:
    """Blue Ocean's overnight session now: open, or closed with when it opens next."""
    today = now.astimezone(ET).date()
    for d in (today, today + timedelta(days=1)):
        start = cal.overnight_open(d)
        end = datetime.combine(d, time(4, 0), ET)
        if start and start <= now < end:
            return {"state": "open", "since": _iso(start), "until": _iso(end), "next": None}
    upcoming = next((o for o in (cal.overnight_open(today + timedelta(days=i)) for i in range(0, 8)) if o and o > now), None)
    return {"state": "closed", "since": None, "until": None, "next": {"state": "open", "at": _iso(upcoming)} if upcoming else None}


def state_of(vid: str, asset: str, cal: MarketCalendar, now: datetime, product: str | None = None) -> dict:
    """Whether a venue trades now, since/until when, and its next change."""
    if vid == "BOATS":
        return _overnight_state(cal, now)
    if asset == "stock":
        s = us_equities(cal, now)
        return {k: s.get(k) for k in ("state", "since", "until", "next")}
    if asset == "index":
        s = us_equities(cal, now)
        return {"state": "open" if s["state"] == "open" else "closed", "since": s["since"] if s["state"] == "open" else None,
                "until": s["until"] if s["state"] == "open" else None, "next": s["next"]}
    if asset == "crypto":
        return {"state": "open", "since": None, "until": None, "next": None}
    s = _around_the_clock(cal, asset, product, now)
    return {k: s.get(k) for k in ("state", "since", "until", "next")}


def _directories(store) -> tuple[dict[str, dict], dict[str, dict]]:
    """The directory's stocks and futures products, apart: a futures code can be a stock's ticker
    too (CL: crude oil, and Colgate-Palmolive)."""
    stocks, futures = {}, {}
    for d in store.directory():
        (futures if d["kind"] == "future" else stocks)[d["ticker"]] = d
    return stocks, futures


def _products(watch: list[dict], stocks: dict[str, dict], futures: dict[str, dict], markets: dict) -> dict:
    """{venue: {ticker: product}} from the watchlist (once per venue, the feeds holding it listed)."""
    futures_mic = {t: d["exchange"] for t, d in futures.items()}
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for w in watch:
        vid = venue_of(w, futures_mic, markets)
        p = out[vid].get(w["ticker"])
        d = futures.get(w["source_ticker"], {}) if w["asset"] == "future" else stocks.get(w["ticker"], {})
        if p is None:
            p = out[vid][w["ticker"]] = {
                "ticker": w["ticker"], "name": w.get("name") or d.get("name"), "asset": w["asset"],
                "sector": d.get("type") or w.get("kind") or w["asset"], "product": w["source_ticker"],
                "feeds": [], "synced": None, "filled": False, "listed": str(w["listed_from"]) if w.get("listed_from") else None}
        p["feeds"].append(w["source"])
        if w.get("synced_at") and (p["synced"] is None or w["synced_at"].isoformat() > p["synced"]):
            p["synced"] = w["synced_at"].isoformat()
        p["filled"] = p["filled"] or bool(w.get("backfilled_to") or w.get("closed_at"))
    return out


def _delivery(scheme, asset: str, market: str | None) -> list[dict]:
    """The delivery scheme's eras for a venue's market (sourcing.py): who delivers each job."""
    if scheme is None:
        return []
    return [{"job": e.resolution, "start": e.start.isoformat(), "end": e.end.isoformat() if e.end else None,
             "sources": e.sources, "unique": e.unique}
            for e in scheme.eras if e.asset == asset and (market is None or e.market == market)]


def _market(vid: str, asset: str) -> str | None:
    return {"BOATS": "boats", "INDEX": "index", "FX": "otc"}.get(vid) or (
        vid.split(":", 1)[1] if vid.startswith("CRYPTO:") else "sip" if asset == "stock" else "cme" if asset == "future" else None)


def summary(store, cal: MarketCalendar, feeds: dict, scheme=None, now: datetime | None = None) -> dict:
    """Every tracked venue for the left column, with its insight (see the module's docstring)."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    stocks, futures = _directories(store)
    products = _products(store.watchlist(), stocks, futures, {name: p.venues for name, p in feeds.items()})
    moves = store.snapshot_changes()
    fresh_after = (now - timedelta(days=2)).isoformat()
    out = []
    for vid, items in products.items():
        name, group, asset, tz, hours = _venue_meta(vid)
        rows = list(items.values())
        sample = next((r["product"] for r in rows if asset == "future"), None)
        synced = [r["synced"] for r in rows if r["synced"]]
        movers = sorted(((r["ticker"], moves[r["ticker"]]) for r in rows if asset == "stock" and r["ticker"] in moves),
                        key=lambda m: m[1])
        out.append({
            "id": vid, "name": name, "group": group, "asset": asset, "timezone": tz, "hours": hours,
            **state_of(vid, asset, cal, now, sample),
            "products": len(rows), "sectors": Counter(r["sector"] for r in rows).most_common(12),
            "feeds": Counter(f for r in rows for f in r["feeds"]).most_common(),
            "collected": {"filled": sum(r["filled"] for r in rows), "synced": len(synced),
                          "fresh": sum(s >= fresh_after for s in synced), "never": len(rows) - len(synced),
                          "newest": max(synced, default=None)},
            "movers": {"up": [{"ticker": t, "pct": round(v, 2)} for t, v in movers[::-1][:5] if v > 0],
                       "down": [{"ticker": t, "pct": round(v, 2)} for t, v in movers[:5] if v < 0]},
            "delivery": _delivery(scheme, asset, _market(vid, asset)),
        })
    order, place = {g: i for i, g in enumerate(GROUPS)}, {vid: i for i, vid in enumerate(VENUES)}
    out.sort(key=lambda v: (order.get(v["group"], len(order)), place.get(v["id"], len(place)), -v["products"], v["name"]))
    return {"now": now.isoformat(), "groups": list(GROUPS), "venues": out}


def products(store, feeds: dict, vid: str) -> dict:
    """A venue's products for the middle column, most recently collected first."""
    stocks, futures = _directories(store)
    items = _products(store.watchlist(), stocks, futures, {name: p.venues for name, p in feeds.items()}).get(vid, {})
    moves = store.snapshot_changes()
    rows = sorted(items.values(), key=lambda r: (r["synced"] is None, r["ticker"]))
    for r in rows:
        r["change"] = round(moves[r["ticker"]], 2) if r["asset"] == "stock" and r["ticker"] in moves else None
    name, group, asset, *_ = _venue_meta(vid)
    return {"id": vid, "name": name, "group": group, "asset": asset, "products": rows}
