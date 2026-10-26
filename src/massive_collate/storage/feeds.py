"""Market data in each feed's own files: bars (feeds/<feed>.db), ticks (feeds/<feed>-ticks.db), the composite model's bars and deviation, and the old single-file layout's detection.

What was known when: each bar records when this version was stored (`stored_at`; NULL for bars
stored before tracking began). A download that brings the same values leaves the bar untouched; one
that brings different values (a correction) replaces it, and the replaced version is kept in
`bar_revisions` with the time it was replaced. `bars_window(..., as_of=...)` rebuilds a series as
it stood at a moment: only bars the feed had published by then (by its plan's rule, see
freshness.published_at), each in the version in effect then. The composite model's file keeps no
revisions: its bars are derived, rebuilt from the exchanges' bars.

Raw apart from derived: `bars` holds what the feed published; bars built here from the feed's own
ticks are derived and live apart in `built_bars`, each pointing to the derivation record that made
it (provenance.py). Reads combine the two, built bars first (they replace the feed's own for the
spans its ticks cover, as the coverage ledger records)."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from .common import BUSY_TIMEOUT_S, _date_ms, _distinct, _feed_name, _now


# A feed's own files, in the `feeds` folder beside the main file: <feed>.db holds its bars (the
# composite model's file also their deviation stats) and <feed>-ticks.db its trades, attached as
# `ticks`. Ticks get a file of their own: at about 1.5 GB a day for a hundred stocks they would
# otherwise make the bars slow to back up.
FEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,   -- bar start, ms since epoch (UTC)
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL, transactions INTEGER,
    stored_at    INTEGER,            -- ms: when this version was stored (NULL: before tracking)
    PRIMARY KEY (ticker, timespan, multiplier, ts)
) WITHOUT ROWID;
-- Bars built here (from the feed's ticks, or its finer bars for the time frames it does not send):
-- derived, apart from what the feed published. A rebuild replaces them; a bar the feed sends for
-- the same time replaces them for good (bars_replace_built). `derivation` is the record of how
-- (derivations table, main file).
CREATE TABLE IF NOT EXISTS built_bars (
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL, transactions INTEGER,
    derivation   INTEGER NOT NULL,
    stored_at    INTEGER,
    PRIMARY KEY (ticker, timespan, multiplier, ts)
) WITHOUT ROWID;
-- Bad data (docs/ARCHITECTURE.md, "Bad data"): bars taken out of the filtered view, as they were,
-- with the rule that caught them and what it compared. Source data is never deleted: a feed's own
-- bar stays in `bars`, only left out of filtered reads (excludes=1); a bar built here that a
-- rebuild replaced is kept here alone (excludes=0). Released: back in the filtered view, the record
-- kept.
CREATE TABLE IF NOT EXISTS pruned (
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL, transactions INTEGER,
    stored_at    INTEGER,            -- the pruned version's
    derivation   INTEGER,            -- built here: its derivation record; NULL: the feed's own
    rule         TEXT    NOT NULL,   -- malformed | spike | inconsistent | rebuilt | out_of_line
    excludes     INTEGER NOT NULL,   -- 1: the stored bar at this time is left out of filtered reads
    evidence     TEXT,               -- JSON: what the rule compared
    pruned_at    INTEGER NOT NULL,   -- ms
    code         TEXT,               -- the code version that pruned it
    released_at  INTEGER,            -- ms: put back in the filtered view
    PRIMARY KEY (ticker, timespan, multiplier, ts, rule, pruned_at)
) WITHOUT ROWID;
-- A symbol's newest entries, and its counts by rule, without sorting or reading millions of entries
-- (a rebuild keeps every bar it replaces: 3.8 million for one stock's year of seconds).
CREATE INDEX IF NOT EXISTS pruned_by_time ON pruned (ticker, ts);
CREATE INDEX IF NOT EXISTS pruned_by_rule ON pruned (ticker, rule, released_at);
"""


def _excluded(conn: sqlite3.Connection, ticker: str, timespan: str, multiplier: int, lo_ms: int | None = None,
              hi_ms: int | None = None) -> set:
    """Times of a series' bars left out of the filtered view (bad data, pruned and not released)."""
    sql = ("SELECT DISTINCT ts FROM pruned WHERE ticker=? AND timespan=? AND multiplier=? AND excludes=1 "
           "AND released_at IS NULL")
    args: list = [ticker.upper(), timespan, multiplier]
    if lo_ms is not None:
        sql += " AND ts >= ?"
        args.append(int(lo_ms))
    if hi_ms is not None:
        sql += " AND ts < ?"
        args.append(int(hi_ms))
    return {ts for (ts,) in conn.execute(sql, args)}


# What the feed sent first: a bar built here is used only where the feed sent none for that time.
_BAR_TABLES = ("bars", "built_bars")

# Corrections: the version a re-download replaced, with when it was stored and when it was replaced.
REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_revisions (
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL, transactions INTEGER,
    stored_at    INTEGER,            -- ms: when the replaced version was stored (NULL: before tracking)
    replaced_at  INTEGER NOT NULL,   -- ms: when the correction replaced it
    derivation   INTEGER,            -- a version built here (derivations table); NULL: the feed's own
    PRIMARY KEY (ticker, timespan, multiplier, ts, replaced_at)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS bars_revised BEFORE UPDATE ON bars
BEGIN
    INSERT OR REPLACE INTO bar_revisions (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap,
        transactions, stored_at, replaced_at) VALUES (old.ticker, old.timespan, old.multiplier, old.ts, old.open,
        old.high, old.low, old.close, old.volume, old.vwap, old.transactions, old.stored_at, new.stored_at);
END;
"""

TICKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks.trades (
    ticker     TEXT    NOT NULL,
    ts_ns      INTEGER NOT NULL,     -- SIP timestamp, ns since epoch (UTC)
    seq        INTEGER NOT NULL,     -- sequence number
    price      REAL    NOT NULL,
    size       REAL    NOT NULL,
    exchange   INTEGER,              -- exchange id, see the exchanges table
    conditions TEXT,                 -- comma-separated condition codes
    trade_id   TEXT,
    tape       INTEGER,
    PRIMARY KEY (ticker, ts_ns, seq)
) WITHOUT ROWID;
"""

# Composite model of an asset traded on several exchanges (the bars of the 'composite' feed): the
# deviation across exchanges per bar (see composite.py). Kept in the composite's own file.
COMPOSITE = "composite"

COMPOSITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS composite_stats (
    ticker    TEXT    NOT NULL,
    timespan  TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    venues    INTEGER,           -- exchanges in the bar
    sd_bps    REAL,              -- weighted standard deviation of their closes, bps
    range_bps REAL,              -- highest minus lowest close, bps
    env_h     REAL,              -- highest high of any exchange
    env_l     REAL,              -- lowest low of any exchange
    members   TEXT,              -- the exchanges, comma-separated
    outliers  TEXT,              -- exchanges far from the others' composite
    derivation INTEGER,          -- the derivation record of the build (provenance.py)
    PRIMARY KEY (ticker, timespan, ts)
) WITHOUT ROWID;
"""

# The composite model's bars carry their derivation; every rebuild sets it.
_UPSERT_COMPOSITE = """
INSERT INTO bars (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions, stored_at,
                  derivation)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, timespan, multiplier, ts) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume,
    vwap=excluded.vwap, transactions=excluded.transactions, stored_at=excluded.stored_at, derivation=excluded.derivation
"""

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "vwap", "transactions"]

MISSING_RECHECK_S = 5.0  # a feed found without files is looked for again after this long
STORED_SERIES_S = 60.0  # another process's writes show in stored_series() within this

# Only a bar whose values changed is rewritten (and so revised); the same values leave it as stored.
_UPSERT_BAR = """
INSERT INTO bars (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions, stored_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, timespan, multiplier, ts) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, vwap=excluded.vwap, transactions=excluded.transactions, stored_at=excluded.stored_at
WHERE bars.open IS NOT excluded.open OR bars.high IS NOT excluded.high OR bars.low IS NOT excluded.low
   OR bars.close IS NOT excluded.close OR bars.volume IS NOT excluded.volume OR bars.vwap IS NOT excluded.vwap
   OR bars.transactions IS NOT excluded.transactions
"""


# A bar the feed sends replaces the one built here for the same time: the built one is kept as the
# version it replaced (for what was known when), then removed.
REPLACE_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS bars_replace_built AFTER INSERT ON bars
BEGIN
    INSERT OR REPLACE INTO bar_revisions (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap,
        transactions, stored_at, replaced_at, derivation)
    SELECT ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions, stored_at,
        MAX(COALESCE(new.stored_at, 0), COALESCE(stored_at, 0)), derivation FROM built_bars
    WHERE ticker=new.ticker AND timespan=new.timespan AND multiplier=new.multiplier AND ts=new.ts;
    DELETE FROM built_bars WHERE ticker=new.ticker AND timespan=new.timespan AND multiplier=new.multiplier AND ts=new.ts;
END;
"""


# The change log: what was written when, per series and minute of stored_at (a removal: the minute
# it was removed), with the earliest and latest bar time written then. Whether a series changed,
# and what changed since a moment, are then a seek or two here instead of reading the series: the
# chart's live check counted a stock's 13 million bars (11 s) and its trades (31 GB of the tick
# file held in Windows' file cache) every few seconds while the sync wrote. Kept by triggers, so
# every writer keeps it whatever its code version; complete from `changes_since` on (set when it
# was made, moved up when old minutes are pruned: CHANGES_KEEP_MS).
CHANGES_KEEP_MS = 7 * 86_400_000  # about a million entries with the live recorder writing forty symbols
CHANGES_PRUNE_MS = 3_600_000  # pruned at most this often (a pass reads the whole log)
CHANGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    minute       INTEGER NOT NULL,   -- stored_at // 60,000 of the rows written (a removal: when removed)
    lo           INTEGER NOT NULL,   -- the earliest bar time written in it (ms)
    hi           INTEGER NOT NULL,   -- the latest
    n            INTEGER NOT NULL,   -- rows written (inserted, changed or removed)
    PRIMARY KEY (ticker, timespan, multiplier, minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS changes_since (ms INTEGER NOT NULL);
""" + "".join(f"""
CREATE TRIGGER IF NOT EXISTS {table}_logged_{event} AFTER {event.upper()} ON {table}
BEGIN
    INSERT INTO changes VALUES ({row}.ticker, {row}.timespan, {row}.multiplier, {minute}, {row}.ts, {row}.ts, 1)
    ON CONFLICT (ticker, timespan, multiplier, minute) DO UPDATE SET
        lo = MIN(lo, excluded.lo), hi = MAX(hi, excluded.hi), n = n + 1;
END;
""" for table in ("bars", "built_bars") for event, row, minute in (
    ("insert", "new", "COALESCE(new.stored_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000) / 60000"),
    ("update", "new", "COALESCE(new.stored_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000) / 60000"),
    ("delete", "old", "CAST(strftime('%s', 'now') AS INTEGER) / 60")))


class TicksBlocked(RuntimeError):
    """Bars-only mode (BARS_ONLY, config.py): this process does not open the tick files."""


# Whether a Store opens the feeds' tick files when it is not told (Store(ticks=...)): a process
# sets it once it knows its mode (cli.py: off in bars-only mode, where only the live recorder,
# which records the streams' trades, opens them).
_TICKS_DEFAULT = [True]


def set_tick_access(allowed: bool) -> None:
    _TICKS_DEFAULT[0] = bool(allowed)


def tick_access() -> bool:
    return _TICKS_DEFAULT[0]


def _stored_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _bar_row(ticker: str, timespan: str, multiplier: int, bar: dict, stored_at: int) -> tuple:
    return (
        ticker, timespan, multiplier, int(bar["t"]),
        bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
        bar.get("v"), bar.get("vw"), bar.get("n"), stored_at,
    )


def feeds_backup_dir(dest: Path) -> Path:
    """Where a backup's feed files go: a folder beside it, named after it (open the backup with
    Store(dest, feeds_dir=feeds_backup_dir(dest), datasets_dir=datasets_backup_dir(dest)))."""
    return dest.with_name(dest.stem + "-feeds")


class OldLayout(RuntimeError):
    """The database still has every feed's data in the main file and one ticks.db."""


def old_layout(conn: sqlite3.Connection, path: Path | None) -> list[str]:
    """What is left of the single-file layout (every feed's bars in the main file, every feed's
    ticks in one ticks.db beside it): the places still holding data, empty when there is nothing
    to move. Leftover tables with no rows are dropped."""
    found = []
    for table in ("bars", "composite_stats", "trades"):
        if not conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        if conn.execute(f"SELECT 1 FROM main.{table} LIMIT 1").fetchone():
            found.append(f"{table} in {path.name if path else 'the main file'}")
        else:
            with conn:
                conn.execute(f"DROP TABLE main.{table}")
    if path is not None and path.with_name("ticks.db").exists():
        found.append("ticks.db")
    return found


def _ascending(rows: list[tuple]) -> list[tuple]:
    """Bar rows (ticker, timespan, multiplier, ts, ...) oldest first. Feeds send history newest
    first, and a WITHOUT ROWID table filled from the left splits its pages half empty: the same
    bars written in descending order took 1.76 times the pages (8,007 against 4,553 for 200,000).
    The sort is stable: of two rows for one time in a write, the later still wins."""
    return sorted(rows, key=lambda r: r[3])


class _Feed:
    """One feed's files: its bars in this connection's main database and its ticks attached as
    `ticks` once it has any (both in memory when the store is)."""

    def __init__(self, name: str, folder: Path | None, check_same_thread: bool, ticks: bool = True):
        self.name = name
        self.ticks_allowed = ticks
        self.path = folder / f"{name}.db" if folder else None
        self.ticks_path = folder / f"{name}-ticks.db" if folder else None
        self.conn = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=check_same_thread,
                                    timeout=BUSY_TIMEOUT_S)
        self.conn.execute("PRAGMA journal_mode=WAL")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(bars)")}
        if cols and "stored_at" not in cols:
            with self.conn:  # a file from before versioning: a column only, the bars are not rewritten
                self.conn.execute("ALTER TABLE bars ADD COLUMN stored_at INTEGER")
        self.conn.executescript(FEED_SCHEMA + (COMPOSITE_SCHEMA if name == COMPOSITE else REVISIONS_SCHEMA))
        if name != COMPOSITE:
            if "derivation" not in {r[1] for r in self.conn.execute("PRAGMA table_info(bar_revisions)")}:
                with self.conn:  # a replaced version that was built here says so (NULL: the feed's own)
                    self.conn.execute("ALTER TABLE bar_revisions ADD COLUMN derivation INTEGER")
                    self.conn.execute("DROP TRIGGER IF EXISTS bars_revised")  # it named no columns
                self.conn.executescript(REVISIONS_SCHEMA)
            self.conn.executescript(REPLACE_SCHEMA)
        if name == COMPOSITE:  # the derived model's rows point to their derivation (columns only)
            with self.conn:
                for table in ("bars", "composite_stats"):
                    if "derivation" not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN derivation INTEGER")
        self.conn.executescript(CHANGES_SCHEMA)
        if self.conn.execute("SELECT 1 FROM changes_since").fetchone() is None:  # a write only the first time
            with self.conn:  # complete from now: the triggers exist, so every write after this is logged
                self.conn.execute("INSERT INTO changes_since SELECT ? WHERE NOT EXISTS (SELECT 1 FROM changes_since)",
                                  (_stored_ms(),))
        self.ticks = False
        self._lock = threading.Lock()
        self.has_ticks()

    def has_ticks(self) -> bool:
        """Whether the feed has a tick file, attaching it when one has appeared (another process
        may have written the feed's first ticks). Never in bars-only mode."""
        if self.ticks_allowed and not self.ticks and self.ticks_path is not None and self.ticks_path.exists():
            self.attach_ticks()
        return self.ticks

    def attach_ticks(self) -> None:
        if not self.ticks_allowed:
            raise TicksBlocked(f"bars-only mode: {self.name}'s tick file is not opened here (BARS_ONLY=off allows it)")
        with self._lock:
            if self.ticks:
                return
            self.conn.execute("ATTACH DATABASE ? AS ticks", (str(self.ticks_path) if self.ticks_path else ":memory:",))
            self.conn.execute("PRAGMA ticks.journal_mode=WAL")
            self.conn.executescript(TICKS_SCHEMA)
            self.ticks = True

    def version(self) -> tuple:
        bars = self.conn.execute("PRAGMA data_version").fetchone()[0]
        return (bars, self.conn.execute("PRAGMA ticks.data_version").fetchone()[0]) if self.ticks else (bars,)

    def files(self) -> list[Path]:
        if self.path is None:
            return []
        return [f for base in (self.path, self.ticks_path) for f in (base, base.with_name(base.name + "-wal"))
                if f.exists()]


class FeedStore:
    """Store methods: market data in each feed's own files: bars (feeds/<feed>.db), ticks (feeds/<feed>-ticks.db), the composite model's bars and deviation, and the old single-file layout's detection."""

    # -- feed files ----------------------------------------------------------

    def _scan_feeds(self) -> None:
        """Open the files of feeds that appeared since the last look (another process may have
        written a feed's first data). Only when the folder has changed: one stat per call."""
        if self.feeds_dir is None or not self.feeds_dir.is_dir():
            return
        seen = self.feeds_dir.stat().st_mtime_ns
        if seen == self._feeds_seen:
            return
        self._feeds_seen = seen
        for f in self.feeds_dir.glob("*.db"):
            name = f.stem.removesuffix("-ticks")
            if _feed_name(name):
                self._missing.pop(name, None)  # it has files now, whatever a look a moment ago found
                self._feed(name)

    def _feed(self, name: str, create: bool = False) -> _Feed | None:
        """A feed's files, opened on first use. None for a feed without files unless `create`."""
        feed = self._feeds.get(name)
        if feed is not None:
            return feed
        if not _feed_name(name):
            if create:
                raise ValueError(f"not a feed name: {name!r}")
            return None
        if not create:
            # A feed without files is looked for again only after a few seconds: asked about for every
            # symbol (thousands per page), checking the disk each time cost seconds.
            seen = self._missing.get(name)
            if seen is not None and time.monotonic() - seen < MISSING_RECHECK_S:
                return None
            if self.feeds_dir is None or not any(
                    (self.feeds_dir / f).exists() for f in (f"{name}.db", f"{name}-ticks.db")):
                self._missing[name] = time.monotonic()
                return None
        self._missing.pop(name, None)
        with self._feeds_lock:
            feed = self._feeds.get(name)
            if feed is None:
                if self.feeds_dir is not None:
                    self.feeds_dir.mkdir(parents=True, exist_ok=True)
                feed = self._feeds[name] = _Feed(name, self.feeds_dir, self._check_same_thread, self.ticks_allowed)
        return feed

    def _open_feeds(self, scan: bool = True) -> list[_Feed]:
        """Every feed with files, by name."""
        if scan:
            self._scan_feeds()
        with self._feeds_lock:
            return [f for _, f in sorted(self._feeds.items())]

    def feed_names(self) -> list[str]:
        return [f.name for f in self._open_feeds()]

    def _bars(self, source: str) -> sqlite3.Connection | None:
        """The connection holding a feed's bars, or None if the feed has no files."""
        feed = self._feed(source)
        return feed.conn if feed else None

    def _ticks(self, source: str) -> sqlite3.Connection | None:
        """The connection a feed's ticks are attached to (as `ticks`), or None if it has none."""
        feed = self._feed(source)
        return feed.conn if feed and feed.has_ticks() else None

    # -- trades (ticks) ----------------------------------------------------

    def upsert_trades(self, source: str, ticker: str, trades: Iterable[dict]) -> int:
        ticker = ticker.upper()
        rows = sorted((  # oldest first: see _ascending
            (
                ticker, int(t["sip_timestamp"]), int(t.get("sequence_number") or 0),
                t["price"], t["size"], t.get("exchange"),
                ",".join(str(c) for c in t.get("conditions") or []) or None,
                t.get("id"), t.get("tape"),
            )
            for t in trades
        ), key=lambda r: (r[1], r[2]))
        feed = self._feed(source, create=True)
        feed.attach_ticks()
        with feed.conn:
            feed.conn.executemany("INSERT OR REPLACE INTO ticks.trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._wrote()
        self.rows_saved += len(rows)
        return len(rows)

    def withdraw_trades(self, source: str, ticker: str, seqs: Iterable[int], since_ns: int) -> list[int]:
        """Remove trades the exchange withdrew (canceled, or replaced by a correction), by sequence
        number, from `since_ns` on. Returns the times (ns) of those removed."""
        conn = self._ticks(source)
        if conn is None:
            return []
        ticker, gone = ticker.upper(), []
        with conn:
            for seq in set(seqs):
                where = ("WHERE ticker=? AND ts_ns>=? AND seq=?", (ticker, since_ns, int(seq)))
                gone += [ts for (ts,) in conn.execute("SELECT ts_ns FROM ticks.trades " + where[0], where[1])]
                conn.execute("DELETE FROM ticks.trades " + where[0], where[1])
        self._wrote()
        return gone

    def trade_range(self, source: str, ticker: str) -> tuple[int, int] | None:
        """(oldest ns, newest ns) stored for a ticker's trades, or None."""
        conn = self._ticks(source)
        if conn is None:
            return None
        # two statements: SQLite answers a lone MIN or MAX from the index, both together by a scan
        lo = conn.execute("SELECT MIN(ts_ns) FROM ticks.trades WHERE ticker=?", (ticker.upper(),)).fetchone()[0]
        if lo is None:
            return None
        return lo, conn.execute("SELECT MAX(ts_ns) FROM ticks.trades WHERE ticker=?", (ticker.upper(),)).fetchone()[0]

    def load_trades(
        self,
        ticker: str,
        source: str | None = None,
        start: date | None = None,
        end: date | None = None,
        exchanges: Sequence[int] | None = None,
    ) -> pd.DataFrame:
        """Trades with a `time` column in US/Eastern (ns precision) and the exchange name."""
        sql = "SELECT ts_ns, seq, price, size, exchange, conditions FROM ticks.trades WHERE ticker=?"
        params: list = [ticker.upper()]
        if start:
            sql += " AND ts_ns >= ?"
            params.append(_date_ms(start) * 1_000_000)
        if end:
            sql += " AND ts_ns < ?"
            params.append((_date_ms(end) + 2 * 86_400_000) * 1_000_000)
        if exchanges:
            sql += f" AND exchange IN ({','.join('?' * len(exchanges))})"
            params += list(exchanges)
        frames = []
        for feed in [self._feed(source)] if source else self._open_feeds():
            if feed is not None and feed.has_ticks():
                frames.append(pd.read_sql_query(sql + " ORDER BY ts_ns, seq", feed.conn, params=params))
                frames[-1].insert(0, "source", feed.name)
        cols = ["source", "time", "price", "size", "exchange", "exchange_name", "conditions", "seq"]
        if not frames:
            return pd.DataFrame(columns=cols)
        df = pd.concat(frames, ignore_index=True)
        if len(frames) > 1:
            df = df.sort_values(["ts_ns", "seq"], kind="stable", ignore_index=True)
        names = {(s, i): n for s, i, n in self.conn.execute("SELECT source, id, name FROM exchanges")}
        df["exchange_name"] = [None if pd.isna(e) else names.get((s, int(e)))
                               for s, e in zip(df["source"], df["exchange"])]
        df["time"] = pd.to_datetime(df.pop("ts_ns"), unit="ns", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        return df[cols].reset_index(drop=True)

    # -- bars --------------------------------------------------------------

    def upsert_bars(self, source: str, ticker: str, timespan: str, multiplier: int, bars: Iterable[dict]) -> int:
        ticker = ticker.upper()
        stored = _stored_ms()
        rows = _ascending([_bar_row(ticker, timespan, multiplier, b, stored) for b in bars])
        conn = self._feed(source, create=True).conn
        with conn:
            conn.executemany(_UPSERT_BAR, rows)
        self._wrote()
        self.rows_saved += len(rows)
        return len(rows)

    def upsert_built_bars(self, source: str, ticker: str, timespan: str, multiplier: int, bars: Iterable[dict],
                          derivation: int) -> int:
        """Bars built here (derived: from the feed's ticks, or its finer bars): kept apart from its
        own, replacing an earlier build of the same bars, each pointing to `derivation`. None goes
        where the feed sent a bar for that time. Returns bars stored."""
        ticker = ticker.upper()
        stored = _stored_ms()
        rows = _ascending([(*_bar_row(ticker, timespan, multiplier, b, stored)[:11], derivation, stored) for b in bars])
        conn = self._feed(source, create=True).conn
        with conn:  # the statement's own rows (total_changes would count the change log's too)
            n = conn.executemany(
                "INSERT OR REPLACE INTO built_bars SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM bars WHERE ticker=? AND timespan=? AND multiplier=? AND ts=?)",
                [(*r, *r[:4]) for r in rows]).rowcount
        self._wrote()
        return max(0, n)

    def close_before(self, source: str, ticker: str, timespan: str, before_ms: int) -> float | None:
        """The close of a series' last bar before `before_ms` (the feed's own or built): what a
        window of bars built from ticks starts from (ticks.build_bars' prior_close)."""
        conn = self._bars(source)
        if conn is None:
            return None
        best = None
        for table in _BAR_TABLES:
            row = conn.execute(f"SELECT ts, close FROM {table} WHERE ticker=? AND timespan=? AND multiplier=1 AND ts<? "
                               "ORDER BY ts DESC LIMIT 1", (ticker.upper(), timespan, before_ms)).fetchone()
            if row and (best is None or row[0] > best[0]):
                best = row
        return best[1] if best else None

    def drop_built_frame(self, source: str, ticker: str, timespan: str, multiplier: int, lo_ms: int, hi_ms: int) -> int:
        """Remove a frame's built bars in [lo, hi) ms (before building them again: a bin whose base
        bars are gone must go too). Returns bars removed."""
        conn = self._bars(source)
        if conn is None:
            return 0
        with conn:
            n = conn.execute("DELETE FROM built_bars WHERE ticker=? AND timespan=? AND multiplier=? AND ts>=? AND ts<?",
                             (ticker.upper(), timespan, multiplier, lo_ms, hi_ms)).rowcount
        self._wrote()
        return n

    def drop_built_between(self, source: str, ticker: str, timespans: Sequence[str], lo_ms: int, hi_ms: int) -> int:
        """Remove a series' built bars in [lo, hi) ms, before they are built again from ticks some of
        which were withdrawn (a bar left without trades must not stay). Returns bars removed."""
        conn = self._bars(source)
        if conn is None:
            return 0
        marks = ",".join("?" * len(timespans))
        with conn:
            n = conn.execute(f"DELETE FROM built_bars WHERE ticker=? AND timespan IN ({marks}) AND multiplier=1 "
                             "AND ts>=? AND ts<?", (ticker.upper(), *timespans, lo_ms, hi_ms)).rowcount
        self._wrote()
        return n

    def drop_superseded_built(self, source: str) -> int:
        """Built bars the feed has since sent its own for (from before bars_replace_built): kept
        as the versions they were, then removed. Returns bars removed."""
        conn = self._bars(source)
        if conn is None:
            return 0
        same = ("b.ticker=d.ticker AND b.timespan=d.timespan AND b.multiplier=d.multiplier AND b.ts=d.ts")
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO bar_revisions (ticker, timespan, multiplier, ts, open, high, low, close, volume, "
                "vwap, transactions, stored_at, replaced_at, derivation) SELECT d.ticker, d.timespan, d.multiplier, d.ts, "
                "d.open, d.high, d.low, d.close, d.volume, d.vwap, d.transactions, d.stored_at, "
                f"MAX(COALESCE(b.stored_at, 0), COALESCE(d.stored_at, 0), 1), d.derivation FROM built_bars d JOIN bars b ON {same}")
            n = conn.execute(f"DELETE FROM built_bars AS d WHERE EXISTS (SELECT 1 FROM bars b WHERE {same})").rowcount
        self._wrote()
        return n

    def bars_window(self, sources: Sequence[str], ticker: str, timespan: str, lo_ms: int, hi_ms: int,
                    as_of: int | None = None, published=None, filtered: bool = True) -> pd.DataFrame:
        """Bars of a series in [lo, hi) ms from the given feeds (an index range scan per feed), with
        a `time` column in US/Eastern like load_bars: filtered (bad data left out, the pruned
        list), or with `filtered=False` all of them. With `as_of` (ms): the series as it stood
        then: only bars published by then (`published(ts array) -> ms array`, the feed's rule; all
        if None), each in the version in effect then (the earliest stored if none was stored yet),
        bad data included (it was there)."""
        if as_of is not None:
            frames = [self._bars_as_of(src, ticker, timespan, lo_ms, hi_ms, as_of, published) for src in sources]
            frames = [f for f in frames if f is not None]
        else:
            frames = []
            for conn in (self._bars(src) for src in sources):
                if conn is None:
                    continue
                gone = _excluded(conn, ticker, timespan, 1, lo_ms, hi_ms) if filtered else ()
                for table in _BAR_TABLES:
                    f = pd.read_sql_query(
                        f"SELECT ts, open, high, low, close, volume, vwap, transactions, {int(table == 'built_bars')} "
                        f"AS derived FROM {table} WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ? "
                        "ORDER BY ts", conn, params=(ticker.upper(), timespan, int(lo_ms), int(hi_ms)))
                    frames.append(f[~f["ts"].isin(gone)] if gone else f)
        frames = [f for f in frames if len(f)]  # an empty read has untyped columns: it would turn numbers to objects
        df = pd.concat(frames) if frames else pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "vwap", "transactions", "derived"])
        df = df.sort_values("ts", kind="stable").drop_duplicates("ts")  # one feed per span: no real duplicates
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York")  # ts kept: ms
        return df.reset_index(drop=True)

    def _bars_as_of(self, source: str, ticker: str, timespan: str, lo_ms: int, hi_ms: int, as_of: int,
                    published) -> pd.DataFrame | None:
        feed = self._feed(source)
        if feed is None:
            return None
        cols = ["ts", *BAR_COLUMNS]
        args = (ticker.upper(), timespan, int(lo_ms), int(hi_ms))
        cur, built = (pd.read_sql_query(f"SELECT {', '.join(cols)}, stored_at FROM {table} WHERE ticker=? AND "
                                        "timespan=? AND multiplier=1 AND ts >= ? AND ts < ?", feed.conn, params=args)
                      for table in ("bars", "built_bars"))
        cur["until"], cur["prio"] = float("inf"), 0  # the feed's own first where both were in effect
        built["until"], built["prio"] = float("inf"), 1
        has_revisions = feed.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_revisions'").fetchone()
        rev = pd.read_sql_query(f"SELECT {', '.join(cols)}, stored_at, replaced_at AS until, derivation FROM bar_revisions "
                                "WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ?",
                                feed.conn, params=args) if has_revisions else cur.iloc[0:0].assign(derivation=None)
        rev["prio"] = rev.pop("derivation").notna().astype(int)  # a replaced built version ranks as built
        versions = pd.concat([f for f in (built, cur, rev) if len(f)] or [cur], ignore_index=True)
        if versions.empty:
            return versions[cols]
        since = versions["stored_at"].astype("float").fillna(float("-inf"))
        versions["effective"] = (since <= as_of) & (versions["until"] > as_of)
        versions["order"] = versions["until"]  # the earliest version: the one replaced first
        versions = versions.sort_values(["ts", "effective", "prio", "order"], ascending=[True, False, True, True])
        out = versions.drop_duplicates("ts")
        if published is not None:
            out = out[published(out["ts"].to_numpy()) <= as_of]
        return out[cols].sort_values("ts").reset_index(drop=True)

    def bar_revisions(self, source: str, ticker: str, timespan: str = "minute") -> pd.DataFrame:
        """Every replaced version of a series' bars, with when it was stored and replaced."""
        conn = self._bars(source)
        if conn is None or not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_revisions'").fetchone():
            return pd.DataFrame(columns=["ts", *BAR_COLUMNS, "stored_at", "replaced_at"])
        return pd.read_sql_query(
            f"SELECT ts, {', '.join(BAR_COLUMNS)}, stored_at, replaced_at FROM bar_revisions WHERE ticker=? "
            "AND timespan=? AND multiplier=1 ORDER BY ts, replaced_at", conn, params=(ticker.upper(), timespan))

    def bar_rows(self, source: str, ticker: str, timespan: str, lo_ms: int, hi_ms: int) -> list[tuple]:
        """(ts, open, high, low, close, volume, vwap, transactions) of one feed's series in [lo, hi) ms."""
        conn = self._bars(source)
        if conn is None:
            return []
        rows: dict[int, tuple] = {}
        for table in reversed(_BAR_TABLES):  # the feed's own last, so they replace any built here
            for r in conn.execute(f"SELECT ts, open, high, low, close, volume, vwap, transactions FROM {table} "
                                  "WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ?",
                                  (ticker.upper(), timespan, lo_ms, hi_ms)):
                rows[r[0]] = r
        return [rows[t] for t in sorted(rows)]

    def bar_range(self, source: str, ticker: str, timespan: str, multiplier: int = 1) -> tuple[int, int] | None:
        """(oldest ts, newest ts) in ms stored for a series, or None if empty."""
        return self.bar_edges(source, ticker, timespan, multiplier)

    def load_bars(
        self,
        tickers: Sequence[str] | None = None,
        timespan: str = "day",
        multiplier: int = 1,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
        filtered: bool = True,
    ) -> pd.DataFrame:
        """Bars with a `time` column in US/Eastern time: filtered (bad data left out, the pruned
        list), or with `filtered=False` all of them."""
        where = " WHERE timespan=? AND multiplier=?"
        params: list = [timespan, multiplier]
        if tickers:
            where += f" AND ticker IN ({','.join('?' * len(tickers))})"
            params += [t.upper() for t in tickers]
        if start:
            where += " AND ts >= ?"
            params.append(_date_ms(start))
        if end:
            where += " AND ts < ?"
            params.append(_date_ms(end) + 2 * 86_400_000)
        sql = "SELECT ticker, ts, " + ", ".join(BAR_COLUMNS) + " FROM {table}" + where
        frames = []
        for feed in [self._feed(source)] if source else self._open_feeds():
            if feed is not None:
                gone = pd.read_sql_query("SELECT DISTINCT ticker, ts FROM pruned" + where + " AND excludes=1 "
                                         "AND released_at IS NULL", feed.conn, params=params) if filtered else None
                for table in _BAR_TABLES:
                    f = pd.read_sql_query(sql.format(table=table) + " ORDER BY ticker, ts", feed.conn, params=params)
                    if gone is not None and len(gone) and len(f):
                        keys = pd.MultiIndex.from_frame(f[["ticker", "ts"]])
                        f = f[~keys.isin(pd.MultiIndex.from_frame(gone))].copy()
                    frames.append(f)
                    frames[-1].insert(0, "source", feed.name)
                    frames[-1]["derived"] = table == "built_bars"
        frames = [f for f in frames if len(f)]  # an empty read has untyped columns
        if not frames:
            return pd.DataFrame(columns=["source", "ticker", "time", *BAR_COLUMNS, "derived"])
        df = pd.concat(frames, ignore_index=True).drop_duplicates(["source", "ticker", "ts"])  # the feed's own first
        if len(frames) > 1:
            df = df.sort_values(["ticker", "ts"], kind="stable", ignore_index=True)
        df["time"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        return df[["source", "ticker", "time", *BAR_COLUMNS, "derived"]].reset_index(drop=True)

    # -- grouped (whole-market daily) --------------------------------------

    def save_grouped_day(self, source: str, day: date, bars: list[dict]) -> int:
        stored = _stored_ms()
        rows = [_bar_row(b["T"].upper(), "day", 1, b, stored) for b in bars if b.get("T")]
        if rows:
            conn = self._feed(source, create=True).conn
            with conn:
                conn.executemany(_UPSERT_BAR, rows)
        with self.conn:  # marked after the bars are in: a failure between means fetching the day again
            self._mark_day(source, day, "ok" if rows else "empty", len(rows))
        self._wrote()
        return len(rows)

    def mark_grouped_day(self, source: str, day: date, status: str) -> None:
        with self.conn:
            self._mark_day(source, day, status, 0)

    def _mark_day(self, source: str, day: date, status: str, count: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO grouped_days VALUES (?, ?, ?, ?, ?)",
            (source, day.isoformat(), status, count, _now()),
        )

    def grouped_days(self, source: str) -> dict[date, str]:
        cur = self.conn.execute("SELECT day, status FROM grouped_days WHERE source=?", (source,))
        return {date.fromisoformat(d): s for d, s in cur}

    def clear_denied(self, source: str) -> int:
        """Forget the market days a plan refused (asked again after a plan change, or by
        `admin reset-denied`). Returns days."""
        with self.conn:
            return self.conn.execute(
                "DELETE FROM grouped_days WHERE source=? AND status='denied'", (source,)
            ).rowcount

    def delete_before(self, source: str, ticker: str, cutoff_ns: int | None = None) -> int:
        """Delete a ticker's ticks and bars older than cutoff_ns (all of them if None)."""
        ticker = ticker.upper()
        cutoff_ns = cutoff_ns if cutoff_ns is not None else 2**62
        feed = self._feed(source)
        if feed is None:
            return 0
        ticks = feed.has_ticks()
        with feed.conn:
            n = feed.conn.execute(
                "DELETE FROM ticks.trades WHERE ticker=? AND ts_ns < ?", (ticker, cutoff_ns)
            ).rowcount if ticks else 0
            for table in _BAR_TABLES:
                n += feed.conn.execute(f"DELETE FROM {table} WHERE ticker=? AND ts < ?",
                                       (ticker, cutoff_ns // 10**6)).rowcount
        self._wrote()
        return n

    def series_fingerprint(self, source: str, ticker: str) -> tuple:
        """Value that changes whenever a ticker's stored data changes (for caching).
        Recomputed only after that feed's files have changed: the live recorder writing one
        feed every few seconds must not make every other feed count its rows again."""
        feed = self._feed(source)
        version = (feed.version() if feed is not None else None, self._writes)
        key = ("fp", source, ticker.upper())
        hit = self._cache.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
        value = self._fingerprint(source, ticker)
        self._cache[key] = (version, value)
        return value

    @staticmethod
    def _logged_since(conn: sqlite3.Connection) -> int | None:
        """From when the change log is complete (ms), or None without one."""
        row = conn.execute("SELECT MAX(ms) FROM changes_since").fetchone()
        return row[0] if row else None

    def prune_changes(self, keep_ms: int = CHANGES_KEEP_MS, now_ms: int | None = None) -> int:
        """Forget the change log's minutes older than `keep_ms` in every feed (a reader further
        behind reads the series' stored_at instead, once). Returns entries removed."""
        horizon = (now_ms if now_ms is not None else _stored_ms()) - keep_ms
        n = 0
        for feed in self._open_feeds():
            if (self._logged_since(feed.conn) or 0) >= horizon - CHANGES_PRUNE_MS:
                continue
            with feed.conn:
                n += feed.conn.execute("DELETE FROM changes WHERE minute < ?", (horizon // 60_000,)).rowcount
                feed.conn.execute("UPDATE changes_since SET ms=?", (horizon // 60_000 * 60_000,))
        return n

    def changed_span(self, source: str, ticker: str, timespan: str, stored_after: int) -> tuple[int, int] | None:
        """(first, last) start in ms of a series' bars, its own or built, stored after
        `stored_after` (ms) (or removed since: from the change log, which may also name the rest of
        that minute's writes): the span a build from them has to redo. None if nothing changed."""
        conn = self._bars(source)
        if conn is None:
            return None
        since = self._logged_since(conn)
        if since is not None and stored_after >= since:
            lo, hi = conn.execute("SELECT MIN(lo), MAX(hi) FROM changes WHERE ticker=? AND timespan=? AND multiplier=1 "
                                  "AND minute >= ?", (ticker.upper(), timespan, stored_after // 60_000)).fetchone()
            return None if lo is None else (lo, hi)
        spans = [conn.execute(f"SELECT MIN(ts), MAX(ts) FROM {table} WHERE ticker=? AND timespan=? AND multiplier=1 "
                              "AND stored_at > ?", (ticker.upper(), timespan, stored_after)).fetchone()
                 for table in _BAR_TABLES]
        spans = [s for s in spans if s[0] is not None]
        return (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else None

    def count_bars(self, source: str, ticker: str, timespan: str, multiplier: int = 1) -> int:
        """Bars one feed holds for a series at one level, its own and built (a range count each)."""
        conn = self._bars(source)
        if conn is None:
            return 0
        key = (ticker.upper(), timespan, multiplier)
        return sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ticker=? AND timespan=? AND multiplier=?", key).fetchone()[0]
                   for t in _BAR_TABLES)

    def frame_inventory(self, source: str, ticker: str, counts: bool = True) -> list[dict]:
        """Every frame one feed holds for a symbol, as stored: {table (bars: the feed's own,
        built_bars: built here), timespan, multiplier, first, last (bar starts, ms), rows,
        derivation (of the newest built bar)}. Index seeks and range counts only; without
        `counts`, seeks only (rows None): a count runs through every bar of a frame, twenty
        seconds for a stock's millions of seconds and minutes."""
        conn = self._bars(source)
        if conn is None:
            return []
        t = ticker.upper()
        out = []
        for table in _BAR_TABLES:
            frame = conn.execute(f"SELECT timespan, multiplier FROM {table} WHERE ticker=? "
                                 "ORDER BY timespan, multiplier LIMIT 1", (t,)).fetchone()
            while frame is not None:  # one seek per frame (a loose index scan within the ticker)
                key = (t, *frame)
                where = f"FROM {table} WHERE ticker=? AND timespan=? AND multiplier=?"
                if counts:
                    first, last, rows = conn.execute(f"SELECT MIN(ts), MAX(ts), COUNT(*) {where}", key).fetchone()
                else:  # each an index seek when asked alone
                    first = conn.execute(f"SELECT MIN(ts) {where}", key).fetchone()[0]
                    last, rows = conn.execute(f"SELECT MAX(ts) {where}", key).fetchone()[0], None
                derivation = None
                if table == "built_bars":
                    derivation = conn.execute(f"SELECT derivation {where} ORDER BY ts DESC LIMIT 1", key).fetchone()[0]
                out.append({"table": table, "timespan": frame[0], "multiplier": frame[1], "first": first,
                            "last": last, "rows": rows, "derivation": derivation})
                # the next frame: a larger multiplier of this timespan, else the next timespan (two range
                # seeks; one row-value comparison walked through every bar of the frame, 0.5 s for a
                # stock's minutes)
                frame = conn.execute(f"SELECT timespan, multiplier FROM {table} WHERE ticker=? AND timespan=? "
                                     "AND multiplier > ? ORDER BY multiplier LIMIT 1", key).fetchone() \
                    or conn.execute(f"SELECT timespan, multiplier FROM {table} WHERE ticker=? AND timespan > ? "
                                    "ORDER BY timespan, multiplier LIMIT 1", (t, frame[0])).fetchone()
        return out

    def feeds_holding(self, ticker: str) -> list[str]:
        """The feeds with bars or trades of a symbol (a seek each: stored_series reads every symbol
        of every feed, again whenever any file changes)."""
        t = ticker.upper()
        out = []
        for feed in self._open_feeds():
            held = any(feed.conn.execute(f"SELECT 1 FROM {table} WHERE ticker=? LIMIT 1", (t,)).fetchone()
                       for table in _BAR_TABLES)
            if not held and feed.has_ticks():
                held = feed.conn.execute("SELECT 1 FROM ticks.trades WHERE ticker=? LIMIT 1", (t,)).fetchone() is not None
            if held:
                out.append(feed.name)
        return out

    def bars_changed_since(self, source: str, ticker: str, timespans: Sequence[str], stored_after: int) -> dict:
        """Per timespan, (earliest bar time, newest stored_at) of the ticker's bars, its own or
        built, stored after `stored_after` (ms): what a cached series has to load again."""
        feed = self._feed(source)
        out: dict[str, tuple[int, int]] = {}
        if feed is None or not timespans:
            return out
        marks = ",".join("?" * len(timespans))
        since = self._logged_since(feed.conn)
        if since is not None and stored_after >= since:  # the change log: the minute's end for the newest
            for span, lo, minute in feed.conn.execute(
                    f"SELECT timespan, MIN(lo), MAX(minute) FROM changes WHERE ticker=? AND timespan IN ({marks}) "
                    "AND multiplier=1 AND minute >= ? GROUP BY timespan",
                    (ticker.upper(), *timespans, stored_after // 60_000)):
                out[span] = (lo, minute * 60_000 + 59_999)
            return out
        for table in _BAR_TABLES:
            rows = feed.conn.execute(
                f"SELECT timespan, MIN(ts), MAX(stored_at) FROM {table} WHERE ticker=? AND timespan IN ({marks})"
                " AND multiplier=1 AND stored_at > ? GROUP BY timespan",
                (ticker.upper(), *timespans, stored_after)).fetchall()
            for span, lo, hi in rows:
                if lo is None:
                    continue
                was = out.get(span)
                out[span] = (min(was[0], lo), max(was[1], hi)) if was else (lo, hi)
        return out

    def _fingerprint(self, source: str, ticker: str) -> tuple:
        # From the change log (a seek), never by reading the series: its writes, corrections and
        # removals since the log began (its start too: pruning it changes every fingerprint once).
        # Ticks: the newest one (an index seek), where this process opens tick files at all.
        feed = self._feed(source)
        if feed is None:
            return (None, 0, None, None)
        t = ticker.upper()
        n, newest = feed.conn.execute("SELECT SUM(n), MAX(minute) FROM changes WHERE ticker=?", (t,)).fetchone()
        tick = feed.conn.execute("SELECT MAX(ts_ns) FROM ticks.trades WHERE ticker=?", (t,)).fetchone()[0] \
            if feed.has_ticks() else None
        return (self._logged_since(feed.conn), n or 0, newest, tick)

    def stored_series(self) -> list[tuple]:
        """(source, ticker, kind) for every series with data, kind = trades | second | minute | ...
        Listed again at once after this store's own writes; after another process's, at most once
        a STORED_SERIES_S (a new series is rare): a listing is a seek per series, some 200 MB of
        index pages for a large feed, and the sync and the live recorder write every few seconds
        (the chart app's status poll relisted them every two seconds, reading 110 MB/s)."""
        self._scan_feeds()  # one stat: a feed's first file (another process wrote it) is listed at once
        with self._feeds_lock:
            feeds = tuple(sorted(self._feeds))
        hit = self._cache.get("stored_series")
        now = time.monotonic()
        if hit is not None and hit[0][-1] == self._writes and hit[3] == feeds:
            if now - hit[2] < STORED_SERIES_S:
                return hit[1]
            version = self._version()
            if version == hit[0]:
                self._cache["stored_series"] = (version, hit[1], now, feeds)
                return hit[1]
        version = self._version()
        value = self._stored_series()
        self._cache["stored_series"] = (version, value, now, feeds)
        return value

    def _stored_series(self) -> list[tuple]:
        # Loose index scans: one primary-key seek per distinct series instead of reading every row.
        trades, bars = [], []
        for feed in self._open_feeds():
            if feed.has_ticks():
                trades += [(feed.name, t, "trades") for (t,) in _distinct(feed.conn, "ticks.trades", ("ticker",),
                                                                           ("ts_ns", "seq"))]
            for table in _BAR_TABLES:
                bars += [(feed.name, t, ts) for t, ts, m in _distinct(feed.conn, table, ("ticker", "timespan", "multiplier"),
                                                                      ("ts",)) if m == 1]
        return trades + list(dict.fromkeys(bars))  # a series both built and published: once

    def trade_edges(self, source: str, ticker: str) -> tuple[int, int] | None:
        """(oldest, newest) tick in ns: two index seeks."""
        conn = self._ticks(source)
        if conn is None:
            return None
        key = (ticker.upper(),)
        lo = conn.execute("SELECT MIN(ts_ns) FROM ticks.trades WHERE ticker=?", key).fetchone()[0]
        if lo is None:
            return None
        return lo, conn.execute("SELECT MAX(ts_ns) FROM ticks.trades WHERE ticker=?", key).fetchone()[0]

    def trades_between(self, source: str, ticker: str, start_ns: int, end_ns: int) -> pd.DataFrame:
        """Ticks in [start, end) for building bars: ts_ns, price, size, exchange, conditions, tape."""
        conn = self._ticks(source)
        if conn is None:
            return pd.DataFrame(columns=["ts_ns", "price", "size", "exchange", "conditions", "tape"])
        return pd.read_sql_query(
            "SELECT ts_ns, price, size, exchange, conditions, tape FROM ticks.trades WHERE ticker=? "
            "AND ts_ns >= ? AND ts_ns < ? ORDER BY ts_ns, seq", conn, params=(ticker.upper(), start_ns, end_ns))

    def ticks_bytes(self) -> int:
        """Size of the feeds' tick files (and their write-ahead logs)."""
        if self.feeds_dir is None or not self.feeds_dir.is_dir():
            return 0
        return sum(f.stat().st_size for f in self.feeds_dir.iterdir() if f.name.endswith(("-ticks.db", "-ticks.db-wal")))

    def feed_bytes(self) -> dict[str, int]:
        """{feed: bytes of its bar file and its write-ahead log}."""
        return {f.name: sum(p.stat().st_size for p in f.files() if "-ticks.db" not in p.name) for f in self._open_feeds()}

    def series_holders(self) -> dict[tuple[str, str], list[str]]:
        """{(ticker, kind): [feeds holding it]}, from one look at the stored series: callers asking
        about thousands of symbols take it once (each stored_series() call checks every feed file
        for changes, and a sync writing meanwhile makes each check rebuild the list)."""
        out: dict[tuple[str, str], list[str]] = {}
        for s, t, k in self.stored_series():
            out.setdefault((t, k), []).append(s)
        return out

    def newest_any(self, ticker: str, timespan: str, holders: dict | None = None) -> tuple[str, int] | None:
        """(feed, ms) of the newest bar of a series in any feed: one index seek per feed holding it
        (`holders`: series_holders() taken once by a caller asking about many symbols)."""
        ticker = ticker.upper()
        best = None
        feeds = (holders.get((ticker, timespan), ()) if holders is not None
                 else {s for s, t, k in self.stored_series() if t == ticker and k == timespan})
        for src in feeds:
            newest = max((m for m in (self._bars(src).execute(  # MAX alone is answered from the index's last entry
                f"SELECT MAX(ts) FROM {table} WHERE ticker=? AND timespan=? AND multiplier=1",
                (ticker, timespan)).fetchone()[0] for table in _BAR_TABLES) if m is not None), default=None)
            if newest is not None and (best is None or newest > best[1]):
                best = (src, newest)
        return best

    # -- composite model and quality -----------------------------------------

    def save_composite(self, ticker: str, timespan: str, comp: pd.DataFrame, derivation: int | None = None) -> int:
        """Composite bars (the 'composite' feed) and their deviation across exchanges, together, both
        pointing to the build's derivation record."""
        if comp.empty:
            return 0
        ticker = ticker.upper()
        stored = _stored_ms()
        bars = _ascending([_bar_row(ticker, timespan, 1, {
            "t": int(t), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v,
            "vw": None if pd.isna(r.vw) else r.vw, "n": None if pd.isna(r.n) else int(r.n)}, stored) + (derivation,)
            for t, r in zip(comp.index, comp.itertuples())])
        rows = [(ticker, timespan, int(t), int(r.venues), float(r.sd_bps), float(r.range_bps), float(r.env_h),
                 float(r.env_l), r.members, r.outliers, derivation) for t, r in zip(comp.index, comp.itertuples())]
        conn = self._feed(COMPOSITE, create=True).conn
        with conn:
            conn.executemany(_UPSERT_COMPOSITE, bars)
            conn.executemany("INSERT OR REPLACE INTO composite_stats (ticker, timespan, ts, venues, sd_bps, range_bps, "
                             "env_h, env_l, members, outliers, derivation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._wrote()
        return len(rows)

    def drop_single_exchange_composites(self) -> int:
        """Composite bars built from one exchange only (they are copies of it): removed."""
        conn = self._bars(COMPOSITE)
        if conn is None:
            return 0
        with conn:
            rows = conn.execute("SELECT ticker, timespan, ts FROM composite_stats WHERE venues < 2").fetchall()
            conn.executemany("DELETE FROM bars WHERE ticker=? AND timespan=? AND multiplier=1 AND ts=?", rows)
            conn.execute("DELETE FROM composite_stats WHERE venues < 2")
        self._wrote()
        return len(rows)

    def composite_stats(self, ticker: str, timespan: str, lo_ms: int | None = None, hi_ms: int | None = None) -> pd.DataFrame:
        cols = ["ts", "venues", "sd_bps", "range_bps", "env_h", "env_l", "members", "outliers"]
        conn = self._bars(COMPOSITE)
        if conn is None:
            return pd.DataFrame(columns=cols)
        sql = f"SELECT {', '.join(cols)} FROM composite_stats WHERE ticker=? AND timespan=?"
        args: list = [ticker.upper(), timespan]
        if lo_ms is not None:
            sql += " AND ts >= ?"
            args.append(lo_ms)
        if hi_ms is not None:
            sql += " AND ts < ?"
            args.append(hi_ms)
        return pd.read_sql_query(sql + " ORDER BY ts", conn, params=args)

    def series_moves(self, source: str, tickers: Iterable[str]) -> dict[str, dict]:
        """Latest daily change of stored series from their own day bars: {ticker: {day (UTC date
        of the newest bar), close, prevClose, changePct}}. Tickers without two day bars are left out."""
        out = {}
        conn = self._bars(source)
        for t in tickers if conn is not None else ():
            rows = conn.execute(
                "SELECT ts, close FROM bars WHERE ticker=? AND timespan='day' AND multiplier=1 "
                "ORDER BY ts DESC LIMIT 2", (t.upper(),)).fetchall()
            if len(rows) == 2 and rows[0][1] and rows[1][1]:
                day = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).date().isoformat()
                out[t] = {"day": day, "close": rows[0][1], "prevClose": rows[1][1],
                          "changePct": (rows[0][1] / rows[1][1] - 1) * 100}
        return out

    def daily_snapshot(self, source: str | None = None, sessions: int = 30) -> dict[str, dict]:
        """Per ticker from local daily bars: average dollar volume over the last `sessions`
        sessions (volume x VWAP, close when VWAP is missing), last close and the one before."""
        def compute():
            out: dict[str, dict] = {}
            for src, ticker, kind in self.stored_series():
                if kind != "day" or (source and src != source) or ticker in out:
                    continue
                rows = self._bars(src).execute(
                    "SELECT close, volume * COALESCE(vwap, close) FROM bars WHERE ticker=? "
                    "AND timespan='day' AND multiplier=1 ORDER BY ts DESC LIMIT ?", (ticker, sessions)).fetchall()
                if rows:
                    out[ticker] = {"close": rows[0][0], "prevClose": rows[1][0] if len(rows) > 1 else None,
                                   "dollarVolume": sum(v or 0.0 for _, v in rows) / len(rows)}
            return out
        return self._cached(("daily_snapshot", source, sessions), compute)

    def last_days(self, source: str, tickers: list[str], n: int = 2) -> dict[str, list[tuple]]:
        """{ticker: [(ts, close, volume), ...]}: each ticker's newest `n` daily bars on a feed, newest
        first (one index seek a ticker: a filter on the date alone scanned every minute and second
        bar the feed holds)."""
        conn = self._bars(source)
        if conn is None:
            return {}
        out = {}
        for t in tickers:
            rows = conn.execute("SELECT ts, close, volume FROM bars WHERE ticker=? AND timespan='day' AND multiplier=1 "
                                "ORDER BY ts DESC LIMIT ?", (t.upper(), n)).fetchall()
            if rows:
                out[t.upper()] = rows
        return out

    def bar_edges(self, source: str, ticker: str, timespan: str = "minute", multiplier: int = 1,
                  received_only: bool = False) -> tuple | None:
        """(first, last) bar start in ms for one series (with `received_only`, of what the feed sent,
        not bars built here). Two index lookups, however much is stored."""
        conn = self._bars(source)
        if conn is None:
            return None
        key = (ticker.upper(), timespan, multiplier)
        edges = []
        for table in ("bars",) if received_only else _BAR_TABLES:
            where = f"FROM {table} WHERE ticker=? AND timespan=? AND multiplier=?"
            first = conn.execute(f"SELECT MIN(ts) {where}", key).fetchone()[0]
            if first is not None:
                edges.append((first, conn.execute(f"SELECT MAX(ts) {where}", key).fetchone()[0]))
        return (min(e[0] for e in edges), max(e[1] for e in edges)) if edges else None

    def separate_built_bars(self, log=print) -> int:
        """One-off: bars built from ticks before they were kept apart (in the feed's own bars, over the
        spans the coverage ledger marks as built from ticks) move to built_bars, under one derivation
        record per series whose settings are unknown. Returns bars moved."""
        moved = 0
        spans = self.conn.execute("SELECT source, ticker, timespan, start_ms, end_ms FROM coverage "
                                  "WHERE origin='ticks' ORDER BY source, ticker, timespan, start_ms").fetchall()
        for src, ticker, timespan, a, b in spans:
            conn = self._bars(src)
            if conn is None:
                continue
            where = "WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ?"
            args = (ticker, timespan, a, b)
            n = conn.execute(f"SELECT COUNT(*) FROM bars {where}", args).fetchone()[0]
            if not n:
                continue
            did = self.record_derivation(
                "bars-from-ticks", f"{src}:{ticker}:{timespan}", "before-provenance",
                {"note": "built before provenance was recorded; settings not known"},
                {"ticks": f"{src}:{ticker}", "moved_from": "the feed's bars"}, (a, b), n)
            with conn:
                conn.execute(f"INSERT OR REPLACE INTO built_bars SELECT ticker, timespan, multiplier, ts, open, high, "
                             f"low, close, volume, vwap, transactions, ?, stored_at FROM bars {where}", (did, *args))
                conn.execute(f"DELETE FROM bars {where}", args)
            moved += n
            log(f"{src} {ticker} {timespan}: {n:,} bars built from ticks kept apart (derivation {did})")
        self._wrote()
        return moved
