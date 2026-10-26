"""The pruned list: each feed's bad data (docs/ARCHITECTURE.md, "Bad data"), kept apart, never lost.

An entry is a bar as it was when it was pruned, the rule that caught it, the evidence (the numbers
the rule compared, JSON), when and by which code. A feed's own bar stays where it is and is only
left out of filtered reads (`excludes`); a bar built here that a rebuild replaced lives on here
alone. Releasing an entry puts the bar back in the filtered view and keeps the record."""

from __future__ import annotations

import json
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .common import write_transaction
from .feeds import BAR_COLUMNS, _log_changes, _stored_ms

RULES = ("malformed", "spike", "inconsistent", "rebuilt", "out_of_line")
_VALUES = ["open", "high", "low", "close", "volume", "vwap", "transactions"]
_ms_now = _stored_ms  # the clock stored_at is kept by


class PrunedStore:
    """Store methods: the pruned list (bad data) of each feed."""

    def prune(self, source: str, ticker: str, timespan: str, multiplier: int, bars: pd.DataFrame, rule: str,
              excludes: bool, evidence: Sequence[dict] | None = None, code: str | None = None) -> int:
        """Put bars (columns ts and the bar values; stored_at, derivation if known) on a feed's
        pruned list under `rule`, each with its evidence. A time already pruned by this rule (also
        one released since: a release is a decision) is left as it is; under "rebuilt", a version
        already listed (its stored_at and derivation): each rebuild keeps the version it replaced,
        so a second repair keeps the one the first made (by time alone it was passed over, and lost).
        Looked for and written in one write transaction (two scans of a series at once listed a bar
        twice). A bar left out of the filtered view (`excludes`) is logged in the change log, as a
        write is (feeds._LOG_CHANGE). Returns entries added."""
        if rule not in RULES:
            raise ValueError(f"rule must be one of {RULES}")
        if bars is None or bars.empty:
            return 0
        from ..provenance import code_version

        feed = self._feed(source, create=True)
        ticker = ticker.upper()
        now, code = _ms_now(), code or code_version()
        evidence = list(evidence) if evidence is not None else [{}] * len(bars)
        cols = {k: (bars[k].to_numpy(dtype=float) if k in bars else np.full(len(bars), np.nan))
                for k in (*_VALUES, "stored_at", "derivation")}
        num = lambda v, whole=False: None if np.isnan(v) else (int(v) if whole else float(v))
        ts = bars["ts"].to_numpy(dtype="int64")
        version = rule == "rebuilt"  # a version is listed once; any other rule lists a time once
        same = " AND stored_at IS ? AND derivation IS ?" if version else ""
        rows = []
        for i in range(len(ts)):
            key = (ticker, timespan, multiplier, int(ts[i]), rule)
            stored_at, derivation = num(cols["stored_at"][i], True), num(cols["derivation"][i], True)
            rows.append((*key[:4], *(num(cols[k][i], k == "transactions") for k in _VALUES), stored_at, derivation,
                         rule, int(bool(excludes)), json.dumps(evidence[i], default=float), now, code,
                         *key, *((stored_at, derivation) if version else ())))
        with write_transaction(feed.conn):
            added = feed.conn.executemany(
                "INSERT OR IGNORE INTO pruned (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, "
                "transactions, stored_at, derivation, rule, excludes, evidence, pruned_at, code) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM pruned "
                f"WHERE ticker=? AND timespan=? AND multiplier=? AND ts=? AND rule=?{same})", rows).rowcount
            if excludes and added:  # the entries this call made (its pruned_at) change the filtered view
                _log_changes(feed.conn, ticker, timespan, multiplier, [t for (t,) in feed.conn.execute(
                    "SELECT ts FROM pruned WHERE ticker=? AND timespan=? AND multiplier=? AND rule=? AND pruned_at=? "
                    "AND ts >= ? AND ts <= ?", (ticker, timespan, multiplier, rule, now, int(ts.min()), int(ts.max())))],
                             now)
        self._wrote()
        return max(0, added)

    def pruned(self, source: str | None = None, ticker: str | None = None, timespan: str | None = None,
               rule: str | None = None, active: bool = False, limit: int | None = None) -> pd.DataFrame:
        """Pruned-list entries, newest first, with a `source` column; `active`: only those still
        left out (not released)."""
        where, args = [], []
        for col, val in (("ticker", ticker.upper() if ticker else None), ("timespan", timespan), ("rule", rule)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if active:
            where.append("released_at IS NULL")
        sql = "SELECT * FROM pruned" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        frames = []
        for feed in [self._feed(source)] if source else self._open_feeds():
            if feed is not None:
                f = pd.read_sql_query(sql, feed.conn, params=args)
                if len(f):
                    f.insert(0, "source", feed.name)
                    frames.append(f)
        if not frames:
            return pd.DataFrame(columns=["source", "ticker", "timespan", "multiplier", "ts", *_VALUES, "stored_at",
                                         "derivation", "rule", "excludes", "evidence", "pruned_at", "code",
                                         "released_at"])
        out = pd.concat(frames, ignore_index=True).sort_values("ts", ascending=False, kind="stable")
        return out.head(limit).reset_index(drop=True) if limit else out.reset_index(drop=True)

    def pruned_counts(self, ticker: str) -> dict[str, dict[str, int]]:
        """{feed: {rule: entries}} of a symbol's pruned list (released ones under "released")."""
        out: dict[str, dict[str, int]] = {}
        for feed in self._open_feeds():
            for rule, released, n in feed.conn.execute(
                    "SELECT rule, released_at IS NOT NULL, COUNT(*) FROM pruned WHERE ticker=? GROUP BY 1, 2",
                    (ticker.upper(),)):
                key = "released" if released else rule
                out.setdefault(feed.name, {})[key] = out.get(feed.name, {}).get(key, 0) + n
        return out

    def release_pruned(self, source: str, ticker: str, timespan: str, multiplier: int, ts: int,
                       rule: str | None = None) -> int:
        """Put a pruned bar back in the filtered view (the entry stays, marked released). Logged in
        the change log as a write of that bar (feeds._LOG_CHANGE), so the frames built above it
        are built again with it and the chart's caches see it; nothing changed that otherwise.
        (The command line also marks derive's span dirty, for a series whose last build is older
        than the change log.)"""
        feed = self._feed(source)
        if feed is None:
            return 0
        where = "WHERE ticker=? AND timespan=? AND multiplier=? AND ts=? AND released_at IS NULL"
        args: list = [ticker.upper(), timespan, multiplier, int(ts)]
        if rule:
            where += " AND rule=?"
            args.append(rule)
        now = _ms_now()
        with write_transaction(feed.conn):
            shown = feed.conn.execute(f"SELECT 1 FROM pruned {where} AND excludes=1 LIMIT 1", args).fetchone()
            n = feed.conn.execute(f"UPDATE pruned SET released_at=? {where}", [now, *args]).rowcount
            if shown:  # it was left out of the filtered view: now it is in it
                _log_changes(feed.conn, ticker.upper(), timespan, multiplier, [int(ts)], now)
        self._wrote()
        return n

    def pruned_tickers(self, source: str | None = None) -> list[str]:
        """The symbols with a pruned list on a feed (every feed without one), without reading the
        list: one seek per symbol in pruned_by_time (a loose index scan; the list holds millions of
        entries, and reading them whole to list their symbols took gigabytes)."""
        out: set[str] = set()
        for feed in [self._feed(source)] if source else self._open_feeds():
            if feed is None:
                continue
            row = feed.conn.execute("SELECT ticker FROM pruned INDEXED BY pruned_by_time ORDER BY ticker LIMIT 1").fetchone()
            while row is not None:
                out.add(row[0])
                row = feed.conn.execute("SELECT ticker FROM pruned INDEXED BY pruned_by_time WHERE ticker > ? "
                                        "ORDER BY ticker LIMIT 1", row).fetchone()
        return sorted(out)

    def series_rows(self, source: str, ticker: str, timespan: str, lo_ms: int, hi_ms: int) -> pd.DataFrame:
        """All of a series' bars in [lo, hi) as stored, bad data included: the feed's own first, those
        built here where it sent none; with stored_at, and derivation (NULL for the feed's own)."""
        conn = self._bars(source)
        cols = ["ts", *BAR_COLUMNS, "stored_at"]
        if conn is None:
            return pd.DataFrame(columns=[*cols, "derivation"])
        args = (ticker.upper(), timespan, int(lo_ms), int(hi_ms))
        where = "WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ?"
        own = pd.read_sql_query(f"SELECT {', '.join(cols)}, NULL AS derivation FROM bars {where}", conn, params=args)
        built = pd.read_sql_query(f"SELECT {', '.join(cols)}, derivation FROM built_bars {where}", conn, params=args)
        frames = [f for f in (own, built) if len(f)]
        if not frames:
            return own
        df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)

    def built_rows(self, source: str, ticker: str, lo_ms: int, hi_ms: int,
                   frames: Iterable[tuple[str, int]] | None = None) -> pd.DataFrame:
        """A series' bars built here in [lo, hi) (every frame, or those given as (timespan,
        multiplier)), with their derivation and stored_at: what a rebuild may replace."""
        conn = self._bars(source)
        cols = ["timespan", "multiplier", "ts", *BAR_COLUMNS, "derivation", "stored_at"]
        if conn is None:
            return pd.DataFrame(columns=cols)
        df = pd.read_sql_query(f"SELECT {', '.join(cols)} FROM built_bars WHERE ticker=? AND ts >= ? AND ts < ?",
                               conn, params=(ticker.upper(), int(lo_ms), int(hi_ms)))
        if frames is not None:
            wanted = set(frames)
            df = df[[(t, m) in wanted for t, m in zip(df["timespan"], df["multiplier"])]]
        return df.reset_index(drop=True)

    def prune_replaced(self, source: str, ticker: str, before: pd.DataFrame, after: pd.DataFrame, why: str) -> int:
        """Keep on the pruned list (rule "rebuilt") the built bars of `before` (built_rows) that a
        rebuild changed or removed (`after`: the same rows read again), each with what replaced it.
        Unchanged bars are not listed. Returns entries added."""
        if before is None or before.empty:
            return 0
        key = ["timespan", "multiplier", "ts"]
        merged = before.merge(after[key + _VALUES] if after is not None and len(after) else
                              pd.DataFrame(columns=key + _VALUES), on=key, how="left", suffixes=("", "_now"),
                              indicator=True)
        gone = merged["_merge"] == "left_only"
        changed = np.zeros(len(merged), dtype=bool)
        for col in _VALUES:
            a, b = merged[col].to_numpy(dtype=float), merged[f"{col}_now"].to_numpy(dtype=float)
            same = np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
            changed |= ~same
        hit = merged[gone.to_numpy() | changed]
        added = 0
        for (timespan, multiplier), group in hit.groupby(["timespan", "multiplier"], sort=False):
            now = {col: group[f"{col}_now"].to_numpy(dtype=float) for col in _VALUES}
            left = (group["_merge"] == "left_only").to_numpy()
            evidence = [{"why": why, "now": None if left[i] else
                         {col: (None if np.isnan(now[col][i]) else float(now[col][i])) for col in _VALUES}}
                        for i in range(len(group))]
            added += self.prune(source, ticker, timespan, int(multiplier), group, "rebuilt", excludes=False,
                                evidence=evidence)
        return added
