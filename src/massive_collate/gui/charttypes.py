"""Chart types that transform bars: TradingView's Heikin Ashi, Renko, Line break, Kagi,
Point & figure and Range.

Each function takes time bars (indexed by time; columns open/high/low/close/volume/
turnover/trades) and returns bars in the same shape plus any extra columns the
renderer needs. They run over the whole history, so results are path-correct.

Price-based types can produce several bars from one source bar, and ticks can share
a timestamp; every output bar gets a distinct, increasing time (the source time, or
1 ms after the previous output bar). `src_time` keeps the true source time.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume", "turnover", "trades"]
EPS = 1e-9


def atr(bars: pd.DataFrame, length: int = 14) -> float:
    """Average true range (Wilder) at the last bar, as TradingView uses for default box sizes."""
    if bars.empty:
        return 0.0
    prev_close = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - prev_close).abs(),
                    (bars["low"] - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / length, adjust=False).mean().iloc[-1])


def round_box(box: float, precision: int) -> float:
    step = 10.0 ** -precision
    return max(step, round(box / step) * step)


def _clean(v: float) -> float:
    return round(float(v), 9)


class _Out:
    """Collects output bars with distinct increasing timestamps and carried-over volume."""

    def __init__(self, extra: tuple[str, ...] = ()):
        self.rows: list[dict] = []
        self.index: list[pd.Timestamp] = []
        self.extra = extra
        self.vol = self.turn = 0.0
        self.trades = 0

    def add_volume(self, v, tv, n) -> None:
        self.vol += float(v)
        self.turn += float(tv)
        self.trades += int(n)

    def emit(self, t: pd.Timestamp, o: float, c: float, high: float | None = None, low: float | None = None, **extra):
        stamp = t if not self.index or t > self.index[-1] else self.index[-1] + pd.Timedelta(milliseconds=1)
        # Box levels are built by repeated addition; drop the floating-point noise
        # (e.g. 158.85140000000018 -> 158.8514) without losing real precision.
        o, c = _clean(o), _clean(c)
        high = max(o, c) if high is None else _clean(high)
        low = min(o, c) if low is None else _clean(low)
        self.rows.append({"open": o, "high": high, "low": low, "close": c,
                          "volume": self.vol, "turnover": self.turn, "trades": self.trades,
                          "src_time": t, **extra})
        self.index.append(stamp)
        self.vol = self.turn = 0.0
        self.trades = 0

    def frame(self) -> pd.DataFrame:
        cols = COLUMNS + ["src_time", *self.extra]
        if not self.rows:
            return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="America/New_York"))
        return pd.DataFrame(self.rows, index=pd.DatetimeIndex(self.index))[cols]


def _rows(bars: pd.DataFrame):
    return zip(bars.index, bars["open"].to_numpy(float), bars["high"].to_numpy(float),
               bars["low"].to_numpy(float), bars["close"].to_numpy(float),
               bars["volume"], bars["turnover"], bars["trades"])


# -- Heikin Ashi ----------------------------------------------------------------

def heikin_ashi(bars: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    ha_close = (o + h + l + c) / 4
    ha_open = np.empty_like(ha_close)
    if len(ha_open):
        ha_open[0] = (o[0] + c[0]) / 2
        for i in range(1, len(ha_open)):
            ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2
    out = bars.copy()
    out["open"], out["close"] = ha_open, ha_close
    out["high"] = np.maximum.reduce([h, ha_open, ha_close])
    out["low"] = np.minimum.reduce([l, ha_open, ha_close])
    return out


# -- Renko ------------------------------------------------------------------------

def renko(bars: pd.DataFrame, box: float) -> pd.DataFrame:
    """Traditional Renko on closes. Bricks are `box` tall on a grid anchored at the first
    close; continuing needs one box beyond the last brick, reversing needs two."""
    if box <= 0:
        raise ValueError("box size must be positive")
    _check_bricks(bars["close"].to_numpy(dtype=float) if not bars.empty else np.array([]), box)
    out = _Out()
    if bars.empty:
        return out.frame()
    ref = math.floor(float(bars["close"].iloc[0]) / box + EPS) * box
    last = None  # (open, close) of the last brick
    for t, _o, _h, _l, c, v, tv, n in _rows(bars):
        out.add_volume(v, tv, n)
        while True:
            if last is None:
                if c >= ref + box - EPS:
                    brick = (ref, ref + box)
                elif c <= ref - box + EPS:
                    brick = (ref, ref - box)
                else:
                    break
            else:
                lo, lc = last
                if lc > lo:  # up trend
                    brick = (lc, lc + box) if c >= lc + box - EPS else (lo, lo - box) if c <= lo - box + EPS else None
                else:
                    brick = (lc, lc - box) if c <= lc - box + EPS else (lo, lo + box) if c >= lo + box - EPS else None
                if brick is None:
                    break
            out.emit(t, *brick)
            last = brick
    return out.frame()


# -- Line break -----------------------------------------------------------------

def line_break(bars: pd.DataFrame, lines: int = 3) -> pd.DataFrame:
    """N-line break on closes: a new line extends the trend on a new close beyond the last
    line; a reversal must break the extreme of the last N lines."""
    out = _Out()
    if bars.empty:
        return out.frame()
    first_open = float(bars["open"].iloc[0])
    for t, _o, _h, _l, c, v, tv, n in _rows(bars):
        out.add_volume(v, tv, n)
        if not out.rows:
            if c != first_open:
                out.emit(t, first_open, c)
            continue
        last = out.rows[-1]
        up = last["close"] > last["open"]
        recent = out.rows[-lines:]
        hi, lo = max(r["high"] for r in recent), min(r["low"] for r in recent)
        if (up and c > last["close"]) or (not up and c < last["close"]):
            out.emit(t, last["close"], c)
        elif (up and c < lo) or (not up and c > hi):
            out.emit(t, last["open"], c)
    return out.frame()


# -- Kagi --------------------------------------------------------------------------

def kagi(bars: pd.DataFrame, reversal: float) -> pd.DataFrame:
    """Kagi on closes. Each row is one vertical line from open to close.

    Thickness: the line turns thick (yang) when it rises above the previous shoulder
    and thin (yin) when it falls below the previous waist. `yang_start`/`yang` give the
    thickness at each end and `switch` the price where it changed (NaN if it did not).
    """
    if reversal <= 0:
        raise ValueError("reversal amount must be positive")
    out = _Out(("yang_start", "yang", "switch"))
    if bars.empty:
        return out.frame()
    closes = bars["close"].to_numpy(dtype=float)
    start = end = closes[0]
    direction = 0
    yang = yang_start = True
    switch = math.nan
    shoulder, waist = math.inf, -math.inf
    seg_time = bars.index[0]

    def update_thickness():
        nonlocal yang, switch
        if not yang and end > shoulder:
            yang, switch = True, shoulder
        elif yang and end < waist:
            yang, switch = False, waist

    for t, _o, _h, _l, c, v, tv, n in _rows(bars):
        out.add_volume(v, tv, n)
        if direction == 0:
            if abs(c - start) >= reversal - EPS:
                direction, end = (1 if c > start else -1), c
                yang = yang_start = direction > 0
            continue
        if (direction > 0 and c > end) or (direction < 0 and c < end):
            end = c
            update_thickness()
        elif abs(c - end) >= reversal - EPS:
            out.emit(seg_time, start, end, yang_start=yang_start, yang=yang, switch=switch)
            if direction > 0:
                shoulder = end
            else:
                waist = end
            start, end, direction, seg_time = end, c, -direction, t
            yang_start, switch = yang, math.nan
            update_thickness()
    if direction != 0:
        out.emit(seg_time, start, end, yang_start=yang_start, yang=yang, switch=switch)
    return out.frame()


# -- Point & figure ---------------------------------------------------------------

def point_figure(bars: pd.DataFrame, box: float, reversal: int = 3) -> pd.DataFrame:
    """Point & figure (high/low method). Each row is a column of X (`up`) or O boxes;
    `low`/`high` are the lowest and highest box levels in the column."""
    if box <= 0 or reversal < 1:
        raise ValueError("box must be positive and reversal at least 1")
    out = _Out(("up",))
    if bars.empty:
        return out.frame()
    col_lo = col_hi = None
    up = None
    col_time = bars.index[0]

    def emit():
        o, c = (col_lo, col_hi) if up else (col_hi, col_lo)
        out.emit(col_time, o, c, high=col_hi, low=col_lo, up=bool(up))

    for t, o, h, l, c, v, tv, n in _rows(bars):
        out.add_volume(v, tv, n)
        hi_box = math.floor(h / box + EPS) * box
        lo_box = math.ceil(l / box - EPS) * box
        if up is None:
            col_lo = lo_box if col_lo is None else min(col_lo, lo_box)
            col_hi = hi_box if col_hi is None else max(col_hi, hi_box)
            if col_hi - col_lo >= box - EPS:
                up = c >= o
            continue
        if up:
            if hi_box > col_hi + EPS:
                col_hi = hi_box
            elif col_hi - lo_box >= reversal * box - EPS:
                emit()
                up, col_time, col_hi, col_lo = False, t, col_hi - box, lo_box
        else:
            if lo_box < col_lo - EPS:
                col_lo = lo_box
            elif hi_box - col_lo >= reversal * box - EPS:
                emit()
                up, col_time, col_lo, col_hi = True, t, col_lo + box, hi_box
    if up is not None:
        emit()
    return out.frame()


# -- Range bars -----------------------------------------------------------------

MAX_BRICKS = 200_000  # more would be a page no chart can draw and a server's memory gone


def _check_bricks(prices: np.ndarray, box: float) -> None:
    """Refuse a box so small that the moves would make more than MAX_BRICKS bricks."""
    if len(prices) > 1 and float(np.abs(np.diff(prices)).sum()) / box > MAX_BRICKS:
        raise ValueError(f"box size too small for this range: more than {MAX_BRICKS:,} bricks; choose a larger box")


def range_bars(fine: pd.DataFrame, size: float) -> pd.DataFrame:
    """Bars that each span exactly `size` from high to low; each opens at the previous close.

    Exact when `fine` is ticks. From bars, the order of high and low inside a bar is
    unknown; the path is assumed open -> nearer extreme -> farther extreme -> close.
    """
    if size > 0 and not fine.empty:
        _check_bricks(fine["close"].to_numpy(dtype=float), size)
    if size <= 0:
        raise ValueError("range size must be positive")
    out = _Out()
    cur = None

    def path(o, h, l, c):
        return (o, l, h, c) if abs(o - l) < abs(o - h) else (o, h, l, c)

    for t, o, h, l, c, v, tv, n in _rows(fine):
        for p in path(o, h, l, c):
            if cur is None:
                cur = {"open": p, "high": p, "low": p}
            while True:
                if p > cur["high"] and p - cur["low"] >= size - EPS:
                    close = cur["low"] + size
                    out.emit(t, cur["open"], close, high=close, low=cur["low"])
                elif p < cur["low"] and cur["high"] - p >= size - EPS:
                    close = cur["high"] - size
                    out.emit(t, cur["open"], close, high=cur["high"], low=close)
                else:
                    cur["high"], cur["low"] = max(cur["high"], p), min(cur["low"], p)
                    cur["close"] = p
                    break
                cur = {"open": close, "high": close, "low": close}
        out.add_volume(v, tv, n)
    if cur is not None and "close" in cur:
        out.emit(fine.index[-1], cur["open"], cur["close"], high=cur["high"], low=cur["low"])
    return out.frame()


TRANSFORMS = ("heikin_ashi", "renko", "line_break", "kagi", "point_figure", "range")
