"""Mathematical models of a price series: 1st and 2nd derivatives and the integral.

Market data is discrete, noisy and has gaps, so:

- Derivatives use a Savitzky-Golay filter: a polynomial is least-squares fitted over a
  sliding window of bars and differentiated exactly. It smooths without the lag of a
  moving average and is exact for polynomials up to the fit order. Edges use the fit
  of the first/last full window.
- The series is split into segments at gaps (overnight, weekends, halts) so a gap never
  appears as a spike. Within a segment, bars are taken as evenly spaced at the chart
  interval, and results are scaled to the chosen time unit.
- Basis "log" works on ln(price) and reports in percent, comparable across symbols and
  price levels.
- The integral is the trapezoid area of (basis - first value of the series) over time,
  accumulated across segments; gaps contribute nothing (the market was closed).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

MODELS = ("price", "d1", "d2", "integral")
UNITS_SECONDS = {"bar": None, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True)
class Model:
    kind: str = "price"  # price | d1 | d2 | integral
    basis: str = "price"  # price | log
    per: str = "hour"  # bar | minute | hour | day: time unit of slopes and areas
    window: int = 9  # Savitzky-Golay window (bars, odd)
    order: int = 3  # Savitzky-Golay polynomial order
    reset_gaps: bool = True  # treat gaps as breaks

    def validate(self) -> "Model":
        if self.kind not in MODELS:
            raise ValueError(f"model must be one of {MODELS}")
        if self.basis not in ("price", "log"):
            raise ValueError("basis must be price or log")
        if self.per not in UNITS_SECONDS:
            raise ValueError(f"per must be one of {tuple(UNITS_SECONDS)}")
        if self.window < 3 or self.window % 2 == 0 or self.window > 501:
            raise ValueError("window must be an odd number of bars from 3 to 501")
        need = 2 if self.kind == "d2" else 1
        if not need <= self.order < self.window:
            raise ValueError(f"order must be at least {need} and less than the window")
        return self

    @property
    def is_price(self) -> bool:
        return self.kind == "price"

    def unit_label(self) -> str:
        base = "%" if self.basis == "log" else "price"
        per = self.per
        return {"d1": f"{base}/{per}", "d2": f"{base}/{per}²", "integral": f"{base}·{per}"}.get(self.kind, base)


def savgol_coefficients(window: int, order: int, deriv: int) -> np.ndarray:
    """Convolution weights giving the `deriv`-th derivative (per sample) at the window centre."""
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    vander = np.vander(x, order + 1, increasing=True)  # columns: x^0 .. x^order
    pinv = np.linalg.pinv(vander)  # rows: polynomial coefficients
    return pinv[deriv] * math.factorial(deriv)


def savgol_derivative(y: np.ndarray, window: int, order: int, deriv: int) -> np.ndarray:
    """Derivative (per sample) of y. Edges are evaluated from the first/last full window's fit."""
    n = len(y)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    w = min(window, n if n % 2 else n - 1)
    if w < order + 1 or w < 3:
        if deriv == 1 and n >= 2:
            return np.gradient(y)
        return out if n < 3 else np.gradient(np.gradient(y))
    half = w // 2
    coeffs = savgol_coefficients(w, order, deriv)
    out[half:n - half] = np.convolve(y, coeffs[::-1], mode="valid")
    # Edges: fit the first and last full windows and differentiate the polynomial there.
    xs = np.arange(w, dtype=float)
    for lo, idx in ((0, range(0, half)), (n - w, range(n - half, n))):
        poly = np.polynomial.polynomial.polyfit(xs, y[lo:lo + w], order)
        der = np.polynomial.polynomial.polyder(poly, deriv) if deriv else poly
        for i in idx:
            out[i] = np.polynomial.polynomial.polyval(i - lo, der)
    return out


def segments(index: pd.DatetimeIndex, step_seconds: float, reset_gaps: bool) -> np.ndarray:
    """Segment number of each bar: a new segment starts after a gap much longer than the step."""
    if len(index) == 0 or not reset_gaps:
        return np.zeros(len(index), dtype=int)
    t = index.as_unit("s").asi8.astype(float)
    gap = max(step_seconds * 10, 30 * 60) if step_seconds < 86400 else step_seconds * 4
    breaks = np.r_[False, np.diff(t) > gap]
    return np.cumsum(breaks)


def apply_model(bars: pd.DataFrame, model: Model, step_seconds: float) -> pd.DataFrame:
    """The model's value per bar, as OHLC (open=high=low=close=value) so it can be drawn,
    aggregated for zoomed-out views and measured like prices. Volume is kept."""
    model.validate()
    if model.is_price or bars.empty:
        return bars
    close = bars["close"].to_numpy(dtype=float)
    y = np.log(close) * 100 if model.basis == "log" else close
    unit = UNITS_SECONDS[model.per]
    steps_per_unit = 1.0 if unit is None else unit / step_seconds  # bars per time unit
    seg = segments(bars.index, step_seconds, model.reset_gaps)
    value = np.full(len(y), np.nan)
    if model.kind in ("d1", "d2"):
        deriv = 1 if model.kind == "d1" else 2
        for s in np.unique(seg):
            m = seg == s
            value[m] = savgol_derivative(y[m], model.window, model.order, deriv) * steps_per_unit ** deriv
    else:  # integral: trapezoid area of (y - y0) in (basis x time unit), gaps contribute nothing
        base = y - y[0]
        dt = 1.0 / steps_per_unit  # time units per bar
        area = np.zeros(len(y))
        same = np.r_[False, seg[1:] == seg[:-1]]
        area[1:] = np.where(same[1:], (base[1:] + base[:-1]) / 2 * dt, 0.0)
        value = np.cumsum(area)
    out = pd.DataFrame(index=bars.index)
    for col in ("open", "high", "low", "close"):
        out[col] = value
    for col in ("volume", "turnover", "trades"):
        out[col] = bars[col].to_numpy() if col in bars else 0
    return out.dropna(subset=["close"])
