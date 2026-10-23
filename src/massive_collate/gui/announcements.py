"""The announcements ticker on the Markets globe: what is happening at the world's exchanges now.

Two kinds of item, each naming its source and the exchange it belongs to (lit on the globe):
- exchange events from each exchange's calendar: just opened, closing soon, lunch break, closed
  for a holiday today (source: the exchange calendar);
- news headlines stored by the fetcher (source: the publisher), tied to the exchange of the
  tickers they mention (US listings: New York).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from . import world

US_MICS = {"XNAS", "XNYS", "ARCX", "BATS", "XASE", "IEXG"}
NEWS_HOURS = 48


def _minutes(a: datetime, b: datetime) -> int:
    return int(round((b - a).total_seconds() / 60))


def exchange_events(now: datetime, markets: list[dict] | None = None) -> list[dict]:
    markets = markets if markets is not None else world.world_markets(now)
    out = []
    for m in markets:
        if m.get("state") in (None, "unknown"):
            continue
        until = datetime.fromisoformat(m["until"]) if m.get("until") else None
        idx = m["index"].split(" · ")[0]
        item = None
        if m["state"] == "open" and m.get("openedAt"):
            opened = datetime.fromisoformat(m["openedAt"])
            if _minutes(opened, now) <= 60:
                item = (f"{m['name']} opened {_minutes(opened, now)} min ago · {idx} trading", opened, 2)
        if item is None and m["state"] == "open" and until and m.get("event") == "closes" and _minutes(now, until) <= 45:
            item = (f"{m['city']} closes in {_minutes(now, until)} min · {idx}", now, 2)
        if item is None and m["state"] == "break" and until:
            local = pd.Timestamp(until).tz_convert(m["timezone"]).strftime("%H:%M")
            item = (f"{m['city']} at lunch · {idx} resumes {local} local", now, 1)
        if item is None and m["state"] == "closed" and m.get("holidayToday"):
            item = (f"{m['name']} closed today for a holiday", now, 1)
        if item is None and m["state"] == "closed" and until and 0 < _minutes(now, until) <= 30:
            item = (f"{m['city']} opens in {_minutes(now, until)} min · {idx}", now, 2)
        if item:
            text, when, weight = item
            out.append({"kind": "event", "text": text, "source": f"{m['code']} calendar",
                        "time": when.isoformat(), "exchange": m["code"], "weight": weight})
    return out


def news_items(store, watch: list[dict], now: datetime) -> list[dict]:
    since = int((now - timedelta(hours=NEWS_HOURS)).timestamp() * 1000)
    exch = {w["ticker"]: w.get("exchange") for w in watch}
    out = []
    for n in store.news(since_ms=since, limit=60):
        mics = {exch.get(t) for t in n["tickers"]} - {None}
        code = "XNYS" if not mics or mics & US_MICS else next(iter(mics))
        watched = [t for t in n["tickers"] if t in exch]
        out.append({"kind": "news", "text": n["title"], "source": n["publisher"] or n["source"],
                    "time": datetime.fromtimestamp(n["published_ms"] / 1000, timezone.utc).isoformat(),
                    "exchange": code, "url": n["url"], "tickers": n["tickers"][:6], "watched": watched,
                    "weight": 3 if watched else 1})
    return out


def announcements(store, watch: list[dict], now: datetime | None = None, markets: list[dict] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    events = exchange_events(now, markets)
    news = news_items(store, watch, now)
    # Interleave: exchange events first (they are about right now), then headlines, watched first.
    news.sort(key=lambda x: (-x["weight"], x["time"]), reverse=False)
    news.sort(key=lambda x: x["weight"], reverse=True)
    return {"now": now.isoformat(), "items": events + news}
