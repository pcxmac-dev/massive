"""Mathematical models of a price series: 1st and 2nd derivatives and the integral.

Market data is discrete, noisy and has gaps, so:

- Derivatives use a Savitzky-Golay filter: a polynomial is least-squares fitted over a
  sliding window of bars and differentiated exactly. It smooths without the lag of a
  moving average and is exact for polynomials up to the fit order.
- Edges: a centred window does not fit in the first and last half window of bars. Taking
  the first/last full window for all of them would draw one polynomial there (the slope
  a parabola, the 2nd derivative a straight line, for hours of the newest bars). Instead
  each edge bar has its own full-length window: on the right the one ending at it (the
  value a live system had at that bar; it becomes the centred value as later bars
  arrive), on the left the one starting at it.
- Two windows. Centred (the default, for viewing): the value at a bar is fitted from the
  bars around it, so it uses later bars and a historical value is not what was knowable
  then. Causal (`Model.causal`, for backtests and signals): the window ends at the bar,
  so each value uses that bar and earlier ones only and never changes when later bars
  arrive; it lags a little (half a window) in exchange. At the start of a segment the
  causal window grows from the first bar and stays empty until it holds order + 1 bars.
- The series is split into segments at gaps (overnight, weekends, halts) so a gap never
  appears as a spike. A centred derivative needs a whole window inside its segment: in a
  segment shorter than the window the fit would be one polynomial through the whole
  segment (a parabola for the slope, a straight line for the 2nd derivative, whatever the
  smoothing), so such a segment has no values rather than a misleading curve. Within a segment, bars are taken as evenly spaced at the chart
  interval, and results are scaled to the chosen time unit.
- Basis "log" works on ln(price) and reports in percent, comparable across symbols and
  price levels.
- The integral is the trapezoid area of (basis - first value of the series) over time,
  accumulated across segments; gaps contribute nothing (the market was closed).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

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
    causal: bool = False  # trailing window only (no look-ahead): for backtests and signals

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


@lru_cache(maxsize=4096)
def savgol_point_coefficients(window: int, order: int, deriv: int, at: int) -> np.ndarray:
    """Weights giving the `deriv`-th derivative (per sample) of the window's fit at its sample
    `at` (0 = oldest, window - 1 = newest)."""
    x = np.arange(window, dtype=float) - at
    pinv = np.linalg.pinv(np.vander(x, order + 1, increasing=True))
    out = pinv[deriv] * math.factorial(deriv)
    out.flags.writeable = False  # shared by the cache
    return out


def savgol_derivative(y: np.ndarray, window: int, order: int, deriv: int) -> np.ndarray:
    """Derivative (per sample) of y. Each edge bar uses its own full-length window: ending at it
    on the right, starting at it on the left (see the module notes)."""
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
    # Right edge: the window ending at the bar; bars before the first full window use its fit.
    start = max(n - half, w - 1)
    out[start:] = np.convolve(y[start - w + 1:], savgol_point_coefficients(w, order, deriv, w - 1)[::-1], mode="valid")
    if start > n - half:
        out[n - half:start] = _fit_at(y[:w], order, deriv, np.arange(n - half, start))
    # Left edge: the window starting at the bar; bars after the last full window's start use its fit.
    end = min(half, n - w + 1)
    out[:end] = np.convolve(y[:end + w - 1], savgol_point_coefficients(w, order, deriv, 0)[::-1], mode="valid")
    if end < half:
        out[end:half] = _fit_at(y[n - w:], order, deriv, np.arange(end, half) - (n - w))
    return out


def _fit_at(window_y: np.ndarray, order: int, deriv: int, xs: np.ndarray) -> np.ndarray:
    """The `deriv`-th derivative of the window's polynomial fit at positions xs (0 = its first sample)."""
    poly = np.polynomial.polynomial.polyfit(np.arange(len(window_y), dtype=float), window_y, order)
    return np.polynomial.polynomial.polyval(xs, np.polynomial.polynomial.polyder(poly, deriv) if deriv else poly)


def savgol_trailing_coefficients(window: int, order: int, deriv: int) -> np.ndarray:
    """Weights giving the `deriv`-th derivative (per sample) at the newest sample of the window:
    samples at x = -(window - 1) .. 0, evaluated at 0."""
    return savgol_point_coefficients(window, order, deriv, window - 1)


def savgol_derivative_causal(y: np.ndarray, window: int, order: int, deriv: int) -> np.ndarray:
    """Derivative (per sample) of y from the window ending at each sample: value i depends on
    y[:i + 1] only. Before a full window, the window grows from the first sample; with fewer
    than order + 1 samples (or 3) the value is NaN."""
    n = len(y)
    out = np.full(n, np.nan)
    if n >= window:
        out[window - 1:] = np.convolve(y, savgol_trailing_coefficients(window, order, deriv)[::-1], mode="valid")
    for i in range(min(n, window - 1)):
        k = i + 1  # samples so far
        if k < max(order + 1, 3):
            continue
        out[i] = savgol_trailing_coefficients(k, order, deriv) @ y[:k]
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
        derivative = savgol_derivative_causal if model.causal else savgol_derivative
        for s in np.unique(seg):
            m = seg == s
            if not model.causal and m.sum() < model.window:
                continue  # shorter than the window: no honest slope here (see the module notes)
            value[m] = derivative(y[m], model.window, model.order, deriv) * steps_per_unit ** deriv
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
