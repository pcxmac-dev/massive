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
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pandas as pd

from .bars import (CALENDAR, COLUMNS, MERGED, MODEL_SOURCES, Series, _aggregate, _regular_close, calendar_key,
                   decimals, epoch_ms, frame_from_bars, merge_frames, resample, session_hours, to_records, utc_days)

# Every level the chart moves through, finest first.
LEVELS = (("second", 1), ("second", 30), ("minute", 1), ("minute", 5), ("minute", 30), ("hour", 1), ("hour", 2),
          ("hour", 4), ("day", 1), ("week", 1), ("month", 1), ("year", 1))
UNIT_MS = {"second": 1_000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 604_800_000,
           "month": 2_629_746_000, "year": 31_556_952_000}
BASE = {"second": "second", "minute": "minute", "hour": "minute"}  # the stored bars a level is built from
PAGE = 2000  # bars per page, about three screens of candles at their narrowest
LEVELS_TTL_S = 60.0


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
                rows = sum(r["rows"] for r in self.store.frame_inventory(feed, ticker)
                           if r["timespan"] == base and r["multiplier"] == 1)
                best = max(best, rows)
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
        feeds = self.feeds(source, ticker)
        out = (self._calendar_page if period in CALENDAR else self._intraday_page)(
            s, feeds, period, span, session, to_ms, after_ms, since_ms, max(1, limit))
        out["level"] = {"type": period, "span": span, "label": level_label(period, span)}
        return out

    def _intraday_page(self, s, feeds, period, span, session, to_ms, after_ms, since_ms, limit) -> dict:
        base = BASE[period]
        step, base_ms = UNIT_MS[period] * span, UNIT_MS[base]
        with self.engine.lock:
            edges = [e for e in (self.store.bar_edges(f, s.ticker, base) for f in feeds) if e]
        if not edges:
            return {"bars": [], "more": False, "newer": False}
        first, end = min(e[0] for e in edges), max(e[1] for e in edges) + base_ms
        # Wall-clock time a bar takes, sessions and nights included: the first window's width
        factor = 1.0 if s.around_the_clock else (5.5 if session == "regular" else 2.2)
        if since_ms is not None and end - since_ms > limit * step * factor:
            after_ms, since_ms = since_ms - 1, None  # far behind the newest: a page of newer bars, not years of them
        if since_ms is not None:  # the live tail: from the bar the chart has last, whole
            bars = self._built(s, feeds, base, period, span, session, since_ms - 2 * step, end)
            return {"bars": to_records(bars[epoch_ms(bars.index) >= since_ms]), "more": False, "newer": False}
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
            return {"bars": to_records(bars.head(limit)), "more": True, "newer": not complete or len(bars) > limit}
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
        return {"bars": to_records(bars.tail(limit)), "more": more or len(bars) > limit,
                "newer": to_ms is not None and to_ms < end}

    def _built(self, s, feeds, base, period, span, session, lo, hi) -> pd.DataFrame:
        """The bars of [lo, hi) at the level, built from the feeds' base bars in it (best feed
        first per bar), with the feeds' own bars at the level in place of those built here."""
        with self.engine.lock:
            parts = [frame_from_bars(df) for df in
                     (self.store.bars_window([f], s.ticker, base, int(lo), int(hi)) for f in feeds) if len(df)]
        if not parts:
            return pd.DataFrame(columns=[*COLUMNS, "derived"], index=pd.DatetimeIndex([], tz="America/New_York"))
        merged = merge_frames(parts) if len(parts) > 1 else parts[0]
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
                df = self.store.load_bars([s.ticker], period, span, start=start, end=end, source=f)
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
                     (self.store.load_bars([s.ticker], "day", 1, source=f) for f in feeds) if len(df)]
        if not parts:
            return pd.DataFrame(columns=[*COLUMNS, "derived"], index=pd.DatetimeIndex([], tz="America/New_York"))
        daily = merge_frames(parts) if len(parts) > 1 else parts[0]
        daily.index = calendar_key(daily.index, "day", utc=utc_days(s), session_hours=session_hours(s))
        daily = daily[~daily.index.duplicated(keep="first")]
        if s.asset in ("stock", "index"):  # no session on weekends and holidays: stray feed rows
            daily = daily[[_regular_close(d.date()) is not None for d in daily.index]]
        return daily

    def _calendar_page(self, s, feeds, period, span, session, to_ms, after_ms, since_ms, limit) -> dict:
        daily = self._days(s, feeds)
        bars = daily if period == "day" else _aggregate(
            daily.groupby(calendar_key(daily.index, period, utc=utc_days(s)))).assign(derived=True)
        ts = epoch_ms(bars.index)
        if since_ms is not None:
            return {"bars": to_records(bars[ts >= since_ms]), "more": False, "newer": False}
        if after_ms is not None:
            newer = bars[ts > after_ms]
            return {"bars": to_records(newer.head(limit)), "more": True, "newer": len(newer) > limit}
        older = bars[ts < to_ms] if to_ms is not None else bars
        return {"bars": to_records(older.tail(limit)), "more": len(older) > limit,
                "newer": to_ms is not None and bool((ts >= to_ms).any())}

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
                         (self.store.bars_window([f], s.ticker, base, lo, hi) for f in feeds) if len(df)]
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
