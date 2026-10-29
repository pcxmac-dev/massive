"""What one symbol holds, feed by feed and frame by frame.

For each feed with data for the symbol (or watching it): the frames it sent (prime data), the
frames built here from its finer data (*derived, with the derivation record saying from what and
how), its ticks, what it is set to collect, and which frames it should have built but has not yet
(derive.py builds every standard frame above each base frame a feed holds: 10 and 30 s from seconds;
5 and 30 min, 1, 2 and 4 h and 1 day from minutes; weeks, months and years from days). Feeds come
in the order a merged chart takes them. Read by the chart's side panel (/api/inventory) and
`python -m massive_collate inventory TICKER`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import derive

# Every standard frame, finest first: (timespan, multiplier).
FRAMES = (("second", 1), ("second", 10), ("second", 30), ("minute", 1), *derive.FROM_MINUTES[:-1], ("day", 1),
          ("week", 1), ("month", 1), ("year", 1))
MODEL_FEEDS = ("composite",)  # derived through and through: a model of several exchanges


def expected(bases: set[str]) -> set[tuple[str, int]]:
    """The frames derive.py builds above the base frames a feed holds (days from minutes feed
    weeks, months and years too)."""
    out = set()
    for base, frames in derive.CHAINS:
        if base in bases or (base == "day" and "minute" in bases):
            out |= set(frames)
    return out


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms is not None else None


def symbol_inventory(store, ticker: str, rank: list[str] | None = None, counts: bool | dict = True) -> dict:
    """The symbol's data on every feed (see the module docstring). `rank`: feeds in the order a
    merged chart takes them (BarEngine.sources_for), listed first. `counts`: True counts each
    frame's bars (exact, slow for long series); a dict {(feed, table, timespan, multiplier): rows}
    gives counts made before (the chart app's, row_counts), None where it has none."""
    t = ticker.upper()
    watch = {w["source"]: w for w in store.watchlist(ticker=t)}
    series = {feed: _series_tickers(store, feed, t, watch.get(feed)) for feed in set(store.feeds_holding(t)) | set(watch)}
    held = {feed for feed, tickers in series.items() if any(store.feeds_holding(tk) for tk in tickers)}
    rank = [f for f in (rank or []) if f in held or f in watch]
    order = rank + sorted((held | set(watch)) - set(rank))
    records: dict[int, dict | None] = {}
    feeds = []
    for feed in order:
        tickers = series.get(feed, [t])
        stored = _merged_frames(store, feed, tickers, counts is True)
        frames: dict[tuple[str, int], dict] = {}
        for row in stored:
            key = (row["timespan"], row["multiplier"])
            rows = row["rows"] if counts is True else (counts or {}).get((feed, row["table"], *key))
            part = {"first": _iso(row["first"]), "last": _iso(row["last"]), "rows": rows}
            if row["table"] == "bars":
                frames.setdefault(key, {})["sent"] = part
            else:
                did = row["derivation"]
                if did not in records:
                    records[did] = store.derivation(did) if did is not None else None
                d = records[did]
                part["how"] = {"id": did, "kind": d["kind"], "built": d["created_at"],
                               "from": (d["settings"] or {}).get("from")} if d else None
                frames.setdefault(key, {})["derived"] = part
        bases = {ts for (ts, m) in frames if m == 1 and ts in dict(derive.CHAINS)}
        edges = [e for e in (store.trade_edges(feed, tk) for tk in tickers) if e]
        ticks = (min(e[0] for e in edges), max(e[1] for e in edges)) if edges else None
        w = watch.get(feed)
        feeds.append({
            "feed": feed, "model": feed in MODEL_FEEDS, "merged": feed in rank,
            "watch": {k: (str(w[k]) if w.get(k) is not None else None)
                      for k in ("asset", "resolution", "backfilled_to", "synced_at", "closed_at")} if w else None,
            "ticks": {"first": _iso(ticks[0] // 1_000_000), "last": _iso(ticks[1] // 1_000_000)} if ticks else None,
            # a continuous future's bars are its contracts' (its roll schedule): which ones
            "contracts": tickers if tickers != [t] else None,
            "frames": [{"frame": derive.label(ts, m), "timespan": ts, "multiplier": m, **frames[(ts, m)]}
                       for ts, m in sorted(frames, key=_duration)],
            "notBuilt": [derive.label(ts, m) for ts, m in sorted(expected(bases) - set(frames), key=_duration)],
        })
    return {"ticker": t, "frames": [derive.label(ts, m) for ts, m in FRAMES], "feeds": feeds}


_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400, "week": 604_800, "month": 2_629_746,
            "year": 31_556_952}


def _duration(frame: tuple[str, int]) -> int:
    return _SECONDS.get(frame[0], 0) * frame[1]


def row_counts(store, ticker: str) -> dict:
    """{(feed, table, timespan, multiplier): bars} of a symbol on every feed (a continuous future's
    over its contracts): the counts symbol_inventory leaves out when asked to be quick (slow: every
    bar is counted)."""
    t = ticker.upper()
    watch = {w["source"]: w for w in store.watchlist(ticker=t)}
    return {(feed, r["table"], r["timespan"], r["multiplier"]): r["rows"]
            for feed in set(store.feeds_holding(t)) | set(watch)
            for r in _merged_frames(store, feed, _series_tickers(store, feed, t, watch.get(feed)), True)}


def _series_tickers(store, feed: str, ticker: str, watch_row: dict | None) -> list[str]:
    """The tickers a symbol's bars are stored under on a feed: its own; a continuous future's are its
    contracts (its roll schedule), never the continuous symbol."""
    if watch_row and watch_row.get("asset") == "future" and watch_row.get("source_ticker"):
        contracts = sorted({tk for _, tk in store.rolls(feed, watch_row["source_ticker"], watch_row.get("rank") or 1)})
        return contracts or [ticker]
    return [ticker]


def _merged_frames(store, feed: str, tickers: list[str], counts: bool) -> list[dict]:
    """frame_inventory over several tickers (a future's contracts), frame by frame: the first and
    last bar over all, the counts summed, the newest built bar's derivation."""
    merged: dict[tuple, dict] = {}
    for tk in tickers:
        for row in store.frame_inventory(feed, tk, counts=counts):
            key = (row["table"], row["timespan"], row["multiplier"])
            m = merged.get(key)
            if m is None:
                merged[key] = dict(row)
                continue
            m["first"] = min(x for x in (m["first"], row["first"]) if x is not None) if row["first"] or m["first"] else None
            if row["last"] is not None and (m["last"] is None or row["last"] > m["last"]):
                m["last"], m["derivation"] = row["last"], row["derivation"]
            m["rows"] = None if m["rows"] is None or row["rows"] is None else m["rows"] + row["rows"]
    return list(merged.values())
