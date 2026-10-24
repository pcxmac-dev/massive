"""What one symbol holds, feed by feed and frame by frame.

For each feed with data for the symbol (or watching it): the frames it sent (prime data), the
frames built here from its finer data (*derived, with the derivation record saying from what and
how), its ticks, what it is set to collect, and which frames it should have built but has not yet
(derive.py builds every standard frame above each base frame a feed holds: 30 s from seconds;
5 and 30 min, 1, 2 and 4 h and 1 day from minutes; weeks, months and years from days). Feeds come
in the order a merged chart takes them. Read by the chart's side panel (/api/inventory) and
`python -m massive_collate inventory TICKER`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import derive

# Every standard frame, finest first: (timespan, multiplier).
FRAMES = (("second", 1), ("second", 30), ("minute", 1), *derive.FROM_MINUTES[:-1], ("day", 1),
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


def symbol_inventory(store, ticker: str, rank: list[str] | None = None) -> dict:
    """The symbol's data on every feed (see the module docstring). `rank`: feeds in the order a
    merged chart takes them (BarEngine.sources_for), listed first."""
    t = ticker.upper()
    watch = {w["source"]: w for w in store.watchlist(ticker=t)}
    held = {s for s, tk, _ in store.stored_series() if tk == t}
    rank = [f for f in (rank or []) if f in held or f in watch]
    order = rank + sorted((held | set(watch)) - set(rank))
    records: dict[int, dict | None] = {}
    feeds = []
    for feed in order:
        stored = store.frame_inventory(feed, t)
        frames: dict[tuple[str, int], dict] = {}
        for row in stored:
            key = (row["timespan"], row["multiplier"])
            part = {"first": _iso(row["first"]), "last": _iso(row["last"]), "rows": row["rows"]}
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
        ticks = store.trade_edges(feed, t)
        w = watch.get(feed)
        feeds.append({
            "feed": feed, "model": feed in MODEL_FEEDS, "merged": feed in rank,
            "watch": {k: (str(w[k]) if w.get(k) is not None else None)
                      for k in ("asset", "resolution", "backfilled_to", "synced_at", "closed_at")} if w else None,
            "ticks": {"first": _iso(ticks[0] // 1_000_000), "last": _iso(ticks[1] // 1_000_000)} if ticks else None,
            "frames": [{"frame": derive.label(ts, m), "timespan": ts, "multiplier": m, **frames[(ts, m)]}
                       for ts, m in sorted(frames, key=_duration)],
            "notBuilt": [derive.label(ts, m) for ts, m in sorted(expected(bases) - set(frames), key=_duration)],
        })
    return {"ticker": t, "frames": [derive.label(ts, m) for ts, m in FRAMES], "feeds": feeds}


_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400, "week": 604_800, "month": 2_629_746,
            "year": 31_556_952}


def _duration(frame: tuple[str, int]) -> int:
    return _SECONDS.get(frame[0], 0) * frame[1]
