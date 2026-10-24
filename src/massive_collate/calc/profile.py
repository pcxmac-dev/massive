"""Traded volume by price: the distribution of what changed hands inside a span of time.

Used by the virtual level 2 (volume and trades at each price in the current bar, the day's
range) and by box plots (each bar's quartiles of traded price). Neither is an order book: they
are what traded, not what was resting. Where the prices are exact (ticks, each at its own
price) nothing is derived; anything summed into price segments, or estimated from bars, is
derived and is marked as such by the callers:

- From ticks: each trade at its price, exact. Summed into segments (every $0.10, $1 ...) when
  there are too many distinct prices to list: derived (summed).
- From bars: a bar holds no prices between its high and low, so its volume (and trades) are
  spread evenly across its range: derived (estimated).
- Box plots: volume-weighted quartiles of the trades' prices inside each bar (exact from
  ticks), or of the finer bars' average prices (VWAP) inside it (derived).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

EXACT_ROWS = 60  # up to this many distinct traded prices are listed one by one
TARGET_ROWS = 40  # an automatic segment gives about this many rows


def nice_step(span: float, rows: int = TARGET_ROWS) -> float:
    """A round price segment (1, 2, 2.5 or 5 x 10^k) giving about `rows` rows over `span`."""
    if not span or span <= 0 or not math.isfinite(span):
        return 0.01
    raw = span / rows
    k = math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * 10.0**k
        if step >= raw:
            return float(f"{step:.12g}")
    return float(f"{10.0 ** (k + 1):.12g}")


def _segment(price: np.ndarray, step: float) -> np.ndarray:
    """The low edge of each price's segment, rounded to the step's decimals."""
    decimals = max(0, -math.floor(math.log10(step)) + 2)
    return np.round(np.floor(price / step + 1e-9) * step, decimals)


def from_trades(price: np.ndarray, size: np.ndarray, step: float | None = None) -> tuple[pd.DataFrame, bool]:
    """Volume and trades at each price, highest first: (rows, derived). Exact when the trades
    have no more than EXACT_ROWS distinct prices and no segment is asked for; otherwise summed
    into segments of `step` (automatic when None), which is derived."""
    price, size = np.asarray(price, float), np.asarray(size, float)
    if not len(price):
        return pd.DataFrame(columns=["price", "volume", "trades"]), False
    derived = step is not None or len(np.unique(price)) > EXACT_ROWS
    key = _segment(price, step or nice_step(price.max() - price.min())) if derived else price
    rows = (pd.DataFrame({"price": key, "volume": size, "trades": 1}).groupby("price", sort=False)
            .agg(volume=("volume", "sum"), trades=("trades", "sum")).reset_index())
    return rows.sort_values("price", ascending=False, ignore_index=True), derived


def from_bars(low: np.ndarray, high: np.ndarray, volume: np.ndarray, trades: np.ndarray,
              step: float | None = None) -> tuple[pd.DataFrame, float]:
    """Volume and trades by price segment, estimated from bars: each bar's are spread evenly
    across its range (a bar with no range puts them all at its price). Always derived.
    Returns (rows highest first, the step used)."""
    low, high = np.asarray(low, float), np.asarray(high, float)
    volume, trades = np.asarray(volume, float), np.asarray(trades, float)
    if not len(low):
        return pd.DataFrame(columns=["price", "volume", "trades"]), step or 0.01
    step = step or nice_step(high.max() - low.min())
    edges0 = _segment(np.array([low.min()]), step)[0]
    n = int(round((_segment(np.array([high.max()]), step)[0] - edges0) / step)) + 1
    acc_v, acc_t = np.zeros(n), np.zeros(n)
    lo_i = np.clip(np.round((_segment(low, step) - edges0) / step).astype(int), 0, n - 1)
    hi_i = np.clip(np.round((_segment(high, step) - edges0) / step).astype(int), 0, n - 1)
    span = np.maximum(high - low, 0)
    for j in range(len(low)):  # bars x segments covered: small for one window
        a, b = lo_i[j], hi_i[j]
        if a == b or span[j] == 0:
            acc_v[a] += volume[j]
            acc_t[a] += trades[j]
            continue
        seg_lo = edges0 + np.arange(a, b + 1) * step
        overlap = np.clip(np.minimum(seg_lo + step, high[j]) - np.maximum(seg_lo, low[j]), 0, None)
        share = overlap / overlap.sum() if overlap.sum() else np.full(len(overlap), 1 / len(overlap))
        acc_v[a:b + 1] += volume[j] * share
        acc_t[a:b + 1] += trades[j] * share
    prices = np.round(edges0 + np.arange(n) * step, max(0, -math.floor(math.log10(step)) + 2))
    rows = pd.DataFrame({"price": prices, "volume": acc_v, "trades": acc_t})
    rows = rows[rows["volume"] > 0]
    return rows.sort_values("price", ascending=False, ignore_index=True), step


def weighted_quartiles(group: np.ndarray, price: np.ndarray, weight: np.ndarray,
                       groups: int) -> np.ndarray:
    """Volume-weighted 25th, 50th and 75th percentiles of price in each group 0..groups-1:
    an array (groups, 3), NaN for a group with no prices. A group whose weights are all zero
    counts each price once."""
    out = np.full((groups, 3), np.nan)
    group, price, weight = np.asarray(group), np.asarray(price, float), np.asarray(weight, float)
    keep = (group >= 0) & np.isfinite(price)
    group, price, weight = group[keep], price[keep], np.where(np.isfinite(weight[keep]), weight[keep], 0.0)
    if not len(group):
        return out
    total = np.bincount(group, weights=weight, minlength=groups)
    weight = np.where(total[group] > 0, weight, 1.0)  # no volume recorded: each price counts once
    order = np.lexsort((price, group))
    g, p, w = group[order], price[order], weight[order]
    cw = np.cumsum(w)
    starts = np.r_[0, np.flatnonzero(np.diff(g)) + 1]
    ends = np.r_[starts[1:], len(g)]
    before = np.r_[0.0, cw[ends[:-1] - 1]]
    tot = cw[ends - 1] - before
    for k, q in enumerate((0.25, 0.5, 0.75)):
        idx = np.clip(np.searchsorted(cw, before + q * tot - 1e-12 * tot, side="left"), starts, ends - 1)
        out[g[starts], k] = p[idx]
    return out
