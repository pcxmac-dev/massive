"""What the fetchers are doing: the live run, plan usage, each symbol's backfill and the log tail.

Everything here is read from the local database and log file; nothing calls the API.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..collector import ET, history_floor
from .. import freshness
from ..config import Settings, Tier
from ..storage import Store

LOG_TAIL_BYTES = 64 * 1024
# Rough request cost of backfilling minute bars: Massive returns up to 50,000 bars per request and
# a stock trades ~960 minutes a day including extended hours. Futures and FX trade around the clock.
BARS_PER_REQUEST = 50_000
MINUTES_PER_DAY = {"stock": 960, "index": 390, "fx": 1440, "future": 1380}


def log_path(db_path: Path) -> Path:
    return db_path.parent / "logs" / "massive-collate.log"


def log_tail(path: Path, lines: int = 200) -> list[str]:
    """The last lines of the log, reading only its end however large it grows."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = data.splitlines()
    if size > LOG_TAIL_BYTES:
        out = out[1:]  # first line is probably cut
    return out[-lines:]


def lock_owner(db_path: Path) -> int | None:
    """Process id holding the run lock, if a run is in progress."""
    from ..cli import pid_alive

    lock = db_path.with_suffix(".lock")
    try:
        pid = int(lock.read_text().strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if pid and pid_alive(pid) else None


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)


def _utc(text: str | None) -> datetime | None:
    if not text:
        return None
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def symbol_status(item: dict, edges: tuple | None, floor: date, today: date, now: datetime,
                  tier: Tier, current: str | None, probe: tuple | None = None) -> dict:
    """One watchlist row: how far back it is stored, whether backfill is done, whether newer
    data should exist (plan calendar), and what the last scan found at the source."""
    target = max(floor, item["listed_from"]) if item.get("listed_from") else floor
    first = datetime.fromtimestamp(edges[0] / 1000, ET).date() if edges else None
    last = datetime.fromtimestamp(edges[1] / 1000, ET) if edges else None
    reached = item.get("backfilled_to") or first
    span = max(1, (today - target).days)
    if item["asset"] == "future":
        done = first is not None and first <= target + timedelta(days=7)
    else:
        done = item.get("backfilled_to") is not None and item["backfilled_to"] <= target
    progress = 1.0 if done else (0.0 if reached is None else min(1.0, max(0.0, (today - reached).days / span)))
    missing_days = 0 if done else (today - target).days if reached is None else max(0, (reached - target).days)
    per_day = MINUTES_PER_DAY.get(item["asset"], 960) * 5 / 7  # calendar days to trading days
    left = 0 if done else math.ceil(missing_days * per_day / BARS_PER_REQUEST) + (2 if reached is None else 0)

    fresh = freshness.assess(item["asset"], tier, last, now)
    source_through = None
    if probe and probe[1] is not None:
        source_through = datetime.fromtimestamp(probe[1] / 1000, ET)
    if item["ticker"] == current:
        state = "fetching"
    elif not done:
        state = "backfilling" if reached else "queued"
    elif fresh["state"] == "behind":
        state = "new"  # the plan should have published newer data than is stored
    else:
        state = "current"
    return {
        "ticker": item["ticker"], "exchange": item.get("exchange"), "asset": item["asset"],
        "name": item.get("name"), "resolution": item["resolution"], "state": state,
        "first": _iso(first), "last": _iso(last), "target": _iso(target), "backfilledTo": _iso(item.get("backfilled_to")),
        "progress": round(progress, 4), "requestsLeft": left,
        "syncedAt": _iso(item.get("synced_at")),
        "expectedThrough": _iso(fresh["expectedThrough"]), "nextDue": _iso(fresh["nextDue"]), "rule": fresh["rule"],
        "probedAt": _iso(probe[0]) if probe else None, "sourceThrough": _iso(source_through),
        "confirmed": bool(source_through and last and source_through > last),
    }


def symbol_rows(store: Store, settings: Settings, source: str, now: datetime, current: str | None = None) -> list[dict]:
    today = now.date()
    floor = history_floor(settings.tier, today)
    rows = []
    for item in store.watchlist(source):
        if item["asset"] == "future":
            contracts = {t for _, t in store.rolls(source, item["source_ticker"])}  # source_ticker = product
            spans = [e for e in (store.bar_edges(source, t) for t in contracts) if e]
            edges = (min(e[0] for e in spans), max(e[1] for e in spans)) if spans else None
        else:
            edges = store.bar_edges(source, item["ticker"])
        rows.append(symbol_status(item, edges, floor, today, now, settings.tier, current,
                                  store.probe(source, item["ticker"])))
    return rows


def _directory(store: Store, source: str, settings: Settings, used_today: int) -> dict:
    from ..collector import COMPANY_TYPES, DAILY_RESERVE

    s = store.directory_stats(source, COMPANY_TYPES)
    left = max(0, s["companies"] - s["iconsChecked"])
    per_day = max(0, settings.max_requests_per_day - DAILY_RESERVE)
    return {**s, "remaining": left, "requestsLeft": left * 2,
            "days": round(left * 2 / per_day, 1) if per_day else None,
            "updated": store.kv_get(f"directory:{source}")}


RECORDER_KEY = "live-recorder"  # a live recorder process heartbeats here (kv table) while recording


def live_feeds(store: Store, settings: Settings, source: str, now: datetime) -> dict:
    """Feeds of the current session (the fetcher only handles past sessions): for each asset
    class on the watchlist, how current its data can be on this plan and whether it is being
    recorded live right now."""
    beat = store.kv_get(RECORDER_KEY)
    recording = bool(beat and (now - datetime.fromisoformat(beat)).total_seconds() < 120)
    by_asset: dict[str, list[str]] = {}
    for w in store.watchlist(source):
        by_asset.setdefault(w["asset"], []).append(w["ticker"])
    feeds = []
    for asset, tickers in sorted(by_asset.items()):
        d = freshness.plan_delay(settings.tier, asset)
        live = d.kind == "delay"
        feeds.append({
            "asset": asset, "symbols": sorted(tickers), "delay": d.describe(),
            "delayMinutes": int(d.lag.total_seconds() // 60) if live else None,
            "available": live, "realtime": live and d.lag.total_seconds() == 0,
            "recording": recording and live,
            "note": None if live else "your plan has end-of-day data only: no current-session feed",
        })
    return {"recording": recording and any(f["recording"] for f in feeds),
            "available": any(f["available"] for f in feeds), "feeds": feeds,
            "checks": exchange_checks(store, source, now)}


def exchange_checks(store: Store, source: str, now: datetime) -> list[dict]:
    """The background checks that keep the Markets globe and the news current: when each last
    ran, what it found, when it runs next and what it costs."""
    from . import world
    from ..collector import CALENDAR_REFRESH_DAYS, NEWS_REFRESH_MINUTES

    checks = []
    try:
        markets = world.world_markets(now)
        open_now = sum(m.get("state") == "open" for m in markets)
        lunch = sum(m.get("state") == "break" for m in markets)
        checks.append({"name": "World exchange status", "last": now.isoformat(), "next": "continuous",
                       "found": f"{open_now} of {len(markets)} exchanges open" + (f", {lunch} at lunch" if lunch else ""),
                       "cost": "free (exchange calendars)"})
    except Exception as e:  # calendars still loading
        checks.append({"name": "World exchange status", "last": None, "next": "continuous", "found": str(e), "cost": "free"})
    days = store.proxy_days(source)
    moves = store.proxy_moves(source)
    checks.append({"name": "Market moves (proxy funds)",
                   "last": days[-1].isoformat() if days else None,
                   "next": "each sync, once the next session is published (overnight)",
                   "found": f"{len(moves)} markets, {days[-2].isoformat()} → {days[-1].isoformat()}" if len(days) >= 2
                   else "waiting for two published days",
                   "cost": "1 request per trading day"})
    cal = store.kv_get(f"calendar:{source}")
    holidays = len(store.market_holidays(source))
    checks.append({"name": "Exchange holidays & futures schedules", "last": cal,
                   "next": (date.fromisoformat(cal) + timedelta(days=CALENDAR_REFRESH_DAYS)).isoformat() if cal else "next sync",
                   "found": f"{holidays} upcoming US closures/early closes; CME session schedules",
                   "cost": "~5 requests per week"})
    news = store.kv_get(f"news:{source}")
    recent = len(store.news(source, since_ms=int((now - timedelta(hours=48)).timestamp() * 1000), limit=500))
    checks.append({"name": "Headlines", "last": news,
                   "next": (datetime.fromisoformat(news) + timedelta(minutes=NEWS_REFRESH_MINUTES)).isoformat() if news else "next sync",
                   "found": f"{recent} in the last 48 h", "cost": f"1 request, at most every {NEWS_REFRESH_MINUTES} min"})
    return checks


def activity(store: Store, settings: Settings, db_path: Path, source: str = "massive",
             now: datetime | None = None, log_lines: int = 200) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    today = now.date()
    floor = history_floor(settings.tier, today)

    runs = store.runs(25)
    owner = lock_owner(db_path)
    live = next((r for r in runs if r["status"] == "running"), None) if owner else None
    current = live["current"] if live and live["source"] == source else None
    for r in runs:
        start, end = _utc(r["started_at"]), _utc(r["finished_at"])
        r["seconds"] = round(((end or now) - start).total_seconds()) if start else None
        if r["status"] == "running" and not owner:
            r["status"] = "abandoned"  # the process died; the next run marks it so in the database

    symbols = symbol_rows(store, settings, source, now, current)

    used = store.requests_today(source)
    left = sum(s["requestsLeft"] for s in symbols)
    per_day = settings.max_requests_per_day
    rpm = settings.tier.requests_per_minute
    return {
        "now": now.isoformat(), "source": source,
        "plan": {
            "tier": settings.tier.name, "requestsPerMinute": rpm, "historyFloor": floor.isoformat(),
            "maxPerRun": settings.max_requests_per_run, "maxPerDay": per_day, "refreshHours": settings.refresh_hours,
        },
        "today": {"requests": used, "remaining": max(0, per_day - used)},
        "running": live is not None,
        "live": live,
        "scanning": bool(live and live["command"].startswith("scan")),
        "newData": sum(x["state"] == "new" for x in symbols),
        "backfill": {
            "done": sum(s["progress"] >= 1 for s in symbols), "total": len(symbols), "requestsLeft": left,
            "runs": math.ceil(left / settings.max_requests_per_run) if settings.max_requests_per_run else None,
            "days": round(left / per_day, 1) if per_day else None,
        },
        "symbols": symbols,
        "directory": _directory(store, source, settings, used),
        "liveFeeds": live_feeds(store, settings, source, now),
        "runs": runs,
        "log": log_tail(log_path(db_path), log_lines),
    }
