"""The coverage ledger: which feed holds which span of a series (one feed per span)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from .common import _ET, write_transaction
from .schema import COVERAGE_TABLE


def _key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in sorted(conn.execute(f"PRAGMA table_info({table})"), key=lambda r: r[5]) if r[5]]


def key_coverage_by_origin(conn: sqlite3.Connection) -> bool:
    """A ledger from before origin was part of its key (a 'ticks' span and a 'download' span of a
    series starting at the same ms replaced each other, and the lost one was fetched again) is
    rebuilt with it: a new table, every span copied, the old table dropped and the new one renamed,
    all in one transaction (a failure leaves the old ledger as it was). The ledger holds a few rows
    per series: well under a second. Returns whether it ran."""
    if "origin" in _key_columns(conn, "coverage"):
        return False
    with write_transaction(conn):
        if "origin" in _key_columns(conn, "coverage"):  # another process upgraded it meanwhile
            return False
        conn.execute("DROP TABLE IF EXISTS coverage_keyed")  # a leftover cannot exist (one transaction); to be sure
        conn.execute(COVERAGE_TABLE.format(name="coverage_keyed"))
        conn.execute("INSERT INTO coverage_keyed (source, ticker, timespan, start_ms, end_ms, origin) "
                     "SELECT source, ticker, timespan, start_ms, end_ms, origin FROM coverage")
        conn.execute("DROP TABLE coverage")
        conn.execute("ALTER TABLE coverage_keyed RENAME TO coverage")
        conn.execute("CREATE INDEX IF NOT EXISTS coverage_series ON coverage (ticker, timespan)")
    return True


class LedgerStore:
    """Store methods: the coverage ledger: which feed holds which span of a series (one feed per span)."""

    # -- coverage: which feed holds which span (one feed per span) ----------

    STEP_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 7 * 86_400_000}

    def add_coverage(self, source: str, ticker: str, timespan: str, start_ms: int, end_ms: int,
                     origin: str = "download") -> None:
        """Record that `source` holds [start, end) of a series, merged with its touching spans of
        the same origin (downloaded, or built here from its stored ticks). Read and written in one
        write transaction: the sync and the live recorder record spans of the same series."""
        if end_ms <= start_ms:
            return
        with write_transaction(self.conn):
            self._add_coverage(source, ticker.upper(), timespan, start_ms, end_ms, origin)

    def _add_coverage(self, source: str, ticker: str, timespan: str, start_ms: int, end_ms: int, origin: str) -> None:
        """add_coverage inside the caller's write transaction."""
        key = (source, ticker, timespan, origin)
        rows = self.conn.execute(
            "SELECT start_ms, end_ms FROM coverage WHERE source=? AND ticker=? AND timespan=? AND origin=? "
            "AND end_ms >= ? AND start_ms <= ?", (*key, start_ms, end_ms)).fetchall()
        lo = min([start_ms] + [r[0] for r in rows])
        hi = max([end_ms] + [r[1] for r in rows])
        self.conn.executemany("DELETE FROM coverage WHERE source=? AND ticker=? AND timespan=? AND origin=? "
                              "AND start_ms=?", [(*key, r[0]) for r in rows])
        self.conn.execute("INSERT OR REPLACE INTO coverage (source, ticker, timespan, origin, start_ms, end_ms) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (*key, lo, hi))

    def drop_coverage(self, source: str, ticker: str, before_ms: int | None = None) -> int:
        """Forget a feed's spans of a ticker (those starting before `before_ms`, or all), after its
        rows there were deleted (the symbol now means another instrument): they are asked for again."""
        with self.conn:
            if before_ms is None:
                return self.conn.execute("DELETE FROM coverage WHERE source=? AND ticker=?",
                                         (source, ticker.upper())).rowcount
            n = self.conn.execute("DELETE FROM coverage WHERE source=? AND ticker=? AND end_ms <= ?",
                                  (source, ticker.upper(), before_ms)).rowcount
            self.conn.execute("UPDATE coverage SET start_ms=? WHERE source=? AND ticker=? AND start_ms < ?",
                              (before_ms, source, ticker.upper(), before_ms))
        return n

    def mark_empty_heads(self, min_ms: int = 7 * 86_400_000) -> int:
        """Downloaded spans whose first stored bar comes long after the span starts (the feed's
        history begins later than it was asked for): the empty head becomes 'none' for that feed,
        so another feed may fill it. Returns spans changed."""
        rows = self.conn.execute("SELECT source, ticker, timespan, start_ms, end_ms FROM coverage "
                                 "WHERE origin='download' AND timespan IN ('minute', 'day', 'second')").fetchall()
        changed = 0
        for src, ticker, ts, a, b in rows:
            conn = self._bars(src)
            first = conn.execute("SELECT MIN(ts) FROM bars WHERE ticker=? AND timespan=? AND multiplier=1 "
                                 "AND ts >= ? AND ts < ?", (ticker, ts, a, b)).fetchone()[0] if conn else None
            head_end = first if first is not None else b
            if head_end - a < min_ms:
                continue
            # The span split in one transaction (it was three: a failure between them lost the span,
            # and its history was asked for again); only if no one changed it since it was read.
            with write_transaction(self.conn):
                key = (src, ticker, ts, a)
                if self.conn.execute("SELECT 1 FROM coverage WHERE source=? AND ticker=? AND timespan=? AND "
                                     "origin='download' AND start_ms=? AND end_ms=?", (*key, b)).fetchone() is None:
                    continue
                self.conn.execute("DELETE FROM coverage WHERE source=? AND ticker=? AND timespan=? AND "
                                  "origin='download' AND start_ms=?", key)
                self._add_coverage(src, ticker, ts, a, head_end, "none")
                if first is not None:
                    self._add_coverage(src, ticker, ts, first, b, "download")
            changed += 1
        return changed

    def coverage_detail(self, ticker: str, timespan: str) -> list[tuple[str, str, int, int]]:
        """(feed, origin, start, end) for every span of a series, oldest first (for attribution)."""
        self._seed_coverage(ticker.upper(), timespan)
        return [tuple(r) for r in self.conn.execute(
            "SELECT source, origin, start_ms, end_ms FROM coverage WHERE ticker=? AND timespan=? AND origin != 'none' "
            "ORDER BY start_ms",
            (ticker.upper(), timespan))]

    def coverage(self, ticker: str, timespan: str, source: str | None = None,
                 include_none: bool = False, received_only: bool = False) -> dict[str, list[tuple[int, int]]]:
        """{feed: [(start, end), ...]} for a series, oldest first: the spans each feed holds. With
        `include_none`, also the spans a feed was asked for and has nothing in (its history starts
        later): done for that feed, still open for others. Series stored before the ledger existed
        are entered from their stored range first (`_seed_coverage`). With `received_only`, only
        what feeds sent (not bars built here from their ticks)."""
        ticker = ticker.upper()
        self._seed_coverage(ticker, timespan)
        sql = "SELECT source, start_ms, end_ms FROM coverage WHERE ticker=? AND timespan=?"
        if received_only:
            sql += " AND origin = 'download'"
        elif not include_none:
            sql += " AND origin != 'none'"
        args: list = [ticker, timespan]
        if source:
            sql += " AND source=?"
            args.append(source)
        out: dict[str, list[tuple[int, int]]] = {}
        for src, a, b in self.conn.execute(sql + " ORDER BY start_ms", args):
            out.setdefault(src, []).append((a, b))
        return out

    def daily_from(self) -> dict[str, int]:
        """{ticker: the start (ms) of the oldest daily span any feed holds}: one pass over the
        ledger, no bars read (the audit of daily history and the daily-first sweep)."""
        return dict(self.conn.execute(
            "SELECT ticker, MIN(start_ms) FROM coverage WHERE timespan='day' AND origin != 'none' GROUP BY ticker"))

    def holds_any(self, source: str) -> bool:
        """Whether `source` has ever stored a span (a feed that never sent anything holds none)."""
        return self.conn.execute("SELECT 1 FROM coverage WHERE source=? LIMIT 1", (source,)).fetchone() is not None

    def _seed_coverage(self, ticker: str, timespan: str) -> None:
        """A series stored before the ledger: its feed holds from its first bar (or the start of
        the day it was backfilled to) through its last bar."""
        have = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT source FROM coverage WHERE ticker=? AND timespan=?", (ticker, timespan))}
        # the feeds holding the symbol: a seek or two each (stored_series lists every series of every
        # feed, again whenever any file changes: rebuilt 15 times in one status request while the live
        # recorder wrote); a feed without bars of this timespan has no edges below and is passed over
        sources = {r[0] for r in self.conn.execute(
            "SELECT source FROM watchlist WHERE ticker=?", (ticker,))} | set(self.feeds_holding(ticker))
        for src in sources - have:
            r = self.bar_edges(src, ticker, timespan, received_only=True)  # two index seeks; bars built here are not held
            if not r:
                continue
            start = r[0]
            row = self.conn.execute("SELECT backfilled_to FROM watchlist WHERE source=? AND ticker=?",
                                    (src, ticker)).fetchone()
            if row and row[0]:
                d = date.fromisoformat(row[0])
                start = min(start, int(datetime(d.year, d.month, d.day, tzinfo=_ET).timestamp() * 1000))
            self.add_coverage(src, ticker, timespan, start, r[1] + self.STEP_MS.get(timespan, 60_000))
