"""Candles for every standard time frame on every feed, derived where the feed does not send them.

A feed sends some time frames itself (most send 1-minute and daily bars). Every other standard
frame (STANDARD) is built here from the finest data the feed holds, kept in the feed's own
database apart from what it sent (built_bars, under a derivation record) and read as derived. A
bar the feed sends for the same time replaces the built one (storage: bars_replace_built), and a
built bar is never stored where the feed's own exists, so history fetched later takes over, except
where the feed's own is pruned: there the built bar takes its place in the filtered view.

Frames are only ever built upward, from finer data (CHAINS); nothing finer is made from coarser
bars, so a gap in the finer data stays a gap.

- 1 s and 1 min from ticks: ticks.py (with the trade-condition rules), as the ticks arrive.
- 10 and 30 s from the 1-second bars.
- 5 and 30 min, 1, 2 and 4 h and 1 day from the 1-minute bars (the feed's own first, built ones
  where it sent none). Minutes are clock bins; hours are wall-clock bins from midnight in the
  series' day zone (New York, UTC for crypto and forex; the same on days the clocks change). Days
  are New York dates (regular session for stocks and indices), CME sessions for futures (18:00 to
  17:00 New York, dated by the day they end), and UTC dates for crypto and forex, stamped as those
  feeds stamp their own. A series takes the asset class of its symbol on any feed (a futures
  contract that of its product), so Coinbase's coins and NQZ6 are not built as New York stocks.
- A rebuilt window loses the bins left without base bars (a trade withdrawn, a bar deleted).
- 1 week (from Monday), 1 month and 1 year from the daily bars (the feed's official ones first).
- Incremental: the span of a feed's base bars stored since the last build (stored_at) is
  rebuilt, from the day before its first changed bar to the day after its last (weeks, months and
  years: all of them, they are few), so a corrected minute rebuilds the frames above it and a
  backfill of old history rebuilds that history only; a first build covers everything held.
- Built in chunks of days, each read with a day of base bars either side and keeping the bins
  that start inside it, so a bin across a chunk's edge (a CME session from 18:00) is whole.
- One symbol at a time (build_symbol): the sync builds each symbol's frames as soon as its fetch
  is stored, and a pass over everything (run) after the sync and in the live recorder. While it
  runs, kv `derive:progress` says what it is building (the status bar).
"""
from __future__ import annotations

import json
import os
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import provenance
from .sessions import is_nyse_holiday, nyse_early_close

ET = ZoneInfo("America/New_York")
# Each base frame and the frames built from it, finest first.
CHAINS = (
    ("second", (("second", 10), ("second", 30))),
    ("minute", (("minute", 5), ("minute", 30), ("hour", 1), ("hour", 2), ("hour", 4), ("day", 1))),
    ("day", (("week", 1), ("month", 1), ("year", 1))),
)
FROM_MINUTES = dict(CHAINS)["minute"]
STANDARD = (("second", 1), ("minute", 1), *(f for _, frames in CHAINS for f in frames))
KEY = "derive:v3:{}:{}:{}"  # source, ticker, base -> base bars stored after this (ms) are not built from yet
# A chain given another frame: its key's version, so its next build is a first one over everything
# held (the other chains' are not built again). Seconds: 10 s added 2026-10-28.
CHAIN_VERSIONS = {"second": 2}
PROGRESS_KEY = "derive:progress"
CHUNK_DAYS = {"second": 5, "minute": 60}
SLACK_MS = 60_000  # writers' stored_at can commit out of order: look back a minute
DAY_MS = 86_400_000  # the base bars read either side of a chunk: a bin's whole extent (a CME session is 23 h)
UTC_DAYS = ("crypto", "fx")
LABELS = {"second": "s", "minute": "m", "hour": "h", "day": "D", "week": "W", "month": "M", "year": "Y"}


def label(timespan: str, multiplier: int = 1) -> str:
    """1m, 30s, 4h, 1D ... as the chart's interval menu writes them."""
    return f"{multiplier}{LABELS.get(timespan, timespan)}"


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


def _local_midnights(times: pd.Series, zone) -> np.ndarray:
    """Each row's midnight (ms) in `zone`."""
    dates = times.dt.tz_convert(zone).dt.date
    midnight = {d: _midnight_ms(d, zone) for d in dates.unique()}
    return dates.map(midnight).to_numpy(dtype="int64")


def _period_starts(times: pd.Series, timespan: str, zone) -> np.ndarray:
    """The midnight (ms, in `zone`) of each row's week (from Monday), month or year."""
    dates = times.dt.tz_convert(zone).dt.date

    def start(d: date) -> date:
        if timespan == "week":
            return d - timedelta(days=d.weekday())
        return d.replace(day=1) if timespan == "month" else d.replace(month=1, day=1)
    starts = {d: _midnight_ms(start(d), zone) for d in dates.unique()}
    return dates.map(starts).to_numpy(dtype="int64")


def aggregate(bars: pd.DataFrame, timespan: str, multiplier: int, asset: str) -> list[dict]:
    """Bars of one frame from finer bars (columns ts, open, high, low, close, volume, vwap,
    transactions, time), as feeds send them: t (bar start, ms), o, h, l, c, v, vw, n."""
    m = bars
    zone = day_zone(asset)
    if timespan == "day" and asset in ("stock", "index"):
        m = m[_regular(m["time"])]
    if m.empty:
        return []
    ts = m["ts"].to_numpy(dtype="int64")
    if timespan == "second":
        key = ts // (multiplier * 1000) * (multiplier * 1000)
    elif timespan == "minute":
        key = ts // (multiplier * 60_000) * (multiplier * 60_000)
    elif timespan == "hour" and multiplier == 1:
        key = ts // 3_600_000 * 3_600_000  # whole-hour offsets: an hour is an hour in every zone
    elif timespan == "hour":  # by the wall clock from midnight: the same bins on a 23 or 25-hour day
        wall = m["time"].dt.tz_convert(zone).dt.tz_localize(None).dt.floor(f"{multiplier}h")
        key = pd.DatetimeIndex(wall).tz_localize(zone, ambiguous=np.ones(len(wall), dtype=bool),
                                                 nonexistent="shift_forward").as_unit("ms").asi8
    elif timespan in ("week", "month", "year"):
        key = _period_starts(m["time"], timespan, zone)
    elif asset in UTC_DAYS:
        key = ts // 86_400_000 * 86_400_000
    elif asset == "future":  # the CME session: from 18:00 New York, dated by the day it ends
        key = pd.DatetimeIndex((m["time"].dt.tz_convert(ET) + pd.Timedelta(hours=6)).dt.normalize()).as_unit("ms").asi8
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


def build_chain(store, source: str, ticker: str, base: str, asset: str = "stock") -> int:
    """Build (or rebuild) the frames above one base frame (CHAINS) for one series from what
    changed since the last build. Returns bars stored."""
    frames = dict(CHAINS)[base]
    key = KEY.format(source, ticker, base) + (f":c{CHAIN_VERSIONS[base]}" if base in CHAIN_VERSIONS else "")
    started = int(time_module.time() * 1000)
    built_to = int(store.kv_get(key) or -1)
    edges = store.bar_edges(source, ticker, base)
    if edges is None:
        return 0
    dirty = take_dirty(store, source, ticker, base)  # bad data pruned there: what was built over it goes (done_dirty)
    if built_to < 0:  # a first build covers everything held (bars from before stored_at have none)
        lo, hi = edges
    else:
        changed = store.changed_span(source, ticker, base, built_to)
        if dirty:
            changed = (min([dirty[0]] + ([changed[0]] if changed else [])),
                       max([dirty[1]] + ([changed[1]] if changed else [])))
        if changed is None:
            return 0
        lo, hi = edges if base == "day" else changed  # weeks, months, years: all again
    zone = day_zone(asset)
    first = datetime.fromtimestamp(lo / 1000, zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(hi / 1000, zone).date() + timedelta(days=2)  # a session dated by the day it ends
    did = provenance.record(store, "bars-from-bars", f"{source}:{ticker}", {
        "from": base, "frames": [label(t, m) for t, m in frames],
        "days": "UTC dates" if zone is timezone.utc else
                ("New York dates, regular session" if asset in ("stock", "index") else "New York dates"),
        "method": "first open, highest high, lowest low, last close, summed volume and trades, volume-weighted "
                  "average price; the feed's own bars first, built ones where it sent none; only upward"},
        {"bars": f"{source}:{ticker}:{base}"}, (_midnight_ms(first, zone), _midnight_ms(last, zone)))
    stored = 0
    if base == "day":  # weeks, months, years: rebuilt whole, so a bin left without days goes
        for timespan, multiplier in frames:
            store.drop_built_frame(source, ticker, timespan, multiplier, 0, 2**62)
    day = first
    while day < last:
        end = min(last, day + timedelta(days=CHUNK_DAYS.get(base, 100_000)))
        lo_ms, hi_ms = _midnight_ms(day, zone), _midnight_ms(end, zone)
        window = store.bars_window([source], ticker, base, lo_ms - DAY_MS, hi_ms + DAY_MS)  # whole bins at the edges
        for timespan, multiplier in frames:
            if base != "day":  # bins left without base bars go (weeks to years: all rebuilt below)
                store.drop_built_frame(source, ticker, timespan, multiplier, lo_ms, hi_ms)
            if len(window):
                bins = aggregate(window, timespan, multiplier, asset)
                if base != "day":  # this chunk's bins; the margin's belong to the chunks either side
                    bins = [b for b in bins if lo_ms <= b["t"] < hi_ms]
                stored += store.upsert_built_bars(source, ticker, timespan, multiplier, bins, did)
        day = end
    store.set_derivation_rows(did, stored)
    # Bars stored before this build started reading are in it, but a write committed late can
    # carry an earlier stored_at: the next build looks again from a minute before.
    store.kv_set(key, str(started - SLACK_MS))
    done_dirty(store, source, ticker, base, dirty)
    return stored


DIRTY_KEY = "derive:dirty:{}:{}:{}"  # source, ticker, base -> [lo, hi] ms built again whatever its stored_at


def mark_dirty(store, source: str, ticker: str, base: str, lo_ms: int, hi_ms: int) -> None:
    """Have the next build redo the frames above [lo, hi) of a base frame: bars there were pruned
    (baddata.py) without being stored again, so stored_at alone would not say so."""
    store.kv_update(DIRTY_KEY.format(source, ticker.upper(), base),
                    lambda held: [min(held[0], lo_ms), max(held[1], hi_ms)] if held else [lo_ms, hi_ms], None)


def take_dirty(store, source: str, ticker: str, base: str) -> tuple[int, int] | None:
    """The span marked dirty for a build. It stays marked until the build is done (done_dirty):
    when it was cleared as it was taken, a build that failed lost it, and the frames above stayed
    built over the pruned bars."""
    held = store.kv_json(DIRTY_KEY.format(source, ticker.upper(), base), None)
    return (int(held[0]), int(held[1])) if held else None


def done_dirty(store, source: str, ticker: str, base: str, taken: tuple[int, int] | None) -> None:
    """Clear the dirty mark a finished build took; one widened meanwhile (another check pruned more)
    stays for the next build."""
    if taken is not None:
        store.kv_update(DIRTY_KEY.format(source, ticker.upper(), base),
                        lambda held: None if held and (int(held[0]), int(held[1])) == taken else held, None)


def build_series(store, source: str, ticker: str, asset: str = "stock", log=print) -> int:
    """The frames built from a series' 1-minute bars (see build_chain). Returns bars stored."""
    return build_chain(store, source, ticker, "minute", asset)


LEASE_KEY = "derive:lease:{}:{}"  # source, ticker -> {pid, start, until}: the process building it
LEASE_MS = 30 * 60_000  # a lease older than this is taken over (its build is stuck or gone)


def take_lease(store, source: str, ticker: str, now_ms: int | None = None) -> bool:
    """Claim the build of one series' frames for this process (the sync, the live recorder and a
    "Build missing" may each start one): False while another live process holds an unexpired
    lease. A lease of a process that has ended, or one past LEASE_MS, is taken over."""
    from .proc import pid_alive, started

    pid = os.getpid()
    now_ms = now_ms or int(time_module.time() * 1000)
    taken = [False]

    def claim(held: dict) -> dict:
        other = held.get("pid")
        if other and other != pid and held.get("until", 0) > now_ms and pid_alive(other) \
                and (not held.get("start") or started(other) in (None, held["start"])):
            return held  # another build of this series is going on
        taken[0] = True
        return {"pid": pid, "start": started(pid) or 0, "until": now_ms + LEASE_MS}

    store.kv_update(LEASE_KEY.format(source, ticker), claim, {})
    return taken[0]


def drop_lease(store, source: str, ticker: str) -> None:
    pid = os.getpid()
    store.kv_update(LEASE_KEY.format(source, ticker), lambda held: {} if held.get("pid") == pid else held, {})


def build_symbol(store, source: str, ticker: str, asset: str = "stock", log=print,
                 progress: tuple[int, int] = (1, 1)) -> int:
    """Every frame above every base frame one series holds (CHAINS: seconds, then minutes, then
    days, so days built from minutes feed the weeks), from what changed since its last build.
    `progress` (n, of) is what the status bar shows while it builds. One build of a series at a
    time across processes (take_lease): another one going on, it is left to that. Returns bars stored."""
    if not take_lease(store, source, ticker):
        log(f"derive {source}:{ticker}: another process is building it; left to that one")
        return 0
    try:
        return _build_symbol(store, source, ticker, asset, log, progress)
    finally:
        drop_lease(store, source, ticker)


def _build_symbol(store, source: str, ticker: str, asset: str, log, progress: tuple[int, int]) -> int:
    from . import baddata

    try:  # bad data first (baddata.py): the frames above are then built without it
        pruned = baddata.scan_symbol(store, source, ticker, asset)
        if pruned:
            log(f"bad data {source}:{ticker}: {pruned:,} bars on the pruned list (left out of the filtered view)")
    except Exception as e:  # a failing check must not stop the build; it is tried again next time
        log(f"bad data {source}:{ticker}: check failed ({type(e).__name__}: {e})")
    total = 0
    for base, frames in CHAINS:
        if store.bar_edges(source, ticker, base) is None:
            continue
        store.kv_set(PROGRESS_KEY, json.dumps({
            "source": source, "ticker": ticker, "from": label(base), "to": [label(t, m) for t, m in frames],
            "n": progress[0], "of": progress[1], "at": int(time_module.time() * 1000)}))
        n = build_chain(store, source, ticker, base, asset)
        if n:
            log(f"derive {source}:{ticker}: {n:,} bars for {', '.join(label(t, m) for t, m in frames)} from {label(base)}")
        total += n
    return total


def asset_of(store, source: str, ticker: str) -> str:
    """The asset class a series is built as: its feed's watch row, else the symbol's on any
    feed, else a futures contract's, else a stock."""
    rows = store.watchlist(ticker=ticker)
    own = next((w["asset"] for w in rows if w["source"] == source), None)
    if own or rows:
        return own or rows[0]["asset"]
    contracts = {c["ticker"] for w in store.watchlist() if w["asset"] == "future"
                 for c in store.contracts(w["source"], w["source_ticker"])}
    return "future" if ticker.upper() in contracts else "stock"


def run(store, sources=None, tickers=None, log=print) -> int:
    """Build the frames above every base frame of every series on the given feeds (every feed by
    default), or for the given tickers only: seconds, then minutes, then days (days built from
    minutes feed the weeks). Returns bars stored."""
    wanted = {t.upper() for t in tickers} if tickers else None
    watch = store.watchlist()
    assets = {(w["source"], w["ticker"]): w["asset"] for w in watch}
    any_feed = {w["ticker"]: w["asset"] for w in watch}  # a symbol's class on whichever feed watches it
    contracts = {c["ticker"] for w in watch if w["asset"] == "future"
                 for c in store.contracts(w["source"], w["source_ticker"])}  # stored under contract tickers
    bases = dict.fromkeys(base for base, _ in CHAINS)
    series: dict[tuple[str, str], set] = {}
    for source, ticker, kind in store.stored_series():
        if kind not in bases or (sources and source not in sources) or (wanted and ticker not in wanted):
            continue
        # The composite model (composite.py builds its minutes and days) gets the frames above them
        # like any feed: every level of every series, built upward from what it holds.
        series.setdefault((source, ticker), set()).add(kind)
    total = 0
    for i, ((source, ticker), kinds) in enumerate(sorted(series.items())):
        asset = assets.get((source, ticker)) or any_feed.get(ticker) or ("future" if ticker in contracts else "stock")
        total += build_symbol(store, source, ticker, asset, log=log, progress=(i + 1, len(series)))
    if series:
        store.kv_set(PROGRESS_KEY, "")
    store.prune_changes()  # the change log's old minutes (at most hourly)
    return total
