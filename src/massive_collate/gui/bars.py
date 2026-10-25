"""Bars for the chart at any interval, built from the finest data stored.

Values are never interpolated or rounded: open/high/low/close are taken from
the underlying ticks or bars, volume and turnover are summed, and empty
intervals are left out (as TradingView does).
"""
from __future__ import annotations

import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from dataclasses import replace
from datetime import datetime, time
from time import monotonic
from time import time as _wall_clock
from zoneinfo import ZoneInfo
from threading import RLock

import numpy as np
import pandas as pd

from ..live import PARTIAL_FEEDS
from ..storage import Store
from ..calc.derivatives import Model, apply_model


def _money(step: float | None) -> str:
    """'$0.10', '$1', '$2.50', '$0.005' (a price segment)."""
    if not step:
        return "exact"
    if step >= 1 and step == int(step):
        return f"${int(step):,}"
    decimals = len(f"{step:.8f}".rstrip("0").split(".")[1])
    return f"${step:,.{max(2, decimals)}f}"


def model_precision(values) -> int:
    """Decimals that show model values to about 5 significant digits."""
    v = np.abs(np.asarray(values, dtype=float))
    v = v[np.isfinite(v) & (v > 0)]
    if not len(v):
        return 2
    scale = float(np.percentile(v, 95))
    return int(min(10, max(2, 4 - math.floor(math.log10(scale)))))

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
    asset: str = "stock"  # stock | index | fx | future
    rolls: list = field(default_factory=list)  # continuous futures: contract changes
    adjust: str = "none"  # continuous futures back-adjustment: none | difference | ratio
    # One-second bars built from ticks, kept apart from `frame` (a year of them would not fit in
    # memory): the feeds holding them and the spans [start ms, end ms) they cover. Second
    # intervals read only the window the chart asks for.
    fine_sources: list = field(default_factory=list)
    fine_spans: list = field(default_factory=list)
    # Tail updates (BarEngine._refresh_tail): the newest stored_at among the bars loaded, and
    # when the series was last loaded whole (monotonic seconds).
    stored_hw: int = 0
    loaded_at: float = 0.0
    fp: tuple = ()  # the store's fingerprint this series was loaded at (what results from it are cached under)

    @property
    def around_the_clock(self) -> bool:
        """Forex, crypto and futures trade (nearly) 24h: no regular-hours filter applies."""
        return self.asset in ("fx", "crypto", "future")

    def session_for(self, wanted: str) -> str:
        """The session actually drawn: around-the-clock markets have one session; an index is
        only calculated in regular hours (prints outside them are feed errors)."""
        if self.around_the_clock:
            return "extended"
        return "regular" if self.asset == "index" else wanted


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
        # built here (from ticks, or finer bars) rather than sent by the feed at this interval
        "derived": df["derived"].astype(bool) if "derived" in df else False,
    })


def regular_session(frame: pd.DataFrame) -> pd.DataFrame:
    """Bars inside US regular trading hours: 09:30-16:00 ET on trading days, 13:00 on early-close
    days (the rest of those afternoons is after-hours trading), nothing on weekends or holidays
    (stray prints a feed sometimes has then are not part of any session)."""
    if frame.empty:
        return frame
    return frame[regular_mask(frame.index)]


def regular_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """Which times are inside US regular trading hours (see regular_session)."""
    if len(index) == 0:
        return np.zeros(0, dtype=bool)
    local = index.tz_convert(ET)
    # wall-clock time of day in microseconds (as datetime.time compares; elapsed time from midnight
    # differs on the days the clocks change), against each day's close: one look-up a day, not a bar
    hour, minute, second, micro = (np.asarray(f, dtype=np.int64) for f in
                                   (local.hour, local.minute, local.second, local.microsecond))  # int32 overflows
    tod = ((hour * 60 + minute) * 60 + second) * 1_000_000 + micro
    days = local.normalize()
    unique = days.unique()
    closes = np.array([-1 if c is None else (c.hour * 60 + c.minute) * 60_000_000
                       for c in (_regular_close(d.date()) for d in unique)], dtype=np.int64)
    close = closes[unique.get_indexer(days)]
    opens = (REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute) * 60_000_000
    return (tod >= opens) & (tod < close)  # closed days: close -1


def _regular_close(d) -> time | None:
    """Regular-session close of a day by the exchange calendar, None when the market is shut."""
    from ..sessions import is_nyse_holiday, nyse_early_close

    if d.weekday() >= 5 or is_nyse_holiday(d):
        return None
    return time(13, 0) if nyse_early_close(d) else REGULAR_CLOSE


def _aggregate(groups) -> pd.DataFrame:
    spec = {"open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "turnover": "sum", "trades": "sum"}
    if "derived" in getattr(groups, "obj", pd.DataFrame()).columns:
        spec["derived"] = "max"
    if "env_high" in getattr(groups, "obj", pd.DataFrame()).columns:  # composite: the deviation across exchanges
        spec.update(env_high="max", env_low="min", venues_n="max", dev_sq="mean")
    out = groups.agg(spec)
    if "dev_sq" in out:
        out["dev_sd"] = np.sqrt(out.pop("dev_sq"))  # root mean square of the bars' deviations
    count = groups["close"].count()
    return out[count > 0]


def resample(
    frame: pd.DataFrame, period: str, span: int = 1, session: str = "regular", align_hours_to_open: bool = True,
    utc_days: bool = False, session_hours: int = 0,
) -> pd.DataFrame:
    """Aggregate a fine frame into bars. `session` is "regular" (09:30-16:00 ET) or "extended"."""
    if frame.empty:
        return frame.copy()
    if session == "regular":
        frame = regular_session(frame)
        if frame.empty:
            return frame.copy()
    return _aggregate(frame.groupby(bin_keys(frame.index, period, span, align_hours_to_open, utc_days, session_hours)))


def bin_keys(index: pd.DatetimeIndex, period: str, span: int = 1, align_hours_to_open: bool = True,
             utc_days: bool = False, session_hours: int = 0) -> pd.DatetimeIndex:
    """The start of the bar each timestamp falls in (what `resample` groups by)."""
    if period in INTRADAY:
        # US equities: hour bars line up with the 09:30 open, like TradingView. Around-the-clock
        # markets (fx, futures) use clock hours.
        offset = 570 * 60 if period == "hour" and align_hours_to_open else 0
        return wall_clock_bins(index, period, span, offset)
    return calendar_key(index, period, utc=utc_days, session_hours=session_hours)


def bar_end(start: pd.Timestamp, period: str, span: int, utc: bool = False) -> pd.Timestamp:
    """Where a bar starting at `start` ends (exclusive): in New York wall-clock time, or in UTC for
    the UTC days, weeks and months of crypto and forex (a New York step is an hour off on the
    days the clocks change, and a UTC month's end is not New York's)."""
    if period in INTRADAY:
        return start + pd.Timedelta(seconds=span * _UNIT_SECONDS[period])
    zone = "UTC" if utc else ET
    local = start.tz_convert(zone).tz_localize(None)
    step = {"day": pd.DateOffset(days=1), "week": pd.DateOffset(days=7), "month": pd.DateOffset(months=1),
            "year": pd.DateOffset(years=1)}[period]
    return (local + step).tz_localize(zone, nonexistent="shift_forward", ambiguous=True).tz_convert(ET)


_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
_ET_ZONE = ZoneInfo(ET)
_ET_STANDARD_NS = int(_ET_ZONE.utcoffset(datetime(2001, 1, 15)).total_seconds() * 1e9)


def wall_clock_bins(index: pd.DatetimeIndex, period: str, span: int, offset_seconds: int = 0) -> pd.DatetimeIndex:
    """Start of each timestamp's bar: every `span` units of New York wall-clock time from
    midnight (+ offset), the same on every day of the year whatever the daylight-saving
    change and however much data is loaded. The repeated hour when clocks go back keeps its
    two halves apart (they are different moments)."""
    local = index.tz_convert(ET)
    naive = local.tz_localize(None).as_unit("ns")
    day = naive.normalize()
    secs = (naive.asi8 - day.asi8) // 1_000_000_000
    step = span * _UNIT_SECONDS[period]
    start = (secs - offset_seconds) // step * step + offset_seconds
    bins = pd.DatetimeIndex(day.asi8 + start * 1_000_000_000).as_unit("ns")
    utc_offset = naive.asi8 - local.tz_convert("UTC").tz_localize(None).as_unit("ns").asi8
    is_dst = utc_offset > _ET_STANDARD_NS
    return bins.tz_localize(ET, ambiguous=is_dst, nonexistent="shift_forward")


def calendar_key(index: pd.DatetimeIndex, period: str, utc: bool = False, session_hours: int = 0) -> pd.DatetimeIndex:
    """Start of the day/week (Monday)/month/year each timestamp falls in, in US/Eastern; with
    `utc`, of the UTC day/week/month (crypto and forex days are UTC days, as their feeds stamp
    them), still given in US/Eastern. `session_hours`: a session that starts that many hours
    before midnight belongs to the next day (futures: 6, CME's 18:00 open)."""
    zone = "UTC" if utc else ET
    local = (index.tz_convert(zone).tz_localize(None) + pd.Timedelta(hours=session_hours)).normalize()
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
    return pd.DatetimeIndex(key).tz_localize(zone).tz_convert(ET)


def epoch_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch milliseconds, whatever unit (ns/us/ms) pandas stores the index in."""
    return index.as_unit("ms").asi8


MAX_LOD = 4096
# Types whose candles can be shown as exact aggregates of consecutive candles (first open, last
# close, high/low envelope, summed volume). Kagi and point & figure are drawn from per-segment
# fields and are left at full detail.
LOD_TYPES = ("candles", "hollow", "bars", "volume_candles", "line", "line_markers", "step", "area",
             "hlc_area", "baseline", "columns", "high_low", "heikin_ashi", "renko", "line_break", "range")


def lod_aggregate(bars: pd.DataFrame, k: int) -> pd.DataFrame:
    """Level of detail: exact OHLCV of every k consecutive bars, for drawing when bars are
    thinner than a pixel. Groups are counted from the first bar of the series, so they stay
    put while scrolling and paging. Each group is stamped with its first bar's time."""
    if k <= 1 or bars.empty:
        return bars
    groups = np.arange(len(bars)) // k
    g = bars.groupby(groups, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "turnover": g["turnover"].sum(), "trades": g["trades"].sum(),
    })
    if "derived" in bars:
        out["derived"] = g["derived"].max()
    out.index = bars.index[::k][: len(out)]
    return out


EXTRA_COLUMNS = ("yang_start", "yang", "switch", "up", "env_high", "env_low", "dev_sd", "venues_n", "q1", "med", "q3",
                 "derived", "interp")
# Derived models stored as bars: not feeds of their own in the merged view.
MODEL_SOURCES = ("composite",)
# Symbols whose second intervals continue back past their one-second data with values interpolated
# from their minute bars. None for now: crypto has free sources of real seconds (trades), which are
# collected instead; kept for symbols with no such source.
INTERPOLATED: frozenset = frozenset()
INTERP_GAP_MS = 5 * 60_000  # minutes further apart than this are a gap: nothing is drawn across it


def session_hours(s) -> int:
    """Hours before midnight a series' day starts: futures trade CME sessions from 18:00 New York."""
    return 6 if getattr(s, "asset", None) == "future" else 0


def utc_days(s) -> bool:
    """Crypto and forex days are UTC days (their feeds stamp daily bars 00:00 UTC)."""
    return s.asset in ("crypto", "fx")
BAR_KINDS = ("second", "minute", "hour", "day")  # series of bars (not ticks) that can take tail updates


def summarize_range(bars: pd.DataFrame, lo_ms: int, hi_ms: int, model: Model = Model()) -> dict:
    """Statistics over the bars whose start lies in [lo_ms, hi_ms]: what the chart's highlight shows."""
    ts = epoch_ms(bars.index)
    sel = bars[(ts >= lo_ms) & (ts <= hi_ms)]
    if sel.empty:
        return {"bars": 0}
    sel_ts = epoch_ms(sel.index)
    volume = float(sel["volume"].sum())
    turnover = float(sel["turnover"].sum())
    first_open, last_close = float(sel["open"].iloc[0]), float(sel["close"].iloc[-1])
    hi_i, lo_i = int(np.argmax(sel["high"].to_numpy())), int(np.argmin(sel["low"].to_numpy()))
    return {
        "bars": int(len(sel)),
        "from": int(sel_ts[0]), "to": int(sel_ts[-1]),
        "open": first_open, "close": last_close,
        "change": last_close - first_open,
        "changePct": (last_close / first_open - 1) * 100 if first_open else None,
        "high": float(sel["high"].iloc[hi_i]), "highTime": int(sel_ts[hi_i]),
        "low": float(sel["low"].iloc[lo_i]), "lowTime": int(sel_ts[lo_i]),
        "rangePct": (float(sel["high"].max()) / float(sel["low"].min()) - 1) * 100 if sel["low"].min() else None,
        "volume": volume,
        # VWAP and range % are price concepts: not shown for derivatives or integrals.
        "vwap": turnover / volume if volume and model.is_price else None,
        "trades": int(sel["trades"].sum()),
        **({} if model.is_price else {"rangePct": None, "model": model.kind, "unit": model.unit_label()}),
    }


def to_records(bars: pd.DataFrame) -> list[dict]:
    ts = epoch_ms(bars.index).tolist()
    cols = {c: bars[c].to_numpy(dtype=float).tolist() for c in COLUMNS}
    extra = {c: bars[c].tolist() for c in EXTRA_COLUMNS if c in bars}
    if "src_time" in bars:
        extra["srcTime"] = epoch_ms(pd.DatetimeIndex(bars["src_time"])).tolist()
    records = []
    for i, t in enumerate(ts):
        r = {"timestamp": int(t), **{c: cols[c][i] for c in COLUMNS}}
        for c, values in extra.items():
            v = values[i]
            r[c] = None if isinstance(v, float) and np.isnan(v) else (
                bool(v) if c in ("yang", "yang_start", "up", "derived", "interp") else v)
        records.append(r)
    return records


@dataclass(frozen=True)
class Style:
    """Chart type and its parameters. Box/reversal of None means ATR(14), like TradingView."""
    type: str = "candles"
    box: float | None = None
    reversal: float | None = None  # kagi: price amount; point & figure: number of boxes
    lines: int = 3  # line break


TIME_TYPES = ("candles", "hollow", "bars", "volume_candles", "line", "line_markers", "step", "area",
              "hlc_area", "baseline", "columns", "high_low")
# Box plots: time bars with each bar's quartiles of traded price (calc/profile.py).
BOX = "box"
# Chart types that follow price, not time: a brick or line only when the price moves far enough.
PRICE_BASED = ("renko", "line_break", "kagi", "point_figure", "range")


def style_used(style: Style, base: pd.DataFrame, precision: int) -> dict:
    """The parameters a transformed chart type (charttypes.TRANSFORMS) is drawn with: a box or
    reversal not given is ATR(14) of `base` at its last bar, rounded to the prices' decimals."""
    from . import charttypes as ct

    default = ct.round_box(ct.atr(base), precision) if not base.empty else 1.0
    box = style.box or default
    used = {"type": style.type}
    if style.type == "kagi":
        used["reversal"] = style.reversal or default
    elif style.type == "point_figure":
        boxes = style.reversal if style.reversal is not None else 3
        if boxes < 1 or boxes != int(boxes):
            raise ValueError("point & figure reversal is a whole number of boxes (1 or more)")
        used.update(box=box, reversal=int(boxes))
    elif style.type == "line_break":
        used["lines"] = style.lines
    elif style.type in ("renko", "range"):
        used["box"] = box
    return used


def transformed(base: pd.DataFrame, used: dict, fine: pd.DataFrame | None = None) -> pd.DataFrame:
    """`base`'s bars drawn as the chart type `used` names, with its parameters (style_used).
    Range bars are built from `fine` where given (the finest data), else from `base`."""
    from . import charttypes as ct

    kind = used["type"]
    if kind == "heikin_ashi":
        return ct.heikin_ashi(base)
    if kind == "renko":
        return ct.renko(base, used["box"])
    if kind == "line_break":
        return ct.line_break(base, used["lines"])
    if kind == "kagi":
        return ct.kagi(base, used["reversal"])
    if kind == "point_figure":
        return ct.point_figure(base, used["box"], used["reversal"])
    return ct.range_bars(base if fine is None else fine, used["box"])


def model_info(model: Model, values: pd.DataFrame, hidden: int) -> dict:
    """What a page of model values says about them: the model, its unit, the decimals that show
    it, the bars it leaves out (stretches too short for its window) and how it was derived."""
    from dataclasses import asdict

    from ..provenance import code_version

    return {"kind": model.kind, "unit": model.unit_label(), "precision": model_precision(values["close"].to_numpy()),
            "hidden": int(hidden), "window": model.window,
            # an indicator is derived on request: how, with what code, and whether it looks ahead
            "derivation": {"kind": "indicator", "code": code_version(), "settings": asdict(model),
                           "lookAhead": not model.causal}}


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
    slot = both.index.tz_convert("UTC").floor(bucket) if bucket else both.index  # UTC: the repeated hour is fine
    owner = pd.Series(both["_rank"].to_numpy(), index=slot).groupby(level=0).min()
    keep = both["_rank"].to_numpy() == owner.reindex(slot).to_numpy()
    return both[keep].sort_index(kind="stable").drop(columns="_rank")


class _BoundedCache(OrderedDict):
    """A small LRU dict for projected time axes."""

    def __init__(self, size: int):
        super().__init__()
        self._size = size

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._size:
            self.popitem(last=False)


def _weight(value) -> int:
    """Bytes a cached value holds: its frames' columns and index (a whole series: its bars and
    its official days)."""
    if isinstance(value, pd.DataFrame):
        return int(value.memory_usage(index=True, deep=True).sum())
    if isinstance(value, tuple):
        return sum(_weight(v) for v in value)
    if isinstance(value, Series):
        return _weight(value.frame) + (_weight(value.daily) if value.daily is not None else 0)
    return 0


class _SizedCache(OrderedDict):
    """An LRU dict kept under `budget` bytes as well as `size` entries: counted by entries only,
    a chart app holding a few long histories (twenty years of minutes, a year of seconds) and their
    resampled frames grew to 13 GB and slowed the whole machine. An entry over the budget on its
    own is served but not kept."""

    def __init__(self, budget: int, size: int):
        super().__init__()
        self._budget, self._size, self._bytes, self._weights = budget, size, 0, {}

    def __setitem__(self, key, value):
        if key in self:
            self._bytes -= self._weights.pop(key, 0)
        super().__setitem__(key, value)
        self.move_to_end(key)
        self._weights[key] = w = _weight(value)
        self._bytes += w
        while self and (self._bytes > self._budget or len(self) > self._size):
            self.popitem(last=False)

    def popitem(self, last: bool = True):
        key, value = super().popitem(last=last)
        self._bytes -= self._weights.pop(key, 0)
        return key, value

    def __delitem__(self, key):
        super().__delitem__(key)
        self._bytes -= self._weights.pop(key, 0)

    @property
    def nbytes(self) -> int:
        return self._bytes


# Every cache of the chart app together (CHART_CACHE_MB, config.py: a gigabyte by default), shared
# out: the whole histories the series path still loads (continuous futures, the composite, box
# plots), resampled and styled frames, the price-based charts' bricks and the 30-minute bins
# (window.py). They had 4 GB between them, and the app once grew to 13 GB and slowed the machine.
CHART_CACHE_BYTES = 1024 * 2**20
CACHE_SHARES = {"series": 0.40, "bars": 0.30, "bricks": 0.15, "rollup": 0.15}


class BarEngine:
    """Loads series from the store, caches them, and serves pages of bars.

    `source` may be a single feed or MERGED ("*") for all of a ticker's feeds,
    combined in `priority` order (feeds not listed come after, alphabetically).
    """

    def __init__(self, store: Store, cache_size: int = 64, priority: list[str] | None = None, quality=None,
                 cache_bytes: int | None = None):
        self.priority = priority or []
        self.quality = quality  # feed -> 0..1, for display priority (best first)
        self.store = store
        self.cache_bytes = int(cache_bytes or CHART_CACHE_BYTES)  # every cache's budget together (budget())
        self._series: dict[tuple, tuple] = _SizedCache(self.budget("series"), 48)  # whole histories: a memory budget
        self._bars: OrderedDict[tuple, pd.DataFrame] = _SizedCache(self.budget("bars"), max(1, cache_size))
        self._cache_size = cache_size
        self._future_cache: OrderedDict = _BoundedCache(16)
        self._stream_feeds: OrderedDict = _BoundedCache(64)  # ticker -> (ranked at, feeds): fingerprint()
        # One SQLite connection is shared by the server's threads: every store access holds this lock.
        self.lock = RLock()
        self._lock = self.lock

    def budget(self, cache: str) -> int:
        """Bytes one of the chart app's caches may hold (CACHE_SHARES of cache_bytes)."""
        return int(self.cache_bytes * CACHE_SHARES[cache])

    def _watch_item(self, source: str, ticker: str) -> dict | None:
        return next(iter(self.store.watchlist(source, ticker)), None)

    def sources_for(self, ticker: str) -> list[str]:
        """Feeds holding a ticker, best first: a feed with ticks (finer data) for it ranks first,
        then the asset class's reference exchange (Coinbase for crypto), then quality and the
        configured priority. A merged chart takes each bar from the first feed that has it, so it
        shows the reference where it has data and every other feed's history where it has none.
        A feed of one exchange's share of a market (IEX's trades) comes after every feed of the
        whole market, so a merged chart shows it only where none of them has bars (the current
        session, until a sync downloads it)."""
        from ..composite import REFERENCE

        with self._lock:
            stored = [(s, k) for s, t, k in self.store.stored_series() if t == ticker.upper()]
            have = {s for s, _ in stored}
            rows = self.store.watchlist(ticker=ticker)
            have |= {w["source"] for w in rows if w["asset"] == "future"}  # continuous futures are stored under their contracts
            reference = REFERENCE.get(next((w["asset"] for w in rows), "stock"))
            fine = {s for s, k in stored if k in ("trades", "second")}
            return self.rank_sources(have, fine, reference, self.quality or (lambda s: 1.0))

    def rank_sources(self, have: set, fine: set, reference: str | None, quality) -> list[str]:
        """`have` best first, as sources_for ranks them (for callers that hold the series and the
        watchlist already: the symbol list ranks thousands at once)."""
        order = {s: i for i, s in enumerate(self.priority)}
        return sorted(set(have) - set(MODEL_SOURCES),
                      key=lambda s: (s in PARTIAL_FEEDS, s not in fine, s != reference, -quality(s),
                                     order.get(s, len(order)), s))

    def _fingerprint(self, source: str, ticker: str) -> tuple:
        item = self._watch_item(source, ticker)
        if item and item["asset"] == "future":
            rolls = self.store.rolls(source, item["source_ticker"], item["rank"])
            contracts = sorted({t for _, t in rolls})
            return (len(rolls), rolls[-1] if rolls else None,
                    tuple(self.store.series_fingerprint(source, c) for c in contracts))
        return self.store.series_fingerprint(source, ticker)

    def series(self, source: str, ticker: str, adjust: str = "none") -> Series | None:
        """A ticker's finest stored series. `adjust` applies to continuous futures only."""
        ticker = ticker.upper()
        with self._lock:
            if source == MERGED:
                parts = [self.series(s, ticker, adjust) for s in self.sources_for(ticker)]
                fp = tuple(p.fp for p in parts if p)  # from the parts: the cache may have let one go since
            else:
                parts = None
                fp = self._fingerprint(source, ticker)
            key = (source, ticker, adjust)
            cached = self._series.get(key)
            if cached and cached[0] == fp:
                return cached[1]
            s = self._refresh_tail(cached[1]) if parts is None and cached and cached[1] is not None else None
            if s is None:
                s = self._merge(ticker, [p for p in parts if p]) if parts is not None else self._load(source, ticker, adjust)
            if s is not None:
                s.adjust = adjust
                s.fp = fp
            self._series[key] = (fp, s)
            return s

    def _merge(self, ticker: str, parts: list[Series]) -> Series | None:
        if not parts:
            return None
        if len(parts) == 1:
            p = parts[0]
            return Series(MERGED, ticker, p.kind, p.frame, p.daily, p.price_precision, p.volume_precision,
                          p.asset, p.rolls, fine_sources=p.fine_sources, fine_spans=p.fine_spans)
        # A feed of daily bars only (Tiingo, Cboe) adds days, not the base: with it in the base, every
        # intraday interval of the merged chart would go.
        day_only = [p for p in parts if p.kind == "day"]
        finer = [p for p in parts if p.kind != "day"] or day_only
        # Common base = the coarsest of the other feeds; finer feeds are aggregated up to it exactly
        # (and so are derived).
        base = max((p.kind for p in finer), key=BASE_KINDS.index)
        frames = []
        for p in finer:
            if p.kind == base or base == "trades":
                frames.append(p.frame)
            elif base in INTRADAY:
                frames.append(resample(p.frame, base, 1, session="extended", align_hours_to_open=p.asset in ("stock", "index"),
                                       utc_days=utc_days(p)).assign(derived=True))
            else:
                frames.append(_aggregate(p.frame.groupby(calendar_key(p.frame.index, "day", utc=utc_days(p),
                                                                      session_hours=session_hours(p)))).assign(derived=True))
        # Official daily bars come from whichever feed holds each day (one feed per span), so they
        # are combined day by day like the intraday bars, best-ranked feed first; then the daily-only
        # feeds' days.
        dailies = [p.daily for p in parts if p.daily is not None and not p.daily.empty]
        dailies += [p.frame for p in day_only if p is not finer[0] and not p.frame.empty]
        daily = merge_frames(dailies) if dailies else None
        return Series(
            MERGED, ticker, base, merge_frames(frames, "1min" if base == "trades" else None), daily,
            max(p.price_precision for p in parts), max(p.volume_precision for p in parts),
            parts[0].asset, parts[0].rolls,
            fine_sources=[src for p in parts for src in p.fine_sources],
            fine_spans=sorted(span for p in parts for span in p.fine_spans),
        )

    def _load(self, source: str, ticker: str, adjust: str = "none") -> Series | None:
        item = self._watch_item(source, ticker)
        if item is None:  # a feed holding a symbol it does not watch (Bitstamp's live BTCUSD): the symbol's class
            item_asset = next((w["asset"] for w in self.store.watchlist(ticker=ticker)), "stock")
        asset = item["asset"] if item else item_asset
        if asset == "future" and item is not None:
            return self._load_future(source, item, adjust)
        s = self._load_plain(source, ticker)
        if s is not None:
            s.asset = asset
        return s

    def _load_future(self, source: str, item: dict, adjust: str) -> Series | None:
        from ..futures import continuous

        rolls = self.store.rolls(source, item["source_ticker"], item["rank"])
        if not rolls:
            return None
        contracts = sorted({t for _, t in rolls})
        kinds = {k for s, t, k in self.store.stored_series() if s == source and t in contracts}
        kind = next((k for k in BASE_KINDS if k in kinds and k != "day"), None)
        if kind is None:
            return None
        frames = {c: frame_from_bars(self.store.load_bars([c], kind, 1, source=source)) for c in contracts}
        dailies = {c: frame_from_bars(self.store.load_bars([c], "day", 1, source=source)) for c in contracts}
        joined, roll_list = continuous(frames, rolls, adjust, sessions=dailies)
        daily, _ = continuous(dailies, rolls, adjust, sessions=dailies)
        frame = joined.drop(columns="contract")
        prices = frame[["open", "high", "low", "close"]].to_numpy().ravel()
        return Series(
            source, item["ticker"], kind, frame.sort_index(), daily.drop(columns="contract") if not daily.empty else None,
            price_precision=decimals(prices, cap=6, minimum=2),
            volume_precision=decimals(frame["volume"].to_numpy(), cap=4),
            asset="future",
            rolls=[{"time": int(epoch_ms(pd.DatetimeIndex([r.time]))[0]), "session": str(r.session), "from": r.old,
                    "to": r.new, "gap": r.gap} for r in roll_list],
        )

    STREAM_FEEDS_S = 60.0

    def fingerprint(self, source: str, ticker: str) -> tuple:
        """Changes whenever the symbol's stored data changes (the live stream's trigger)."""
        ticker = ticker.upper()
        with self._lock:
            if source == MERGED:
                # the feeds ranked once a minute: a live chart asks twice a second, and the ranking
                # lists every series of every feed again after any write
                hit = self._stream_feeds.get(ticker)
                if hit is None or monotonic() - hit[0] > self.STREAM_FEEDS_S:
                    hit = self._stream_feeds[ticker] = (monotonic(), self.sources_for(ticker))
                return tuple(self._fingerprint(s, ticker) for s in hit[1])
            return self._fingerprint(source, ticker)

    # A change further back than this, or a series loaded whole longer ago than that, reloads
    # it whole (deletions leave no stored_at behind; the reload catches them).
    TAIL_DAYS = 3
    FULL_RELOAD_SECONDS = 600
    STORE_SLACK_MS = 60_000  # writers' stored_at can commit out of order: look back a minute

    def _refresh_tail(self, s: Series) -> Series | None:
        """The cached series with what was stored since it was loaded spliced in: the live
        recorder adds or rebuilds the newest bars every few seconds, and reloading every bar
        for that took a third of a second each time. Bars are reloaded from the day before the
        earliest changed one (both tables, built bars first, exactly as a whole load reads
        them). None when a whole reload is needed: futures, ticks, the composite, a change
        older than TAIL_DAYS, daily bars appearing, or after FULL_RELOAD_SECONDS."""
        if (s.asset == "future" or s.kind not in BAR_KINDS or s.source in MODEL_SOURCES or s.frame.empty
                or monotonic() - s.loaded_at > self.FULL_RELOAD_SECONDS):
            return None
        kinds = [s.kind] if s.kind == "day" else [s.kind, "day"]
        changed = self.store.bars_changed_since(s.source, s.ticker, kinds, s.stored_hw - self.STORE_SLACK_MS)
        newest = int(epoch_ms(s.frame.index[-1:])[0])
        if "day" in changed and s.kind != "day" and s.daily is None:
            return None
        if any(lo < newest - self.TAIL_DAYS * 86_400_000 for lo, _ in changed.values()):
            return None
        frame, daily, prices = s.frame, s.daily, [s.price_precision]
        for kind, (lo, _) in changed.items():
            start = (pd.Timestamp(lo, unit="ms", tz="UTC") - pd.Timedelta(days=1)).date()
            cut = pd.Timestamp(start, tz="UTC")  # load_bars reads ts >= this
            tail = frame_from_bars(self.store.load_bars([s.ticker], kind, 1, start=start, source=s.source))
            prices.append(decimals(tail[["open", "high", "low", "close"]].to_numpy().ravel(), cap=6, minimum=2))
            if kind == s.kind:
                frame = pd.concat([frame[frame.index < cut], tail]).sort_index()
            else:
                daily = pd.concat([daily[daily.index < cut], tail]).sort_index()
        fine = self._fine(s.source, s.ticker, s.kind)
        return replace(s, frame=frame, daily=daily, price_precision=max(prices),
                       fine_sources=[s.source] if fine else [], fine_spans=fine,
                       stored_hw=max([s.stored_hw, *(hi for _, hi in changed.values())]))

    def _fine(self, source: str, ticker: str, kind: str) -> list:
        """Spans of one-second bars a minute series has (read by window, not loaded)."""
        kinds = {k for src, t, k in self.store.stored_series() if src == source and t == ticker}
        return [(a, b) for src, _, a, b in self.store.coverage_detail(ticker, "second") if src == source] \
            if kind == "minute" and "second" in kinds else []

    def _load_plain(self, source: str, ticker: str) -> Series | None:
        kinds = {k for s, t, k in self.store.stored_series() if s == source and t == ticker}
        # Minute bars are the base wherever a feed has them (second bars built from its ticks are a
        # layer read by window); a series of ticks or seconds alone is small and loaded whole.
        kind = "minute" if "minute" in kinds else next((k for k in BASE_KINDS if k in kinds), None)
        if kind is None:
            return None
        # what is stored from here on is read again by _refresh_tail (the change log; reading every
        # bar's stored_at for the newest of them was a second scan of the whole series)
        started = int(_wall_clock() * 1000)
        if kind == "trades":
            frame = frame_from_trades(self.store.load_trades(ticker, source))
        else:
            frame = frame_from_bars(self.store.load_bars([ticker], kind, 1, source=source))
        daily = None
        if "day" in kinds and kind != "day":
            daily = frame_from_bars(self.store.load_bars([ticker], "day", 1, source=source))
        prices = frame[["open", "high", "low", "close"]].to_numpy().ravel()
        fine = self._fine(source, ticker, kind)
        if source in MODEL_SOURCES:  # the composite carries its deviation across exchanges
            stats = self.store.composite_stats(ticker, kind)
            if not stats.empty:
                idx = pd.to_datetime(stats["ts"], unit="ms", utc=True).dt.tz_convert(ET)
                extra = pd.DataFrame({"env_high": stats["env_h"].to_numpy(), "env_low": stats["env_l"].to_numpy(),
                                      "dev_sq": (stats["sd_bps"].to_numpy() / 1e4 * stats["env_h"].to_numpy()) ** 2,
                                      "venues_n": stats["venues"].to_numpy()}, index=pd.DatetimeIndex(idx))
                frame = frame.join(extra, how="left")
        return Series(
            source, ticker, kind, frame.sort_index(), daily,
            price_precision=decimals(prices, cap=6, minimum=2),
            volume_precision=decimals(frame["volume"].to_numpy(), cap=4),
            fine_sources=[source] if fine else [], fine_spans=fine,
            stored_hw=started if kind in BAR_KINDS else 0, loaded_at=monotonic(),
        )

    PERIOD_CHOICES = (("second", (1, 30)), ("minute", (1, 5, 30)), ("hour", (1, 2, 4)),
                      ("day", (1,)), ("week", (1,)), ("month", (1,)), ("year", (1,)))

    def periods(self, series: Series) -> list[dict]:
        """Intervals that can be built exactly from the stored data (seconds where second bars
        built from ticks exist, read by window)."""
        return [p for p in self.all_periods(series) if p.pop("available")]

    def all_periods(self, series: Series) -> list[dict]:
        """Every interval offered, each saying whether this series' data can build it (the chart
        shows the others greyed out)."""
        base = 1 if series.fine_spans else BASE_SECONDS[series.kind]
        out = []
        for period, spans in self.PERIOD_CHOICES:
            for span in spans:
                seconds = PERIOD_SECONDS[period] * span
                ok = seconds >= base and (base == 0 or seconds % base == 0 or period in CALENDAR)
                out.append({"type": period, "span": span, "available": ok})
        return out

    def bars(
        self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
        to_ms: int | None = None, since_ms: int | None = None, limit: int = 1000,
        style: Style = Style(), adjust: str = "none", lod: int = 1, model: Model = Model(),
        after_ms: int | None = None,
    ) -> dict:
        """`lod` > 1 returns exact aggregates of `lod` consecutive bars (time-based chart types
        only) for drawing far zoomed-out views; statistics never use it. `model` turns the
        price into its 1st/2nd derivative or integral (time-based chart types only)."""
        s = self.series(source, ticker, adjust)
        if s is None:
            return {"bars": [], "more": False}
        session = s.session_for(session)
        if not any(p["type"] == period and p["span"] == span for p in self.periods(s)):
            raise ValueError(f"{period}/{span} cannot be built from {s.kind} data")
        if not 1 <= lod <= MAX_LOD:
            raise ValueError(f"lod must be 1-{MAX_LOD}")
        if self._windowed(s, period) and after_ms is not None:
            return {"bars": [], "more": True, "newer": False}  # seconds read by window page backwards only
        if self._windowed(s, period) and style.type in TIME_TYPES and model.is_price and lod == 1:
            page = self._fine_page(s, span, session, to_ms, since_ms, limit)
            page["style"] = {"type": style.type}
            return page
        if self._windowed(s, period) and style.type in LOD_TYPES and model.is_price:  # zoomed out on seconds
            page = self._fine_lod_page(s, span, session, to_ms, since_ms, limit, lod)
            page["style"] = {"type": style.type, "lod": lod}
            return page
        model.validate()
        if not model.is_price and style.type not in TIME_TYPES:
            raise ValueError("derivatives and integrals apply to time-based chart types")
        all_bars, used = self._styled(s, period, span, session, style)
        hidden = 0
        if not model.is_price:
            priced = len(all_bars)
            all_bars = self._modelled(s, all_bars, period, span, session, model)
            hidden = priced - len(all_bars)  # bars in stretches too short for the window
            used = {**used, "model": model.kind, "window": model.window, "basis": model.basis, "per": model.per}
        if lod > 1 and style.type in LOD_TYPES:
            # The chart type and its parameters are part of the key: aggregates of Renko bricks
            # must never be served for line break (or candles) and vice versa.
            key = ("lod", s.source, s.ticker, s.adjust, period, span, session, lod, tuple(sorted(used.items())),
                   model, s.fp)
            with self._lock:
                cached = self._bars.get(key)
            if cached is None:
                cached = lod_aggregate(all_bars, lod)
                with self._lock:
                    self._bars[key] = cached
                    if len(self._bars) > self._cache_size:
                        self._bars.popitem(last=False)
            all_bars = cached
            used = {**used, "lod": lod}
        page = self._page(all_bars, to_ms, since_ms, limit, after_ms)
        page["style"] = used
        page["total"] = int(len(all_bars))
        # What the candles were built from when no feed sends this interval (all of them derived).
        page["derivedFrom"] = None if (period == s.kind and span == 1) or (period == "day" and s.daily is not None) \
            else ("trades" if s.kind == "trades" else f"1-{s.kind} bars")
        if not model.is_price:
            page["model"] = model_info(model, all_bars, hidden)
        if style.type == "range" and s.kind != "trades":
            page["approximate"] = "range bars built from bars, not ticks: intra-bar order of high and low is assumed"
        if style.type == BOX:
            page["approximate"] = (
                "* derived: each box is the volume-weighted quartiles (25 %, median, 75 %) of the average prices of "
                f"the {used['from']} inside the bar, not of trades; whiskers are the bar's high and low"
                if used["from"] and used["from"] != "trades" else
                "box: quartiles of the trades' prices inside each bar; whiskers high and low" if used["from"] else
                "no data finer than this interval: boxes need a finer interval's bars inside each bar")
        return page

    def calendar(self):
        """Trading calendar with the source's official schedules (rebuilt when they change)."""
        from ..sessions import MarketCalendar, overrides_from_store

        with self._lock:
            return self.store._cached(("calendar",), lambda: MarketCalendar(overrides_from_store(self.store)))

    def future(
        self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
        style: Style = Style(), adjust: str = "none", lod: int = 1, model: Model = Model(),
        offset: int = 0, count: int = 500,
    ) -> dict:
        """Start times of the next `count` bar slots after the last real bar (from slot `offset`),
        at this interval, session, chart type and level of detail: the chart's time axis to the right."""
        from .projection import project

        s = self.series(source, ticker, adjust)
        if s is None:
            return {"timestamps": [], "last": None, "total": 0}
        session = s.session_for(session)
        all_bars, _ = self._styled(s, period, span, session, style)
        if not model.is_price:
            all_bars = self._modelled(s, all_bars, period, span, session, model)
        if all_bars.empty:
            return {"timestamps": [], "last": None, "total": 0}
        with self._lock:
            item = next((w for w in self.store.watchlist(ticker=s.ticker) if w["asset"] == "future"), None)
        product = item["source_ticker"] if item else None
        total = len(all_bars)
        k = lod if (lod > 1 and style.type in LOD_TYPES) else 1
        last = all_bars.index[(total - 1) // k * k]  # the last drawn slot's stamp (its first bar)
        ts = project(self.calendar(), s, product, period, span, session, style.type, lod, total,
                     all_bars.index[-1], offset, count, cache=self._future_cache)
        return {"timestamps": ts, "last": int(epoch_ms(pd.DatetimeIndex([last]))[0]), "total": total,
                "lastBar": int(epoch_ms(pd.DatetimeIndex([all_bars.index[-1]]))[0])}

    def _styled(self, s: Series, period: str, span: int, session: str, style: Style) -> tuple[pd.DataFrame, dict]:
        from . import charttypes as ct

        if style.type in TIME_TYPES:
            return self._resampled(s, period, span, session), {"type": style.type}
        if style.type == BOX:
            return self._boxed(s, period, span, session)
        if style.type not in ct.TRANSFORMS:
            raise ValueError(f"unknown chart type {style.type!r}")
        base = self._resampled(s, period, span, session)
        used = style_used(style, base, s.price_precision)
        key = (s.source, s.ticker, s.adjust, period, span, session, tuple(sorted(used.items())),
               s.fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key], used
        # range: from the finest data, not the chart interval
        fine = (s.frame if session == "extended" else regular_session(s.frame)) if style.type == "range" else None
        out = transformed(base, used, fine)
        with self._lock:
            self._bars[key] = out
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return out, used

    def _built_days(self, s: Series, session: str) -> pd.DataFrame:
        """Daily bars built from the series' finer data, keyed like the official ones."""
        frame = regular_session(s.frame) if session == "regular" else s.frame
        if frame.empty:
            return frame.copy()
        return _aggregate(frame.groupby(calendar_key(frame.index, "day", utc=utc_days(s), session_hours=session_hours(s))))

    def _boxed(self, s: Series, period: str, span: int, session: str) -> tuple[pd.DataFrame, dict]:
        """Time bars with q1/med/q3: the volume-weighted quartiles of the price traded inside each
        bar. From ticks (a series of trades) they are exact; from finer bars they are of each
        bar's average price (derived). A bar with nothing finer inside it has none (a series of
        minute bars at a 1-minute interval)."""
        from ..calc.profile import weighted_quartiles

        bars = self._resampled(s, period, span, session)
        finer = BASE_SECONDS[s.kind] < PERIOD_SECONDS[period] * span and not self._windowed(s, period)
        unit = "trades" if s.kind == "trades" else f"{s.kind} bars"
        used = {"type": BOX, "from": unit if finer else None}
        key = ("box", s.source, s.ticker, s.adjust, period, span, session, s.fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key], used
        out = bars.copy()
        out["q1"] = out["med"] = out["q3"] = np.nan
        if finer and not bars.empty:
            sub = regular_session(s.frame) if session == "regular" else s.frame
            group = bars.index.get_indexer(bin_keys(sub.index, period, span, s.asset in ("stock", "index"), utc_days(s)))
            vol = sub["volume"].to_numpy(dtype=float)
            price = np.where(vol > 0, sub["turnover"].to_numpy(dtype=float) / np.where(vol > 0, vol, 1),
                             sub["close"].to_numpy(dtype=float))
            q = weighted_quartiles(group, price, vol, len(bars))
            out["q1"], out["med"], out["q3"] = q[:, 0], q[:, 1], q[:, 2]
        with self._lock:
            self._bars[key] = out
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return out, used

    # -- the virtual level 2: traded volume by price in one bar --------------------------------
    PROFILE_TICK_MS = 86_400_000  # windows up to a day read ticks; longer ones use bars
    PROFILE_MAX_ROWS = 2000

    def profile(self, source: str, ticker: str, period: str, span: int = 1, session: str = "regular",
                at_ms: int | None = None, step: float | None = None, adjust: str = "none") -> dict:
        """Volume and trades by price in the chart's bar at `at_ms` (the newest without), with the
        bar's and its day's range and the last price. Not an order book: what traded, not what
        was resting. Exact prices from ticks where the feed has them; summed into price segments
        or spread from bars otherwise, which is derived and says so (calc/profile.py)."""
        from ..calc.profile import from_bars, from_trades, nice_step

        s = self.series(source, ticker, adjust)
        if s is None:
            raise LookupError(f"no data stored for {source}:{ticker}")
        session = s.session_for(session)
        bars = self._resampled(s, period, span, session)
        if bars.empty:
            return {"ticker": s.ticker, "rows": []}
        ts = epoch_ms(bars.index)
        i = len(ts) - 1 if at_ms is None else max(0, int(np.searchsorted(ts, int(at_ms), side="right")) - 1)
        start = bars.index[i]
        end = bar_end(start, period, span, utc=utc_days(s))
        lo, hi = int(ts[i]), int(end.value // 1_000_000)
        bar = bars.iloc[i]
        # The day the bar is in (as the daily bars count days), from its daily bar.
        d0 = calendar_key(pd.DatetimeIndex([start]), "day", utc=utc_days(s))[0]
        days = self._resampled(s, "day", 1, session)
        hit = days[days.index == d0]
        day = None if hit.empty else {"from": int(epoch_ms(pd.DatetimeIndex([d0]))[0]),
                                      **{k: float(hit[k].iloc[0]) for k in ("open", "high", "low", "close", "volume")}}
        span_px = float(bar["high"] - bar["low"])
        if step is not None and step > 0 and span_px / step > self.PROFILE_MAX_ROWS:
            step = nice_step(span_px, self.PROFILE_MAX_ROWS)  # a segment too fine for this range
        rows, basis, derived = None, None, None
        if hi - lo <= self.PROFILE_TICK_MS and s.asset != "future":  # adjusted futures prices are not the contract's
            for feed in (self.sources_for(s.ticker) if source == MERGED else [source]):
                with self._lock:
                    t = self.store.trades_between(feed, s.ticker, lo * 1_000_000, hi * 1_000_000)
                if len(t):
                    rows, summed = from_trades(t["price"].to_numpy(), t["size"].to_numpy(), step)
                    basis = f"{len(t):,} trade{'' if len(t) == 1 else 's'} on {feed}"
                    used_step = step or (nice_step(float(t["price"].max() - t["price"].min())) if summed else None)
                    derived = f"summed into {_money(used_step)} price segments" if summed else None
                    step = used_step
                    break
        if rows is None:
            if self._windowed(s, period) or (s.fine_spans and hi - lo <= self.PROFILE_TICK_MS
                                             and BASE_SECONDS[s.kind] >= PERIOD_SECONDS[period] * span):
                sub, unit = self._fine_bars(s, 1, session, lo, hi), "1-second bars"
            else:
                sub = s.frame[(s.frame.index >= start) & (s.frame.index < end)]
                unit = "trades" if s.kind == "trades" else f"1-{s.kind} bars"
            rows, step = from_bars(sub["low"].to_numpy(), sub["high"].to_numpy(), sub["volume"].to_numpy(),
                                   sub["trades"].to_numpy(), step)
            basis = f"{len(sub):,} {unit}"
            derived = (f"estimated from {unit}: each one's volume and trades spread evenly across its range, "
                       f"in {_money(step)} price segments")
        return {
            "ticker": s.ticker, "period": period, "span": span,
            "bar": {"from": lo, "to": hi, **{k: float(bar[k]) for k in ("open", "high", "low", "close", "volume")},
                    "trades": float(bar["trades"])},
            "day": day, "last": float(bars["close"].iloc[-1]), "newest": i == len(ts) - 1,
            "rows": json.loads(rows.to_json(orient="records")) if len(rows) else [],
            "step": step, "basis": basis, "derived": derived,
            "note": "Traded volume by price, not an order book: what changed hands, not what was resting.",
        }

    def _modelled(self, s: Series, bars: pd.DataFrame, period: str, span: int, session: str, model: Model) -> pd.DataFrame:
        # the bars' own span too: a range's window of seconds is not the chart's latest day
        edge = (int(epoch_ms(bars.index[:1])[0]), int(epoch_ms(bars.index[-1:])[0]), len(bars)) if len(bars) else ()
        key = ("model", s.source, s.ticker, s.adjust, period, span, session, model, edge, s.fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key]
        out = apply_model(bars, model, PERIOD_SECONDS[period] * span)
        with self._lock:
            self._bars[key] = out
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return out

    @staticmethod
    def _page(all_bars: pd.DataFrame, to_ms: int | None, since_ms: int | None, limit: int,
              after_ms: int | None = None) -> dict:
        """The newest `limit` bars before `to_ms` (or of all), the oldest `limit` after `after_ms`,
        or all from `since_ms`; `more`: older ones exist, `newer`: newer ones exist."""
        ts = epoch_ms(all_bars.index)
        newer = False
        if since_ms is not None:
            sel, more = all_bars[ts >= since_ms], False
        elif after_ms is not None:
            later = all_bars[ts > after_ms]
            sel, more, newer = later.head(limit), True, len(later) > limit
        elif to_ms is not None:
            older = all_bars[ts < to_ms]
            sel, more, newer = older.tail(limit), len(older) > limit, bool((ts >= to_ms).any())
        else:
            sel, more = all_bars.tail(limit), len(all_bars) > limit
        return {"bars": to_records(sel), "more": bool(more), "newer": bool(newer)}

    def range_stats(
        self, source: str, ticker: str, period: str, span: int, session: str, from_ms: int, to_ms: int,
        adjust: str = "none", model: Model = Model(),
    ) -> dict:
        """Statistics over the bars whose start lies in [from_ms, to_ms] (either order),
        computed from exactly the bars the chart shows (in the model's units)."""
        s = self.series(source, ticker, adjust)
        if s is None:
            raise LookupError(f"no data stored for {source}:{ticker}")
        session = s.session_for(session)
        lo_ms, hi_ms = sorted((int(from_ms), int(to_ms)))
        bars = self._fine_bars(s, span, session, lo_ms, hi_ms + 1) if self._windowed(s, period)             else self._resampled(s, period, span, session)
        if not model.validate().is_price:
            bars = self._modelled(s, bars, period, span, session, model)
        return summarize_range(bars, lo_ms, hi_ms, model)

    @staticmethod
    def _windowed(s: Series, period: str) -> bool:
        """Second intervals of a minute-based series come from its second-bar layer, by window."""
        return period == "second" and bool(s.fine_spans) and s.kind not in ("trades", "second")

    def _fine_bars(self, s: Series, span: int, session: str, lo_ms: int, hi_ms: int) -> pd.DataFrame:
        with self._lock:
            raw = self.store.bars_window(s.fine_sources, s.ticker, "second", lo_ms, hi_ms)
        if raw.empty:
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=ET))
        out = resample(frame_from_bars(raw), "second", span, session)
        return out if span == 1 else out.assign(derived=True)  # 30 s from seconds: built here

    def _fine_window(self, s: Series, span: int, session: str, to_ms: int | None, since_ms: int | None,
                     limit: int) -> tuple[pd.DataFrame, bool, int]:
        """The newest `limit` second bars before `to_ms` (or all since `since_ms`) from the layer,
        whether older ones remain, and how many the window held."""
        interp = s.ticker in INTERPOLATED and not s.frame.empty
        first = min(a for a, _ in s.fine_spans)
        if interp:
            first = min(first, int(epoch_ms(s.frame.index[:1])[0]))
        last = max(b for _, b in s.fine_spans)
        if since_ms is not None:
            bars = self._fine_bars(s, span, session, since_ms, last + 1000)
            return bars, False, int(len(bars))
        hi = min(to_ms, last + 1000) if to_ms is not None else last + 1000
        width = max(3_600_000, limit * span * 1000 * 4)
        while True:
            lo = max(first, hi - width)
            bars = self._fine_bars(s, span, session, lo, hi)
            if interp:
                bars = self._with_interpolated(s, bars, span, lo, hi)
            if len(bars) >= limit or lo <= first:
                break
            width *= 4
        return bars.tail(limit), len(bars) > limit or lo > first, int(len(bars))

    FINE_LOD_ROWS = 1_000_000  # second bars read for one page of a zoomed-out view, at most

    def _fine_lod_page(self, s: Series, span: int, session: str, to_ms: int | None, since_ms: int | None,
                       limit: int, lod: int) -> dict:
        """A zoomed-out page of second bars: each drawn bar the exact aggregate of `lod` x `span`
        seconds of the clock, read back page by page like the zoomed-in view (it used to show the
        layer's last day only, with nothing older to scroll to). Buckets are aligned on the clock,
        so pages meet without overlap; a page's oldest bucket, cut by the window, is left for the
        next page whole."""
        step = lod * span * 1000
        if since_ms is not None:
            since_ms = since_ms // step * step  # the newest bucket whole, not from mid-bucket
        if to_ms is not None:
            to_ms = to_ms // step * step
        raw, more, _ = self._fine_window(s, span, session, to_ms, since_ms, min(limit * lod, self.FINE_LOD_ROWS))
        if raw.empty:
            return {"bars": [], "more": bool(more), "total": 0, "fine": True}
        keys = epoch_ms(raw.index) // step * step
        g = raw.groupby(keys, sort=True)
        out = pd.DataFrame({
            "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
            "volume": g["volume"].sum(), "turnover": g["turnover"].sum(), "trades": g["trades"].sum(),
        })
        if "derived" in raw:
            out["derived"] = g["derived"].max()
        out.index = pd.to_datetime(out.index, unit="ms", utc=True).tz_convert(ET)
        if more and len(out) > 1:
            out = out.iloc[1:]  # the oldest bucket may be cut: the next page reads it whole
        return {"bars": to_records(out), "more": bool(more), "total": int(len(out)), "fine": True}

    def _fine_page(self, s: Series, span: int, session: str, to_ms: int | None, since_ms: int | None,
                   limit: int) -> dict:
        """A page of second bars read from the layer: widen the window backwards until it holds
        `limit` bars or the layer's start is reached. For INTERPOLATED symbols the history goes
        on past the layer, interpolated from the minute bars (`_interpolated`)."""
        interp = s.ticker in INTERPOLATED and not s.frame.empty
        sel, more, total = self._fine_window(s, span, session, to_ms, since_ms, limit)
        page = {"bars": to_records(sel), "more": bool(more), "total": total, "fine": True}
        if interp and "interp" in sel and sel["interp"].any():
            page["interpolated"] = (f"no one-second data here: a line interpolated from the {s.kind} bars' "
                                    "volume-weighted prices (derived, not trades)")
        return page

    def _with_interpolated(self, s: Series, bars: pd.DataFrame, span: int, lo: int, hi: int) -> pd.DataFrame:
        """`bars` (real second bars of [lo, hi)) with the time the one-second layer does not cover
        filled every `span` seconds by straight lines between the minute bars' volume-weighted
        prices (each at its minute's middle). Marked interpolated and derived; no volume. Minutes
        more than INTERP_GAP_MS apart are a gap: nothing is drawn across it."""
        from .profile_gaps import uncovered

        step = span * 1000
        holes = uncovered((lo, hi), s.fine_spans)
        if not holes:
            return bars
        m = s.frame[(s.frame.index >= pd.Timestamp(lo - INTERP_GAP_MS, unit="ms", tz="UTC"))
                    & (s.frame.index < pd.Timestamp(hi + INTERP_GAP_MS, unit="ms", tz="UTC"))]
        if len(m) < 2:
            return bars
        knots = epoch_ms(m.index) + 30_000  # each minute's middle
        vol = m["volume"].to_numpy(dtype=float)
        price = np.where(vol > 0, m["turnover"].to_numpy(dtype=float) / np.where(vol > 0, vol, 1), m["close"].to_numpy(dtype=float))
        out = []
        for a, b in holes:
            t = np.arange(-(-a // step) * step, b, step, dtype=np.int64)
            t = t[(t >= knots[0]) & (t <= knots[-1])]
            if not len(t):
                continue
            i = np.searchsorted(knots, t, side="right")
            gap = (knots[np.clip(i, 0, len(knots) - 1)] - knots[np.clip(i - 1, 0, len(knots) - 1)]) > INTERP_GAP_MS
            t = t[~gap]
            out.append(pd.DataFrame({"t": t, "v": np.interp(t, knots, price)}))
        if not out:
            return bars
        f = pd.concat(out)
        idx = pd.to_datetime(f["t"].to_numpy(), unit="ms", utc=True).tz_convert(ET)
        filled = pd.DataFrame({"open": f["v"].to_numpy(), "high": f["v"].to_numpy(), "low": f["v"].to_numpy(),
                               "close": f["v"].to_numpy(), "volume": 0.0, "turnover": 0.0, "trades": 0,
                               "derived": True, "interp": True}, index=idx)
        if bars.empty:
            return filled
        bars = bars.assign(interp=False)
        return pd.concat([bars, filled]).sort_index(kind="stable")

    def _with_sent(self, s: Series, bars: pd.DataFrame, period: str, span: int) -> tuple[pd.DataFrame, bool]:
        """Original data is prime: where a feed sent bars of this very interval (Alpaca's 5-minute
        and hourly bars, say), they take the place of the ones built here at the same times (the
        best-ranked feed's first), unmarked. Only times the chart's own bins have: a feed's clock
        hours do not land on a stock chart's hours from the open, so those stay built; and no
        bars are added where the finer data has none (a gap stays a gap). Returns (bars, whether any
        came from a feed: then every bar carries its own mark)."""
        if bars.empty:
            return bars, False
        feeds = [f for f in (self.sources_for(s.ticker) if s.source == MERGED else [s.source]) if f not in MODEL_SOURCES]
        start = bars.index[0].date()
        parts = []
        with self._lock:
            for feed in feeds:
                df = self.store.load_bars([s.ticker], period, span, start=start, source=feed)
                if len(df):
                    df = frame_from_bars(df)
                    parts.append(df[~df["derived"]])
        sent = merge_frames(parts) if parts else None
        common = bars.index.intersection(sent.index) if sent is not None else []
        if not len(common):
            return bars, False
        bars = bars.assign(derived=True)  # built here from finer bars ...
        bars.loc[common, COLUMNS] = sent.loc[common, COLUMNS].to_numpy()
        bars.loc[common, "derived"] = False  # ... except where the feed sent this interval
        return bars, True

    def _resampled(self, s: Series, period: str, span: int, session: str) -> pd.DataFrame:
        if self._windowed(s, period):  # whole-series uses (projection, chart types): the latest day of seconds
            last = max(b for _, b in s.fine_spans)
            return self._fine_bars(s, span, session, last - 86_400_000, last + 1000)
        fp = s.fp
        key = (s.source, s.ticker, s.adjust, period, span, session, fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key]
        official = s.daily if s.daily is not None and not s.daily.empty else (
            s.frame if s.kind == "day" and not s.frame.empty else None)  # a feed of daily bars only
        if period in CALENDAR and official is not None:
            # Official daily bars (closing auction included); weeks/months group those. Days after the
            # last official one (today's, growing as the session goes; days the feed has not published)
            # are built from the finer data, on the same day boundaries.
            daily = official.copy()
            daily.index = calendar_key(daily.index, "day", utc=utc_days(s), session_hours=session_hours(s))
            built = self._built_days(s, session).assign(derived=True)
            daily = pd.concat([daily, built[built.index > daily.index.max()]])
            if s.asset in ("stock", "index"):  # no session on weekends/holidays: stray feed rows
                daily = daily[[_regular_close(d.date()) is not None for d in daily.index]]
            bars = daily if period == "day" else _aggregate(daily.groupby(calendar_key(daily.index, period, utc=utc_days(s))))
            sent = period == "day" and s.source not in MODEL_SOURCES  # the composite's days are a model's
        else:
            bars = resample(s.frame, period, span, session, align_hours_to_open=s.asset in ("stock", "index"),
                            utc_days=utc_days(s), session_hours=session_hours(s))
            sent = period == s.kind and span == 1 and s.source not in MODEL_SOURCES  # a feed's own interval
            if not sent and period in ("second", "minute", "hour"):
                bars, sent = self._with_sent(s, bars, period, span)
        bars = bars.assign(derived=bars["derived"].astype(bool) if sent and "derived" in bars else True)
        with self._lock:
            self._bars[key] = bars
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return bars
