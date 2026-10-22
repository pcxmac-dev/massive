"""Bars for the chart at any interval, built from the finest data stored.

Values are never interpolated or rounded: open/high/low/close are taken from
the underlying ticks or bars, volume and turnover are summed, and empty
intervals are left out (as TradingView does).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import time
from threading import RLock

import numpy as np
import pandas as pd

from ..storage import Store

ET = "America/New_York"
# Finest first
BASE_KINDS = ("trades", "second", "minute", "hour", "day")
BASE_SECONDS = {"trades": 0, "second": 1, "minute": 60, "hour": 3600, "day": 86400}
INTRADAY = {"second": "s", "minute": "min", "hour": "h"}
CALENDAR = ("day", "week", "month", "year")
PERIOD_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
                  "month": 30 * 86400, "year": 365 * 86400}
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)
COLUMNS = ["open", "high", "low", "close", "volume", "turnover", "trades"]


@dataclass
class Series:
    source: str
    ticker: str
    kind: str  # finest stored data: trades | second | minute | ...
    frame: pd.DataFrame  # indexed by US/Eastern time, COLUMNS
    daily: pd.DataFrame | None  # official daily bars, if stored
    price_precision: int
    volume_precision: int


def decimals(values, cap: int = 6, minimum: int = 0) -> int:
    """Smallest number of decimals that represents every value exactly."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)][:50000]
    for d in range(minimum, cap + 1):
        scaled = v * 10**d
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return d
    return cap


def frame_from_trades(trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.set_index("time")
    return pd.DataFrame({
        "open": df["price"], "high": df["price"], "low": df["price"], "close": df["price"],
        "volume": df["size"], "turnover": df["price"] * df["size"], "trades": 1,
    })


def frame_from_bars(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.set_index("time")
    px = df["vwap"].fillna(df["close"]) if "vwap" in df else df["close"]
    return pd.DataFrame({
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "volume": df["volume"].fillna(0), "turnover": (px * df["volume"]).fillna(0),
        "trades": df["transactions"].fillna(0) if "transactions" in df else 0,
    })


def regular_session(frame: pd.DataFrame) -> pd.DataFrame:
    t = frame.index.time
    return frame[(t >= REGULAR_OPEN) & (t < REGULAR_CLOSE)]


def _aggregate(groups) -> pd.DataFrame:
    out = groups.agg({"open": "first", "high": "max", "low": "min", "close": "last",
                      "volume": "sum", "turnover": "sum", "trades": "sum"})
    count = groups["close"].count()
    return out[count > 0]


def resample(frame: pd.DataFrame, period: str, span: int = 1, session: str = "regular") -> pd.DataFrame:
    """Aggregate a fine frame into bars. `session` is "regular" (09:30-16:00 ET) or "extended"."""
    if frame.empty:
        return frame.copy()
    if session == "regular":
        frame = regular_session(frame)
        if frame.empty:
            return frame.copy()
    if period in INTRADAY:
        rule = f"{span}{INTRADAY[period]}"
        # Hour bars line up with the 09:30 open, like TradingView's US equities.
        offset = "9h30min" if period == "hour" else None
        return _aggregate(frame.resample(rule, origin="start_day", offset=offset, label="left", closed="left"))
    return _aggregate(frame.groupby(calendar_key(frame.index, period)))


def calendar_key(index: pd.DatetimeIndex, period: str) -> pd.DatetimeIndex:
    """Start of the day/week (Monday)/month/year each timestamp falls in, in US/Eastern."""
    local = index.tz_convert(ET).tz_localize(None).normalize()
    if period == "day":
        key = local
    elif period == "week":
        key = local - pd.to_timedelta(local.weekday, unit="D")
    elif period == "month":
        key = local.to_period("M").start_time
    elif period == "year":
        key = local.to_period("Y").start_time
    else:
        raise ValueError(f"not a calendar period: {period}")
    return pd.DatetimeIndex(key).tz_localize(ET)


def epoch_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch milliseconds, whatever unit (ns/us/ms) pandas stores the index in."""
    return index.as_unit("ms").asi8


def to_records(bars: pd.DataFrame) -> list[dict]:
    ts = epoch_ms(bars.index).tolist()
    cols = {c: bars[c].to_numpy(dtype=float).tolist() for c in COLUMNS}
    return [
        {"timestamp": int(t), **{c: cols[c][i] for c in COLUMNS}}
        for i, t in enumerate(ts)
    ]


MERGED = "*"  # pseudo-source: every feed for a ticker combined


def merge_frames(frames: list[pd.DataFrame], bucket: str | None = None) -> pd.DataFrame:
    """Combine feeds already at the same base resolution, highest priority first.

    Coverage is decided per slot: each bar's timestamp, or for ticks each
    `bucket` interval (e.g. "1min"). A slot is taken entirely from the
    highest-priority feed that has data in it, so two feeds never both
    contribute to one slot (no double counting), and rows within a feed are
    never dropped (trades sharing a timestamp all stay).
    """
    tagged = [f.assign(_rank=i) for i, f in enumerate(frames) if not f.empty]
    if not tagged:
        return frames[0].iloc[0:0] if frames else pd.DataFrame(columns=COLUMNS)
    both = pd.concat(tagged)
    slot = both.index.floor(bucket) if bucket else both.index
    owner = pd.Series(both["_rank"].to_numpy(), index=slot).groupby(level=0).min()
    keep = both["_rank"].to_numpy() == owner.reindex(slot).to_numpy()
    return both[keep].sort_index(kind="stable").drop(columns="_rank")


class BarEngine:
    """Loads series from the store, caches them, and serves pages of bars.

    `source` may be a single feed or MERGED ("*") for all of a ticker's feeds,
    combined in `priority` order (feeds not listed come after, alphabetically).
    """

    def __init__(self, store: Store, cache_size: int = 64, priority: list[str] | None = None):
        self.priority = priority or []
        self.store = store
        self._series: dict[tuple, tuple] = {}
        self._bars: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
        self._cache_size = cache_size
        # One SQLite connection is shared by the server's threads: every store access holds this lock.
        self.lock = RLock()
        self._lock = self.lock

    def sources_for(self, ticker: str) -> list[str]:
        with self._lock:
            have = {s for s, t, _ in self.store.stored_series() if t == ticker.upper()}
        ranked = [s for s in self.priority if s in have]
        return ranked + sorted(have - set(ranked))

    def series(self, source: str, ticker: str) -> Series | None:
        ticker = ticker.upper()
        with self._lock:
            if source == MERGED:
                parts = [self.series(s, ticker) for s in self.sources_for(ticker)]
                fp = tuple(self._series[(p.source, ticker)][0] for p in parts if p)
            else:
                parts = None
                fp = self.store.series_fingerprint(source, ticker)
            key = (source, ticker)
            cached = self._series.get(key)
            if cached and cached[0] == fp:
                return cached[1]
            s = self._merge(ticker, [p for p in parts if p]) if parts is not None else self._load(source, ticker)
            self._series[key] = (fp, s)
            return s

    def _merge(self, ticker: str, parts: list[Series]) -> Series | None:
        if not parts:
            return None
        if len(parts) == 1:
            p = parts[0]
            return Series(MERGED, ticker, p.kind, p.frame, p.daily, p.price_precision, p.volume_precision)
        # Common base = the coarsest of the feeds; finer feeds are aggregated up to it exactly.
        base = max((p.kind for p in parts), key=BASE_KINDS.index)
        frames = []
        for p in parts:
            if p.kind == base or base == "trades":
                frames.append(p.frame)
            elif base in INTRADAY:
                frames.append(resample(p.frame, base, 1, session="extended"))
            else:
                frames.append(_aggregate(p.frame.groupby(calendar_key(p.frame.index, "day"))))
        daily = next((p.daily for p in parts if p.daily is not None and not p.daily.empty), None)
        return Series(
            MERGED, ticker, base, merge_frames(frames, "1min" if base == "trades" else None), daily,
            max(p.price_precision for p in parts), max(p.volume_precision for p in parts),
        )

    def _load(self, source: str, ticker: str) -> Series | None:
        kinds = {k for s, t, k in self.store.stored_series() if s == source and t == ticker}
        kind = next((k for k in BASE_KINDS if k in kinds), None)
        if kind is None:
            return None
        if kind == "trades":
            frame = frame_from_trades(self.store.load_trades(ticker, source))
        else:
            frame = frame_from_bars(self.store.load_bars([ticker], kind, 1, source=source))
        daily = None
        if "day" in kinds and kind != "day":
            daily = frame_from_bars(self.store.load_bars([ticker], "day", 1, source=source))
        prices = frame[["open", "high", "low", "close"]].to_numpy().ravel()
        return Series(
            source, ticker, kind, frame.sort_index(), daily,
            price_precision=decimals(prices, cap=6, minimum=2),
            volume_precision=decimals(frame["volume"].to_numpy(), cap=4),
        )

    def periods(self, series: Series) -> list[dict]:
        """Intervals that can be built exactly from the stored data."""
        base = BASE_SECONDS[series.kind]
        out = []
        choices = (("second", (1, 5, 15, 30)), ("minute", (1, 3, 5, 15, 30, 45)),
                   ("hour", (1, 2, 4)), ("day", (1,)), ("week", (1,)), ("month", (1,)))
        for period, spans in choices:
            for span in spans:
                seconds = PERIOD_SECONDS[period] * span
                if seconds >= base and (base == 0 or seconds % base == 0 or period in CALENDAR):
                    out.append({"type": period, "span": span})
        return out

    def bars(
        self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
        to_ms: int | None = None, since_ms: int | None = None, limit: int = 1000,
    ) -> dict:
        s = self.series(source, ticker)
        if s is None:
            return {"bars": [], "more": False}
        if not any(p["type"] == period and p["span"] == span for p in self.periods(s)):
            raise ValueError(f"{period}/{span} cannot be built from {s.kind} data")
        all_bars = self._resampled(s, period, span, session)
        ts = epoch_ms(all_bars.index)
        if since_ms is not None:
            sel, more = all_bars[ts >= since_ms], False
        elif to_ms is not None:
            older = all_bars[ts < to_ms]
            sel, more = older.tail(limit), len(older) > limit
        else:
            sel, more = all_bars.tail(limit), len(all_bars) > limit
        return {"bars": to_records(sel), "more": bool(more)}

    def _resampled(self, s: Series, period: str, span: int, session: str) -> pd.DataFrame:
        fp = self._series[(s.source, s.ticker)][0]
        key = (s.source, s.ticker, period, span, session, fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key]
        if period in CALENDAR and s.daily is not None and not s.daily.empty:
            # Official daily bars (closing auction included); weeks/months group those.
            daily = s.daily.copy()
            daily.index = calendar_key(daily.index, "day")
            bars = daily if period == "day" else _aggregate(daily.groupby(calendar_key(daily.index, period)))
        else:
            bars = resample(s.frame, period, span, session)
        with self._lock:
            self._bars[key] = bars
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return bars
