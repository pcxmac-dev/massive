"""The coverage ledger: which feed holds which span of a series (one feed per span)."""

from __future__ import annotations

from datetime import date, datetime

from .common import _ET


class LedgerStore:
    """Store methods: the coverage ledger: which feed holds which span of a series (one feed per span)."""

    # -- coverage: which feed holds which span (one feed per span) ----------

    STEP_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 7 * 86_400_000}

    def add_coverage(self, source: str, ticker: str, timespan: str, start_ms: int, end_ms: int,
                     origin: str = "download") -> None:
        """Record that `source` holds [start, end) of a series, merged with its touching spans of
        the same origin (downloaded, or built here from its stored ticks)."""
        if end_ms <= start_ms:
            return
        ticker = ticker.upper()
        key = (source, ticker, timespan)
        with self.conn:
            rows = self.conn.execute(
                "SELECT start_ms, end_ms FROM coverage WHERE source=? AND ticker=? AND timespan=? AND origin=? "
                "AND end_ms >= ? AND start_ms <= ?", (*key, origin, start_ms, end_ms)).fetchall()
            lo = min([start_ms] + [r[0] for r in rows])
            hi = max([end_ms] + [r[1] for r in rows])
            self.conn.executemany("DELETE FROM coverage WHERE source=? AND ticker=? AND timespan=? AND start_ms=?",
                                  [(*key, r[0]) for r in rows])
            self.conn.execute("INSERT OR REPLACE INTO coverage (source, ticker, timespan, start_ms, end_ms, origin) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (*key, lo, hi, origin))

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
            with self.conn:
                self.conn.execute("DELETE FROM coverage WHERE source=? AND ticker=? AND timespan=? AND start_ms=?",
                                  (src, ticker, ts, a))
            self.add_coverage(src, ticker, ts, a, head_end, origin="none")
            if first is not None:
                self.add_coverage(src, ticker, ts, first, b)
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

    def _seed_coverage(self, ticker: str, timespan: str) -> None:
        """A series stored before the ledger: its feed holds from its first bar (or the start of
        the day it was backfilled to) through its last bar."""
        have = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT source FROM coverage WHERE ticker=? AND timespan=?", (ticker, timespan))}
        sources = {r[0] for r in self.conn.execute(
            "SELECT source FROM watchlist WHERE ticker=?", (ticker,))} | {
            s for s, t, k in self.stored_series() if t == ticker and k == timespan}
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
