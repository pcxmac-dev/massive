"""Local HTTP server for the chart app. Binds to 127.0.0.1 only and serves data from the local store."""
from __future__ import annotations

import functools
import json
import math
from contextlib import contextmanager
import mimetypes
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .. import config, live
from ..config import Settings, load_settings
from ..storage import LayoutLimit, Store
from . import activity, astro
from .bars import CALENDAR, PRICE_BASED, TIME_TYPES, BarEngine, Style, resample
from ..calc.derivatives import Model
from .stats import details_subset, key_stats

STATIC = Path(__file__).parent / "static"


def finite(data):
    """`data` with every NaN or infinity (a missing value from pandas) as None, for JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {k: finite(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [finite(v) for v in data]
    return data


MAX_LIMIT = 20000
CALENDAR_MAX_DAYS = 3700  # the widest /api/calendar range (timeaxis.js asks for at most CALENDAR_DAYS)
ROW_COUNTS_TTL_S = 600  # a symbol's bar counts (the Data panel) are counted again after this
PRUNED_COUNTS_TTL_S = 300  # a symbol's pruned-list counts by rule (an index scan of up to millions)
ACTIVITY_TTL_S = 2.0  # the status answer shared by polls this close together
SYMBOL_ROWS_TTL_S = 10.0  # its symbols' rows (index seeks in every feed file) kept this long
FEEDS_TTL_S = 60.0  # the feeds' summary (configuration and the watchlist) kept this long
DEPS_TTL_S = 60.0  # the dependency checks' answer is kept this long
MAX_BODY = 5 * 1024 * 1024
WRITE_HEADER = "X-Massive-Charts"
# Chart data built at once (bars, level 2 profiles, projections, range statistics): each holds a
# window's frames, up to MAX_LIMIT bars; the rest wait their turn, then are answered busy.
HEAVY_REQUESTS = 2
HEAVY_WAIT_S = 30.0


class Busy(RuntimeError):
    """More chart requests than HEAVY_REQUESTS waited past HEAVY_WAIT_S (answered 503)."""


def _heavy(method):
    """A ChartApp method that builds chart data: run under its in-flight cap (ChartApp.heavy_work)."""
    @functools.wraps(method)
    def run(self, *a, **k):
        with self.heavy_work():
            return method(self, *a, **k)
    return run


class ChartApp:
    """The API, independent of HTTP so it can be tested directly."""

    SETTINGS_TTL_S = 5.0  # the user's saved keys and plans are looked at this often

    def __init__(self, store: Store, priority: list[str] | None = None, settings: Settings | None = None,
                 reload_settings: Callable[[], Settings] | None = None):
        self.store = store
        self._settings = settings or load_settings(env={}, env_file=None)
        # With `reload_settings` (the app started from the command line), a key or plan saved after
        # it started (tools/set-key.ps1) counts within SETTINGS_TTL_S: the Fetcher shows each feed
        # as it is now, not as it was when the app started.
        self._reload_settings = reload_settings
        self._saved_keys = config.saved_user_keys() if reload_settings else None
        self._settings_at = time.monotonic()
        self._settings_lock = threading.Lock()
        # The engine reads bars through connections of its own, under its own lock: loading a
        # long history (twenty years of minutes) held the one shared lock for over a minute, and
        # the symbol list, the Fetcher's poll and the search dialog all waited behind it.
        reader = store.reader()
        self.engine = BarEngine(reader, priority=priority, quality=lambda feed: self._feed_quality(feed, reader),
                                cache_bytes=self.settings.chart_cache_mb * 2**20)
        self._heavy = threading.BoundedSemaphore(HEAVY_REQUESTS)
        self._lock = threading.RLock()  # the app's own reads and writes through `store`
        from .window import WindowReader

        self.window = WindowReader(self.engine)  # the auto-ranging chart: any level, by window
        # The Data panel's inventory reads through connections of its own, under its own lock, and
        # its row counts are made in the background (counting AAPL's frames took 20 s under the
        # shared lock, and the symbol list for the picker's map, the calendar and the side panel
        # all waited behind it).
        self._panel_store, self._panel_lock = store.reader(), threading.Lock()
        self._row_counts: dict[str, tuple[float, dict]] = {}  # ticker -> (counted at, row_counts)
        self._counting: set[str] = set()
        self._pruned_counts: dict[str, tuple[float, dict]] = {}  # ticker -> (counted at, by feed and rule)
        self._status_store, self._status_lock = store.reader(), threading.Lock()  # the Fetcher view and status bar
        self._activity: dict[tuple, tuple[float, dict]] = {}
        self._activity_rows: dict = {}  # activity.activity's symbol rows (SYMBOL_ROWS_TTL_S)
        self._feeds_summary: tuple[float, list] | None = None
        self._deps_store, self._deps_lock = store.reader(), threading.Lock()  # dependency checks
        self._deps: tuple[float, dict] | None = None
        self._datasets_store, self._datasets_lock = store.reader(), threading.Lock()  # the datasets list
        self._symbols_store, self._symbols_lock = store.reader(), threading.RLock()  # the symbol list
        self._open_noted: dict[str, int] = {}  # ticker -> ms it was last noted open (note_open)
        self._derive_proc: subprocess.Popen | None = None  # a build started by start_derive
        self._gov_db, self._gov_lock = None, threading.Lock()  # gov/gov.db, opened on first use

    @property
    def settings(self) -> Settings:
        """The app's settings, with the user's saved keys and plans as they are now (see __init__)."""
        if self._reload_settings is None or time.monotonic() - self._settings_at < self.SETTINGS_TTL_S:
            return self._settings
        with self._settings_lock:
            if time.monotonic() - self._settings_at >= self.SETTINGS_TTL_S:
                self._settings_at = time.monotonic()
                saved = config.saved_user_keys()
                if saved != self._saved_keys:
                    self._saved_keys = saved
                    self._settings = self._reload_settings()
                    # what was worked out from the old keys: the feeds, their states, the checks
                    self._activity, self._activity_rows = {}, {}
                    self._feeds_summary = self._deps = None
        return self._settings

    @contextmanager
    def heavy_work(self):
        """One of at most HEAVY_REQUESTS requests building chart data at once; Busy after waiting
        HEAVY_WAIT_S (a page asks again), rather than a queue of requests each holding frames."""
        if not self._heavy.acquire(timeout=HEAVY_WAIT_S):
            raise Busy("the chart app is building other charts' data: ask again shortly")
        try:
            yield
        finally:
            self._heavy.release()

    def _feed_quality(self, feed: str, store: Store | None = None) -> float:
        """A feed's best quality over asset classes (display priority between feeds). Read through
        the connection of whoever asks, under its own lock: the engine its reader, the app `store`."""
        from ..composite import quality_of

        store = store or self.store
        return max((quality_of(store, feed, a) for a in ("stock", "crypto", "index", "fx", "future")), default=1.0)

    def models(self, ticker: str, asset: str, sources: list[str], feeds: dict | None = None,
               store: Store | None = None, lock=None) -> dict | None:
        """The models of an asset traded on several exchanges: the gold-standard reference (one
        exchange), the composite across exchanges, the consolidated tape, and each exchange's quality.
        Read through `store` under `lock` (the app's own by default)."""
        from ..composite import exchange_feeds, quality_of, reference_feed
        from ..sources import feed_profiles

        feeds = feeds if feeds is not None else feed_profiles(self.settings)
        exch = {f: v for f, v in exchange_feeds(feeds, asset).items() if f in sources}
        if len(exch) < 2:
            return None
        consolidated = next((f for f in sources if f in feeds and feeds[f].venues.get(asset) in ("consolidated", "sip")), None)
        store, lock = store or self.store, lock or self._lock
        with lock:
            has_composite = store.bar_edges("composite", ticker, "minute") is not None
            exchanges = sorted(({"feed": f, "exchange": v, "quality": round(quality_of(store, f, asset), 3)}
                                for f, v in exch.items()), key=lambda x: -x["quality"])
        return {"reference": reference_feed(feeds, asset, set(sources)), "composite": has_composite,
                "consolidated": consolidated, "exchanges": exchanges}

    def symbols(self) -> list[dict]:
        """One entry per ticker, with every feed that has data for it. One pass over the watchlist
        and the stored series (indexed by ticker): thousands of symbols, no scan of them per symbol;
        each symbol's state without the per-symbol download plan (the Fetcher shows that)."""
        from collections import defaultdict

        from ..composite import REFERENCE
        from ..sources import feed_profiles

        # Its own connections and lock (a second or more for thousands of symbols): under the shared
        # lock the layouts, watchlists and the first chart waited behind it on every page load.
        store = self._symbols_store
        with self._symbols_lock:
            watch = store.watchlist()
            stored = store.stored_series()
            exchanges = store.exchanges()
            snapshot = store.daily_snapshot()
            icons = store.icon_tickers()
            profiles = store.company_profiles()
            members = store.constituents("sp500", "S&P 500")
            now = datetime.now(timezone.utc).astimezone(activity.ET)
            by_feed: dict[str, dict[str, dict]] = {}  # ticker -> feed -> its Fetcher row
            for src in sorted({w["source"] for w in watch}):
                for r in activity.symbol_rows(store, self.settings, src, now, detail=0):
                    by_feed.setdefault(r["ticker"], {})[src] = r
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        # Futures contracts are stored only as parts of a continuous series (NQ1!): not symbols of their own.
        with self._symbols_lock:
            parts = {c["ticker"] for w in watch if w["asset"] == "future"
                     for c in store.contracts(w["source"], w["source_ticker"])}
        watched = {w["ticker"] for w in watch}
        tickers = sorted(({t for _, t, _ in stored} - (parts - watched)) | watched)
        by_ticker: dict[str, list] = defaultdict(list)
        for w in watch:
            by_ticker[w["ticker"]].append(w)
        kinds: dict[str, dict[str, list]] = defaultdict(dict)  # ticker -> {feed: [kinds]}
        for s, t, k in stored:
            kinds[t].setdefault(s, []).append(k)
        qualities: dict[str, float] = {}

        def quality(feed: str) -> float:  # once per feed, not per symbol
            if feed not in qualities:
                with self._symbols_lock:
                    qualities[feed] = self._feed_quality(feed, store)
            return qualities[feed]
        feed_set = feed_profiles(self.settings)
        out = []
        for ticker in tickers:
            ws = by_ticker.get(ticker, [])
            w = next((x for x in ws if x.get("name")), ws[0] if ws else {})
            mic = next((x["exchange"] for x in ws if x.get("exchange")), None)
            held = kinds.get(ticker, {})
            have = set(held) | {x["source"] for x in ws if x["asset"] == "future"}
            fine = {s for s, ks in held.items() if any(k in ("trades", "second") for k in ks)}
            order = self.engine.rank_sources(have, fine, REFERENCE.get(w.get("asset") or "stock"), quality)
            feeds = [{"source": s, "data": sorted(held.get(s, []))} for s in order]
            state = _symbol_state(by_feed.get(ticker, {}), order)
            out.append({
                "ticker": ticker, "name": w.get("name"), "kind": w.get("kind") or w.get("asset") or "stock",
                "asset": w.get("asset") or "stock",
                "exchange": mic, "exchangeName": names.get(mic, mic) if mic else None,
                "listed": str(w["listed_from"]) if w.get("listed_from") else None,
                "feeds": feeds,
                "models": self.models(ticker, w.get("asset") or "stock", [f["source"] for f in feeds], feed_set,
                                      store, self._symbols_lock),
                "icon": ticker in icons,
                "market": _market(snapshot, ticker, w),
                **_sector(ticker, profiles.get(ticker), w.get("asset") or "stock", (snapshot.get(ticker) or {}).get("close"),
                          members.get(ticker)),
                "changePct": _change(snapshot, ticker),
                "data": {k: state[k] for k in ("state", "progress", "last", "expectedThrough", "nextDue")}
                if state else None,
            })
        return out

    def _auto(self, q: dict[str, str], style_type: str = "candles") -> bool:
        """Whether a request takes the auto-ranging path (window.py): asked for, a series it serves
        (not continuous futures or the composite model) and a chart it draws: time-based types in
        price or a model (the integral at days and longer: each of its values sums the history
        before it), Heikin Ashi, and the price-based types (built whole, at a level of their own).
        Box plots keep the series path."""
        if q.get("auto") != "1" or not self.window.handles(q["source"], q["ticker"]):
            return False
        kind = q.get("model", "price")
        if style_type in PRICE_BASED or style_type == "heikin_ashi":
            return kind == "price"
        return style_type in TIME_TYPES and (kind != "integral" or q.get("period", "minute") in CALENDAR)

    def meta(self, source: str, ticker: str, adjust: str = "none", auto: bool = False) -> dict:
        if auto and self.window.handles(source, ticker):  # its levels, without loading its history
            out = self.window.meta(source, ticker)
            out["attribution"] = self.attribution(out["ticker"], "minute" if out["base"] != "day" else "day", out["asset"])
            return out
        s = self.engine.series(source, ticker, _adjust(adjust))
        if s is None or s.frame.empty:
            raise LookupError(f"no data stored for {source}:{ticker}")
        first, last = s.frame.index[0], s.frame.index[-1]
        return {
            "source": source, "ticker": s.ticker, "base": s.kind, "hasDaily": s.daily is not None,
            "asset": s.asset, "aroundTheClock": s.around_the_clock, "rolls": s.rolls, "adjust": s.adjust,
            "feeds": self.engine.sources_for(ticker) if source == "*" else [source],
            "pricePrecision": s.price_precision, "volumePrecision": s.volume_precision,
            "periods": self.engine.periods(s), "allPeriods": self.engine.all_periods(s),
            "first": first.isoformat(), "last": last.isoformat(),
            "rows": len(s.frame), "attribution": self.attribution(s.ticker, s.kind, s.asset),
        }

    def attribution(self, ticker: str, base: str, asset: str) -> dict:
        """Which feed each span of the series came from (one feed per span): {timespan: [[start
        ms, end ms, feed], ...]} for the base series and the official daily bars."""
        if asset == "future" or base == "trades":
            return {}
        with self._lock:
            out = {}
            for timespan in dict.fromkeys((base, "day")):
                spans = [[a, b, src] for src, ss in self.store.coverage(ticker, timespan).items() for a, b in ss]
                out[timespan] = sorted(spans)
            return out

    @_heavy
    def bars(self, q: dict[str, str]) -> dict:
        return self._bars(q)

    def _bars(self, q: dict[str, str]) -> dict:
        """bars() outside the in-flight cap: the live stream's tail (a few bars, never waiting)."""
        limit = int(q.get("limit", 1000))
        if not 1 <= limit:
            raise ValueError("limit must be at least 1")
        limit = min(limit, MAX_LIMIT)
        style = Style(
            type=q.get("type", "candles"),
            box=float(q["box"]) if q.get("box") else None,
            reversal=float(q["reversal"]) if q.get("reversal") else None,
            lines=int(q.get("lines", 3)),
        )
        if (style.box is not None and style.box <= 0) or (style.reversal is not None and style.reversal <= 0) \
                or not 1 <= style.lines <= 10:
            raise ValueError("box, reversal and lines must be positive (lines 1-10)")
        to_ms = int(q["to"]) if q.get("to") else None
        after_ms = int(q["after"]) if q.get("after") else None
        since_ms = int(q["since"]) if q.get("since") else None
        if self._auto(q, style.type):
            return self.window.styled_page(
                q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)), q.get("session", "regular"),
                style=style, model=_model(q), to_ms=to_ms, after_ms=after_ms, since_ms=since_ms, limit=limit)
        return self.engine.bars(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
            q.get("session", "regular"), to_ms=to_ms, since_ms=since_ms, after_ms=after_ms,
            limit=limit, style=style, adjust=_adjust(q.get("adjust")), lod=int(q.get("lod", 1)), model=_model(q),
        )

    @_heavy
    def profile(self, q: dict[str, str]) -> dict:
        """The virtual level 2 for the chart's bar at `at` (the newest without)."""
        step = float(q["step"]) if q.get("step") not in (None, "", "auto") else None
        if step is not None and not 0 < step < 1e9:
            raise ValueError("step must be a positive price")
        if self.window.handles(q["source"], q["ticker"]):  # the one bar's window, not the whole history
            if q.get("from") and q.get("to"):  # the highlight's stretch, with its major prices
                return self.window.range_profile(q["source"], q["ticker"], q.get("period", "minute"),
                                                 int(q.get("span", 1)), q.get("session", "regular"), int(q["from"]),
                                                 int(q["to"]), step=step)
            return self.window.profile(q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
                                       q.get("session", "regular"), at_ms=int(q["at"]) if q.get("at") else None, step=step)
        return self.engine.profile(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)), q.get("session", "regular"),
            at_ms=int(q["at"]) if q.get("at") else None, step=step, adjust=_adjust(q.get("adjust")))

    STREAM_SECONDS = 300  # one stream's life; the page opens the next from where it got to
    STREAM_CHECK = 0.5  # seconds between looks at the stored data
    STREAM_KEEPALIVE = 15

    def stream(self, q: dict[str, str], stop=None):
        """Server-sent events for a live chart: whenever the symbol's stored data changes (the
        live recorder writes every couple of seconds), the bars from the newest one the page
        has, as /api/bars would give them. Runs alongside the page (a thread per stream) so the
        Stock and Analysis charts follow the feed without asking. Ends after STREAM_SECONDS
        with an `end` event."""
        import time as _time

        since = int(q["since"])
        end = _time.monotonic() + self.STREAM_SECONDS
        seen, beat = None, _time.monotonic()
        while _time.monotonic() < end and not (stop and stop()):
            fp = self.engine.fingerprint(q["source"], q["ticker"])
            if fp != seen:
                first, seen = seen is None, fp
                if q.get("model", "price") != "price":
                    # a model's values move back in time when a bar arrives (a centred window; a Fourier
                    # fit, all of it): the page loads again rather than taking the newest bars only
                    if not first:
                        yield b"data: " + json.dumps({"reload": True}).encode() + b"\n\n"
                        beat = _time.monotonic()
                else:
                    page = self._bars({**q, "since": str(since)})  # the bars since its newest: few
                    if page["bars"]:
                        since = page["bars"][-1]["timestamp"]
                        msg = {"bars": page["bars"], "model": None}
                        yield b"data: " + json.dumps(finite(msg), allow_nan=False).encode() + b"\n\n"
                        beat = _time.monotonic()
            if _time.monotonic() - beat > self.STREAM_KEEPALIVE:
                yield b": keep-alive\n\n"
                beat = _time.monotonic()
            _time.sleep(self.STREAM_CHECK)
        yield b"event: end\ndata: {}\n\n"

    def info(self, source: str, ticker: str, adjust: str = "none") -> dict:
        """Side panel: symbol, business details, key statistics and data coverage."""
        if self.window.handles(source, ticker):  # from its days and levels: no history loaded
            return self._info_light(source, ticker)
        s = self.engine.series(source, ticker, _adjust(adjust))
        if s is None or s.frame.empty:
            raise LookupError(f"no data stored for {source}:{ticker}")
        feeds = self.engine.sources_for(s.ticker) if source == "*" else [source]
        with self._lock:
            watch = next((w for w in self.store.watchlist(ticker=s.ticker) if w["source"] in feeds), {})
            details = next((d for d in (self.store.details(f, s.ticker) for f in feeds) if d), None)
            exchanges = self.store.exchanges()
        daily = s.daily if s.daily is not None and not s.daily.empty else resample(
            s.frame, "day", 1, "extended", utc_days=s.asset in ("crypto", "fx"), session_hours=6 if s.asset == "future" else 0)
        mic = watch.get("exchange") or (details or {}).get("primary_exchange")
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        out = {
            "symbol": {
                "ticker": s.ticker, "name": watch.get("name") or (details or {}).get("name"),
                "asset": s.asset, "kind": watch.get("kind"), "exchange": mic, "exchangeName": names.get(mic, mic),
                "sourceTicker": watch.get("source_ticker"), "inverted": bool(watch.get("invert")),
                "listed": str(watch["listed_from"]) if watch.get("listed_from") else None,
                "figi": watch.get("figi"),
            },
            "details": details_subset(details),
            "detailsUpdated": (details or {}).get("_updated_at"),
            "stats": key_stats(daily),
            "attribution": self.attribution(s.ticker, s.kind, s.asset),
            "audits": self.audit_summary(s.ticker),
            "data": {
                "feeds": feeds, "base": s.kind, "rows": int(len(s.frame)),
                "first": s.frame.index[0].isoformat(), "last": s.frame.index[-1].isoformat(),
                "officialDaily": s.daily is not None, "pricePrecision": s.price_precision,
            },
        }
        if s.asset == "future" and s.rolls:
            out["futures"] = {"contract": s.rolls[-1]["to"], "rolls": len(s.rolls), "lastRoll": s.rolls[-1],
                              "adjust": s.adjust}
        return out

    def _info_light(self, source: str, ticker: str) -> dict:
        levels = self.window.levels(source, ticker)
        if not levels:
            raise LookupError(f"no data stored for {source}:{ticker}")
        s = self.window.series(source, ticker)
        feeds = self.window.feeds(source, ticker)
        daily = self.window._days(s, feeds)
        with self._lock:
            watch = next((w for w in self.store.watchlist(ticker=s.ticker) if w["source"] in feeds), {})
            details = next((d for d in (self.store.details(f, s.ticker) for f in feeds) if d), None)
            exchanges = self.store.exchanges()
        mic = watch.get("exchange") or (details or {}).get("primary_exchange")
        names = {r.mic: r.name for r in exchanges.itertuples() if r.mic}
        # the series' base as the interval charts counted it: its minutes (seconds are a layer apart)
        base = next((lv for lv in levels if lv["type"] == "minute"), levels[0])
        iso = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert("America/New_York").isoformat()
        return {
            "symbol": {
                "ticker": s.ticker, "name": watch.get("name") or (details or {}).get("name"),
                "asset": s.asset, "kind": watch.get("kind"), "exchange": mic, "exchangeName": names.get(mic, mic),
                "sourceTicker": watch.get("source_ticker"), "inverted": bool(watch.get("invert")),
                "listed": str(watch["listed_from"]) if watch.get("listed_from") else None,
                "figi": watch.get("figi"),
            },
            "details": details_subset(details),
            "detailsUpdated": (details or {}).get("_updated_at"),
            "stats": key_stats(daily),
            "attribution": self.attribution(s.ticker, "minute" if base["type"] != "day" else "day", s.asset),
            "audits": self.audit_summary(s.ticker),
            "data": {
                "feeds": feeds, "base": base["type"], "rows": self._base_rows(s.ticker, feeds, base["type"]),
                "first": iso(min(lv["first"] for lv in levels)), "last": iso(max(lv["last"] for lv in levels)),
                "officialDaily": bool(len(daily) and (~daily["derived"].astype(bool)).any()), "pricePrecision": 2,
                "levels": levels,
            },
        }

    def markets(self) -> dict:
        from . import world
        from .markets import markets

        with self._lock:
            out = markets(self.engine.calendar(), self.store.exchanges(), self.store.watchlist())
        with self._lock:
            moves = world.market_moves(self.store)
        out["world"] = world.world_markets(moves=moves)
        out["sun"] = world.subsolar()
        out["sky"] = astro.sky()
        return out

    def deps(self) -> dict:
        from .. import deps

        # Its own connections and lock, and its answer kept DEPS_TTL_S (4.6 s cold): under the shared
        # lock the symbol list, and with it the first chart, waited behind it on every page load.
        with self._deps_lock:
            if self._deps is None or time.monotonic() - self._deps[0] > DEPS_TTL_S:
                self._deps = (time.monotonic(), {"checks": deps.as_rows(deps.checks(self._deps_store, self.settings))})
            return self._deps[1]

    def directory(self) -> dict:
        """Every listing in the directory (placeholders), with what is known locally."""
        with self._lock:
            rows = self.store.directory()
            watched = {w["ticker"] for w in self.store.watchlist()}
            profiles = self.store.company_profiles()
            members = self.store.constituents("sp500", "S&P 500")
            changes = self.store.snapshot_changes()
        out = []
        for r in rows:
            dv = (r["close"] or 0) * (r["volume"] or 0) if r["close"] and r["volume"] else None
            out.append({"ticker": r["ticker"], "kind": r["kind"], "name": r["name"], "exchange": r["exchange"],
                        "type": r["type"], "close": r["close"], "dollarVolume": dv, "icon": bool(r["icon"]),
                        "watched": r["ticker"] in watched, "changePct": changes.get(r["ticker"]),
                        **_sector(r["ticker"], profiles.get(r["ticker"]), "future" if r["kind"] == "future" else "stock",
                                  r["close"], members.get(r["ticker"]))})
        return {"items": out}

    def watch(self, ticker: str) -> dict:
        """Add a directory listing to the watchlist (the fetcher then collects it)."""
        from ..symbols import resolve

        with self._lock:
            row = next((r for r in self.store.directory() if r["ticker"] == ticker.upper()), None)
            if row is None:
                raise LookupError(f"{ticker} is not in the directory")
            spec = resolve(f"{row['ticker']}::{row['exchange']}" if row["kind"] == "stock" and row["exchange"] else row["ticker"])
            from ..sources import BUILDERS, supports

            for feed in BUILDERS:  # each span is still downloaded by one feed only
                if supports(feed, spec):
                    self.store.watch_spec(feed, spec)
        return {"watched": spec.ticker}

    def announcements(self) -> dict:
        from .announcements import announcements

        with self._lock:
            watch = self.store.watchlist()
            return announcements(self.store, watch)

    def gov(self) -> dict:
        """The Gov view: the agencies' newest, the current regulatory parameters and each
        source's state (gov.py; fetched by the sync, read here from gov/gov.db)."""
        from .. import gov

        with self._gov_lock:
            if self._gov_db is None:
                self._gov_db = gov.GovDB(gov.db_path(self.store.path))
            return gov.view(self._gov_db, contact_set=bool(self.settings.gov_contact), spx_close=self._spx_close())

    def _spx_close(self) -> tuple[str, float] | None:
        """The S&P 500's newest daily close held (the circuit breakers' reference), or None."""
        try:
            if not self.window.handles("*", "SPX"):
                return None
            bars = self.window.page("*", "SPX", "day", 1, "regular", limit=1)["bars"]
        except (LookupError, KeyError, ValueError):
            return None
        if not bars:
            return None
        day = datetime.fromtimestamp(bars[-1]["timestamp"] / 1000, timezone.utc).date().isoformat()
        return day, float(bars[-1]["close"])

    def calendar(self, q: dict[str, str]) -> dict:
        """Sessions of a symbol between two times (ms), for annotating the chart."""
        from .markets import symbol_sessions

        with self._lock:
            item = next(iter(self.store.watchlist(ticker=q["ticker"])), None)
        source = q.get("source", "*")
        if self.window.handles(source, q["ticker"]):  # the asset is all it needs: no history loaded
            asset = self.window.asset(q["ticker"], None if source == "*" else source)
        else:
            s = self.engine.series(source, q["ticker"])  # under the engine's own lock
            asset = s.asset if s else (item or {}).get("asset", "stock")
        product = item["source_ticker"] if item and item["asset"] == "future" else None
        start = datetime.fromtimestamp(int(q["from"]) / 1000, timezone.utc)
        end = datetime.fromtimestamp(int(q["to"]) / 1000, timezone.utc)
        if end - start > timedelta(days=CALENDAR_MAX_DAYS):
            raise ValueError("calendar range is limited to 10 years")
        return {"asset": asset, "sessions": symbol_sessions(self.engine.calendar(), asset, product, start, end)}

    @_heavy
    def future(self, q: dict[str, str]) -> dict:
        style = Style(type=q.get("type", "candles"))
        if self._auto(q, style.type):  # projected from the level's newest bar, not the whole history
            from .projection import project

            if style.type in PRICE_BASED:  # bricks follow price: no time axis to project
                return {"timestamps": [], "last": None, "total": 0}
            period, span, session = q.get("period", "minute"), int(q.get("span", 1)), q.get("session", "regular")
            s = self.window.series(q["source"], q["ticker"])
            model = _model(q)
            # a model's newest value may be bars back (a stretch shorter than its window has none)
            newest = self.window.styled_page(q["source"], q["ticker"], period, span, session, style=style, model=model,
                                             limit=1 if model.is_price else 2 * model.window)["bars"]
            if not newest:
                return {"timestamps": [], "last": None, "total": 0}
            last = pd.Timestamp(newest[-1]["timestamp"], unit="ms", tz="UTC").tz_convert("America/New_York")
            if period in ("day", "week", "month", "year"):  # days from the exchange's sessions, not every minute
                s.daily = pd.DataFrame({"close": [newest[-1]["close"]]}, index=pd.DatetimeIndex([last]))
            from .window import UNIT_MS

            # slots for at most 50 years ahead: 4,000 months is centuries of sessions to walk
            cap = max(1, int(50 * 365.25 * 86_400_000 // (UNIT_MS[period] * span)))
            ts = project(self.engine.calendar(), s, None, period, span, s.session_for(session), style.type, 1, 1, last,
                         max(0, int(q.get("offset", 0))), min(int(q.get("count", 500)), 5000, cap),
                         cache=self.engine._future_cache)
            return {"timestamps": ts, "last": newest[-1]["timestamp"], "total": 1, "lastBar": newest[-1]["timestamp"]}
        return self.engine.future(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)), q.get("session", "regular"),
            style=style, adjust=_adjust(q.get("adjust")), lod=int(q.get("lod", 1)), model=_model(q),
            offset=max(0, int(q.get("offset", 0))), count=min(int(q.get("count", 500)), 5000),
        )

    @_heavy
    def range_stats(self, q: dict[str, str]) -> dict:
        if self._auto({**q, "auto": "1"}):  # its own window only
            return self.window.range_stats(q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
                                           q.get("session", "regular"), int(q["from"]), int(q["to"]), model=_model(q))
        return self.engine.range_stats(
            q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
            q.get("session", "regular"), int(q["from"]), int(q["to"]), adjust=_adjust(q.get("adjust")),
            model=_model(q),
        )

    # -- schemes and replay (calc/schemes.py, window.py; docs/ARCHITECTURE.md) --------
    def schemes(self) -> dict:
        """The built-in schemes and their settings, for the Analysis panel."""
        from ..calc.schemes import SCHEMES

        return {"schemes": [{"key": s.key, "label": s.label, "about": s.about, "params": [
            {"key": p.key, "label": p.label, "default": p.default, "lo": p.lo, "hi": p.hi, "step": p.step,
             "choices": list(p.choices), "about": p.about} for p in s.params]} for s in SCHEMES.values()]}

    def _windowed(self, q: dict[str, str], what: str) -> None:
        if not self.window.handles(q["source"], q["ticker"]):
            raise ValueError(f"{what} on the chart's own series only (not continuous futures or the composite model yet)")

    @_heavy
    def scheme(self, q: dict[str, str]) -> dict:
        from ..calc.schemes import Costs

        self._windowed(q, "schemes run")
        given = json.loads(q.get("params") or "{}")
        if not isinstance(given, dict):
            raise ValueError("params: an object of settings")
        fee, slip = float(q.get("fee") or 0), float(q.get("slippage") or 0)
        if not (0 <= fee <= 1000 and 0 <= slip <= 1000):
            raise ValueError("fee and slippage: 0 to 1,000 basis points")
        return self.window.scheme(q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
                                  q.get("session", "regular"), int(q["from"]), int(q["to"]), q["scheme"], given,
                                  Costs(fee, slip), q.get("longOnly") == "1")

    @_heavy
    def replay(self, q: dict[str, str]) -> dict:
        from .window import REPLAY_PAGE

        self._windowed(q, "replay runs")
        limit = int(q.get("limit") or REPLAY_PAGE)
        if not 1 <= limit <= 20_000:
            raise ValueError("limit: 1 to 20,000 bars")
        return self.window.replay(q["source"], q["ticker"], q.get("period", "minute"), int(q.get("span", 1)),
                                  q.get("session", "regular"), int(q["from"]), limit)

    # -- trading (trading/; docs/ARCHITECTURE.md, "Trading"): paper first, live trading off ----------
    @property
    def paper(self):
        if getattr(self, "_paper", None) is None:
            from ..trading.paper import PaperBook

            self._paper = PaperBook(self.store.conn, self._fill_bars, self._last_price,
                                    lambda t: self.window.asset(t))
        return self._paper

    def _fill_bars(self, source: str, ticker: str, at_ms: int, until_ms: int):
        """The finest bars stored from `at_ms` to `until_ms` (1-second where held, else minutes), in
        the symbol's regular session (stocks; round the clock for crypto): what a paper order fills from."""
        import pandas as pd

        from ..trading.paper import MATCH_PAGE
        from .bars import epoch_ms

        if not self.window.handles(source, ticker):
            raise ValueError("paper orders fill from the chart's own series (not continuous futures or the composite yet)")
        s = self.window.series(source, ticker)
        feeds = self.window.feeds(source, ticker)
        fine = "second" if self.window.replay_fine(source, ticker, "minute", at_ms) == "second" else "minute"
        page = self.window._intraday_page(s, feeds, fine, 1, s.session_for("regular"), None, at_ms - 1, None, MATCH_PAGE)
        bars = page["frame"]
        t = epoch_ms(bars.index) if len(bars) else []
        out = pd.DataFrame({"t": t, **{k: bars[k].to_numpy() for k in ("open", "high", "low", "close")}}) if len(bars)             else pd.DataFrame(columns=["t", "open", "high", "low", "close"])
        return out[out["t"] < until_ms], 1000 if fine == "second" else 60_000

    def _last_price(self, source: str, ticker: str) -> float | None:
        if not self.window.handles(source, ticker):
            return None
        bars = self.window.page(source, ticker, "minute", 1, "extended", limit=1)["bars"]
        return float(bars[-1]["close"]) if bars else None

    @_heavy
    def trading(self) -> dict:
        """The Trading view: the brokers, and the paper account (its orders matched first, from bars)."""
        from ..trading.brokers import PLANNED

        with self._lock:
            book = self.paper.summary()
        return {"brokers": [{"key": "paper", "label": self.paper.label, "live": False, "status": "ready"}, *PLANNED],
                "liveTrading": False, "broker": "paper", **book}

    def trade_place(self, body: dict) -> dict:
        with self._lock:
            return self.paper.place(body.get("ticker", ""), body.get("source") or "*", body.get("side", ""),
                                    body.get("qty", 0), body.get("type", "market"), body.get("limit"), body.get("stop"),
                                    body.get("tif", "day"))

    def trade_cancel(self, order_id: int) -> dict:
        with self._lock:
            return self.paper.cancel(order_id)

    def trade_reset(self, body: dict) -> dict:
        with self._lock:
            return self.paper.reset(body.get("cash", 100_000), body.get("feeBps", 0))

    # -- watchlists (the rail's lists of symbols) --------------------------------
    WATCHLISTS_KEY = live.WATCHLISTS_KEY  # the live recorder streams their stocks
    MAX_LISTS, MAX_LIST_TICKERS = 50, 1000

    def watchlists(self) -> dict:
        with self._lock:
            raw = self.store.kv_get(self.WATCHLISTS_KEY)
        try:
            lists = json.loads(raw) if raw else []
        except ValueError:
            lists = []
        return {"lists": lists if isinstance(lists, list) else []}

    def save_watchlists(self, body: dict) -> dict:
        """Replace the lists: [{name, tickers}], names unique (1-40 characters), tickers in order."""
        lists = body.get("lists")
        if not isinstance(lists, list) or len(lists) > self.MAX_LISTS:
            raise ValueError(f"lists must be a list of at most {self.MAX_LISTS}")
        out, names = [], set()
        for item in lists:
            name = str((item or {}).get("name", "")).strip() if isinstance(item, dict) else ""
            tickers = item.get("tickers") if isinstance(item, dict) else None
            if not 1 <= len(name) <= 40 or name in names:
                raise ValueError("each list needs a name of 1-40 characters, not used by another list")
            if not isinstance(tickers, list) or len(tickers) > self.MAX_LIST_TICKERS \
                    or not all(isinstance(t, str) and 0 < len(t) <= 32 for t in tickers):
                raise ValueError(f"tickers must be at most {self.MAX_LIST_TICKERS} symbols")
            names.add(name)
            out.append({"name": name, "tickers": list(dict.fromkeys(t.strip().upper() for t in tickers))})
        with self._lock:
            self.store.kv_set(self.WATCHLISTS_KEY, json.dumps(out))
        return {"lists": out}

    # -- layouts (saved markups) ----------------------------------------------

    def layouts(self) -> dict:
        with self._lock:
            return {"layouts": self.store.list_layouts(), "max": self.store.MAX_LAYOUTS}

    def layout(self, layout_id: int) -> dict:
        with self._lock:
            found = self.store.get_layout(layout_id)
        if found is None:
            raise LookupError(f"no layout {layout_id}")
        return found

    def create_layout(self, body: dict) -> dict:
        data = body.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        with self._lock:
            return self.store.create_layout(str(body.get("name", "")), data)

    def update_layout(self, layout_id: int, body: dict) -> dict:
        data = body.get("data")
        if data is not None and not isinstance(data, dict):
            raise ValueError("data must be an object")
        with self._lock:
            found = self.store.update_layout(layout_id, body.get("name"), data)
        if found is None:
            raise LookupError(f"no layout {layout_id}")
        return found

    def delete_layout(self, layout_id: int) -> dict:
        with self._lock:
            if not self.store.delete_layout(layout_id):
                raise LookupError(f"no layout {layout_id}")
        return {"deleted": layout_id}

    OPEN_NOTE_SECONDS = 30

    def note_open(self, ticker: str, now_ms: int | None = None) -> None:
        """A chart page has `ticker` open (its activity poll says so): the live recorder streams it
        if it is a stock (live.wanted_stocks). Noted at most every OPEN_NOTE_SECONDS per symbol;
        symbols not noted for live.OPEN_FRESH_MS drop out."""
        import time as _time

        ticker = ticker.strip().upper()
        now_ms = now_ms or int(_time.time() * 1000)
        last = self._open_noted.get(ticker)
        if not 0 < len(ticker) <= 32 or (last is not None and now_ms - last < self.OPEN_NOTE_SECONDS * 1000):
            return
        self._open_noted[ticker] = now_ms

        def add(opened: dict) -> dict:  # other chart apps note theirs in the same key
            kept = {t: at for t, at in opened.items()
                    if isinstance(at, (int, float)) and now_ms - at <= live.OPEN_FRESH_MS}
            return {**kept, ticker: now_ms}

        with self._lock:
            self.store.kv_update(live.OPEN_KEY, add, {})

    def activity(self, q: dict[str, str]) -> dict:
        if q.get("open"):
            self.note_open(q["open"])
        # On connections and a lock of its own (it took 1 to 3 s under the shared lock, polled by every
        # open page, and the symbol list and watchlists waited 17 s behind it), and one answer for the
        # polls that arrive within ACTIVITY_TTL_S of each other.
        key = (q.get("source", "massive"), min(int(q.get("lines", 200)), 1000), q.get("open"))
        with self._status_lock:
            hit = self._activity.get(key)
            if hit is None or time.monotonic() - hit[0] > ACTIVITY_TTL_S:
                hit = self._activity[key] = (time.monotonic(), activity.activity(
                    self._status_store, self.settings, self.store.path, key[0], log_lines=key[1], focus=key[2],
                    rows_cache=self._activity_rows, rows_ttl_s=SYMBOL_ROWS_TTL_S))
            out = dict(hit[1])
        out["feeds"] = self.feeds()
        return out

    def datasets(self) -> dict:
        """The research datasets: what is held, its span, files fetched and closed, quality."""
        from .. import datasets

        with self._datasets_lock:  # its own connections: never behind (or in front of) the shared lock
            rows = datasets.status(self._datasets_store)
        for r in rows:
            r["fetched"] = r["fetched"].isoformat() if r["fetched"] else None
        return {"datasets": rows}

    def dataset_series(self, name: str) -> dict:
        """A dataset's series (or, for the COT report, its markets) for the viewer's picker."""
        from .. import datasets

        if name not in datasets.DATASETS:
            raise LookupError(f"no dataset {name}")
        ds = datasets.DATASETS[name]
        with self._lock:
            if name == "cftc-cot-legacy":
                return {"name": name, "markets": datasets.cot_markets(self.store)}
            return {"name": name, "series": datasets.series_list(self.store, name),
                    "members": len(self.store.constituents(ds.provider, "S&P 500"))}

    def dataset_values(self, name: str, series: str) -> dict:
        from .. import datasets

        if name not in datasets.DATASETS:
            raise LookupError(f"no dataset {name}")
        wanted = [s for s in series.split(",") if s]
        if not wanted:
            raise ValueError("no series given")
        with self._lock:
            return {"name": name, "series": datasets.values(self.store, name, wanted)}

    def audit_summary(self, ticker: str | None = None, limit: int = 40) -> dict:
        with self._lock:
            rows = self.store.audits(limit, ticker)
            return {"audits": rows, "scores": self.store.audit_scores(), "queued": len(self.store.audit_queue()),
                    "quality": self.store.qualities()}

    def feeds(self) -> list[dict]:
        """The feeds' summary, kept FEEDS_TTL_S (every status poll carries it)."""
        hit = self._feeds_summary
        if hit is not None and time.monotonic() - hit[0] < FEEDS_TTL_S:
            return hit[1]
        out = self._feeds()
        self._feeds_summary = (time.monotonic(), out)
        return out

    def _feeds(self) -> list[dict]:
        from ..sources import ARCHIVAL, BUILDERS, feed_profiles

        profiles = feed_profiles(self.settings)
        with self._lock:
            watch = self.store.watchlist()
        out = []
        for name in BUILDERS:
            p, s = profiles.get(name), self.settings.for_source(name)
            keys = (self.settings.alpaca.has_keys if name == "alpaca" and self.settings.alpaca
                    else bool(self.settings.tiingo_api_key) if name == "tiingo" else None)
            rows = [w for w in watch if w["source"] == name]
            closed = name in ARCHIVAL and all(w.get("closed_at") for w in rows)
            out.append({"name": name, "configured": p is not None, "assets": sorted(p.assets) if p else [],
                        "plan": s.tier.name, "keys": keys, "archive": name in ARCHIVAL, "closed": closed})
        return out

    def icon(self, ticker: str) -> tuple[str, bytes] | None:
        with self._lock:
            return self.store.icon(ticker)

    def start_scan(self, ticker: str | None = None) -> dict:
        """Run `scan --probe --fetch` (for one ticker, or every watched symbol) in its own
        process: it takes the run lock and the plan caps like a scheduled sync, and shows up in
        the activity view and the status bar while it runs."""
        if self.settings.offline:
            raise RuntimeError("offline (MASSIVE_OFFLINE): this copy of the app fetches nothing")
        if self._halted():
            raise RuntimeError("fetching is halted: resume it first")
        if activity.lock_owner(self.store.path):
            raise RuntimeError("a run is already in progress")
        args = [sys.executable, "-m", "massive_collate", "scan", "--probe", "--fetch", "--all-sources"]
        if ticker:
            with self._lock:
                watched = {w["ticker"] for w in self.store.watchlist()}
            if ticker.upper() not in watched:
                raise LookupError(f"{ticker} is not on the watchlist")
            args += ["--only", ticker.upper()]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        self._scan = proc  # keep a reference so it is reaped
        return {"started": True, "pid": proc.pid}

    def inventory(self, ticker: str) -> dict:
        """The symbol's data feed by feed and frame by frame (inventory.py): what each feed sent,
        what was built here from its finer data (*derived) and what is not built yet."""
        from .. import inventory

        t = ticker.upper()
        rank = self.engine.sources_for(t)
        counted = self._row_counts.get(t)
        with self._panel_lock:
            out = inventory.symbol_inventory(self._panel_store, t, rank, counts=counted[1] if counted else {})
        if (counted is None or time.monotonic() - counted[0] > ROW_COUNTS_TTL_S) and t not in self._counting:
            self._counting.add(t)  # the counts follow on the panel's next read
            threading.Thread(target=self._count_rows, args=(t,), daemon=True, name=f"count-{t}").start()
        return out

    def _base_rows(self, ticker: str, feeds: list[str], base: str) -> int | None:
        """The bars a series holds at its base level (the feed holding most, its own and built), from
        the background counts (None until they are made; asked for here)."""
        counted = self._row_counts.get(ticker)
        if (counted is None or time.monotonic() - counted[0] > ROW_COUNTS_TTL_S) and ticker not in self._counting:
            self._counting.add(ticker)
            threading.Thread(target=self._count_rows, args=(ticker,), daemon=True, name=f"count-{ticker}").start()
        if counted is None:
            return None
        rows = counted[1]
        return max((rows.get((f, "bars", base, 1), 0) + rows.get((f, "built_bars", base, 1), 0) for f in feeds), default=0)

    def _count_rows(self, ticker: str) -> None:
        """Count a symbol's bars frame by frame (inventory.row_counts) on connections of this thread."""
        from .. import inventory

        reader = None
        try:
            reader = self.store.reader()
            self._row_counts[ticker] = (time.monotonic(), inventory.row_counts(reader, ticker))
        except Exception:  # noqa: BLE001 - the panel shows no counts; the next read tries again
            pass
        finally:
            self._counting.discard(ticker)
            if reader is not None and reader is not self.store:
                reader.close()

    def pruned(self, ticker: str, source: str | None = None) -> dict:
        """A symbol's bad data (baddata.py): each feed's pruned list, newest first, and its counts."""
        from .. import baddata

        t = ticker.upper()
        with self._panel_lock:  # its own connections: a symbol's list can hold millions of kept bars
            counted = self._pruned_counts.get(t)
            if counted is None or time.monotonic() - counted[0] > PRUNED_COUNTS_TTL_S:
                counted = self._pruned_counts[t] = (time.monotonic(), self._panel_store.pruned_counts(t))
            entries = baddata.entries(self._panel_store, t, None if source in (None, "", "*") else source)
        return {"counts": counted[1], "entries": entries}

    @contextmanager
    def _view(self, q: dict[str, str]):
        """The request's view of the data: filtered (bad data left out) unless it asks unfiltered=1."""
        from .window import UNFILTERED

        token = UNFILTERED.set(q.get("unfiltered") == "1")
        try:
            yield
        finally:
            UNFILTERED.reset(token)

    def start_derive(self, ticker: str) -> dict:
        """Build a symbol's derived frames now (`derive --tickers`) in its own process: from what
        is stored only, no request to any feed. One build at a time from this app."""
        with self._lock:
            watched = {w["ticker"] for w in self.store.watchlist(ticker=ticker)}
        if ticker.upper() not in watched:
            raise LookupError(f"{ticker} is not on the watchlist")
        if self._derive_proc is not None and self._derive_proc.poll() is None:
            raise RuntimeError("a build of derived frames is already running")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._derive_proc = subprocess.Popen([sys.executable, "-m", "massive_collate", "derive", "--tickers", ticker.upper()],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             stdin=subprocess.DEVNULL, creationflags=flags)
        return {"started": True, "pid": self._derive_proc.pid}

    def set_fetching(self, body: dict) -> dict:
        """Pause background fetching ({"paused": true, "minutes": n}: at most two hours; the open
        symbol's own fetches go on, so the chart gets the feeds to itself), halt all fetching until
        resumed ({"paused": true, "halt": true}: the open symbol's too), or resume ({"paused":
        false}): then a sync starts at once unless one is running, as the hourly one would."""
        from .. import collector

        paused = bool(body.get("paused"))
        with self._lock:
            p = collector.set_pause(self.store, paused, minutes=body.get("minutes"), halt=bool(body.get("halt")))
        started = None
        if not paused and not self.settings.offline and not activity.lock_owner(self.store.path):
            exe = Path(sys.executable)
            quiet = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
            proc = subprocess.Popen([str(quiet), "-m", "massive_collate", "sync"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._scan, started = proc, proc.pid
        return {"paused": p, "started": started}

    def set_updates_pause(self, body: dict) -> dict:
        """Pause the updates of the symbols not picked ({"days": n}, 1-30) or resume them
        ({"days": 0}): history and the picked symbols (opened, or on a watchlist) go on."""
        from .. import collector

        days = float(body.get("days") or 0)
        if days and not 1 <= days <= 30:
            raise ValueError("days must be 1-30 (0 resumes)")
        with self._lock:
            return {"updatesPause": collector.set_updates_pause(self.store, days)}

    def _halted(self) -> bool:
        from .. import collector

        with self._lock:
            return bool((collector.pause_state(self.store) or {}).get("halt"))

    PRIORITY_COOLDOWN = 300  # seconds: a symbol opened again sooner is not asked for again

    def priority(self, ticker: str) -> dict:
        """The chart opened `ticker`: every feed holding it updates it before anything else.
        With no run going, a run for that symbol alone starts now (`sync --tickers`: gaps and
        new data on every feed, only what each plan has published); with one going, the request
        waits in the database and the running sync takes the symbol next on each feed it still
        has to visit. Within every feed's limits either way: one fetching process at a time (the
        run lock), and limiters that share their window across runs."""
        from .. import collector

        from ..freshness import expected
        from ..sources import feed_profiles

        ticker = ticker.upper()
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = self.store.watchlist(ticker=ticker)
            feeds = sorted({w["source"] for w in rows})
            if not feeds:
                raise LookupError(f"{ticker} is not on the watchlist")
            profiles = feed_profiles(self.settings)
            tiers = {name: self.settings.for_source(name).tier for name in profiles if name in feeds}
            assets = {name: profiles[name].assets for name in tiers}  # a feed without its key serves nothing
            serving = any(r["source"] in tiers and r["asset"] in assets[r["source"]] for r in rows)
            if (serving and not self.settings.offline and not self._halted()
                    and not collector.needs_fetch(self.store, ticker, tiers, now, assets)):
                # up to date on every feed: nothing new can exist yet (out of session, or not released)
                due = [e.next_due for e in (expected(r["asset"], tiers[r["source"]], now) for r in rows
                                            if r["source"] in tiers) if e.next_due]
                return {"ticker": ticker, "feeds": feeds, "state": "current",
                        "nextDue": min(due).isoformat() if due else None}
            asked = collector.priority_requests(self.store, now).get(ticker)
            if asked is None or (now - asked["at"]).total_seconds() > self.PRIORITY_COOLDOWN:
                collector.request_priority(self.store, ticker, now)
                asked = {"at": now, "runs": 0}
        out = {"ticker": ticker, "feeds": feeds, "asked": asked["at"].isoformat()}
        if self.settings.offline:
            return {**out, "state": "offline"}  # a test copy of the app: nothing is fetched
        if self._halted():
            return {**out, "state": "halted"}  # nothing is fetched until the user resumes
        if activity.lock_owner(self.store.path):
            return {**out, "state": "queued"}  # the running sync takes it next
        if asked["runs"]:
            return {**out, "state": "recent"}  # a run for it already went
        args = [sys.executable, "-m", "massive_collate", "sync", "--tickers", ticker]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._scan = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL, creationflags=flags)
        with self._lock:
            collector.priority_started(self.store, ticker)
        return {**out, "state": "started", "pid": self._scan.pid}

    def exchanges(self) -> list[dict]:
        with self._lock:
            df = self.store.exchanges()
        return json.loads(df.to_json(orient="records"))


def _sector(ticker: str, profile: dict | None, asset: str, close: float | None = None,
            member: dict | None = None) -> dict:
    """Sector, industry and market cap for the market map (stocks with reference data). The cap is
    this share class's own (its shares outstanding x price) where known: a company with two listed
    classes (GOOG, GOOGL) would otherwise be drawn twice its size. An S&P 500 member takes its
    sector from the index list's GICS classification; its industry keeps the map's own name unless
    that belongs to another sector, when the GICS sub-industry is used."""
    from .sectors import SECTORS, classify

    profile = profile or {}
    if asset != "stock" or not (profile or member):
        return {"sector": None, "industry": None, "marketCap": None}
    sector, industry = classify(ticker, profile.get("sic_code"), profile.get("sic_description"))
    basis = "SIC"
    if member and member.get("sector") in SECTORS:
        if member["sector"] != sector:
            industry = member.get("industry") or industry
        sector, basis = member["sector"], "GICS (S&P 500 list)"
    shares = profile.get("class_shares")
    cap = shares * close if shares and close else profile.get("market_cap")
    return {"sector": sector, "industry": industry, "sectorBasis": basis, "marketCap": cap,
            "companyCap": profile.get("market_cap")}


NOT_COLLECTING = ("blocked", "closed", "retired")


def _symbol_state(rows: dict[str, dict], order: list[str]) -> dict | None:
    """One data state for a symbol watched on several feeds (the symbol list's icon): that of the
    best-ranked feed holding its data (the one a merged chart shows first); with none holding
    any, the most pressing of the feeds collecting it. Not whichever feed came last: AMD read "up
    to date" from a feed that leaves its history to another while that one was still backfilling."""
    for feed in order:
        if feed in rows:
            return rows[feed]
    collecting = [r for r in rows.values() if r["state"] not in NOT_COLLECTING] or list(rows.values())
    return min(collecting, key=lambda r: activity.STATE_RANK.get(r["state"], 9), default=None)


def _change(snapshot: dict, ticker: str) -> float | None:
    """Last session's change in percent from local daily bars (any asset class)."""
    d = snapshot.get(ticker)
    return (d["close"] / d["prevClose"] - 1) * 100 if d and d.get("prevClose") else None


def _market(snapshot: dict, ticker: str, watch: dict) -> dict | None:
    """Last close, change and 30-session average dollar volume from local daily bars."""
    d = snapshot.get(ticker)
    if not d or watch.get("asset", "stock") != "stock":
        return None  # dollar volume only means something for shares
    change = (d["close"] / d["prevClose"] - 1) * 100 if d["prevClose"] else None
    return {"close": d["close"], "changePct": change, "dollarVolume": d["dollarVolume"]}


def _model(q: dict[str, str]) -> Model:
    """Model from query parameters: model, basis, per, window, order, gaps, causal."""
    return Model(
        kind=q.get("model", "price"), basis=q.get("basis", "price"), per=q.get("per", "hour"),
        window=int(q.get("window", 9)), order=int(q.get("order", 3)), reset_gaps=q.get("gaps", "1") != "0",
        causal=q.get("causal", "0") == "1",
    ).validate()


def _adjust(value: str | None) -> str:
    value = value or "none"
    if value not in ("none", "difference", "ratio"):
        raise ValueError("adjust must be none, difference or ratio")
    return value


def make_handler(app: ChartApp, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "massive-chart"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self, q: dict[str, str]) -> None:
            if not q.get("since"):
                return self._json({"error": "since is required"}, HTTPStatus.BAD_REQUEST)
            app.bars({**{k: v for k, v in q.items() if k != "since"}, "limit": "1"})  # bad parameters fail here, as JSON
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                for event in app.stream(q):
                    self.wfile.write(event)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the page went away

        def _json(self, data, status: int = 200) -> None:
            try:
                body = json.dumps(data, allow_nan=False)
            except ValueError:  # a NaN slipped in (pandas' missing value): sent as null, not a broken endpoint
                body = json.dumps(finite(data), allow_nan=False)
            self._send(status, body.encode(), "application/json")

        def _local(self) -> bool:
            # Only answer requests addressed to this machine (blocks DNS-rebinding from web pages).
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                return False
            return True

        def _write(self, method: str) -> None:
            """POST/PUT/DELETE for layouts. The custom header can't be sent cross-site without a
            CORS preflight, which this server never grants, so other web pages can't write."""
            if not self._local():
                return
            if self.headers.get(WRITE_HEADER) != "1":
                return self._send(HTTPStatus.FORBIDDEN, b"missing request header", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if method != "DELETE" else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                parts = urlparse(self.path).path.rstrip("/").split("/")
                if method == "POST" and parts == ["", "api", "watch"]:
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1]
                    if not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    return self._json(app.watch(ticker), HTTPStatus.CREATED)
                if method == "POST" and parts == ["", "api", "updates-pause"]:
                    return self._json(app.set_updates_pause(body))
                if method == "PUT" and parts == ["", "api", "watchlists"]:
                    return self._json(app.save_watchlists(body))
                if method == "POST" and parts == ["", "api", "priority"]:
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1]
                    if not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    return self._json(app.priority(ticker), HTTPStatus.ACCEPTED)
                if method == "POST" and parts == ["", "api", "fetching"]:
                    return self._json(app.set_fetching(body))
                if method == "POST" and parts == ["", "api", "derive"]:
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1]
                    if not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    try:
                        return self._json(app.start_derive(ticker), HTTPStatus.ACCEPTED)
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
                if method == "POST" and parts in (["", "api", "scan"], ["", "api", "fetch"]):
                    ticker = parse_qs(urlparse(self.path).query).get("ticker", [None])[-1] if parts[2] == "fetch" else None
                    if parts[2] == "fetch" and not ticker:
                        return self._json({"error": "ticker is required"}, HTTPStatus.BAD_REQUEST)
                    try:
                        return self._json(app.start_scan(ticker), HTTPStatus.ACCEPTED)
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
                if parts[:3] == ["", "api", "trading"]:  # paper only: live trading is off
                    if method == "POST" and parts[3:] == ["orders"]:
                        return self._json(app.trade_place(body), HTTPStatus.CREATED)
                    if method == "DELETE" and len(parts) == 5 and parts[3] == "orders":
                        return self._json(app.trade_cancel(int(parts[4])))
                    if method == "POST" and parts[3:] == ["reset"]:
                        return self._json(app.trade_reset(body))
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                if parts[:3] != ["", "api", "layouts"]:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                if method == "POST" and len(parts) == 3:
                    return self._json(app.create_layout(body), HTTPStatus.CREATED)
                if len(parts) == 4:
                    layout_id = int(parts[3])
                    if method == "PUT":
                        return self._json(app.update_layout(layout_id, body))
                    if method == "DELETE":
                        return self._json(app.delete_layout(layout_id))
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except LayoutLimit as e:
                return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            except LookupError as e:
                return self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            self._write("POST")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

        def do_GET(self) -> None:
            if not self._local():
                return
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/layouts":
                    return self._json(app.layouts())
                if url.path == "/api/watchlists":
                    return self._json(app.watchlists())
                if url.path.startswith("/api/layouts/"):
                    return self._json(app.layout(int(url.path.rsplit("/", 1)[1])))
                if url.path == "/api/symbols":
                    return self._json(app.symbols())
                if url.path == "/api/meta":
                    return self._json(app.meta(q["source"], q["ticker"], q.get("adjust"), q.get("auto") == "1"))
                if url.path == "/api/bars":
                    with app._view(q):
                        return self._json(app.bars(q))
                if url.path == "/api/stream":
                    with app._view(q):
                        return self._stream(q)
                if url.path == "/api/profile":
                    with app._view(q):
                        return self._json(app.profile(q))
                if url.path == "/api/info":
                    return self._json(app.info(q["source"], q["ticker"], q.get("adjust")))
                if url.path == "/api/deps":
                    return self._json(app.deps())
                if url.path == "/api/directory":
                    return self._json(app.directory())
                if url.path == "/api/announcements":
                    return self._json(app.announcements())
                if url.path == "/api/gov":
                    return self._json(app.gov())
                if url.path == "/api/markets":
                    return self._json(app.markets())
                if url.path == "/api/calendar":
                    return self._json(app.calendar(q))
                if url.path == "/api/inventory":
                    return self._json(app.inventory(q["ticker"]))
                if url.path == "/api/pruned":
                    return self._json(app.pruned(q["ticker"], q.get("source")))
                if url.path == "/api/future":
                    with app._view(q):
                        return self._json(app.future(q))
                if url.path == "/api/range":
                    with app._view(q):
                        return self._json(app.range_stats(q))
                if url.path == "/api/schemes":
                    return self._json(app.schemes())
                if url.path == "/api/trading":
                    return self._json(app.trading())
                if url.path == "/api/scheme":
                    with app._view(q):
                        return self._json(app.scheme(q))
                if url.path == "/api/replay":
                    with app._view(q):
                        return self._json(app.replay(q))
                if url.path == "/api/icon":
                    icon = app.icon(q["ticker"])
                    if icon is None:
                        return self._send(HTTPStatus.NOT_FOUND, b"no icon", "text/plain")
                    mime, data = icon
                    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                        return self._send(HTTPStatus.NOT_FOUND, b"no icon", "text/plain")  # no SVG: it can carry script
                    return self._send(HTTPStatus.OK, data, mime)
                if url.path == "/api/activity":
                    # `open` writes (the live recorder streams it): only from this app's own pages
                    mine = self.headers.get("X-Massive-Charts") == "1"
                    return self._json(app.activity({**q, "open": q.get("open") if mine else None}))
                if url.path == "/api/exchanges":
                    return self._json(app.exchanges())
                if url.path == "/api/audits":
                    return self._json(app.audit_summary(q.get("ticker")))
                if url.path == "/api/datasets":
                    return self._json(app.datasets())
                if url.path == "/api/dataset/series":
                    return self._json(app.dataset_series(q["name"]))
                if url.path == "/api/dataset/values":
                    return self._json(app.dataset_values(q["name"], q["series"]))
            except (KeyError, ValueError) as e:
                return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            except LookupError as e:
                return self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Busy as e:
                return self._json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return self._static(url.path)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    return Handler


def serve(db_path: Path, port: int = 8765, open_browser: bool = True, priority: list[str] | None = None,
          settings: Settings | None = None, reload_settings: Callable[[], Settings] | None = None) -> None:
    store = Store(db_path, check_same_thread=False)
    app = ChartApp(store, priority, settings, reload_settings=reload_settings)
    from . import world
    threading.Thread(target=world.warm, daemon=True).start()  # world calendars take a few seconds
    port_ref = [port]
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, port_ref))  # port taken: pick a free one
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"chart app running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
