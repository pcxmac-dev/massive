"""Bars built here from stored ticks, by the consolidated tape's rules.

Not every trade moves a bar. Each SIP trade condition says whether the trade may set the high and
low, the open and close, and whether it counts in volume (odd lots count in volume but set no
high or low; average-price and out-of-sequence prints set neither; ...). The rules come from the
data source's condition reference (stored in the `conditions` table, refreshed weekly); a trade
takes the strictest of its conditions. Tapes A and B use the CTA letters, tape C the UTP ones.
Crypto trades carry no conditions: every trade counts.

Built bars have the same shape as downloaded ones (t ms, o, h, l, c, v, vw, n), so the chart, the
statistics and the audits treat them alike; audits compare them with a feed's own bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TAPE_FAMILY = {1: "cta", 2: "cta", 3: "utp"}
REGULAR = ("", "@", " ")  # no condition: a regular trade
# The consolidated last-sale rules leave extended-hours prints (Form T) out of the high/low and the
# last price, which would make every pre-market and after-hours bar flat. Bars of those hours are
# built from them (as the feeds' own bars are): T counts as regular, and U (extended hours, sold out
# of sequence) as out of sequence (high/low and volume, not open/close). Audits check the result.
BAR_RULES = {("cta", "T"): (True, True, True), ("utp", "T"): (True, True, True),
             ("cta", "U"): (True, False, True), ("utp", "U"): (True, False, True)}


def eligibility(trades: pd.DataFrame, rules: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, set]:
    """(updates high/low, updates open/close, counts in volume) per trade, and the condition codes
    found that the rules do not know (those are treated as regular)."""
    n = len(trades)
    if n == 0 or not rules or "conditions" not in trades:
        ones = np.ones(n, dtype=bool)
        return ones, ones.copy(), ones.copy(), set()
    tape = trades["tape"].to_numpy(dtype=object) if "tape" in trades else np.full(n, None, dtype=object)
    conds = trades["conditions"].fillna("").to_numpy()
    unknown: set = set()
    cache: dict = {}
    hl, oc, vol = np.ones(n, dtype=bool), np.ones(n, dtype=bool), np.ones(n, dtype=bool)
    for i in range(n):
        key = (tape[i], conds[i])
        flags = cache.get(key)
        if flags is None:
            fam = TAPE_FAMILY.get(int(tape[i])) if tape[i] is not None and not pd.isna(tape[i]) else None
            h = o = v = True
            # No tape: not a SIP trade (crypto); its tags (taker side) are not sale conditions.
            for code in (str(conds[i]).split(",") if fam else ()):
                if code in REGULAR:
                    continue
                rule = BAR_RULES.get((fam, code)) or rules.get((fam, code))
                if rule is None:
                    unknown.add(f"{fam or '?'}:{code}")
                    continue
                h, o, v = h and rule[0], o and rule[1], v and rule[2]
            flags = cache[key] = (h, o, v)
        hl[i], oc[i], vol[i] = flags
    return hl, oc, vol, unknown


def build_bars(trades: pd.DataFrame, step_ns: int, rules: dict | None = None) -> tuple[list[dict], set]:
    """Bars of `step_ns` from ticks (columns ts_ns, price, size, conditions, tape; in time order).
    A bar whose trades set no open/close (e.g. odd lots only) carries the previous close as its
    open and close, with the high and low its eligible trades set. Returns (bars, unknown codes)."""
    if trades.empty:
        return [], set()
    hl, oc, vol, unknown = eligibility(trades, rules or {})
    ts = trades["ts_ns"].to_numpy(dtype=np.int64)
    px = trades["price"].to_numpy(dtype=float)
    sz = trades["size"].to_numpy(dtype=float)
    bucket = ts // step_ns * step_ns
    frame = pd.DataFrame({"b": bucket, "p": px, "s": sz})
    o = frame[oc].groupby("b")["p"].agg(["first", "last"])
    h = frame[hl].groupby("b")["p"].agg(["max", "min"])
    v = frame[vol].assign(ps=lambda d: d.p * d.s).groupby("b").agg(v=("s", "sum"), ps=("ps", "sum"), n=("s", "size"))
    out = pd.concat([o, h, v], axis=1).sort_index()
    out["last"] = out["last"].ffill()  # bars with no open/close trade carry the previous close
    out["first"] = out["first"].fillna(out["last"].shift(1).where(out["first"].isna()))
    out["first"] = out["first"].fillna(out["last"])
    out = out[out["first"].notna()]
    opens, closes = out["first"].to_numpy(), out["last"].to_numpy()
    highs = np.fmax(out["max"].to_numpy(), np.fmax(opens, closes))
    lows = np.fmin(out["min"].to_numpy(), np.fmin(opens, closes))
    vols = out["v"].fillna(0).to_numpy()
    vwap = np.where(vols > 0, out["ps"].fillna(0).to_numpy() / np.where(vols > 0, vols, 1), np.nan)
    counts = out["n"].fillna(0).to_numpy()
    bars = [{"t": int(t // 1_000_000), "o": float(a), "h": float(b), "l": float(c), "c": float(d), "v": float(e),
             "vw": None if np.isnan(f) else float(f), "n": int(g)}
            for t, a, b, c, d, e, f, g in zip(out.index, opens, highs, lows, closes, vols, vwap, counts)]
    return bars, unknown
