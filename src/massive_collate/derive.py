"""Candles for every standard time frame on every feed, derived where the feed does not send them.

A feed sends some time frames itself (most send 1-minute and daily bars). Every other standard
frame (STANDARD) is built here from the finest data the feed holds, kept in the feed's own
database apart from what it sent (built_bars, under a derivation record) and read as derived. A
bar the feed sends for the same time replaces the built one (storage: bars_replace_built), and a
built bar is never stored where the feed's own exists, so history fetched later takes over.

- 1 s and 1 min from ticks: ticks.py (with the trade-condition rules), as the ticks arrive.
- 5 min, 1 h and 1 day from the 1-minute bars (the feed's own first, built ones where it sent
  none): here. Minutes and hours are clock bins (the same in New York and UTC: whole-hour
  offsets). Days are New York dates (regular session for stocks and indices, the whole date
  for futures) and UTC dates for crypto and forex, stamped as those feeds stamp their own.
- Incremental: whatever a feed's minutes have had stored since the last build (stored_at) is
  rebuilt, from the start of the day before the earliest change, so a corrected minute rebuilds
  the frames above it; a first build covers everything held.
"""
from __future__ import annotations

import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import provenance
from .sessions import is_nyse_holiday, nyse_early_close

ET = ZoneInfo("America/New_York")
STANDARD = (("second", 1), ("minute", 1), ("minute", 5), ("hour", 1), ("day", 1))
FROM_MINUTES = (("minute", 5), ("hour", 1), ("day", 1))
KEY = "derive:{}:{}"  # source, ticker -> minutes stored after this (ms) are not built from yet
CHUNK_DAYS = 60
SLACK_MS = 60_000  # writers' stored_at can commit out of order: look back a minute
UTC_DAYS = ("crypto", "fx")


def day_zone(asset: str):
    return timezone.utc if asset in UTC_DAYS else ET


def _midnight_ms(d: date, zone) -> int:
    return int(datetime.combine(d, time(0), tzinfo=zone).timestamp() * 1000)


def _regular(times: pd.Series) -> np.ndarray:
    """Rows in the New York regular session: 09:30 to 16:00 (13:00 on early closes), trading days."""
    local = times.dt.tz_convert(ET)
    days = local.dt.date
    closes = {d: (None if d.weekday() >= 5 or is_nyse_holiday(d) else time(13) if nyse_early_close(d) else time(16))
              for d in days.unique()}
    t = local.dt.time
    close = days.map(closes)
    return np.array([c is not None and time(9, 30) <= x < c for x, c in zip(t, close)], dtype=bool)


def aggregate(minutes: pd.DataFrame, timespan: str, multiplier: int, asset: str) -> list[dict]:
    """Bars of one frame from 1-minute bars (columns ts, open, high, low, close, volume, vwap,
    transactions, time), as feeds send them: t (bar start, ms), o, h, l, c, v, vw, n."""
    m = minutes
    if timespan == "day" and asset in ("stock", "index"):
        m = m[_regular(m["time"])]
    if m.empty:
        return []
    ts = m["ts"].to_numpy(dtype="int64")
    if timespan == "minute":
        key = ts // (multiplier * 60_000) * (multiplier * 60_000)
    elif timespan == "hour":
        key = ts // (multiplier * 3_600_000) * (multiplier * 3_600_000)
    elif asset in UTC_DAYS:
        key = ts // 86_400_000 * 86_400_000
    else:  # the New York date, stamped at its midnight
        key = pd.DatetimeIndex(m["time"].dt.tz_convert(ET).dt.normalize()).as_unit("ms").asi8
    vol = m["volume"].fillna(0).to_numpy(dtype=float)
    px = m["vwap"].fillna(m["close"]).to_numpy(dtype=float)
    df = pd.DataFrame({"key": key, "o": m["open"].to_numpy(dtype=float), "h": m["high"].to_numpy(dtype=float),
                       "l": m["low"].to_numpy(dtype=float), "c": m["close"].to_numpy(dtype=float), "v": vol,
                       "pv": px * vol, "n": m["transactions"].fillna(0).to_numpy(dtype=float)})
    g = df.groupby("key", sort=True).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
                                         v=("v", "sum"), pv=("pv", "sum"), n=("n", "sum"))
    vw = np.where(g["v"] > 0, g["pv"] / np.where(g["v"] > 0, g["v"], 1), np.nan)
    return [{"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": v, "vw": None if np.isnan(w) else float(w), "n": int(n)}
            for t, o, h, l, c, v, w, n in zip(g.index, g["o"], g["h"], g["l"], g["c"], g["v"], vw, g["n"])]


def build_series(store, source: str, ticker: str, asset: str = "stock", log=print) -> int:
    """Build (or rebuild) the frames above 1 minute for one series from what changed since the last
    build. Returns bars stored."""
    key = KEY.format(source, ticker)
    started = int(time_module.time() * 1000)
    built_to = int(store.kv_get(key) or -1)
    edges = store.bar_edges(source, ticker, "minute")
    if edges is None:
        return 0
    if built_to < 0:  # a first build covers everything held (minutes from before stored_at have none)
        lo = edges[0]
    else:
        changed = store.bars_changed_since(source, ticker, ["minute"], built_to)
        if "minute" not in changed:
            return 0
        lo = changed["minute"][0]
    zone = day_zone(asset)
    first = datetime.fromtimestamp(lo / 1000, zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(edges[1] / 1000, zone).date() + timedelta(days=1)
    did = provenance.record(store, "bars-from-bars", f"{source}:{ticker}", {
        "from": "minute", "frames": [f"{m}{t}" for t, m in FROM_MINUTES],
        "days": "UTC dates" if zone is timezone.utc else
                ("New York dates, regular session" if asset in ("stock", "index") else "New York dates"),
        "method": "first open, highest high, lowest low, last close, summed volume and trades, volume-weighted "
                  "average price; the feed's own minutes first, built ones where it sent none"},
        {"bars": f"{source}:{ticker}:minute"}, (_midnight_ms(first, zone), _midnight_ms(last, zone)))
    stored = 0
    day = first
    while day < last:
        end = min(last, day + timedelta(days=CHUNK_DAYS))
        minutes = store.bars_window([source], ticker, "minute", _midnight_ms(day, zone), _midnight_ms(end, zone))
        if len(minutes):
            for timespan, multiplier in FROM_MINUTES:
                stored += store.upsert_built_bars(source, ticker, timespan, multiplier,
                                                  aggregate(minutes, timespan, multiplier, asset), did)
        day = end
    store.set_derivation_rows(did, stored)
    # Minutes stored before this build started reading are in it, but a write committed late can
    # carry an earlier stored_at: the next build looks again from a minute before.
    store.kv_set(key, str(started - SLACK_MS))
    return stored


def run(store, sources=None, tickers=None, log=print) -> int:
    """Build the frames for every series of 1-minute bars on the given feeds (every feed by
    default), or for the given tickers only. Returns bars stored."""
    wanted = {t.upper() for t in tickers} if tickers else None
    assets = {(w["source"], w["ticker"]): w["asset"] for w in store.watchlist()}
    total = 0
    for source, ticker, kind in store.stored_series():
        if kind != "minute" or (sources and source not in sources) or (wanted and ticker not in wanted):
            continue
        if source == "composite":  # the composite model is derived through and through: it has its own build
            continue
        n = build_series(store, source, ticker, assets.get((source, ticker), "stock"), log)
        if n:
            log(f"derive {source}:{ticker}: {n:,} bars for {', '.join(f'{m}{t[0]}' for t, m in FROM_MINUTES)}")
        total += n
    return total
