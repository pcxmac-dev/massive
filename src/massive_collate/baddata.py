"""Bad data (docs/ARCHITECTURE.md, "Bad data"): finding it and putting it on each feed's pruned list.

Nothing is deleted. A feed's own bar that a rule catches stays where it is and is left out of the
filtered view (storage/pruned.py); unfiltered reads still have it. The rules:

1. Malformed (`malformed`): prices not positive and finite, the high below the low, the open or
   close outside them, negative volume, a time in the future.
2. Spike without volume (`spikes`): a range at least SPIKE_RANGE times the median range of its
   SPIKE_SIDE neighbours either side in the same part of the session (regular hours against regular
   hours, the rest against the rest: they trade very different volumes) and at least SPIKE_MOVE of
   the price, on a volume at most SPIKE_VOLUME times theirs, with the open and close inside their
   range: the price did not move on, and nothing traded to say it did.
3. Outside its own finer bars (`inconsistent`): a feed's own day or minute whose high or low lies
   more than CONSISTENT beyond its own minutes' or seconds' (what the fetch verifies too,
   collector.verify_fetched).
4. Built from the wrong rules (`repair_from_ticks`): a bar built here that a rebuild changes; the
   old version is kept on the list.

Scans are incremental, like derive.py: each series is judged from where it changed since its last
scan (stored_at), with enough bars around for the neighbours; derive then rebuilds the frames above
what was pruned (derive.DIRTY_KEY), so a 5-minute or daily bar never carries a pruned minute.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

SPIKE_RANGE = 10.0
SPIKE_VOLUME = 2.0
SPIKE_SIDE = 10
SPIKE_MOVE = 0.01
SCAN_KEY = "baddata:v1:{}:{}:{}"  # source, ticker, timespan -> bars stored after this (ms) are not judged yet
SLACK_MS = 60_000
BASES = ("second", "minute", "day")  # what the feeds send and what is built from ticks: the frames above are built
CHUNK_MS = {"second": 2 * 86_400_000, "minute": 30 * 86_400_000, "day": 400 * 365 * 86_400_000}
MARGIN_MS = {"second": 5 * 86_400_000, "minute": 5 * 86_400_000, "day": 45 * 86_400_000}  # the neighbours' reach
STEP_MS = {"second": 1_000, "minute": 60_000, "day": 86_400_000}
TRADES_SHOWN = 10


def malformed(bars: pd.DataFrame, now_ms: int) -> tuple[np.ndarray, list[dict]]:
    """Rule 1 per bar (columns ts, open, high, low, close, volume): (caught, evidence of each caught)."""
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    v = bars["volume"].to_numpy(dtype=float)
    ts = bars["ts"].to_numpy(dtype="int64")
    tol = 1e-9 * np.fmax(np.abs(h), 1.0)
    checks = {
        "price not positive and finite": ~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
                                           & (o > 0) & (h > 0) & (l > 0) & (c > 0)),
        "high below low": h < l - tol,
        "open or close outside high and low": (o > h + tol) | (o < l - tol) | (c > h + tol) | (c < l - tol),
        "negative volume": v < 0,
        "time in the future": ts > now_ms + 60_000,
    }
    caught = np.zeros(len(bars), dtype=bool)
    for hit in checks.values():
        caught |= np.nan_to_num(hit, nan=True).astype(bool)
    evidence = [{"failed": [name for name, hit in checks.items() if hit[i]],
                 "bar": {"open": o[i], "high": h[i], "low": l[i], "close": c[i], "volume": v[i]}}
                for i in np.flatnonzero(caught)]
    return caught, evidence


def _windows(x: np.ndarray, side: int) -> np.ndarray:
    """Each position's 2*side neighbours (side either side, itself left out), for positions with
    that many on both sides: shape (len(x) - 2*side, 2*side)."""
    win = np.lib.stride_tricks.sliding_window_view(x, 2 * side + 1)
    return np.delete(win, side, axis=1)


def spikes(bars: pd.DataFrame, parts: np.ndarray | None = None, side: int = SPIKE_SIDE, range_x: float = SPIKE_RANGE,
           volume_x: float = SPIKE_VOLUME, move: float = SPIKE_MOVE, judge: np.ndarray | None = None,
           chunk: int = 200_000) -> tuple[np.ndarray, list[dict]]:
    """Rule 2 per bar (in time order; columns open, high, low, close, volume): (caught, evidence).
    `parts`: each bar's part of the session (neighbours are taken within a part); `judge`: the bars
    to judge (the rest only serve as neighbours). Bars without `side` neighbours both sides in their
    part are not judged (the newest wait for later bars)."""
    n = len(bars)
    caught = np.zeros(n, dtype=bool)
    found: dict[int, dict] = {}
    if n == 0:
        return caught, []
    o, h, l, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    v = np.nan_to_num(bars["volume"].to_numpy(dtype=float))
    parts = np.zeros(n, dtype=int) if parts is None else np.asarray(parts)
    for part in np.unique(parts):
        idx = np.flatnonzero(parts == part)
        if len(idx) < 2 * side + 1:
            continue
        for start in range(0, len(idx) - 2 * side, chunk):
            sub = idx[start:start + chunk + 2 * side]
            centre = sub[side:len(sub) - side]
            rng, vol = h[sub] - l[sub], v[sub]
            med_r = np.median(_windows(rng, side), axis=1)
            med_v = np.median(_windows(vol, side), axis=1)
            lo_n = _windows(l[sub], side).min(axis=1)
            hi_n = _windows(h[sub], side).max(axis=1)
            r_c = rng[side:len(sub) - side]
            hit = ((r_c >= range_x * med_r) & (r_c >= move * c[centre]) & (v[centre] <= volume_x * med_v)
                   & (o[centre] >= lo_n) & (o[centre] <= hi_n) & (c[centre] >= lo_n) & (c[centre] <= hi_n))
            if judge is not None:
                hit &= judge[centre]
            for j in np.flatnonzero(hit):
                i = int(centre[j])
                caught[i] = True
                found[i] = {"range": float(r_c[j]), "neighbours_range_median": float(med_r[j]),
                            "range_x": None if med_r[j] == 0 else round(float(r_c[j] / med_r[j]), 1),
                            "volume": float(v[i]), "neighbours_volume_median": float(med_v[j]),
                            "volume_x": None if med_v[j] == 0 else round(float(v[i] / med_v[j]), 2),
                            "neighbours_low": float(lo_n[j]), "neighbours_high": float(hi_n[j]),
                            "neighbours": 2 * side, "part": str(part),
                            "thresholds": {"range_x": range_x, "move": move, "volume_x": volume_x}}
    return caught, [found[i] for i in np.flatnonzero(caught)]


def _parts(ts: np.ndarray, timespan: str, asset: str) -> np.ndarray:
    """The part of the session of each bar: US stocks and indices regular hours or not (intraday);
    one part otherwise."""
    if timespan == "day" or asset not in ("stock", "index"):
        return np.full(len(ts), "all", dtype=object)
    from .gui.bars import regular_mask

    return np.where(regular_mask(pd.DatetimeIndex(pd.to_datetime(ts, unit="ms", utc=True))), "regular", "extended")


def _trades_inside(store, source: str, ticker: str, ts: int, step_ms: int, lo: float, hi: float) -> list[dict]:
    """The trades in a bar priced outside its neighbours' range (the furthest first): what made the spike."""
    t = store.trades_between(source, ticker, ts * 1_000_000, (ts + step_ms) * 1_000_000)
    if t.empty:
        return []
    away = np.maximum(lo - t["price"], t["price"] - hi)
    t = t[away > 0].assign(away=away[away > 0]).sort_values("away", ascending=False).head(TRADES_SHOWN)
    return [{"ns": int(r.ts_ns), "price": float(r.price), "size": float(r.size), "exchange": None if pd.isna(r.exchange)
             else int(r.exchange), "conditions": r.conditions} for r in t.itertuples()]


FINER = {"day": "minute", "minute": "second"}  # a feed's own bar is checked against its own bars of this frame
CONSISTENT = 0.01  # how far (a share of the price) its high or low may lie beyond theirs


def _ends(ts: np.ndarray, timespan: str, asset: str) -> np.ndarray:
    """Where each bar ends (ms): a minute later; a day: the next midnight of its day's zone (New York
    for stocks, whatever the clocks do; UTC for crypto and forex)."""
    if timespan == "minute":
        return ts + 60_000
    if asset in ("crypto", "fx"):
        return ts + 86_400_000
    local = pd.DatetimeIndex(pd.to_datetime(ts, unit="ms", utc=True)).tz_convert("America/New_York")
    nxt = (local.normalize().tz_localize(None) + pd.Timedelta(days=1)).tz_localize(
        "America/New_York", nonexistent="shift_forward", ambiguous=True)
    return np.asarray(nxt.as_unit("ms").asi8, dtype="int64")


def inconsistent(store, source: str, ticker: str, timespan: str, lo_ms: int, hi_ms: int,
                 asset: str = "stock") -> tuple[pd.DataFrame, list[dict]]:
    """Rule 3: a feed's own bars in [lo, hi) whose high or low lies beyond its own finer bars' (a
    day's minutes, a minute's seconds) by more than CONSISTENT: the extreme is in no trade the feed
    shows at the finer frame. Judged only where the feed holds that finer frame for the whole bar
    (its coverage) and has bars in it, the finer bars filtered (a pruned one supports nothing).
    Returns (the bars, evidence)."""
    finer = FINER.get(timespan)
    empty = pd.DataFrame(), []
    if finer is None or asset == "future":  # futures' days are CME sessions from 18:00: not a date's minutes
        return empty
    rows = store.series_rows(source, ticker, timespan, lo_ms, hi_ms)
    own = rows[rows["derivation"].isna()].reset_index(drop=True) if len(rows) else rows
    if own.empty:
        return empty
    ts = own["ts"].to_numpy(dtype="int64")
    ends = _ends(ts, timespan, asset)
    spans = sorted(store.coverage(ticker, finer, source=source).get(source, []))
    if not spans:
        return empty
    starts = np.array([a for a, _ in spans], dtype="int64")
    stops = np.array([b for _, b in spans], dtype="int64")
    at = np.searchsorted(starts, ts, side="right") - 1
    covered = (at >= 0) & (ends <= stops[np.clip(at, 0, None)])
    if not covered.any():
        return empty
    fine = store.bars_window([source], ticker, finer, int(ts[covered].min()), int(ends[covered].max()))
    if fine.empty:
        return empty
    f_ts = fine["ts"].to_numpy(dtype="int64")
    k = np.searchsorted(ts, f_ts, side="right") - 1
    inside = (k >= 0) & (f_ts < ends[np.clip(k, 0, None)])
    hi_f = np.full(len(own), -np.inf)
    lo_f = np.full(len(own), np.inf)
    n_f = np.zeros(len(own), dtype=int)
    np.maximum.at(hi_f, k[inside], fine["high"].to_numpy(dtype=float)[inside])
    np.minimum.at(lo_f, k[inside], fine["low"].to_numpy(dtype=float)[inside])
    np.add.at(n_f, k[inside], 1)
    h, l = own["high"].to_numpy(dtype=float), own["low"].to_numpy(dtype=float)
    bad = covered & (n_f > 0) & ((h > hi_f * (1 + CONSISTENT)) | (l < lo_f * (1 - CONSISTENT)))
    hit = np.flatnonzero(bad)
    evidence = [{"finer": finer, "finer_bars": int(n_f[i]), "finer_high": float(hi_f[i]), "finer_low": float(lo_f[i]),
                 "high": float(h[i]), "low": float(l[i]), "tolerance": CONSISTENT} for i in hit]
    return own.iloc[hit], evidence


def scan_series(store, source: str, ticker: str, timespan: str, asset: str = "stock", lo_ms: int | None = None,
                hi_ms: int | None = None, now_ms: int | None = None) -> int:
    """Judge one series' bars in [lo, hi) (all of them by default) by rules 1 and 2 and put what they
    catch on the feed's pruned list; derive rebuilds the frames above what was pruned. Returns
    entries added."""
    edges = store.bar_edges(source, ticker, timespan)
    if edges is None:
        return 0
    now_ms = now_ms or int(time.time() * 1000)
    lo = max(edges[0], lo_ms) if lo_ms is not None else edges[0]
    hi = min(edges[1] + STEP_MS[timespan], hi_ms) if hi_ms is not None else edges[1] + STEP_MS[timespan]
    added, dirty = 0, []
    start = lo
    while start < hi:
        end = min(hi, start + CHUNK_MS[timespan])
        rows = store.series_rows(source, ticker, timespan, start - MARGIN_MS[timespan], end + MARGIN_MS[timespan])
        if len(rows):
            ts = rows["ts"].to_numpy(dtype="int64")
            judge = (ts >= start) & (ts < end)
            broken, why = malformed(rows, now_ms)
            why_at = dict(zip(np.flatnonzero(broken).tolist(), why))
            hit = np.flatnonzero(broken & judge)
            if len(hit):
                added += store.prune(source, ticker, timespan, 1, rows.iloc[hit], "malformed", True,
                                     [why_at[i] for i in hit.tolist()])
                dirty.append((int(ts[hit].min()), int(ts[hit].max()) + STEP_MS[timespan]))
            ok = ~broken  # a malformed bar is no one's neighbour
            sub = rows[ok].reset_index(drop=True)
            caught, evidence = spikes(sub, _parts(sub["ts"].to_numpy(dtype="int64"), timespan, asset),
                                      judge=judge[ok])
            hit = np.flatnonzero(caught)
            if len(hit):
                if timespan != "day":
                    for i, ev in zip(hit, evidence):
                        ev["trades"] = _trades_inside(store, source, ticker, int(sub["ts"].iloc[i]), STEP_MS[timespan],
                                                      ev["neighbours_low"], ev["neighbours_high"])
                added += store.prune(source, ticker, timespan, 1, sub.iloc[hit], "spike", True, evidence)
                t = sub["ts"].to_numpy(dtype="int64")[hit]
                dirty.append((int(t.min()), int(t.max()) + STEP_MS[timespan]))
        bad, evidence = inconsistent(store, source, ticker, timespan, start, end, asset)
        if len(bad):
            added += store.prune(source, ticker, timespan, 1, bad, "inconsistent", True, evidence)
            t = bad["ts"].to_numpy(dtype="int64")
            dirty.append((int(t.min()), int(t.max()) + STEP_MS[timespan]))
        start = end
    if dirty:
        from . import derive

        derive.mark_dirty(store, source, ticker, timespan, min(a for a, _ in dirty), max(b for _, b in dirty))
    return added


def scan_symbol(store, source: str, ticker: str, asset: str = "stock") -> int:
    """Judge what changed in a series since its last scan (each base frame it holds). Returns entries added."""
    added = 0
    for timespan in BASES:
        key = SCAN_KEY.format(source, ticker.upper(), timespan)
        started = int(time.time() * 1000)
        done = int(store.kv_get(key) or -1)
        if done < 0:
            span = store.bar_edges(source, ticker, timespan)
            if span is None:
                continue
            lo, hi = span[0], span[1] + STEP_MS[timespan]
        else:
            span = store.changed_span(source, ticker, timespan, done)
            if span is None:
                continue
            lo, hi = span[0], span[1] + STEP_MS[timespan]
        # the bars just before the change are judged again: their right-hand neighbours came with it
        added += scan_series(store, source, ticker, timespan, asset, lo - MARGIN_MS[timespan], hi)
        store.kv_set(key, str(started - SLACK_MS))
    return added


def repair_from_ticks(store, source: str, tickers=None, log=print) -> dict:
    """Rebuild every bar built from a feed's stored ticks with the condition rules as they are now,
    then the frames above them; each bar the rebuild changes or removes is kept on the pruned list
    (rule "rebuilt"), with what replaced it. The ticks are read, never changed. Returns
    {ticker: entries added}."""
    from . import derive
    from .collector import build_from_ticks
    from .storage import TicksBlocked

    if not store.ticks_allowed:
        raise TicksBlocked("bars-only mode: the ticks are not read here (BARS_ONLY=off to rebuild from them)")
    rows = store.conn.execute("SELECT product, span_lo, span_hi FROM derivations WHERE kind='bars-from-ticks' "
                              "AND product LIKE ?", (f"{source}:%",)).fetchall()
    days: dict[str, set] = {}
    for product, lo, hi in rows:
        ticker = product.split(":", 1)[1]
        if lo is not None and hi is not None and (not tickers or ticker in {t.upper() for t in tickers}):
            days.setdefault(ticker, set()).add((int(lo), int(hi)))
    rules = store.condition_rules(source)
    why = (f"rebuilt from the stored ticks with {source}'s own condition rules: its numeric condition ids had taken "
           "the rules of tape letters with the same digits (2, Average Price Trade, set highs and lows)")
    out = {}
    for n, ticker in enumerate(sorted(days)):
        spans = sorted(days[ticker])
        added = 0
        for lo, hi in spans:
            before = store.built_rows(source, ticker, lo, hi, [("second", 1), ("minute", 1)])
            build_from_ticks(store, source, ticker, lo, hi, rules, log)
            after = store.built_rows(source, ticker, lo, hi, [("second", 1), ("minute", 1)])
            added += store.prune_replaced(source, ticker, before, after, why)
        lo, hi = spans[0][0] - 400 * 86_400_000, spans[-1][1] + 400 * 86_400_000
        above = [frame for _, frames in derive.CHAINS for frame in frames]
        before = store.built_rows(source, ticker, lo, hi, above)
        derive.build_symbol(store, source, ticker, derive.asset_of(store, source, ticker), log=log)
        after = store.built_rows(source, ticker, lo, hi, above)
        added += store.prune_replaced(source, ticker, before, after, why + "; built again from the rebuilt bars")
        out[ticker] = added
        log(f"repair {source}:{ticker} ({n + 1}/{len(days)}): {len(spans)} days rebuilt, {added:,} bars kept on the "
            "pruned list")
    return out


def entries(store, ticker: str, source: str | None = None, limit: int = 200) -> list[dict]:
    """A symbol's pruned list for the chart app and the command line, newest first."""
    df = store.pruned(source, ticker, limit=limit)
    out = []
    for r in df.itertuples():
        out.append({"source": r.source, "timespan": r.timespan, "multiplier": int(r.multiplier), "ts": int(r.ts),
                    "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
                    "rule": r.rule, "excludes": bool(r.excludes), "evidence": json.loads(r.evidence or "{}"),
                    "prunedAt": int(r.pruned_at), "code": r.code,
                    "releasedAt": None if pd.isna(r.released_at) else int(r.released_at)})
    return out
