"""The announcements ticker on the Markets globe and the News view: newsworthy items only.

Routine calendar items (opening, closing soon, lunch, scheduled holidays) are not news. What is:
- headlines stored by the fetcher (source: the publisher), tied to the exchange of the tickers
  they mention (US listings: New York);
- unscheduled closures today (days of mourning, storms, emergencies: a calendar's ad-hoc
  holidays) and early closes today;
- big moves: a market's proxy fund up or down BIG_MOVE_PCT or more on the latest day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import world

US_MICS = {"XNAS", "XNYS", "ARCX", "BATS", "XASE", "IEXG"}
NEWS_HOURS = 48
BIG_MOVE_PCT = 2.0


def exchange_events(now: datetime, markets: list[dict] | None = None) -> list[dict]:
    markets = markets if markets is not None else world.world_markets(now)
    out = []
    seen_moves = set()
    for m in markets:
        if m.get("state") in (None, "unknown"):
            continue
        idx = m["index"].split(" · ")[0]
        if m.get("unscheduledClosure"):
            out.append({"kind": "event", "text": f"{m['name']} closed today (unscheduled closure)",
                        "source": f"{m['code']} calendar", "time": now.isoformat(), "exchange": m["code"], "weight": 4})
        elif m.get("earlyClose") and m.get("today"):
            out.append({"kind": "event", "text": f"{m['name']} closes early today at {m['today']['close']} local",
                        "source": f"{m['code']} calendar", "time": now.isoformat(), "exchange": m["code"], "weight": 2})
        perf = m.get("performance")
        if perf and abs(perf["changePct"]) >= BIG_MOVE_PCT and (m["country"], perf["proxy"]) not in seen_moves:
            seen_moves.add((m["country"], perf["proxy"]))
            pct = perf["changePct"]
            out.append({"kind": "move", "text": f"{m['country']} {'up' if pct > 0 else 'down'} {abs(pct):.1f}% on {perf['day']} · {idx}",
                        "source": f"{perf['proxy']} (US-listed proxy)", "time": now.isoformat(), "exchange": m["code"],
                        "weight": 3, "changePct": pct})
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
    if markets is None:
        markets = world.world_markets(now, moves=store.proxy_moves())
    events = exchange_events(now, markets)
    news = news_items(store, watch, now)
    # Irregular exchange events first (they are about right now), then headlines, watched first,
    # each newest first.
    events.sort(key=lambda x: x["weight"], reverse=True)
    news.sort(key=lambda x: x["time"], reverse=True)
    news.sort(key=lambda x: x["weight"], reverse=True)
    return {"now": now.isoformat(), "items": events + news}
