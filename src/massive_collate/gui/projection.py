"""The time axis after the last bar: where every future bar will start.

Future bars are found by building exactly what the bar engine would build if the market traded
in every open minute from now on: the trading calendar (sessions.py) lists the open minutes (or,
for day/week/month charts drawn from official daily bars, the trading days stamped the way the
feed stamps them), and those go through the very same resampling, calendar grouping and
level-of-detail grouping as real data. So the projected axis skips nights, weekends, holidays,
early closes and session breaks exactly as real bars will, at whatever interval and zoom is on
screen, as far ahead as asked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..sessions import ET, MarketCalendar
from .bars import (CALENDAR, COLUMNS, LOD_TYPES, PERIOD_SECONDS, REGULAR_OPEN, TIME_TYPES, Series, _aggregate,
                   _regular_close, calendar_key, regular_session, wall_clock_bins)

PROJECTABLE = set(TIME_TYPES) | {"heikin_ashi"}  # price-based types (Renko...) have no time axis to project
MAX_YEARS = 400  # far enough for any zoom; each request is bounded by `count` anyway


def daily_stamps_utc_midnight(daily: pd.DataFrame | None) -> bool:
    """Whether a feed stamps daily bars at 00:00 UTC (forex) rather than at the session date."""
    if daily is None or daily.empty:
        return False
    utc = daily.index[-20:].tz_convert("UTC")
    local = daily.index[-20:].tz_convert(ET)
    return bool(((utc.hour == 0) & (utc.minute == 0) & (local.hour != 0)).all())


def _daily_frame(stamps: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({c: 1.0 for c in COLUMNS}, index=stamps)


def future_daily_stamps(cal: MarketCalendar, s: Series, product: str | None, after: datetime, until: datetime) -> pd.DatetimeIndex:
    """Stamps of the official daily bars the feed will publish in (after, until]."""
    first = after.astimezone(ET).date() - timedelta(days=2)
    sessions = cal.sessions(s.asset, first, until.astimezone(ET).date(), product)
    if daily_stamps_utc_midnight(s.daily):
        days = set()
        for se in sessions:  # every UTC day the market is open in
            d = se.open.astimezone(timezone.utc).date()
            last = (se.close - timedelta(microseconds=1)).astimezone(timezone.utc).date()
            while d <= last:
                days.add(d)
                d += timedelta(days=1)
        stamps = [datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in sorted(days)]
    else:
        stamps = [datetime(se.day.year, se.day.month, se.day.day, tzinfo=ET) for se in sessions]
    idx = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True)).tz_convert(ET).unique().sort_values()
    return idx[(idx > pd.Timestamp(after)) & (idx <= pd.Timestamp(until))]


def future_bar_starts_reference(cal: MarketCalendar, s: Series, product: str | None, period: str, span: int, session: str,
                      last: pd.Timestamp, until: datetime) -> pd.DatetimeIndex:
    """Start times of the bars after `last` (the last real bar), up to `until`, from every open
    minute. Exact but slow far ahead: used for day/week/month and to check `SessionBins`."""
    if period in CALENDAR and s.daily is not None and not s.daily.empty:
        # As the engine does: official daily bars keyed by calendar day, weeks/months grouped from them.
        daily = _daily_frame(future_daily_stamps(cal, s, product, last.to_pydatetime() - timedelta(days=40), until))
        daily.index = calendar_key(daily.index, "day")
        bars = daily if period == "day" else _aggregate(daily.groupby(calendar_key(daily.index, period)))
        idx = bars.index
    else:
        # Bar starts straight from the open minutes with the engine's own binning (what
        # `resample` keys its groups by; a bin with at least one open minute is a bar).
        step = 1 if period == "second" else 60
        regular_only = s.asset == "stock" and session == "regular"
        minutes = cal.open_minutes(s.asset, last.to_pydatetime(), until, product, step_seconds=step,
                                   extended=not regular_only)
        if session == "regular" and s.asset in ("stock", "index"):
            minutes = regular_session(_daily_frame(minutes)).index  # the engine's own filter
        if period in CALENDAR:  # no official daily bars: calendar groups of the intraday frame
            idx = calendar_key(minutes, period).unique()
        else:
            offset = 570 * 60 if period == "hour" and s.asset in ("stock", "index") else 0
            idx = wall_clock_bins(minutes, period, span, offset).unique()
    return idx[idx > last]



_STEP_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


def _date_of(ns: int):
    return pd.Timestamp(ns, tz="UTC").tz_convert(ET).date()


class SessionBins:
    """Intraday bar starts after `last`, counted per session instead of per minute.

    Inside a session every minute is open, so the bars it holds are exactly the bins from its
    first minute's bin to its last minute's bin (no daylight-saving change falls inside a
    session: clocks change at 02:00 on Sundays, when every market here is shut). A bin shared
    with the previous session (a 4-hour bar across a futures session break) is one bar. The
    j-th future bar is then found by searching cumulative counts: exact, and as fast decades
    ahead as tomorrow.
    """

    def __init__(self, cal: MarketCalendar, s: Series, product: str | None, period: str, span: int,
                 session: str, last: pd.Timestamp):
        self.cal, self.s, self.product, self.period, self.span = cal, s, product, period, span
        self.unit_ns = (1 if period == "second" else 60) * 1_000_000_000
        self.bin_ns = span * _STEP_SECONDS[period] * 1_000_000_000
        self.offset = 570 * 60 if period == "hour" and s.asset in ("stock", "index") else 0
        self.regular = session == "regular" and s.asset in ("stock", "index")
        self.last_ns = int(last.value)
        # The first bar that can follow: the bin after the one holding the last real bar.
        own = wall_clock_bins(pd.DatetimeIndex([last]), period, span, self.offset)[0]
        self.after_ns = int(own.value) + self.bin_ns
        self.first: list[int] = []  # first future bar start (ns) in each session
        self.count: list[int] = []
        self.prev_last_bin: int | None = None
        self.next_day = _date_of(self.last_ns) - timedelta(days=1)
        self.cum = np.zeros(0, dtype=np.int64)

    def _extend(self, days: int) -> None:
        end = self.next_day + timedelta(days=days)
        extended = not (self.s.asset == "stock" and self.regular)
        sessions = self.cal.sessions(self.s.asset, self.next_day, end, self.product, extended=extended)
        self.next_day = end + timedelta(days=1)
        for se in sessions:
            a, b = se.open, se.close
            if self.regular:  # the engine's regular-hours filter
                close = _regular_close(se.day)  # the engine's regular hours: 13:00 on early closes
                if close is None:
                    continue
                a = max(a, datetime.combine(se.day, REGULAR_OPEN, ET))
                b = min(b, datetime.combine(se.day, close, ET))
            a_ns = -(-int(pd.Timestamp(a).value) // self.unit_ns) * self.unit_ns
            b_ns = int(pd.Timestamp(b).value)
            if a_ns >= b_ns:
                continue
            last_unit = (b_ns - 1) // self.unit_ns * self.unit_ns
            edges = wall_clock_bins(pd.DatetimeIndex([a_ns, last_unit]).tz_localize("UTC"),
                                    self.period, self.span, self.offset)
            first_bin, last_bin = int(edges[0].value), int(edges[1].value)
            if self.prev_last_bin is not None and first_bin <= self.prev_last_bin:
                first_bin = self.prev_last_bin + self.bin_ns
            if first_bin <= self.last_ns:  # the session holding the last real bar
                first_bin = max(first_bin, self.after_ns)
            if first_bin > last_bin:
                self.prev_last_bin = max(self.prev_last_bin or last_bin, last_bin)
                continue
            self.first.append(first_bin)
            self.count.append((last_bin - first_bin) // self.bin_ns + 1)
            self.prev_last_bin = last_bin
        self.cum = np.cumsum(np.asarray(self.count, dtype=np.int64))

    def picks(self, j: np.ndarray) -> np.ndarray:
        """Start (epoch ms) of future bars number j (0 = the first bar after the last real one)."""
        if not len(j):
            return np.zeros(0, dtype=np.int64)
        need = int(j.max()) + 1
        days = 14
        horizon_end = _date_of(self.last_ns) + timedelta(days=365 * MAX_YEARS)
        while (not len(self.cum) or self.cum[-1] < need) and self.next_day < horizon_end:
            self._extend(days)
            days = min(days * 2, 3650)
        have = int(self.cum[-1]) if len(self.cum) else 0
        j = j[j < have]
        first = np.asarray(self.first, dtype=np.int64)
        count = np.asarray(self.count, dtype=np.int64)
        k = np.searchsorted(self.cum, j, side="right")
        before = self.cum[k] - count[k]
        return (first[k] + (j - before) * self.bin_ns) // 1_000_000


def _horizon(period: str, span: int, needed: int, asset: str) -> timedelta:
    """A first guess at how much calendar time holds `needed` bars (refined by doubling)."""
    open_fraction = {"stock": 0.45, "index": 0.18, "fx": 0.70, "future": 0.65}.get(asset, 0.2)
    seconds = PERIOD_SECONDS[period] * span * needed / (1.0 if period in CALENDAR else open_fraction)
    return timedelta(seconds=seconds * 1.3 + 7 * 86400)


def project(cal: MarketCalendar, s: Series, product: str | None, period: str, span: int, session: str,
            style_type: str, lod: int, total: int, last: pd.Timestamp, offset: int, count: int,
            cache: dict | None = None) -> list[int]:
    """Epoch-ms start times of future slots offset..offset+count-1 on the chart.

    A slot is one bar of the chart as drawn: with level of detail `lod` > 1 each slot is `lod`
    bars, grouped from the series' first bar exactly as `lod_aggregate` groups real ones, so
    the group holding the last real bar is not a future slot and the next group starts where
    the global bar count reaches a multiple of `lod`.
    """
    if style_type not in PROJECTABLE or total <= 0 or count <= 0:
        return []
    k = lod if (lod > 1 and style_type in LOD_TYPES) else 1
    last_group = (total - 1) // k
    first_base = (last_group + 1 + offset) * k - total  # future bar index of the first wanted slot
    last_base = (last_group + offset + count) * k - total
    key = (id(s), period, span, session, total, int(last.value))
    picks = np.arange(first_base, last_base + 1, k, dtype=np.int64)
    if period not in CALENDAR:
        bins = cache.get(key) if cache is not None else None
        if bins is None:
            bins = SessionBins(cal, s, product, period, span, session, last)
            if cache is not None:
                cache[key] = bins
        return bins.picks(picks).tolist()
    starts = cache.get(key) if cache is not None else None
    if starts is None or len(starts) <= last_base:
        horizon = _horizon(period, span, last_base + 1, s.asset)
        limit = timedelta(days=365 * MAX_YEARS)
        while True:
            starts = future_bar_starts_reference(cal, s, product, period, span, session, last,
                                                 last.to_pydatetime() + horizon)
            if len(starts) > last_base or horizon >= limit:
                break
            horizon = min(limit, horizon * 2)
        if cache is not None:
            cache[key] = starts
    ms = starts.as_unit("ms").asi8
    picks = picks[picks < len(ms)]
    return ms[picks].tolist()
