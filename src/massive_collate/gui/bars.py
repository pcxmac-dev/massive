"""Bars for the chart at any interval, built from the finest data stored.

Values are never interpolated or rounded: open/high/low/close are taken from
the underlying ticks or bars, volume and turnover are summed, and empty
intervals are left out (as TradingView does).
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo
from threading import RLock

import numpy as np
import pandas as pd

from ..storage import Store
from ..calc.derivatives import Model, apply_model


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
    })


def regular_session(frame: pd.DataFrame) -> pd.DataFrame:
    """Bars inside US regular trading hours: 09:30-16:00 ET on trading days, 13:00 on early-close
    days (the rest of those afternoons is after-hours trading), nothing on weekends or holidays
    (stray prints a feed sometimes has then are not part of any session)."""
    if frame.empty:
        return frame
    local = frame.index.tz_convert(ET)
    t = local.time
    days = local.normalize()
    unique = days.unique()
    closes = {d: _regular_close(d.date()) for d in unique}
    close = days.map(lambda d: closes[d])
    keep = (t >= REGULAR_OPEN) & np.array([c is not None for c in close])
    keep &= np.array([c is not None and tt < c for tt, c in zip(t, close)])
    return frame[keep]


def _regular_close(d) -> time | None:
    """Regular-session close of a day by the exchange calendar, None when the market is shut."""
    from ..sessions import is_nyse_holiday, nyse_early_close

    if d.weekday() >= 5 or is_nyse_holiday(d):
        return None
    return time(13, 0) if nyse_early_close(d) else REGULAR_CLOSE


def _aggregate(groups) -> pd.DataFrame:
    spec = {"open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "turnover": "sum", "trades": "sum"}
    if "env_high" in getattr(groups, "obj", pd.DataFrame()).columns:  # composite: the deviation across exchanges
        spec.update(env_high="max", env_low="min", venues_n="max", dev_sq="mean")
    out = groups.agg(spec)
    if "dev_sq" in out:
        out["dev_sd"] = np.sqrt(out.pop("dev_sq"))  # root mean square of the bars' deviations
    count = groups["close"].count()
    return out[count > 0]


def resample(
    frame: pd.DataFrame, period: str, span: int = 1, session: str = "regular", align_hours_to_open: bool = True,
) -> pd.DataFrame:
    """Aggregate a fine frame into bars. `session` is "regular" (09:30-16:00 ET) or "extended"."""
    if frame.empty:
        return frame.copy()
    if session == "regular":
        frame = regular_session(frame)
        if frame.empty:
            return frame.copy()
    if period in INTRADAY:
        # US equities: hour bars line up with the 09:30 open, like TradingView. Around-the-clock
        # markets (fx, futures) use clock hours.
        offset = 570 * 60 if period == "hour" and align_hours_to_open else 0
        return _aggregate(frame.groupby(wall_clock_bins(frame.index, period, span, offset)))
    return _aggregate(frame.groupby(calendar_key(frame.index, period)))


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
    out.index = bars.index[::k][: len(out)]
    return out


EXTRA_COLUMNS = ("yang_start", "yang", "switch", "up", "env_high", "env_low", "dev_sd", "venues_n")
# Derived models stored as bars: not feeds of their own in the merged view.
MODEL_SOURCES = ("composite",)


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
            r[c] = None if isinstance(v, float) and np.isnan(v) else (bool(v) if c in ("yang", "yang_start", "up") else v)
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


class BarEngine:
    """Loads series from the store, caches them, and serves pages of bars.

    `source` may be a single feed or MERGED ("*") for all of a ticker's feeds,
    combined in `priority` order (feeds not listed come after, alphabetically).
    """

    def __init__(self, store: Store, cache_size: int = 64, priority: list[str] | None = None, quality=None):
        self.priority = priority or []
        self.quality = quality  # feed -> 0..1, for display priority (best first)
        self.store = store
        self._series: dict[tuple, tuple] = {}
        self._bars: OrderedDict[tuple, pd.DataFrame] = OrderedDict()
        self._cache_size = cache_size
        self._future_cache: OrderedDict = _BoundedCache(16)
        # One SQLite connection is shared by the server's threads: every store access holds this lock.
        self.lock = RLock()
        self._lock = self.lock

    def _watch_item(self, source: str, ticker: str) -> dict | None:
        return next((w for w in self.store.watchlist(source) if w["ticker"] == ticker), None)

    def sources_for(self, ticker: str) -> list[str]:
        """Feeds holding a ticker, best first: a feed with ticks (finer data) for it ranks first,
        then the configured priority."""
        with self._lock:
            stored = [(s, k) for s, t, k in self.store.stored_series() if t == ticker.upper()]
            have = {s for s, _ in stored}
            have |= {w["source"] for w in self.store.watchlist() if w["ticker"] == ticker.upper()
                     and w["asset"] == "future"}  # continuous futures are stored under their contracts
        fine = {s for s, k in stored if k in ("trades", "second")}
        order = {s: i for i, s in enumerate(self.priority)}
        have -= set(MODEL_SOURCES)
        quality = self.quality or (lambda s: 1.0)
        return sorted(have, key=lambda s: (s not in fine, -quality(s), order.get(s, len(order)), s))

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
                fp = tuple(self._series[(p.source, ticker, adjust)][0] for p in parts if p)
            else:
                parts = None
                fp = self._fingerprint(source, ticker)
            key = (source, ticker, adjust)
            cached = self._series.get(key)
            if cached and cached[0] == fp:
                return cached[1]
            if parts is not None:
                s = self._merge(ticker, [p for p in parts if p])
            else:
                s = self._load(source, ticker, adjust)
            if s is not None:
                s.adjust = adjust
            self._series[key] = (fp, s)
            return s

    def _merge(self, ticker: str, parts: list[Series]) -> Series | None:
        if not parts:
            return None
        if len(parts) == 1:
            p = parts[0]
            return Series(MERGED, ticker, p.kind, p.frame, p.daily, p.price_precision, p.volume_precision,
                          p.asset, p.rolls, fine_sources=p.fine_sources, fine_spans=p.fine_spans)
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
        # Official daily bars come from whichever feed holds each day (one feed per span), so they
        # are combined day by day like the intraday bars, best-ranked feed first.
        dailies = [p.daily for p in parts if p.daily is not None and not p.daily.empty]
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
        asset = item["asset"] if item else "stock"
        if asset == "future":
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

    def _load_plain(self, source: str, ticker: str) -> Series | None:
        kinds = {k for s, t, k in self.store.stored_series() if s == source and t == ticker}
        # Minute bars are the base wherever a feed has them (second bars built from its ticks are a
        # layer read by window); a series of ticks or seconds alone is small and loaded whole.
        kind = "minute" if "minute" in kinds else next((k for k in BASE_KINDS if k in kinds), None)
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
        fine = [(a, b) for src, _, a, b in self.store.coverage_detail(ticker, "second") if src == source] \
            if kind == "minute" and "second" in kinds else []
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
        )

    def periods(self, series: Series) -> list[dict]:
        """Intervals that can be built exactly from the stored data (seconds where second bars
        built from ticks exist, read by window)."""
        base = 1 if series.fine_spans else BASE_SECONDS[series.kind]
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
        style: Style = Style(), adjust: str = "none", lod: int = 1, model: Model = Model(),
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
        if self._windowed(s, period) and style.type in TIME_TYPES and model.is_price and lod == 1:
            page = self._fine_page(s, span, session, to_ms, since_ms, limit)
            page["style"] = {"type": style.type}
            return page
        if not 1 <= lod <= MAX_LOD:
            raise ValueError(f"lod must be 1-{MAX_LOD}")
        model.validate()
        if not model.is_price and style.type not in TIME_TYPES:
            raise ValueError("derivatives and integrals apply to time-based chart types")
        all_bars, used = self._styled(s, period, span, session, style)
        if not model.is_price:
            all_bars = self._modelled(s, all_bars, period, span, session, model)
            used = {**used, "model": model.kind, "window": model.window, "basis": model.basis, "per": model.per}
        if lod > 1 and style.type in LOD_TYPES:
            # The chart type and its parameters are part of the key: aggregates of Renko bricks
            # must never be served for line break (or candles) and vice versa.
            key = ("lod", s.source, s.ticker, s.adjust, period, span, session, lod, tuple(sorted(used.items())),
                   self._series[(s.source, s.ticker, s.adjust)][0])
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
        page = self._page(all_bars, to_ms, since_ms, limit)
        page["style"] = used
        page["total"] = int(len(all_bars))
        if not model.is_price:
            page["model"] = {"kind": model.kind, "unit": model.unit_label(),
                             "precision": model_precision(all_bars["close"].to_numpy())}
        if style.type == "range" and s.kind != "trades":
            page["approximate"] = "range bars built from bars, not ticks: intra-bar order of high and low is assumed"
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
        item = next((w for w in self.store.watchlist() if w["ticker"] == s.ticker and w["asset"] == "future"), None)
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
        if style.type not in ct.TRANSFORMS:
            raise ValueError(f"unknown chart type {style.type!r}")
        base = self._resampled(s, period, span, session)
        default = ct.round_box(ct.atr(base), s.price_precision) if not base.empty else 1.0
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
        key = (s.source, s.ticker, s.adjust, period, span, session, tuple(sorted(used.items())),
               self._series[(s.source, s.ticker, s.adjust)][0])
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key], used
        if style.type == "heikin_ashi":
            out = ct.heikin_ashi(base)
        elif style.type == "renko":
            out = ct.renko(base, box)
        elif style.type == "line_break":
            out = ct.line_break(base, style.lines)
        elif style.type == "kagi":
            out = ct.kagi(base, used["reversal"])
        elif style.type == "point_figure":
            out = ct.point_figure(base, box, used["reversal"])
        else:  # range: from the finest data, not the chart interval
            fine = s.frame if session == "extended" else regular_session(s.frame)
            out = ct.range_bars(fine, box)
        with self._lock:
            self._bars[key] = out
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return out, used

    def _modelled(self, s: Series, bars: pd.DataFrame, period: str, span: int, session: str, model: Model) -> pd.DataFrame:
        key = ("model", s.source, s.ticker, s.adjust, period, span, session, model,
               self._series[(s.source, s.ticker, s.adjust)][0])
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
    def _page(all_bars: pd.DataFrame, to_ms: int | None, since_ms: int | None, limit: int) -> dict:
        ts = epoch_ms(all_bars.index)
        if since_ms is not None:
            sel, more = all_bars[ts >= since_ms], False
        elif to_ms is not None:
            older = all_bars[ts < to_ms]
            sel, more = older.tail(limit), len(older) > limit
        else:
            sel, more = all_bars.tail(limit), len(all_bars) > limit
        return {"bars": to_records(sel), "more": bool(more)}

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

    @staticmethod
    def _windowed(s: Series, period: str) -> bool:
        """Second intervals of a minute-based series come from its second-bar layer, by window."""
        return period == "second" and bool(s.fine_spans) and s.kind not in ("trades", "second")

    def _fine_bars(self, s: Series, span: int, session: str, lo_ms: int, hi_ms: int) -> pd.DataFrame:
        with self._lock:
            raw = self.store.bars_window(s.fine_sources, s.ticker, "second", lo_ms, hi_ms)
        if raw.empty:
            return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=ET))
        return resample(frame_from_bars(raw), "second", span, session)

    def _fine_page(self, s: Series, span: int, session: str, to_ms: int | None, since_ms: int | None,
                   limit: int) -> dict:
        """A page of second bars read from the layer: widen the window backwards until it holds
        `limit` bars or the layer's start is reached."""
        first = min(a for a, _ in s.fine_spans)
        last = max(b for _, b in s.fine_spans)
        if since_ms is not None:
            bars = self._fine_bars(s, span, session, since_ms, last + 1000)
            return {"bars": to_records(bars), "more": False, "total": int(len(bars)), "fine": True}
        hi = min(to_ms, last + 1000) if to_ms is not None else last + 1000
        width = max(3_600_000, limit * span * 1000 * 4)
        while True:
            lo = max(first, hi - width)
            bars = self._fine_bars(s, span, session, lo, hi)
            if len(bars) >= limit or lo <= first:
                break
            width *= 4
        sel = bars.tail(limit)
        more = len(bars) > limit or lo > first
        return {"bars": to_records(sel), "more": bool(more), "total": int(len(bars)), "fine": True}

    def _resampled(self, s: Series, period: str, span: int, session: str) -> pd.DataFrame:
        if self._windowed(s, period):  # whole-series uses (projection, chart types): the latest day of seconds
            last = max(b for _, b in s.fine_spans)
            return self._fine_bars(s, span, session, last - 86_400_000, last + 1000)
        fp = self._series[(s.source, s.ticker, s.adjust)][0]
        key = (s.source, s.ticker, s.adjust, period, span, session, fp)
        with self._lock:
            if key in self._bars:
                self._bars.move_to_end(key)
                return self._bars[key]
        if period in CALENDAR and s.daily is not None and not s.daily.empty:
            # Official daily bars (closing auction included); weeks/months group those.
            daily = s.daily.copy()
            daily.index = calendar_key(daily.index, "day")
            if s.asset in ("stock", "index"):  # no session on weekends/holidays: stray feed rows
                daily = daily[[_regular_close(d.date()) is not None for d in daily.index]]
            bars = daily if period == "day" else _aggregate(daily.groupby(calendar_key(daily.index, period)))
        else:
            bars = resample(s.frame, period, span, session, align_hours_to_open=s.asset in ("stock", "index"))
        with self._lock:
            self._bars[key] = bars
            if len(self._bars) > self._cache_size:
                self._bars.popitem(last=False)
        return bars
