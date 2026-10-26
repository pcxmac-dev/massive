"""What the fetchers are doing: the live run, plan usage, each symbol's backfill and the log tail.

Everything here is read from the local database and log file; nothing calls the API.
"""
from __future__ import annotations

import math
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import collector
from ..collector import ET, history_floor
from .. import freshness
from ..config import Settings, Tier
from .. import killswitch
from ..live import RECORDER_KEY, STATUS_KEY
from ..storage import Store

LOG_TAIL_BYTES = 64 * 1024
# Rough request cost of backfilling minute bars: Massive returns up to 50,000 bars per request and
# a stock trades ~960 minutes a day including extended hours. Futures and FX trade around the clock.
BARS_PER_REQUEST = 50_000
MINUTES_PER_DAY = {"stock": 960, "index": 390, "fx": 1440, "crypto": 1440, "future": 1380}


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
    """Process id holding the run lock, if a run is in progress (its owner alive, the same process)."""
    from ..proc import holder

    return holder(db_path.with_suffix(".lock"))


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
                  tier: Tier, current: str | None, probe: tuple | None = None, newest_any: int | None = None,
                  bars_per_request: int | None = None) -> dict:
    """One watchlist row: how far back it is stored, whether backfill is done, whether newer
    data should exist (plan calendar), and what the last scan found at the source.
    `newest_any` is the newest bar any feed holds (each span is downloaded by one feed only)."""
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
    # calendar days to trading days (crypto trades every day)
    per_day = MINUTES_PER_DAY.get(item["asset"], 960) * (1 if item["asset"] == "crypto" else 5 / 7)
    left = 0 if done else math.ceil(missing_days * per_day / (bars_per_request or BARS_PER_REQUEST)) + (
        2 if reached is None else 0)

    newest = datetime.fromtimestamp(newest_any / 1000, ET) if newest_any else None
    fresh = freshness.assess(item["asset"], tier, max(t for t in (last, newest) if t) if last or newest else None, now)
    source_through = None
    if probe and probe[1] is not None:
        source_through = datetime.fromtimestamp(probe[1] / 1000, ET)
    # Behind by its last bar is not enough: a stock nobody traded after 16:37 stays short of the
    # plan's last minute, yet it is complete once synced after the session was published. The
    # sync's own rule (collector._due) decides: newer data can exist only if published since.
    published_since = item.get("synced_at") is None or collector._due(item["asset"], tier, item["synced_at"], now, 0.0)
    if item["ticker"] == current:
        state = "fetching"
    elif not done:
        state = "backfilling" if reached else "queued"
    elif fresh["state"] == "behind" and published_since:
        state = "new"  # the plan has published newer data than is stored
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


# The Fetcher's table: the rows most worth seeing first (what is being fetched, then new data,
# backfills, the queue ...), the initializer's order (most traded) within each state.
STATE_RANK = {"fetching": 0, "next": 1, "new": 2, "backfilling": 3, "queued": 4, "held": 5, "blocked": 6,
              "current": 7, "retired": 8, "closed": 9}
DETAIL_ROWS = 300  # rows the Fetcher shows, each with what it still needs; the rest are counted


def symbol_rows(store: Store, settings: Settings, source: str, now: datetime, current: str | None = None,
                detail: int | None = None, focus: frozenset = frozenset(),
                order: dict[str, int] | None = None) -> list[dict]:
    """The Fetcher's rows for one feed. With several feeds, what a row still needs is the spans
    this feed should download (coverage.plan: nobody holds them and no better-ranked feed serves
    them), not everything it lacks itself. That plan costs a few queries a symbol: with `detail`,
    only the first `detail` rows by relevance (`ranked`), and the `focus` tickers, get it (each
    marked `planned`); the others keep the estimate from what the feed holds itself."""
    from ..coverage import plan
    from ..sources import feed_profiles

    today = now.date()
    profiles = feed_profiles(settings)
    settings = settings.for_source(source)
    me = profiles.get(source)
    holders = store.series_holders()  # once, not once a symbol
    from .. import collector

    asked = collector.priority_requests(store, now)  # opened in the chart: taken before anything else
    paused = collector.updates_pause(store, now)
    picked = collector.picked_symbols(store, now) if paused else set()
    rows, items = [], []
    for item in store.watchlist(source):
        floor = history_floor(settings.tier, today, item["asset"])
        if item["asset"] == "future":
            contracts = {t for _, t in store.rolls(source, item["source_ticker"])}  # source_ticker = product
            spans = [e for e in (store.bar_edges(source, t) for t in contracts) if e]
            edges = (min(e[0] for e in spans), max(e[1] for e in spans)) if spans else None
        else:
            edges = store.bar_edges(source, item["ticker"])
        newest = store.newest_any(item["ticker"], "minute", holders) if item["asset"] != "future" else None
        row = symbol_status(item, edges, floor, today, now, settings.tier, current,
                            store.probe(source, item["ticker"]), newest[1] if newest else None,
                            10_000 if source == "alpaca" else None)
        if item.get("closed_at"):
            row.update(state="closed", requestsLeft=0, progress=1.0, note="archive complete: not asked again")
        elif item.get("retired_on"):
            row.update(state="retired" if row["state"] in ("new", "current") else row["state"],
                       note=f"no longer listed since {item['retired_on']}: history only, no new data expected")
        elif me is not None and item["asset"] != "future" and item["resolution"] != "trades" \
                and item["asset"] not in me.assets:
            row.update(state="blocked", requestsLeft=0,
                       note="keys not set" if source == "alpaca" else f"{source} has no {item['asset']} data here")
        elif row["state"] != "fetching" and item["ticker"] in asked and (
                item.get("synced_at") is None or item["synced_at"] < asked[item["ticker"]]["at"]):
            row.update(state="next", note="opened in the chart: taken before anything else")
        elif paused and row["state"] == "new" and item["ticker"] not in picked and item.get("synced_at") \
                and now - item["synced_at"] < timedelta(days=collector.UPDATES_GAP_DAYS):
            row.update(state="held", note=f"updates paused until {paused['until'][:10]}: not picked, not missing much")
        rows.append(row)
        items.append((item, floor))
    chosen = set(range(len(rows))) if detail is None else \
        {i for i, r in enumerate(rows) if r["ticker"] in focus} | set(ranked(rows, order)[:detail])
    for i in sorted(chosen):
        row, (item, floor) = rows[i], items[i]
        row["planned"] = True
        if (me is None or len(profiles) < 2 or item["asset"] == "future" or item["resolution"] == "trades"
                or row["state"] in ("closed", "retired", "blocked")):
            continue
        item_floor = max(floor, item["listed_from"]) if item.get("listed_from") else floor
        gaps = plan(store, me, profiles, item["ticker"], item["asset"], "minute", item_floor, now, item.get("listed_from"))
        missing_days = sum(b - a for a, b in gaps) / 86_400_000
        per_day = MINUTES_PER_DAY.get(item["asset"], 960) * (1 if item["asset"] == "crypto" else 5 / 7)
        row["requestsLeft"] = math.ceil(missing_days * per_day / (10_000 if source == "alpaca" else BARS_PER_REQUEST))
        row["gapDays"] = round(missing_days, 1)
        if row["state"] in ("queued", "backfilling") and missing_days < 1:
            row.update(state="current", progress=1.0)  # other feeds hold the rest
    return rows


def ranked(rows: list[dict], order: dict[str, int] | None = None) -> list[int]:
    """Row indexes, most worth seeing first: by state (STATE_RANK), then the initializer's order
    (initializer.load_order: {ticker: rank})."""
    order = order or {}
    return sorted(range(len(rows)), key=lambda i: (STATE_RANK.get(rows[i]["state"], 9),
                                                   order.get(rows[i]["ticker"], len(order)), rows[i]["ticker"]))


def _ticks(store: Store, settings: Settings) -> dict:
    """The tick store: size, free disk, the backfill window and whether backfill is paused."""
    import shutil

    free = shutil.disk_usage(store.feeds_dir if store.feeds_dir and store.feeds_dir.is_dir() else Path.home()).free
    series = sum(1 for _, _, k in store.stored_series() if k == "trades")
    return {"bytes": store.ticks_bytes(), "freeBytes": free, "series": series,
            "days": getattr(settings, "tick_history_days", 365), "paused": store.kv_get("ticks:backfill-paused") or None}


def _directory(store: Store, source: str, settings: Settings, used_today: int) -> dict:
    from ..collector import COMPANY_TYPES, DAILY_RESERVE

    s = store.directory_stats(source, COMPANY_TYPES)
    left = max(0, s["companies"] - s["iconsChecked"])
    per_day = max(0, settings.max_requests_per_day - DAILY_RESERVE)
    return {**s, "remaining": left, "requestsLeft": left * 2,
            "days": round(left * 2 / per_day, 1) if per_day else None,
            "updated": store.kv_get(f"directory:{source}")}


def live_feeds(store: Store, settings: Settings, source: str, now: datetime) -> dict:
    """Feeds of the current session (the fetcher only handles past sessions): for each asset
    class on the watchlist, how current its data can be on this plan and whether it is being
    recorded live right now."""
    beat = store.kv_get(RECORDER_KEY)
    recording = bool(beat and (now - datetime.fromisoformat(beat)).total_seconds() < 120)
    streams, live_tickers = _streams(store, now) if recording else ({}, [])
    settings = settings.for_source(source)
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
    return {"recording": recording and (any(f["recording"] for f in feeds) or bool(live_tickers)),
            "available": any(f["available"] for f in feeds) or bool(streams), "feeds": feeds,
            "streams": streams, "liveTickers": live_tickers, "recorder": recording,
            "checks": exchange_checks(store, source, now)}


LIVE_FRESH_SECONDS = 300  # a pair whose last trade is older than this is not shown as live


def _streams(store: Store, now: datetime) -> tuple[dict, list[str]]:
    """The live recorder's streams (live.py): per exchange whether it is connected and, per pair,
    trades recorded, the last price and when; and the tickers trading live right now."""
    streams = store.kv_json(STATUS_KEY, {}).get("streams", {})
    if not isinstance(streams, dict):
        return {}, []
    now_ms = now.timestamp() * 1000
    live = sorted({t for s in streams.values() if s.get("connected")
                   for t, p in s.get("pairs", {}).items() if p.get("last") and now_ms - p["last"] < LIVE_FRESH_SECONDS * 1000})
    return streams, live


def exchange_checks(store: Store, source: str, now: datetime) -> list[dict]:
    """The background checks that keep the Markets globe and the news current: when each last
    ran, what it found, when it runs next and what it costs."""
    from . import world
    from ..collector import CALENDAR_REFRESH_DAYS, NEWS_REFRESH_MINUTES

    checks = []
    try:
        markets = world.world_markets(now)
        stocks = [m for m in markets if m.get("kind", "stock") == "stock"]
        centres = [m for m in markets if m.get("kind") == "fx"]
        crypto = [m for m in markets if m.get("kind") == "crypto"]
        open_now = sum(m.get("state") == "open" for m in stocks)
        lunch = sum(m.get("state") == "break" for m in stocks)
        checks.append({"name": "World exchange status", "last": now.isoformat(), "next": "continuous",
                       "found": f"{open_now} of {len(stocks)} exchanges open" + (f", {lunch} at lunch" if lunch else "")
                       + f"; FX centres {sum(m.get('state') == 'open' for m in centres)} of {len(centres)} in business hours"
                       + f"; {len(crypto)} crypto exchanges (24/7)",
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


def limits(store: Store, settings: Settings, now: datetime) -> list[dict]:
    """Every server's limits beside what this machine keeps to and has used: the request window
    (and how full it is now, from the window runs share), the caps per run and per day, today's
    requests, and a wait in progress (a full window or a server's Retry-After) with its reason."""
    from ..config import PUBLISHED_LIMITS
    from ..ratelimit import describe, load_times, wait_key, window_state
    from ..sources import BUILDERS, rate_window

    t = now.timestamp()
    rows = []
    for name in BUILDERS:
        feed = settings.for_source(name)
        calls, period = rate_window(name, settings)
        published = PUBLISHED_LIMITS.get(name) or (
            f"{feed.tier.name} plan: {describe(feed.tier.requests_per_minute, 60.0)}")
        wait = store.kv_json(wait_key(name))
        used = store.requests_today(name)
        rows.append({
            "source": name, "plan": feed.tier.name, "published": published, "keptTo": describe(calls, period),
            "window": window_state(load_times(store, name), calls, period, t),
            "perRun": feed.max_requests_per_run, "perDay": feed.max_requests_per_day,
            "today": used, "leftToday": max(0, feed.max_requests_per_day - used),
            "wait": wait if wait and wait.get("until", 0) > t else None,
        })
    return rows


KEY_NAMES = {"massive": "MASSIVE_API_KEY", "tiingo": "TIINGO_API_KEY",
             "alpaca": "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"}


def health(store: Store, settings: Settings, now: datetime, limit_rows: list[dict], running: dict | None) -> list[dict]:
    """Each source's state, for the Fetcher view and the status bar, with why:
    - fetching: a run of it is going now;
    - limited: today's requests are used up, the server rate-limited its last run, or it is
      waiting out a full request window now;
    - down: its last run failed (the server or the network);
    - off: not set up here (no key, or this copy of the app is offline);
    - done: a public archive with every symbol filled (not asked again);
    - ok: its last run went through (stopping at its per-run cap is how backfills go);
    - idle: it has not run yet."""
    from ..sources import ARCHIVAL, BUILDERS, feed_profiles

    profiles = feed_profiles(settings)
    limits_by = {r["source"]: r for r in limit_rows}
    last = store.last_runs()
    t = now.timestamp()
    out = []
    for name in BUILDERS:
        p, lim, run = profiles.get(name), limits_by.get(name, {}), last.get(name)
        msg = (run or {}).get("message") or ""
        if settings.offline:
            state, why = "off", "this copy of the app is offline (MASSIVE_OFFLINE): it fetches nothing"
        elif p is None or not p.assets:
            state, why = "off", f"key not set ({KEY_NAMES.get(name, 'key')})"
        elif kill := killswitch.killed(store, name, "fetch", now):
            state, why = "killed", (f"killed until {kill['until'][11:16]} UTC (strike {kill.get('strikes', 1)}): "
                                    f"{kill['why'][:140]}; `kills release {name}` lifts it")
        elif running and running.get("source") == name:
            state, why = "fetching", f"running now: {running.get('requests') or 0:,} requests"
        elif lim.get("leftToday") == 0:
            state, why = "limited", f"today's {lim.get('perDay', 0):,} requests are used; resets at 00:00 UTC"
        elif run and run["status"] == "error":
            state, why = "down", f"last run failed: {msg[:160] or 'no message'}"
        elif run and "rate limited" in msg:
            state, why = "limited", f"the server rate-limited the last run: {msg[:160]}"
        elif lim.get("wait") and lim["wait"].get("until", 0) - t > 30:
            state, why = "limited", f"waiting {round(lim['wait']['until'] - t)} s: {lim['wait'].get('reason', '')}"
        elif name in ARCHIVAL and all(w.get("closed_at") for w in store.watchlist(name)) and store.watchlist(name):
            state, why = "done", "archive complete: every symbol filled, not asked again"
        elif run is None:
            state, why = "idle", "has not run yet"
        else:
            state = "ok"
            why = f"last run: {run.get('rows') or 0:,} rows, {run.get('requests') or 0:,} requests" + (
                " (stopped at its per-run cap: continues next run)" if "cap" in msg and "reached" in msg else "")
        out.append({"source": name, "state": state, "why": why, "lastRun": run and {
            k: run.get(k) for k in ("started_at", "finished_at", "status", "requests", "rows", "message")}})
    return out


ABLE_STATES = ("ok", "fetching", "idle", "done")


def can_fetch(rows: list[dict]) -> bool:
    """Whether any set-up source can fetch now (the status bar says so when none can)."""
    return any(r["state"] in ABLE_STATES for r in rows if r["state"] != "off")


DERIVE_FRESH_MS = 120_000  # a build not heard from for this long has ended (or died)


def _pause(store: Store, now: datetime) -> dict | None:
    from ..collector import pause_state

    return pause_state(store, now)


def derive_progress(store: Store, now: datetime) -> dict | None:
    """What derive.py is building now (frames from finer bars, for the status bar), or None."""
    from ..derive import PROGRESS_KEY

    p = store.kv_json(PROGRESS_KEY, {})
    if not p or now.timestamp() * 1000 - p.get("at", 0) > DERIVE_FRESH_MS:
        return None
    return p


def activity(store: Store, settings: Settings, db_path: Path, source: str = "massive",
             now: datetime | None = None, log_lines: int = 200, focus: str | None = None,
             rows_cache: dict | None = None, rows_ttl_s: float = 0.0) -> dict:
    """The Fetcher view and the status bar: runs, the feed's symbols (the DETAIL_ROWS most worth
    seeing, with `focus` (the open symbol) always among them; every symbol counted by state),
    limits, each source's state, live feeds, the log. With `rows_cache`, the symbols' rows (a few
    index seeks a symbol in every feed file) are kept `rows_ttl_s`, built again sooner when the
    symbol being fetched changes."""
    from ..initializer import load_order
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    today = now.date()
    settings = settings.for_source(source)  # the feed's own plan, rate and caps
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

    order = load_order(store)
    focus_set = frozenset({focus.upper()}) if focus else frozenset()
    key = (source, current, focus_set)
    hit = rows_cache.get(key) if rows_cache is not None else None
    if hit is not None and _time.monotonic() - hit[0] < rows_ttl_s:
        every = hit[1]
    else:
        every = symbol_rows(store, settings, source, now, current, detail=DETAIL_ROWS, focus=focus_set, order=order)
        if rows_cache is not None:
            rows_cache.clear()  # one feed, one fetching symbol at a time: only the newest is kept
            rows_cache[key] = (_time.monotonic(), every)
    symbols = [every[i] for i in ranked(every, order) if every[i].get("planned")]
    counts: dict[str, int] = {}
    for s in every:
        counts[s["state"]] = counts.get(s["state"], 0) + 1

    used = store.requests_today(source)
    left = sum(s["requestsLeft"] for s in every)
    per_day = settings.max_requests_per_day
    limit_rows = limits(store, settings.origin or settings, now)
    sources_health = health(store, settings.origin or settings, now, limit_rows, live)
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
        "newData": counts.get("new", 0),
        "backfill": {
            "done": sum(s["progress"] >= 1 for s in every), "total": len(every), "requestsLeft": left,
            "runs": math.ceil(left / settings.max_requests_per_run) if settings.max_requests_per_run else None,
            "days": round(left / per_day, 1) if per_day else None,
        },
        "symbols": symbols,
        "symbolCounts": counts,
        "symbolTotal": len(every),
        "directory": _directory(store, source, settings, used),
        "ticks": _ticks(store, settings),
        "liveFeeds": live_feeds(store, settings, source, now),
        "derive": derive_progress(store, now),
        "paused": _pause(store, now),
        "updatesPause": collector.updates_pause(store, now),
        "limits": limit_rows,
        "health": sources_health,
        "canFetch": can_fetch(sources_health),
        "offline": bool(settings.offline),  # a copy that fetches nothing (MASSIVE_OFFLINE): said so, not "unable"
        "priority": {t: {"at": v["at"].isoformat(), "runs": v["runs"]}
                     for t, v in collector.priority_requests(store, now).items()},
        "runs": runs,
        "log": log_tail(log_path(db_path), log_lines),
    }
