"""Bars at any level for the window of time the chart shows, read from the store as it asks:
the auto-ranging chart.

The chart no longer offers intervals to pick: it shows a symbol's whole history at the finest
level whose candles fit the screen, and as it zooms in moves to finer levels (days, hours,
minutes, seconds) while the data has them; where it has none finer, candles just grow. Each
request is one page of one level, read from the window of base bars it needs (seconds for the
second levels, minutes up to 4 hours, the days for days, weeks, months and years), never a
whole history: twenty years of minutes took a minute to load and gigabytes to hold.

Bars are built exactly as the interval charts built them (bars.resample: the session filter,
stock hours from the open, UTC days for crypto and forex, CME sessions for futures' days), and a
feed's own bars at the level replace the ones built here at the same times (sent data is prime).
Merged charts take each base bar from the best-ranked feed holding it (bars.merge_frames).
Continuous futures (contracts stitched at rolls) and the composite model keep the series path.

Chart types and models are drawn from the same pages, as the series path draws them: Heikin Ashi
and the 1st and 2nd derivatives from the page and the bars beside it that they need, Fourier from
the page's whole segments (sessions), the integral at days and longer (each value sums the history
before it; finer, it keeps the series path). Price-based types (Renko, Kagi, line break, point &
figure, range) follow price, not time: they are built whole, once, from one level (brick_level),
and paged. Box plots keep the series path.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from datetime import datetime, timezone

import pandas as pd

from ..calc.derivatives import Model, apply_model, segments
from .bars import (CALENDAR, COLUMNS, MERGED, MODEL_SOURCES, PERIOD_SECONDS, PRICE_BASED, TIME_TYPES, Series, Style,
                   _aggregate, _regular_close, _SizedCache, calendar_key, decimals, epoch_ms, frame_from_bars,
                   merge_frames, model_info, resample, session_hours, style_used, to_records, transformed, utc_days)

# Every level the chart moves through, finest first.
LEVELS = (("second", 1), ("second", 30), ("minute", 1), ("minute", 5), ("minute", 30), ("hour", 1), ("hour", 2),
          ("hour", 4), ("day", 1), ("week", 1), ("month", 1), ("year", 1))
UNIT_MS = {"second": 1_000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 604_800_000,
           "month": 2_629_746_000, "year": 31_556_952_000}
BASE = {"second": "second", "minute": "minute", "hour": "minute"}  # the stored bars a level is built from
PAGE = 2000  # bars per page, about three screens of candles at their narrowest
LEVELS_TTL_S = 60.0
# Price-based charts (Renko, Kagi, line break, point & figure, range) are built whole: each brick
# depends on every one before it. From the finest level whose whole history is at most BRICK_ROWS
# bars, read from at most BRICK_READ base bars, reaching back to within COVER_MS of the series'
# first bar (days for a long history: twenty years of minutes is millions of bars to walk).
BRICK_ROWS = 100_000
BRICK_READ = 400_000
COVER_MS = 7 * 86_400_000
BRICK_TTL_S = 120.0  # a build is kept this long (a bar corrected inside the history shows after it)
BRICK_CACHE_BYTES = 300_000_000
HA_WARMUP = 64  # Heikin Ashi's open halves its first bar's weight each bar: gone below a float's precision
# Fourier fits each segment (a session: up to the next gap) whole; where a market runs without
# gaps (crypto, or gaps not treated as breaks) it is fitted over this many bars either side.
FOURIER_CONTEXT = 100_000
MODEL_SKIP_PAGES = 10  # pages of bars all too short for a model's window passed over to reach its values
HALF_HOUR_MS = 1_800_000  # the hour levels and 30 minutes are built from 30-minute bins (_half_hours)
ROLLUP_TTL_S = 300.0  # a month of them is read again after this: a correction or backfill shows within it
ROLLUP_CACHE_BYTES = 200_000_000  # twenty years of a stock's bins is about 15 MB


# The view a request reads (server.ChartApp._view): filtered, bad data left out (the pruned list,
# baddata.py), unless it asks for all of it (unfiltered). Per request thread; caches key on it.
UNFILTERED = contextvars.ContextVar("unfiltered", default=False)


def _filtered() -> bool:
    return not UNFILTERED.get()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=[*COLUMNS, "derived"], index=pd.DatetimeIndex([], tz="America/New_York"))


def _paged(bars: pd.DataFrame, to_ms, after_ms, since_ms, limit) -> dict:
    """A page of bars held whole: the newest `limit` before `to_ms` (or of all), the oldest after
    `after_ms`, or all from `since_ms`."""
    ts = epoch_ms(bars.index)
    if since_ms is not None:
        return {"frame": bars[ts >= since_ms], "more": False, "newer": False}
    if after_ms is not None:
        newer = bars[ts > after_ms]
        return {"frame": newer.head(limit), "more": True, "newer": len(newer) > limit}
    older = bars[ts < to_ms] if to_ms is not None else bars
    return {"frame": older.tail(limit), "more": len(older) > limit,
            "newer": to_ms is not None and bool((ts >= to_ms).any())}


def per_day(asset: str, period: str, span: int, session: str) -> float:
    """Bars of a level in an average calendar day: the minutes a market trades in a day, the
    share of days it trades, the level's length (days and longer: one a trading day, a week ...)."""
    trading_days = {"stock": 252 / 365, "index": 252 / 365, "crypto": 1.0}.get(asset, 5 / 7)
    if period in CALENDAR:
        return trading_days * {"day": 1, "week": 1 / 5, "month": 1 / 21, "year": 1 / 252}[period] / span \
            if asset in ("stock", "index") else {"day": 1, "week": 1 / 7, "month": 1 / 30.44, "year": 1 / 365.25}[period] / span
    minutes = {"stock": 390 if session == "regular" else 960, "index": 390}.get(asset, 1440 if asset == "crypto" else 1380)
    return trading_days * minutes * 60_000 / (UNIT_MS[period] * span)


def estimated_rows(level: dict, session: str = "extended") -> int:
    """About how many bars a level holds from its first to its last (from its bars a day)."""
    return max(1, round(level["perDay"][session] * (level["last"] - level["first"]) / 86_400_000))


def level_label(period: str, span: int) -> str:
    names = {"second": "sec", "minute": "min", "hour": "hour", "day": "day", "week": "week", "month": "month",
             "year": "year"}
    return f"{span} {names[period]}"


class WindowReader:
    """Pages of bars at a level, for a window of time, from the store (see the module docstring).
    Shares the chart engine's store and lock."""

    def __init__(self, engine):
        self.engine = engine
        self._levels: dict[tuple, tuple[float, list]] = {}
        self._bricks = _SizedCache(BRICK_CACHE_BYTES, 16)
        self._bricks_lock = threading.Lock()
        self._rollup = _SizedCache(ROLLUP_CACHE_BYTES, 50_000)  # (feeds, ticker, month) -> (built at, 30-minute bins)
        self._rollup_lock = threading.Lock()

    @property
    def store(self):
        return self.engine.store

    # -- what a symbol is ------------------------------------------------------------------
    def asset(self, ticker: str, source: str | None = None) -> str:
        with self.engine.lock:
            rows = self.store.watchlist(ticker=ticker)
        own = next((w["asset"] for w in rows if w["source"] == source), None)
        return own or (rows[0]["asset"] if rows else "stock")

    def handles(self, source: str, ticker: str) -> bool:
        """Whether this path serves the series (continuous futures and models keep theirs)."""
        return source not in MODEL_SOURCES and self.asset(ticker, None if source == MERGED else source) != "future"

    def feeds(self, source: str, ticker: str) -> list[str]:
        if source != MERGED:
            return [source]
        return [f for f in self.engine.sources_for(ticker) if f not in MODEL_SOURCES]

    def series(self, source: str, ticker: str) -> Series:
        """A series without its bars: what the helpers read (asset, its day zone, its sessions)."""
        return Series(source, ticker.upper(), "minute", pd.DataFrame(columns=COLUMNS), None, 2, 0,
                      self.asset(ticker, None if source == MERGED else source))

    def levels(self, source: str, ticker: str) -> list[dict]:
        """The levels the series can be drawn at: {type, span, first, last (ms), perDay, label}. A
        level is there where its base is (seconds, minutes or days: two index seeks a feed, never
        a count); `perDay` is how many of its bars a calendar day holds in each session, from the
        market's hours (a stock: 390 minutes a regular day, 960 extended, 252 days a year; crypto:
        1,440 every day), for the chart to tell how many candles a span of time would be."""
        key = (source, ticker.upper())
        hit = self._levels.get(key)
        if hit and time.monotonic() - hit[0] < LEVELS_TTL_S:
            return hit[1]
        edges: dict[str, list] = {}
        with self.engine.lock:
            for feed in self.feeds(source, ticker):
                for base in ("second", "minute", "day"):
                    e = self.store.bar_edges(feed, ticker, base)
                    if e:
                        a = edges.setdefault(base, [e[0], e[1]])
                        a[0], a[1] = min(a[0], e[0]), max(a[1], e[1])
        s = self.series(source, ticker)
        out = []
        for ts, m in LEVELS:
            base = edges.get(BASE.get(ts, "day"))
            if base is None:
                continue
            out.append({"type": ts, "span": m, "first": int(base[0]), "last": int(base[1]),
                        "perDay": {session: per_day(s.asset, ts, m, session) for session in ("regular", "extended")},
                        "label": level_label(ts, m)})
        self._levels[key] = (time.monotonic(), out)
        return out

    def base_rows(self, source: str, ticker: str, base: str) -> int:
        """The bars a series holds at its base level (the side panel's count), exact: the feed
        holding most of them (a range count each, kept a minute)."""
        key = ("rows", source, ticker.upper(), base)
        hit = self._levels.get(key)
        if hit and time.monotonic() - hit[0] < LEVELS_TTL_S:
            return hit[1]
        best = 0
        with self.engine.lock:
            for feed in self.feeds(source, ticker):
                best = max(best, self.store.count_bars(feed, ticker, base))  # that level only
        self._levels[key] = (time.monotonic(), best)
        return best

    # -- pages -------------------------------------------------------------------------------
    def page(self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
             to_ms: int | None = None, after_ms: int | None = None, since_ms: int | None = None,
             limit: int = PAGE) -> dict:
        """One page of bars at (period, span): the newest `limit` before `to_ms` (or the newest of
        all), the oldest `limit` after `after_ms`, or every bar from `since_ms` (the live tail).
        {"bars", "more": older ones exist, "newer": newer ones exist, "level"}."""
        s = self.series(source, ticker)
        session = s.session_for(session)
        out = self._frame_page(s, self.feeds(source, ticker), period, span, session, to_ms, after_ms, since_ms,
                               max(1, limit))
        out["bars"] = to_records(out.pop("frame"))
        out["level"] = {"type": period, "span": span, "label": level_label(period, span)}
        return out

    def _frame_page(self, s, feeds, period, span, session, to_ms, after_ms, since_ms, limit) -> dict:
        """page() with the bars as a frame ("frame"), for what is drawn from them."""
        return (self._calendar_page if period in CALENDAR else self._intraday_page)(
            s, feeds, period, span, session, to_ms, after_ms, since_ms, limit)

    def _intraday_page(self, s, feeds, period, span, session, to_ms, after_ms, since_ms, limit) -> dict:
        base = BASE[period]
        step, base_ms = UNIT_MS[period] * span, UNIT_MS[base]
        with self.engine.lock:
            edges = [e for e in (self.store.bar_edges(f, s.ticker, base) for f in feeds) if e]
        if not edges:
            return {"frame": _empty(), "more": False, "newer": False}
        first, end = min(e[0] for e in edges), max(e[1] for e in edges) + base_ms
        # Wall-clock time a bar takes, sessions and nights included: the first window's width
        factor = 1.0 if s.around_the_clock else (5.5 if session == "regular" else 2.2)
        if since_ms is not None and end - since_ms > limit * step * factor:
            after_ms, since_ms = since_ms - 1, None  # far behind the newest: a page of newer bars, not years of them
        if since_ms is not None:  # the live tail: from the bar the chart has last, whole
            bars = self._built(s, feeds, base, period, span, session, since_ms - 2 * step, end)
            return {"frame": bars[epoch_ms(bars.index) >= since_ms], "more": False, "newer": False}
        width = limit * step * factor
        if after_ms is not None:  # newer: the bars after the newest the chart has
            while True:
                hi = min(end, after_ms + width)
                bars = self._built(s, feeds, base, period, span, session, after_ms, hi)
                bars = bars[epoch_ms(bars.index) > after_ms]
                if len(bars) > limit or hi >= end:
                    break
                width *= 3
            complete = hi >= end
            if not complete and len(bars):
                bars = bars.iloc[:-1]  # the newest may be cut by the window: the next page reads it whole
            return {"frame": bars.head(limit), "more": True, "newer": not complete or len(bars) > limit}
        hi = min(to_ms, end) if to_ms is not None else end
        while True:
            lo = max(first, hi - width)
            bars = self._built(s, feeds, base, period, span, session, lo, hi)
            if len(bars) > limit or lo <= first:
                break
            width *= 3
        more = lo > first
        if more and len(bars):
            bars = bars.iloc[1:]  # the oldest may be cut by the window: the next page reads it whole
        return {"frame": bars.tail(limit), "more": more or len(bars) > limit,
                "newer": to_ms is not None and to_ms < end}

    def _merged(self, s, feeds, base, lo, hi) -> pd.DataFrame | None:
        """The feeds' base bars in [lo, hi), best feed first per bar."""
        with self.engine.lock:
            parts = [frame_from_bars(df) for df in
                     (self.store.bars_window([f], s.ticker, base, int(lo), int(hi), filtered=_filtered()) for f in feeds)
                     if len(df)]
        if not parts:
            return None
        return merge_frames(parts) if len(parts) > 1 else parts[0]

    def _half_hours(self, s, feeds, lo, hi) -> pd.DataFrame | None:
        """30-minute bins of the merged minutes (all hours) for [lo, hi), widened to whole bins: the
        hour levels and 30 minutes are built from them. Every edge of those is on a half hour of New
        York wall-clock time (stock hours from 09:30) and the regular session opens and closes on
        one, so they are the bars the minutes give, from a 30th of the rows. Read a UTC month at a
        time and kept ROLLUP_TTL_S (a correction or backfill shows within it), the month holding the
        newest minute never kept: zooming between days, hours and 30 minutes reads each minute once."""
        with self.engine.lock:
            edges = [e for e in (self.store.bar_edges(f, s.ticker, "minute") for f in feeds) if e]
        if not edges:
            return None
        newest = max(e[1] for e in edges)
        start = int(lo) // HALF_HOUR_MS * HALF_HOUR_MS
        months = [pd.Timestamp(start, unit="ms").to_period("M").start_time]  # UTC month starts, naive
        while months[-1].value // 1_000_000 < hi:
            months.append(months[-1] + pd.offsets.MonthBegin())
        bounds = [int(m.value // 1_000_000) for m in months]
        parts = []
        for m0, m1 in zip(bounds, bounds[1:]):
            key = (tuple(feeds), s.ticker, m0, _filtered())
            settled = m1 <= newest  # wholly before the newest minute: only a correction or backfill changes it
            with self._rollup_lock:
                hit = self._rollup.get(key) if settled else None
            if hit is not None and time.monotonic() - hit[0] < ROLLUP_TTL_S:
                parts.append(hit[1])
                continue
            minutes = self._merged(s, feeds, "minute", m0, m1)
            bins = _empty() if minutes is None else resample(minutes, "minute", 30, "extended")
            if settled:
                with self._rollup_lock:
                    self._rollup[key] = (time.monotonic(), bins)
            parts.append(bins)
        parts = [p for p in parts if len(p)]
        if not parts:
            return None
        bins = pd.concat(parts) if len(parts) > 1 else parts[0]
        t = epoch_ms(bins.index)
        return bins[(t >= start) & (t < hi)]

    def _built(self, s, feeds, base, period, span, session, lo, hi) -> pd.DataFrame:
        """The bars of [lo, hi) at the level, built from the feeds' base bars in it (best feed
        first per bar; hours and 30 minutes from their 30-minute bins, _half_hours), with the
        feeds' own bars at the level in place of those built here."""
        rolled = base == "minute" and (period == "hour" or (period == "minute" and span == 30))
        merged = self._half_hours(s, feeds, lo, hi) if rolled else self._merged(s, feeds, base, lo, hi)
        if merged is None or not len(merged):
            return _empty()
        own = period == base and span == 1
        bars = resample(merged, period, span, session, align_hours_to_open=s.asset in ("stock", "index"),
                        utc_days=utc_days(s), session_hours=session_hours(s))
        if own:
            return bars
        bars = bars.assign(derived=True)  # built here from finer bars ...
        sent = self._sent(s, feeds, period, span, lo, hi)
        common = bars.index.intersection(sent.index) if sent is not None else []
        if len(common):
            bars.loc[common, COLUMNS] = sent.loc[common, COLUMNS].to_numpy()
            bars.loc[common, "derived"] = False  # ... except where a feed sent this level
        return bars

    def _sent(self, s, feeds, period, span, lo, hi) -> pd.DataFrame | None:
        """The feeds' own bars at the level in [lo, hi), best feed first per time."""
        start = datetime.fromtimestamp(lo / 1000, timezone.utc).date()
        end = datetime.fromtimestamp(hi / 1000, timezone.utc).date()
        parts = []
        with self.engine.lock:
            for f in feeds:
                df = self.store.load_bars([s.ticker], period, span, start=start, end=end, source=f, filtered=_filtered())
                if len(df):
                    df = frame_from_bars(df)
                    df = df[~df["derived"]]
                    t = epoch_ms(df.index)
                    parts.append(df[(t >= lo) & (t < hi)])
        parts = [p for p in parts if len(p)]
        return merge_frames(parts) if parts else None

    def _days(self, s, feeds) -> pd.DataFrame:
        """Every day of the series: the feeds' official daily bars first, the days built here where
        none was sent (one feed per day, best first), dated as each market dates its days."""
        with self.engine.lock:
            parts = [frame_from_bars(df) for df in
                     (self.store.load_bars([s.ticker], "day", 1, source=f, filtered=_filtered()) for f in feeds)
                     if len(df)]
        if not parts:
            return _empty()
        daily = merge_frames(parts) if len(parts) > 1 else parts[0]
        daily.index = calendar_key(daily.index, "day", utc=utc_days(s), session_hours=session_hours(s))
        daily = daily[~daily.index.duplicated(keep="first")]
        if s.asset in ("stock", "index"):  # no session on weekends and holidays: stray feed rows
            daily = daily[[_regular_close(d.date()) is not None for d in daily.index]]
        return daily

    def _calendar_bars(self, s, feeds, period) -> pd.DataFrame:
        """Every bar of a calendar level (days, weeks, months, years): few, read whole."""
        daily = self._days(s, feeds)
        return daily if period == "day" else _aggregate(
            daily.groupby(calendar_key(daily.index, period, utc=utc_days(s)))).assign(derived=True)

    def _calendar_page(self, s, feeds, period, span, session, to_ms, after_ms, since_ms, limit) -> dict:
        return _paged(self._calendar_bars(s, feeds, period), to_ms, after_ms, since_ms, limit)

    def _level_bars(self, s, feeds, period, span, session) -> pd.DataFrame:
        """Every bar of a level, from its base's first bar to its last."""
        if period in CALENDAR:
            return self._calendar_bars(s, feeds, period)
        base = BASE[period]
        with self.engine.lock:
            edges = [e for e in (self.store.bar_edges(f, s.ticker, base) for f in feeds) if e]
        if not edges:
            return _empty()
        return self._built(s, feeds, base, period, span, session, min(e[0] for e in edges),
                           max(e[1] for e in edges) + UNIT_MS[base])

    # -- chart types and models ----------------------------------------------------------------
    def styled_page(self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
                    style: Style = Style(), model: Model = Model(), to_ms: int | None = None,
                    after_ms: int | None = None, since_ms: int | None = None, limit: int = PAGE) -> dict:
        """A page as page() gives it, drawn as `style` and in `model`'s units, the same as the
        series path draws them (BarEngine.bars): price-based types from their own level, built
        whole (bricks); Heikin Ashi and the models from the page's bars and the bars beside them
        that they need (a model's window either side; Fourier to the ends of the page's segments)."""
        model.validate()
        if style.type in PRICE_BASED:
            if not model.is_price:
                raise ValueError("derivatives and integrals apply to time-based chart types")
            return self.bricks(source, ticker, session, style, to_ms=to_ms, after_ms=after_ms, since_ms=since_ms,
                               limit=limit)
        if style.type not in TIME_TYPES and style.type != "heikin_ashi":
            raise ValueError(f"{style.type} is not drawn from a window")
        if not model.is_price and style.type not in TIME_TYPES:
            raise ValueError("derivatives and integrals apply to time-based chart types")
        if model.kind == "integral" and period not in CALENDAR:
            raise ValueError("the integral sums the whole history: drawn from a window at days and longer only")
        s = self.series(source, ticker)
        session = s.session_for(session)
        feeds = self.feeds(source, ticker)
        out = self._frame_page(s, feeds, period, span, session, to_ms, after_ms, since_ms, max(1, limit))
        bars = out.pop("frame")
        used = {"type": style.type}
        if style.type == "heikin_ashi":
            bars = self._heikin_ashi(s, feeds, period, span, session, bars)
        elif not model.is_price:
            values, fitted = self._modelled(s, feeds, period, span, session, bars, model)
            for _ in range(MODEL_SKIP_PAGES):  # every bar in stretches too short for the window: the next page
                onward = out["newer"] if after_ms is not None else out["more"]
                if len(values) or not len(bars) or since_ms is not None or not onward:
                    break
                edge = int(epoch_ms(bars.index[-1:] if after_ms is not None else bars.index[:1])[0])
                out = self._frame_page(s, feeds, period, span, session, None if after_ms is not None else edge,
                                       edge if after_ms is not None else None, None, max(1, limit))
                bars = out.pop("frame")
                values, fitted = self._modelled(s, feeds, period, span, session, bars, model)
            out["model"] = model_info(model, values, len(bars) - len(values))
            used.update(model=model.kind, window=model.window, basis=model.basis, per=model.per)
            if fitted:
                out["approximate"] = (f"Fourier fitted over the {fitted:,} bars around this window: the market runs "
                                      "without a break that long, so a fit over its whole history may differ slightly")
            bars = values
        out.update(bars=to_records(bars), style=used,
                   level={"type": period, "span": span, "label": level_label(period, span)})
        return out

    def _heikin_ashi(self, s, feeds, period, span, session, bars) -> pd.DataFrame:
        """Heikin Ashi of the page's bars: each open carries the bar before it, so from HA_WARMUP
        bars before the page (the whole level for days and longer)."""
        from . import charttypes as ct

        if not len(bars):
            return bars
        if period in CALENDAR:
            return ct.heikin_ashi(self._calendar_bars(s, feeds, period)).loc[bars.index]
        first = int(epoch_ms(bars.index[:1])[0])
        before = self._frame_page(s, feeds, period, span, session, first, None, None, HA_WARMUP)["frame"]
        return ct.heikin_ashi(pd.concat([before, bars])).iloc[len(before):]

    def _modelled(self, s, feeds, period, span, session, bars, model: Model) -> tuple[pd.DataFrame, int]:
        """The model's values at `bars` (those it gives: none in stretches shorter than its window)
        as from the whole history, and 0; or, for a Fourier fit cut short in a market without gaps,
        the number of bars it was fitted over."""
        step_s = PERIOD_SECONDS[period] * span
        if not len(bars):
            return apply_model(bars, model, step_s), 0
        if period in CALENDAR:  # few: the whole level
            values = apply_model(self._calendar_bars(s, feeds, period), model, step_s)
            return values[values.index.isin(bars.index)], 0
        first, last = (int(t) for t in epoch_ms(bars.index[[0, -1]]))
        before, cut_before = self._beside(s, feeds, period, span, session, first, True, model)
        after, cut_after = self._beside(s, feeds, period, span, session, last, False, model)
        held = pd.concat([before, bars, after])
        values = apply_model(held, model, step_s)
        return values[values.index.isin(bars.index)], (len(held) if cut_before or cut_after else 0)

    def _beside(self, s, feeds, period, span, session, edge_ms, older, model: Model) -> tuple[pd.DataFrame, bool]:
        """The bars beside a page that its model values need (older: before `edge_ms`, else after):
        a derivative's window; for Fourier the rest of the edge bar's segment, up to the next gap
        (a session), at most FOURIER_CONTEXT bars (then True: the fit is cut there)."""
        def read(n):
            p = self._frame_page(s, feeds, period, span, session, edge_ms if older else None,
                                 None if older else edge_ms, None, n)
            return p["frame"], not (p["more"] if older else p["newer"])

        if model.kind != "fourier":
            return read(model.window)[0], False
        step_s = PERIOD_SECONDS[period] * span
        edge = pd.DatetimeIndex([pd.Timestamp(edge_ms, unit="ms", tz="UTC")]).tz_convert("America/New_York")
        n = min(FOURIER_CONTEXT, max(model.window, 2000))
        while True:
            bars, exhausted = read(n)
            if model.reset_gaps and len(bars):
                idx = bars.index.append(edge) if older else edge.append(bars.index)
                seg = segments(idx, step_s, True)
                if seg[0] != seg[-1]:  # a gap: the segment ends there
                    own = seg[-1] if older else seg[0]
                    return bars[(seg[:-1] if older else seg[1:]) == own], False
            if exhausted:
                return bars, False
            if n >= FOURIER_CONTEXT:
                return bars, True
            n = min(FOURIER_CONTEXT, n * 4)

    # -- price-based chart types ------------------------------------------------------------------
    def brick_level(self, source: str, ticker: str, session: str = "regular") -> dict | None:
        """The level a price-based chart is built from: the finest whose whole history is at most
        BRICK_ROWS bars, read from at most BRICK_READ base bars, and reaches back to the series'
        first (see BRICK_ROWS); the coarsest there is otherwise."""
        levels = self.levels(source, ticker)
        if not levels:
            return None
        session = self.series(source, ticker).session_for(session)
        oldest = min(lv["first"] for lv in levels)
        by_type = {(lv["type"], lv["span"]): lv for lv in levels}
        for lv in levels:
            if lv["first"] > oldest + COVER_MS:
                continue  # its data starts later: bricks from it would start there
            if lv["type"] in CALENDAR:
                if estimated_rows(lv, session) <= BRICK_ROWS:
                    return lv
                continue
            base = BASE[lv["type"]]
            if estimated_rows(by_type[(base, 1)], session) > 10 * BRICK_READ:
                continue  # far too many to count
            read = self.base_rows(source, ticker, base)
            if read <= BRICK_READ and min(read, estimated_rows(lv, session)) <= BRICK_ROWS:
                return lv
        return levels[-1]

    def bricks(self, source: str, ticker: str, session: str, style: Style, to_ms: int | None = None,
               after_ms: int | None = None, since_ms: int | None = None, limit: int = PAGE) -> dict:
        """A page of a price-based chart (Renko, Kagi, line break, point & figure, range), built
        whole from its level (brick_level) as the series path builds it at that interval, and kept
        a while (_built_bricks)."""
        s = self.series(source, ticker)
        session = s.session_for(session)
        lv = self.brick_level(source, ticker, session)
        if lv is None:
            return {"bars": [], "more": False, "newer": False, "style": {"type": style.type}}
        out, used = self._built_bricks(s, source, ticker, lv, session, style, older=to_ms is not None)
        page = _paged(out, to_ms, after_ms, since_ms, max(1, limit))
        page["bars"] = to_records(page.pop("frame"))
        page.update(style=used, total=int(len(out)),
                    level={"type": lv["type"], "span": lv["span"], "label": lv["label"], "fixed": True})
        if style.type == "range":
            page["approximate"] = (f"range bars built from {lv['label']} bars, not ticks: the order of each bar's "
                                   "high and low is assumed")
        return page

    def _built_bricks(self, s, source, ticker, lv, session, style, older) -> tuple[pd.DataFrame, dict]:
        """The chart built whole, kept BRICK_TTL_S: the newest page builds it again sooner when its
        level's base bars reach further (each feed's first and last: two index seeks, not the
        series fingerprint, which counts every row and took over a second while a sync wrote);
        an older page keeps the build the chart is showing (a new bar moves the ATR box of all)."""
        key = (source, s.ticker, lv["type"], lv["span"], session, style, _filtered())
        feeds = self.feeds(source, ticker)
        with self.engine.lock:
            stamp = tuple(self.store.bar_edges(f, s.ticker, BASE.get(lv["type"], "day")) for f in feeds)
        with self._bricks_lock:
            hit = self._bricks.get(key)
        if hit and time.monotonic() - hit[1] < BRICK_TTL_S and (older or hit[0] == stamp):
            return hit[2], hit[3]
        base = self._level_bars(s, feeds, lv["type"], lv["span"], session)
        prices = base[["open", "high", "low", "close"]].to_numpy().ravel()
        used = style_used(style, base, decimals(prices, cap=6, minimum=2) if len(prices) else 2)
        out = transformed(base, used)
        with self._bricks_lock:
            self._bricks[key] = (stamp, time.monotonic(), out, used)
        return out, used

    def range_stats(self, source: str, ticker: str, period: str, span: int, session: str, from_ms: int,
                    to_ms: int, model: Model = Model()) -> dict:
        """The highlight's statistics (bars.summarize_range) from the bars of its own window at the
        level, in the model's units: the same as from the whole history, without loading it."""
        from .bars import summarize_range

        s = self.series(source, ticker)
        session = s.session_for(session)
        feeds = self.feeds(source, ticker)
        lo, hi = sorted((int(from_ms), int(to_ms)))
        if period in CALENDAR:
            bars = self._calendar_bars(s, feeds, period)
        else:
            step = UNIT_MS[period] * span
            bars = self._built(s, feeds, BASE[period], period, span, session, lo - step, hi + step)
        if not model.validate().is_price:
            if model.kind == "integral" and period not in CALENDAR:
                raise ValueError("the integral sums the whole history: drawn from a window at days and longer only")
            ts = epoch_ms(bars.index)
            bars, _ = self._modelled(s, feeds, period, span, session, bars[(ts >= lo) & (ts <= hi)], model)
        return summarize_range(bars, lo, hi, model)

    # -- the virtual level 2: what traded at each price in one bar ------------------------------
    PROFILE_TICK_MS = 86_400_000  # bars up to a day read the feeds' ticks; longer ones their finer bars
    PROFILE_MAX_ROWS = 2000

    def profile(self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
                at_ms: int | None = None, step: float | None = None) -> dict:
        """Volume and trades by price in the bar at `at_ms` (the newest without), as the series
        path gives it (BarEngine.profile), from that bar's window alone: the bar, its day, and the
        trades or finer bars inside it."""
        from ..calc.profile import from_bars, from_trades, nice_step
        from .bars import _money, bar_end, calendar_key

        s = self.series(source, ticker)
        session = s.session_for(session)
        feeds = self.feeds(source, ticker)
        newest = self.page(source, ticker, period, span, session, limit=1)["bars"]
        if not newest:
            return {"ticker": s.ticker, "rows": []}
        at = newest[-1] if at_ms is None or at_ms >= newest[-1]["timestamp"] else \
            (self.page(source, ticker, period, span, session, to_ms=int(at_ms) + 1, limit=1)["bars"] or newest)[-1]
        start = pd.Timestamp(at["timestamp"], unit="ms", tz="UTC").tz_convert("America/New_York")
        lo, hi = at["timestamp"], int(bar_end(start, period, span, utc=utc_days(s)).value // 1_000_000)
        d0 = calendar_key(pd.DatetimeIndex([start]), "day", utc=utc_days(s))[0]
        d0_ms = int(epoch_ms(pd.DatetimeIndex([d0]))[0])
        days = self.page(source, ticker, "day", 1, session, to_ms=d0_ms + 1, limit=1)["bars"]
        day = next(({"from": d0_ms, **{k: float(d[k]) for k in ("open", "high", "low", "close", "volume")}}
                    for d in days if d["timestamp"] == d0_ms), None)
        span_px = float(at["high"] - at["low"])
        if step is not None and step > 0 and span_px / step > self.PROFILE_MAX_ROWS:
            step = nice_step(span_px, self.PROFILE_MAX_ROWS)
        rows, basis, derived = None, None, None
        if hi - lo <= self.PROFILE_TICK_MS:
            for feed in feeds:
                with self.engine.lock:
                    t = self.store.trades_between(feed, s.ticker, lo * 1_000_000, hi * 1_000_000)
                if len(t):
                    rows, summed = from_trades(t["price"].to_numpy(), t["size"].to_numpy(), step)
                    basis = f"{len(t):,} trade{'' if len(t) == 1 else 's'} on {feed}"
                    used = step or (nice_step(float(t["price"].max() - t["price"].min())) if summed else None)
                    derived = f"summed into {_money(used)} price segments" if summed else None
                    step = used
                    break
        if rows is None:
            base = "second" if period == "second" else "minute"
            with self.engine.lock:
                parts = [frame_from_bars(df) for df in
                         (self.store.bars_window([f], s.ticker, base, lo, hi, filtered=_filtered()) for f in feeds)
                         if len(df)]
            sub = merge_frames(parts) if len(parts) > 1 else (parts[0] if parts else
                                                              pd.DataFrame(columns=[*COLUMNS, "derived"]))
            unit = f"1-{base} bars"
            rows, step = from_bars(sub["low"].to_numpy(), sub["high"].to_numpy(), sub["volume"].to_numpy(),
                                   sub["trades"].to_numpy(), step)
            basis = f"{len(sub):,} {unit}"
            derived = (f"estimated from {unit}: each one's volume and trades spread evenly across its range, "
                       f"in {_money(step)} price segments")
        return {
            "ticker": s.ticker, "period": period, "span": span,
            "bar": {"from": lo, "to": hi, **{k: float(at[k]) for k in ("open", "high", "low", "close", "volume")},
                    "trades": float(at["trades"])},
            "day": day, "last": float(newest[-1]["close"]), "newest": at["timestamp"] == newest[-1]["timestamp"],
            "rows": json.loads(rows.to_json(orient="records")) if len(rows) else [],
            "step": step, "basis": basis, "derived": derived,
            "note": "Traded volume by price, not an order book: what changed hands, not what was resting.",
        }

    # -- what the chart asks for when it opens a symbol ---------------------------------------
    def meta(self, source: str, ticker: str) -> dict:
        """What the chart needs to open a symbol, without loading its history: its levels, where
        its data starts and ends, its feeds and its prices' decimals (from its recent bars)."""
        levels = self.levels(source, ticker)
        if not levels:
            raise LookupError(f"no data stored for {source}:{ticker}")
        s = self.series(source, ticker)
        fine = levels[0]
        recent = self.page(source, ticker, fine["type"], fine["span"], "extended", limit=500)["bars"]
        prices = [r[k] for r in recent for k in ("open", "high", "low", "close")]
        coarse = next((lv for lv in levels if lv["type"] == "day"), levels[-1])
        return {
            "source": source, "ticker": s.ticker, "base": fine["type"], "asset": s.asset,
            "aroundTheClock": s.around_the_clock, "feeds": self.feeds(source, ticker), "auto": True,
            "levels": levels, "pricePrecision": decimals(prices, cap=6, minimum=2) if prices else 2,
            "volumePrecision": decimals([r["volume"] for r in recent], cap=4) if recent else 0,
            "first": datetime.fromtimestamp(min(lv["first"] for lv in levels) / 1000, timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(max(lv["last"] for lv in levels) / 1000, timezone.utc).isoformat(),
            "rows": estimated_rows(coarse),
            "periods": [{"type": lv["type"], "span": lv["span"]} for lv in levels],
            "allPeriods": [{"type": lv["type"], "span": lv["span"]} for lv in levels],
        }
