"""Trading schemes and their backtest (docs/ARCHITECTURE.md, "Schemes and replay").

A scheme reads bars and decides, at each bar's close, what to hold from the next bar's open: long,
short or flat, with a stop and a target where it sets them. Every decision uses that bar and the
bars before it, never a later one (the tests run every scheme on prefixes of the bars and require
the same decisions), so the same run serves a backtest over a stretch of history and a replay that
reveals the decisions as the bars they were made at close.

`backtest` turns the decisions into trades: filled at the next bar's open; a stop or target inside a
bar at its level (at the open when the bar opens beyond it; the stop first when one bar reaches
both); a fixed quantity from the entry; a fee and slippage in basis points of what is traded at
every entry and exit. It starts flat at the stretch's first bar: what the bars before it decided is
warm-up only. Nothing here places an order anywhere.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

LONG, SHORT, FLAT = 1, -1, 0
SIDES = {LONG: "long", SHORT: "short", FLAT: "flat"}


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    default: float | str
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    about: str = ""

    def read(self, value) -> float | str:
        """The setting from a request (text or number), checked against its bounds."""
        if value is None or value == "":
            return self.default
        if self.choices:
            if str(value) not in self.choices:
                raise ValueError(f"{self.label}: one of {', '.join(self.choices)}")
            return str(value)
        x = float(value)
        if not math.isfinite(x) or (self.lo is not None and x < self.lo) or (self.hi is not None and x > self.hi):
            raise ValueError(f"{self.label}: from {self.lo:g} to {self.hi:g}")
        return int(x) if float(self.default).is_integer() and (self.step or 1) >= 1 else x


@dataclass
class Decision:
    """What a scheme decided at bar `i`'s close: hold `side` from the next bar's open."""
    i: int
    side: int
    stop: float | None = None
    target: float | None = None
    why: str = ""


@dataclass
class Decisions:
    events: list[Decision] = field(default_factory=list)
    lines: dict[str, np.ndarray] = field(default_factory=dict)  # drawn per bar (NaN: none)
    points: list[dict] = field(default_factory=list)  # {"i", "known" (bar index it was confirmed at), "price", "kind"}


@dataclass(frozen=True)
class Bars:
    t: np.ndarray  # bar start, ms
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 - low, as the other columns are named by their initial
    c: np.ndarray

    @classmethod
    def of(cls, frame: pd.DataFrame) -> "Bars":
        f = lambda k: frame[k].to_numpy(dtype=float)  # noqa: E731
        t = pd.DatetimeIndex(frame.index).as_unit("ms").asi8.astype(np.int64)
        return cls(t, f("open"), f("high"), f("low"), f("close"))

    def __len__(self) -> int:
        return len(self.c)

    def head(self, n: int) -> "Bars":
        return Bars(self.t[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n])


# -- indicators (causal) ------------------------------------------------------------------------

def atr(b: Bars, n: int) -> np.ndarray:
    """Wilder's average true range: the mean of the first n true ranges, then each bar adds its
    own with weight 1/n. NaN before the n-th bar."""
    out = np.full(len(b), np.nan)
    if len(b) < n:
        return out
    tr = b.h - b.l
    if len(b) > 1:
        prev = b.c[:-1]
        tr[1:] = np.maximum(tr[1:], np.maximum(np.abs(b.h[1:] - prev), np.abs(b.l[1:] - prev)))
    a = tr[:n].mean()
    out[n - 1] = a
    for i in range(n, len(b)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def prior_range(b: Bars, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Highest high and lowest low of the n bars before each bar (not the bar itself)."""
    hi = pd.Series(b.h).rolling(n, min_periods=n).max().shift(1).to_numpy()
    lo = pd.Series(b.l).rolling(n, min_periods=n).min().shift(1).to_numpy()
    return hi, lo


# -- 1. ATR swing reversal ------------------------------------------------------------------------

def atr_swing(b: Bars, p: dict) -> Decisions:
    """Stop and reverse on swings of more than `mult` ATRs, judged on closes."""
    n, k = int(p["atr"]), float(p["mult"])
    a = atr(b, n)
    out = Decisions(lines={"reversal": np.full(len(b), np.nan)})
    side, ext, ext_i = FLAT, None, None
    hi, hi_i, lo, lo_i = -math.inf, None, math.inf, None  # before the first swing: both extremes
    for i in range(len(b)):
        if math.isnan(a[i]):
            continue
        h, l, c, d = b.h[i], b.l[i], b.c[i], k * a[i]
        if side == FLAT:
            if h > hi:
                hi, hi_i = h, i
            if l < lo:
                lo, lo_i = l, i
            if c < hi - d:  # the first swing down from the high
                side, ext, ext_i = SHORT, l, i
                out.points.append({"i": hi_i, "known": i, "price": hi, "kind": "high"})
                out.events.append(Decision(i, SHORT, why=f"closed {k:g} ATR below the swing high"))
            elif c > lo + d:
                side, ext, ext_i = LONG, h, i
                out.points.append({"i": lo_i, "known": i, "price": lo, "kind": "low"})
                out.events.append(Decision(i, LONG, why=f"closed {k:g} ATR above the swing low"))
        elif side == LONG:
            if h > ext:
                ext, ext_i = h, i
            if c < ext - d:
                out.points.append({"i": ext_i, "known": i, "price": ext, "kind": "high"})
                out.events.append(Decision(i, SHORT, why=f"closed {k:g} ATR below the swing high"))
                side, ext, ext_i = SHORT, l, i
        else:
            if l < ext:
                ext, ext_i = l, i
            if c > ext + d:
                out.points.append({"i": ext_i, "known": i, "price": ext, "kind": "low"})
                out.events.append(Decision(i, LONG, why=f"closed {k:g} ATR above the swing low"))
                side, ext, ext_i = LONG, h, i
        if side == LONG:
            out.lines["reversal"][i] = ext - d
        elif side == SHORT:
            out.lines["reversal"][i] = ext + d
    return out


# -- 2. Failed breakout reclaim -------------------------------------------------------------------

def failed_breakout(b: Bars, p: dict) -> Decisions:
    """A breakout of the prior range that a close takes back within `window` bars: the other way."""
    n, m = int(p["lookback"]), int(p["window"])
    depth, buf, wick = float(p["depth"]), float(p["stop"]), p["breakout"] == "wick"
    a = atr(b, int(p["atr"]))
    rh, rl = prior_range(b, n)
    out = Decisions(lines={"range_high": rh.copy(), "range_low": rl.copy()})
    down = up = None  # live attempts: {"level", "far", "ext", "since"}
    for i in range(len(b)):
        if math.isnan(a[i]) or math.isnan(rh[i]):
            continue
        h, l, c, d = b.h[i], b.l[i], b.c[i], depth * a[i]
        fresh_down = fresh_up = False
        if down is None and (l if wick else c) < rl[i] - d:
            down, fresh_down = {"level": rl[i], "far": rh[i], "ext": l, "since": i}, True
        if up is None and (h if wick else c) > rh[i] + d:
            up, fresh_up = {"level": rh[i], "far": rl[i], "ext": h, "since": i}, True
        decided = None
        if down is not None:
            down["ext"] = min(down["ext"], l)
            reclaimed = c > down["level"] and (wick or not fresh_down)
            if reclaimed:
                decided = Decision(i, LONG, stop=down["ext"] - buf * a[i], target=down["far"],
                                   why=f"failed breakdown below {down['level']:.6g}: reclaimed")
                down = None
            elif i - down["since"] >= m:
                down = None  # held below: a breakdown that did not fail
        if up is not None:
            up["ext"] = max(up["ext"], h)
            reclaimed = c < up["level"] and (wick or not fresh_up)
            if reclaimed:
                short = Decision(i, SHORT, stop=up["ext"] + buf * a[i], target=up["far"],
                                 why=f"failed breakout above {up['level']:.6g}: reclaimed")
                decided = None if decided is not None else short  # both at once: neither
                up = None
            elif i - up["since"] >= m:
                up = None
        if decided is not None:
            out.events.append(decided)
    return out


# -- 3. CUSUM change detection --------------------------------------------------------------------

def cusum(b: Bars, p: dict) -> Decisions:
    """Page's two-sided CUSUM of the log returns, each scaled by the volatility before it."""
    k, h, span = float(p["k"]), float(p["h"]), int(p["vol"])
    lam = 1 - 2 / (span + 1)
    r = np.zeros(len(b))
    r[1:] = np.log(b.c[1:] / b.c[:-1])
    out = Decisions(lines={"s_up": np.full(len(b), np.nan), "s_down": np.full(len(b), np.nan)})
    var = None
    s_up = s_down = 0.0
    for i in range(1, len(b)):
        if var is None:
            if i > span:  # the first `span` returns start the variance
                var = float(np.mean(r[1:span + 1] ** 2))
            else:
                continue
        sd = math.sqrt(var)
        if sd > 0:
            z = r[i] / sd
            s_up, s_down = max(0.0, s_up + z - k), max(0.0, s_down - z - k)
            if s_up > h:
                out.events.append(Decision(i, LONG, why=f"CUSUM up passed {h:g}: a shift up"))
                s_up = s_down = 0.0
            elif s_down > h:
                out.events.append(Decision(i, SHORT, why=f"CUSUM down passed {h:g}: a shift down"))
                s_up = s_down = 0.0
        out.lines["s_up"][i], out.lines["s_down"][i] = s_up, s_down
        var = lam * var + (1 - lam) * r[i] ** 2  # this bar's return counts from the next bar on
    return out


# -- 4. ZigZag ----------------------------------------------------------------------------------

def zigzag(b: Bars, p: dict) -> Decisions:
    """Stop and reverse on turns of more than `pct` % from the last extreme, by highs and lows (or by
    closes). A turn is known at the bar that came back that far: the classic zigzag redraws its last
    leg; this one never changes a point once drawn."""
    pct = float(p["pct"]) / 100
    up, dn = (b.c, b.c) if p["basis"] == "close" else (b.h, b.l)
    out = Decisions(lines={"reversal": np.full(len(b), np.nan)})
    side, ext, ext_i = FLAT, 0.0, 0
    hi, hi_i, lo, lo_i = -math.inf, 0, math.inf, 0
    for i in range(len(b)):
        h, l = up[i], dn[i]  # noqa: E741
        if side == FLAT:
            if h > hi:
                hi, hi_i = h, i
            if l < lo:
                lo, lo_i = l, i
            if l <= hi * (1 - pct):
                out.points.append({"i": hi_i, "known": i, "price": float(hi), "kind": "high"})
                out.events.append(Decision(i, SHORT, why=f"{pct * 100:g}% down from the high"))
                side, ext, ext_i = SHORT, l, i
            elif h >= lo * (1 + pct):
                out.points.append({"i": lo_i, "known": i, "price": float(lo), "kind": "low"})
                out.events.append(Decision(i, LONG, why=f"{pct * 100:g}% up from the low"))
                side, ext, ext_i = LONG, h, i
        elif side == LONG:
            if h > ext:
                ext, ext_i = h, i
            if l <= ext * (1 - pct):
                out.points.append({"i": ext_i, "known": i, "price": float(ext), "kind": "high"})
                out.events.append(Decision(i, SHORT, why=f"{pct * 100:g}% down from the high"))
                side, ext, ext_i = SHORT, l, i
        else:
            if l < ext:
                ext, ext_i = l, i
            if h >= ext * (1 + pct):
                out.points.append({"i": ext_i, "known": i, "price": float(ext), "kind": "low"})
                out.events.append(Decision(i, LONG, why=f"{pct * 100:g}% up from the low"))
                side, ext, ext_i = LONG, h, i
        if side != FLAT:
            out.lines["reversal"][i] = ext * (1 - pct) if side == LONG else ext * (1 + pct)
    return out


# -- 5. Williams fractals, 6. pivot highs and lows -------------------------------------------------

def smma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothed moving average: the mean of the first n, then each value with weight 1/n."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = float(np.mean(x[:n]))
    out[n - 1] = a
    for i in range(n, len(x)):
        a = (a * (n - 1) + x[i]) / n
        out[i] = a
    return out


def _shifted(x: np.ndarray, k: int) -> np.ndarray:
    """`x` moved k bars later (a line drawn ahead of its data reads only the past)."""
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def alligator(b: Bars) -> dict[str, np.ndarray]:
    """Bill Williams' Alligator on the median price: jaw SMMA 13 moved 8 bars on, teeth SMMA 8 moved
    5, lips SMMA 5 moved 3."""
    med = (b.h + b.l) / 2
    return {"jaw": _shifted(smma(med, 13), 8), "teeth": _shifted(smma(med, 8), 5), "lips": _shifted(smma(med, 5), 3)}


def _pivot_breakouts(b: Bars, left: int, right: int, kind: str, gate: np.ndarray | None = None) -> Decisions:
    """Pivot highs (a high above the `left` highs before it and not below the `right` after it) and
    lows, each known `right` bars after it; long when a close passes the last pivot high, short when
    one passes the last pivot low (each level used once). With `gate`, a pivot high counts only above
    it and a pivot low only below it (the Alligator's teeth for Williams' fractals)."""
    n = len(b)
    out = Decisions(lines={f"{kind}_high": np.full(n, np.nan), f"{kind}_low": np.full(n, np.nan)})
    up = dn = None
    side = FLAT
    for i in range(n):
        j = i - right
        if j - left >= 0:
            if b.h[j] > b.h[j - left:j].max() and b.h[j] >= b.h[j + 1:i + 1].max():
                ok = gate is None or (math.isfinite(gate[j]) and b.h[j] > gate[j])
                out.points.append({"i": j, "known": i, "price": float(b.h[j]),
                                   "kind": f"{kind}_high" if ok else f"{kind}_high_out"})
                if ok:
                    up = float(b.h[j])
            if b.l[j] < b.l[j - left:j].min() and b.l[j] <= b.l[j + 1:i + 1].min():
                ok = gate is None or (math.isfinite(gate[j]) and b.l[j] < gate[j])
                out.points.append({"i": j, "known": i, "price": float(b.l[j]),
                                   "kind": f"{kind}_low" if ok else f"{kind}_low_out"})
                if ok:
                    dn = float(b.l[j])
        c = b.c[i]
        if up is not None and c > up:
            if side != LONG:
                out.events.append(Decision(i, LONG, why=f"closed above the {kind.replace('_', ' ')} high {up:.6g}"))
                side = LONG
            up = None
        elif dn is not None and c < dn:
            if side != SHORT:
                out.events.append(Decision(i, SHORT, why=f"closed below the {kind.replace('_', ' ')} low {dn:.6g}"))
                side = SHORT
            dn = None
        if up is not None:
            out.lines[f"{kind}_high"][i] = up
        if dn is not None:
            out.lines[f"{kind}_low"][i] = dn
    return out


def fractals(b: Bars, p: dict) -> Decisions:
    """Williams fractals: a high above the `n` highs either side (a low below the `n` lows), known n
    bars after it; trade a close beyond the last one, outside the Alligator's teeth when filtered."""
    gate = p["alligator"] == "on"
    lines = alligator(b) if gate else {}
    d = _pivot_breakouts(b, int(p["n"]), int(p["n"]), "fractal", lines.get("teeth"))
    d.lines.update(lines)
    return d


def pivots(b: Bars, p: dict) -> Decisions:
    """Pivot highs and lows (`left` bars before, `right` after): long when a close passes the last
    pivot high, short when one passes the last pivot low."""
    return _pivot_breakouts(b, int(p["left"]), int(p["right"]), "pivot")


# -- 7. Directional change -------------------------------------------------------------------------

def directional_change(b: Bars, p: dict) -> Decisions:
    """Directional-change events on closes: a downturn when a close falls `theta` % from the highest
    close since the last upturn, an upturn the mirror; between them the overshoot. `trend`: hold the
    direction of the last event. `contrarian`: fade an overshoot once it runs `overshoot` × theta past
    its event's price, flat again at the next event."""
    th, fade, k = float(p["theta"]) / 100, p["mode"] == "contrarian", float(p["overshoot"])
    c = b.c
    out = Decisions(lines={"dc_level": np.full(len(b), np.nan)})
    trend, ext, ext_i, at, faded = FLAT, 0.0, 0, 0.0, False
    hi, hi_i, lo, lo_i = -math.inf, 0, math.inf, 0
    for i in range(len(b)):
        x = c[i]
        turned = None
        if trend == FLAT:
            if x > hi:
                hi, hi_i = x, i
            if x < lo:
                lo, lo_i = x, i
            if x <= hi * (1 - th):
                turned, pt = SHORT, {"i": hi_i, "known": i, "price": float(hi), "kind": "high"}
            elif x >= lo * (1 + th):
                turned, pt = LONG, {"i": lo_i, "known": i, "price": float(lo), "kind": "low"}
        elif trend == LONG:
            if x > ext:
                ext, ext_i = x, i
            if x <= ext * (1 - th):
                turned, pt = SHORT, {"i": ext_i, "known": i, "price": float(ext), "kind": "high"}
        else:
            if x < ext:
                ext, ext_i = x, i
            if x >= ext * (1 + th):
                turned, pt = LONG, {"i": ext_i, "known": i, "price": float(ext), "kind": "low"}
        if turned is not None:
            out.points.append(pt)
            trend, ext, ext_i, at, faded = turned, x, i, x, False
            word = "upturn" if turned == LONG else "downturn"
            out.events.append(Decision(i, FLAT if fade else turned,
                                       why=f"{word}: {th * 100:g}% from the extreme" + (" (the fade ends)" if fade else "")))
        elif fade and not faded and trend != FLAT and (x >= at * (1 + k * th) if trend == LONG else x <= at * (1 - k * th)):
            out.events.append(Decision(i, -trend, why=f"overshoot of {k:g} × {th * 100:g}% past the event: faded"))
            faded = True
        if trend != FLAT:
            out.lines["dc_level"][i] = ext * (1 - th) if trend == LONG else ext * (1 + th)
    return out


# -- 8. Hurst cycles ------------------------------------------------------------------------------------

def hurst(b: Bars, p: dict) -> Decisions:
    """Hurst's future line of demarcation (FLD): the median price moved half a cycle on (it reads only
    the past). Long when a close crosses above it, short when one crosses below. Troughs: the lowest
    low within half a cycle either side, known half a cycle after it; the next is due a cycle on."""
    n = int(p["cycle"])
    half = max(1, n // 2)
    fld = _shifted((b.h + b.l) / 2, half)
    out = Decisions(lines={"fld": fld})
    side = FLAT
    for i in range(1, len(b)):
        j = i - half
        if j - half >= 0 and b.l[j] < b.l[j - half:j].min() and b.l[j] <= b.l[j + 1:i + 1].min():
            out.points.append({"i": j, "known": i, "price": float(b.l[j]), "kind": "trough", "due_bars": n})
        if math.isnan(fld[i]) or math.isnan(fld[i - 1]):
            continue
        if b.c[i] > fld[i] and b.c[i - 1] <= fld[i - 1] and side != LONG:
            out.events.append(Decision(i, LONG, why=f"crossed above the {n}-bar FLD"))
            side = LONG
        elif b.c[i] < fld[i] and b.c[i - 1] >= fld[i - 1] and side != SHORT:
            out.events.append(Decision(i, SHORT, why=f"crossed below the {n}-bar FLD"))
            side = SHORT
    return out


@dataclass(frozen=True)
class Scheme:
    key: str
    label: str
    about: str
    params: tuple[Param, ...]
    run: Callable[[Bars, dict], Decisions]
    warmup: Callable[[dict], int]

    def settings(self, given: dict) -> dict:
        return {p.key: p.read(given.get(p.key)) for p in self.params}


ATR_PARAM = Param("atr", "ATR length", 14, 2, 500, 1, about="bars in Wilder's average true range")

SCHEMES: dict[str, Scheme] = {s.key: s for s in (
    Scheme("atr_swing", "ATR swing reversal",
           "Stop and reverse: short when a close falls mult × ATR below the swing's highest high, long when one "
           "rises mult × ATR above its lowest low.",
           (ATR_PARAM, Param("mult", "ATR multiple", 3.0, 0.25, 50, 0.25, about="how far a close must move back")),
           atr_swing, lambda p: max(10 * int(p["atr"]), 200)),
    Scheme("failed_breakout", "Failed breakout reclaim",
           "A close beyond the prior range that a close takes back within the window: long after a failed "
           "breakdown, short after a failed breakout. Stop beyond the failed extreme, target the range's far side.",
           (Param("lookback", "Range bars", 20, 2, 1000, 1, about="bars before each bar that make its range"),
            Param("window", "Reclaim within", 3, 1, 100, 1, about="bars a breakout has to fail"),
            Param("depth", "Breakout depth (ATR)", 0.0, 0, 10, 0.05, about="how far beyond the range counts"),
            Param("stop", "Stop buffer (ATR)", 0.25, 0, 10, 0.05, about="beyond the failed breakout's extreme"),
            ATR_PARAM,
            Param("breakout", "Breakout by", "close", choices=("close", "wick"),
                  about="close: a close beyond the range; wick: a high or low beyond it")),
           failed_breakout, lambda p: int(p["lookback"]) + int(p["window"]) + 5 * int(p["atr"])),
    Scheme("cusum", "CUSUM change detection",
           "Page's two-sided cumulative sum of volatility-scaled returns: long when the upward sum passes h, short "
           "when the downward one does; both start again from zero.",
           (Param("k", "Drift allowance k", 0.5, 0, 10, 0.05, about="scaled return each bar must exceed to count"),
            Param("h", "Threshold h", 5.0, 0.5, 100, 0.5, about="sum that declares a shift"),
            Param("vol", "Volatility span", 50, 5, 5000, 1, about="bars in the volatility average")),
           cusum, lambda p: max(4 * int(p["vol"]), 200)),
    Scheme("zigzag", "ZigZag",
           "Stop and reverse on turns of more than pct % from the last extreme. Each point is known at the bar "
           "that confirmed it and never redrawn.",
           (Param("pct", "Turn (%)", 5.0, 0.05, 90, 0.05, about="how far price must come back from its extreme"),
            Param("basis", "Judged on", "high_low", choices=("high_low", "close"),
                  about="high_low: highs and lows; close: closes only")),
           zigzag, lambda p: 500),
    Scheme("fractals", "Williams fractals",
           "A high above the n highs either side (a low below the n lows), known n bars later; long on a close above "
           "the last fractal high, short below the last fractal low. Filtered: only fractals outside the Alligator's teeth.",
           (Param("n", "Bars either side", 2, 1, 20, 1, about="Williams: 2"),
            Param("alligator", "Alligator filter", "on", choices=("on", "off"),
                  about="on: a buy fractal must be above the teeth, a sell fractal below")),
           fractals, lambda p: 200 + 4 * int(p["n"])),
    Scheme("pivots", "Pivot high/low breakout",
           "Pivot highs and lows (left bars before, right bars after, known right bars later): long on a close above "
           "the last pivot high, short on one below the last pivot low.",
           (Param("left", "Bars before", 5, 1, 500, 1), Param("right", "Bars after", 5, 1, 500, 1)),
           pivots, lambda p: 4 * (int(p["left"]) + int(p["right"])) + 100),
    Scheme("directional_change", "Directional change",
           "Event-based turns: a downturn when a close falls theta % from the high since the last upturn, an upturn "
           "the mirror. Trend: hold the last event's direction. Contrarian: fade the overshoot past an event.",
           (Param("theta", "Threshold θ (%)", 1.0, 0.01, 50, 0.01, about="the move that makes an event"),
            Param("mode", "Trade", "trend", choices=("trend", "contrarian")),
            Param("overshoot", "Fade at overshoot (× θ)", 1.0, 0.1, 20, 0.1, about="contrarian only")),
           directional_change, lambda p: 500),
    Scheme("hurst", "Hurst cycles (FLD)",
           "Hurst's future line of demarcation: the median price moved half a cycle on; long on a close crossing above "
           "it, short below. Troughs confirmed half a cycle late; the next one due a cycle after each.",
           (Param("cycle", "Cycle (bars)", 40, 4, 5000, 2, about="the nominal cycle's length in bars"),),
           hurst, lambda p: 3 * int(p["cycle"])),
)}


# -- backtest -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Costs:
    fee_bps: float = 0.0
    slippage_bps: float = 0.0

    @property
    def rate(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 10_000


@dataclass
class Result:
    trades: list[dict]
    equity: np.ndarray  # at each bar's close, from the stretch's first bar (1.0 before it)
    position: np.ndarray  # held at each bar's close
    stats: dict


def backtest(b: Bars, decisions: Decisions, start: int = 0, costs: Costs = Costs(), long_only: bool = False) -> Result:
    """Trades from the decisions made from bar `start` on (see the module docstring)."""
    n = len(b)
    by_bar = {e.i: e for e in decisions.events if e.i >= start}
    rate = costs.rate
    cash, side, e0, entry_px, entry_i = 1.0, FLAT, 0.0, 0.0, 0
    stop = target = None
    trades: list[dict] = []
    equity = np.ones(max(0, n - start))
    held = np.zeros(max(0, n - start), dtype=int)
    pending: Decision | None = None
    pending_why = ""

    def value(px: float) -> float:
        return max(0.0, e0 * (1 + side * (px / entry_px - 1)))

    def close_trade(i: int, px: float, why: str) -> None:
        nonlocal cash, side, stop, target
        cash = value(px) * (1 - rate)
        trades.append({"side": SIDES[side], "entry_i": entry_i, "entry": entry_px, "exit_i": i, "exit": px,
                       "why": why, "opened_by": trades_open_why, "ret": cash / before_entry - 1, "bars": i - entry_i + 1})
        side, stop, target = FLAT, None, None

    trades_open_why, before_entry = "", 1.0
    for i in range(start, n):
        o, h, l, c = b.o[i], b.h[i], b.l[i], b.c[i]
        if pending is not None:
            want = FLAT if (long_only and pending.side == SHORT) else pending.side
            if side != FLAT and want != side:
                close_trade(i, o, "reversed" if want != FLAT else "exit signal")
            if want != FLAT and side == FLAT:
                before_entry = cash
                side, e0, entry_px, entry_i = want, cash * (1 - rate), o, i
                stop, target, trades_open_why = pending.stop, pending.target, pending_why
            pending = None
        if side == LONG:
            if stop is not None and l <= stop:
                close_trade(i, o if o <= stop else stop, "stop")
            elif target is not None and h >= target:
                close_trade(i, o if o >= target else target, "target")
        elif side == SHORT:
            if stop is not None and h >= stop:
                close_trade(i, o if o >= stop else stop, "stop")
            elif target is not None and l <= target:
                close_trade(i, o if o <= target else target, "target")
        equity[i - start] = value(c) if side != FLAT else cash
        held[i - start] = side
        if i in by_bar:
            pending, pending_why = by_bar[i], by_bar[i].why
    if side != FLAT:  # still open at the stretch's end: marked at its last close
        trades.append({"side": SIDES[side], "entry_i": entry_i, "entry": entry_px, "exit_i": None, "exit": float(b.c[-1]),
                       "why": "open", "opened_by": trades_open_why, "ret": value(b.c[-1]) / before_entry - 1,
                       "bars": n - entry_i})
    return Result(trades, equity, held, _stats(b, start, trades, equity, held))


def _stats(b: Bars, start: int, trades: list[dict], equity: np.ndarray, held: np.ndarray) -> dict:
    if not len(equity):
        return {"bars": 0, "trades": 0}
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    closed = [t["ret"] for t in trades if t["exit_i"] is not None]
    wins, losses = [r for r in closed if r > 0], [r for r in closed if r <= 0]
    return {
        "bars": int(len(equity)),
        "net": float(equity[-1] - 1),
        "buy_hold": float(b.c[-1] / b.o[start] - 1) if b.o[start] else None,
        "max_drawdown": float(min(0.0, (equity / peak - 1).min())),
        "trades": len(trades),
        "long": sum(t["side"] == "long" for t in trades),
        "short": sum(t["side"] == "short" for t in trades),
        "won": (len(wins) / len(closed)) if closed else None,
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": (float(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None),
        "exposure": float(np.mean(held != 0)),
    }


def _due(t: np.ndarray, k: int) -> int:
    """The time of bar k: its own, or past the bars held, counted on at the usual step between bars."""
    if k < len(t):
        return int(t[k])
    step = int(np.median(np.diff(t[-50:]))) if len(t) > 1 else 0
    return int(t[-1] + (k - len(t) + 1) * step)


def run(key: str, frame: pd.DataFrame, start: int, given: dict | None = None, costs: Costs = Costs(),
        long_only: bool = False) -> dict:
    """A scheme over `frame` (bars, oldest first; those before `start` are warm-up), as the chart
    app sends it: settings used, decisions, trades, equity and statistics, times in ms."""
    scheme = SCHEMES.get(key)
    if scheme is None:
        raise ValueError(f"no scheme {key!r}: one of {', '.join(SCHEMES)}")
    p = scheme.settings(given or {})
    b = Bars.of(frame)
    if not 0 <= start < len(b):
        return {"scheme": key, "params": p, "trades": [], "equity": [], "signals": [], "lines": {}, "points": [],
                "stats": {"bars": 0, "trades": 0}}
    d = scheme.run(b, p)
    res = backtest(b, d, start, costs, long_only)
    t = b.t.tolist()
    fmt = lambda x: None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)  # noqa: E731
    return {
        "scheme": key, "params": p,
        "signals": [{"time": t[e.i], "side": SIDES[e.side], "stop": fmt(e.stop), "target": fmt(e.target), "why": e.why}
                    for e in d.events if e.i >= start],
        "trades": [{"side": tr["side"], "entryTime": t[tr["entry_i"]], "entry": tr["entry"],
                    "exitTime": None if tr["exit_i"] is None else t[tr["exit_i"]], "exit": tr["exit"],
                    "signalTime": t[tr["entry_i"] - 1], "why": tr["why"], "openedBy": tr["opened_by"],
                    "ret": tr["ret"], "bars": tr["bars"]} for tr in res.trades],
        "equity": [[t[start + j], float(res.equity[j]), int(res.position[j])] for j in range(len(res.equity))],
        "lines": {name: [[t[i], float(v)] for i, v in enumerate(vals) if i >= start and math.isfinite(v)]
                  for name, vals in d.lines.items()},
        "points": [{"time": t[pt["i"]], "known": t[pt["known"]], "price": float(pt["price"]), "kind": pt["kind"],
                    **({"due": _due(b.t, pt["i"] + pt["due_bars"])} if "due_bars" in pt else {})}
                   for pt in d.points if pt["known"] >= start],
        "stats": res.stats,
    }
