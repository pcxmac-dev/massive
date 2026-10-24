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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from .common import _date_ms, _distinct, _feed_name, _now


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
-- Bars built here from the feed's ticks: derived, apart from what the feed published. A rebuild
-- replaces them; `derivation` is the record of how (derivations table, main file).
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
"""

# Raw first? No: built bars come first where both exist (they replace the feed's own for tick spans).
_BAR_TABLES = ("built_bars", "bars")

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
    PRIMARY KEY (ticker, timespan, multiplier, ts, replaced_at)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS bars_revised BEFORE UPDATE ON bars
BEGIN
    INSERT OR REPLACE INTO bar_revisions VALUES (old.ticker, old.timespan, old.multiplier, old.ts, old.open,
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


class _Feed:
    """One feed's files: its bars in this connection's main database and its ticks attached as
    `ticks` once it has any (both in memory when the store is)."""

    def __init__(self, name: str, folder: Path | None, check_same_thread: bool):
        self.name = name
        self.path = folder / f"{name}.db" if folder else None
        self.ticks_path = folder / f"{name}-ticks.db" if folder else None
        self.conn = sqlite3.connect(str(self.path) if self.path else ":memory:", check_same_thread=check_same_thread)
        self.conn.execute("PRAGMA journal_mode=WAL")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(bars)")}
        if cols and "stored_at" not in cols:
            with self.conn:  # a file from before versioning: a column only, the bars are not rewritten
                self.conn.execute("ALTER TABLE bars ADD COLUMN stored_at INTEGER")
        self.conn.executescript(FEED_SCHEMA + (COMPOSITE_SCHEMA if name == COMPOSITE else REVISIONS_SCHEMA))
        if name == COMPOSITE:  # the derived model's rows point to their derivation (columns only)
            with self.conn:
                for table in ("bars", "composite_stats"):
                    if "derivation" not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN derivation INTEGER")
        self.ticks = False
        self._lock = threading.Lock()
        self.has_ticks()

    def has_ticks(self) -> bool:
        """Whether the feed has a tick file, attaching it when one has appeared (another process
        may have written the feed's first ticks)."""
        if not self.ticks and self.ticks_path is not None and self.ticks_path.exists():
            self.attach_ticks()
        return self.ticks

    def attach_ticks(self) -> None:
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
        if not create and (self.feeds_dir is None or not any(
                (self.feeds_dir / f).exists() for f in (f"{name}.db", f"{name}-ticks.db"))):
            return None
        with self._feeds_lock:
            feed = self._feeds.get(name)
            if feed is None:
                if self.feeds_dir is not None:
                    self.feeds_dir.mkdir(parents=True, exist_ok=True)
                feed = self._feeds[name] = _Feed(name, self.feeds_dir, self._check_same_thread)
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
        rows = [
            (
                ticker, int(t["sip_timestamp"]), int(t.get("sequence_number") or 0),
                t["price"], t["size"], t.get("exchange"),
                ",".join(str(c) for c in t.get("conditions") or []) or None,
                t.get("id"), t.get("tape"),
            )
            for t in trades
        ]
        feed = self._feed(source, create=True)
        feed.attach_ticks()
        with feed.conn:
            feed.conn.executemany("INSERT OR REPLACE INTO ticks.trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._wrote()
        return len(rows)

    def trade_range(self, source: str, ticker: str) -> tuple[int, int] | None:
        """(oldest ns, newest ns) stored for a ticker's trades, or None."""
        conn = self._ticks(source)
        if conn is None:
            return None
        row = conn.execute("SELECT MIN(ts_ns), MAX(ts_ns) FROM ticks.trades WHERE ticker=?", (ticker.upper(),)).fetchone()
        return None if row[0] is None else (row[0], row[1])

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
        rows = [_bar_row(ticker, timespan, multiplier, b, stored) for b in bars]
        conn = self._feed(source, create=True).conn
        with conn:
            conn.executemany(_UPSERT_BAR, rows)
        self._wrote()
        return len(rows)

    def upsert_built_bars(self, source: str, ticker: str, timespan: str, multiplier: int, bars: Iterable[dict],
                          derivation: int) -> int:
        """Bars built here from the feed's ticks (derived): kept apart from its own, replacing an
        earlier build of the same bars, each pointing to `derivation`."""
        ticker = ticker.upper()
        stored = _stored_ms()
        rows = [(*_bar_row(ticker, timespan, multiplier, b, stored)[:11], derivation, stored) for b in bars]
        conn = self._feed(source, create=True).conn
        with conn:
            conn.executemany("INSERT OR REPLACE INTO built_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._wrote()
        return len(rows)

    def bars_window(self, sources: Sequence[str], ticker: str, timespan: str, lo_ms: int, hi_ms: int,
                    as_of: int | None = None, published=None) -> pd.DataFrame:
        """Bars of a series in [lo, hi) ms from the given feeds (an index range scan per feed), with
        a `time` column in US/Eastern like load_bars. With `as_of` (ms): the series as it stood
        then: only bars published by then (`published(ts array) -> ms array`, the feed's rule; all
        if None), each in the version in effect then (the earliest stored if none was stored yet)."""
        if as_of is not None:
            frames = [self._bars_as_of(src, ticker, timespan, lo_ms, hi_ms, as_of, published) for src in sources]
            frames = [f for f in frames if f is not None]
        else:
            frames = [pd.read_sql_query(
                f"SELECT ts, open, high, low, close, volume, vwap, transactions FROM {table} WHERE ticker=? "
                "AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ? ORDER BY ts", conn,
                params=(ticker.upper(), timespan, int(lo_ms), int(hi_ms)))
                for conn in (self._bars(src) for src in sources) if conn is not None for table in _BAR_TABLES]
        frames = [f for f in frames if len(f)]  # an empty read has untyped columns: it would turn numbers to objects
        df = pd.concat(frames) if frames else pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "vwap", "transactions"])
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
        built, cur = (pd.read_sql_query(f"SELECT {', '.join(cols)}, stored_at FROM {table} WHERE ticker=? AND "
                                        "timespan=? AND multiplier=1 AND ts >= ? AND ts < ?", feed.conn, params=args)
                      for table in _BAR_TABLES)
        built["until"], built["prio"] = float("inf"), 0  # derived from ticks: first where both exist
        cur["until"], cur["prio"] = float("inf"), 1
        has_revisions = feed.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_revisions'").fetchone()
        rev = pd.read_sql_query(f"SELECT {', '.join(cols)}, stored_at, replaced_at AS until FROM bar_revisions "
                                "WHERE ticker=? AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ?",
                                feed.conn, params=args) if has_revisions else cur.iloc[0:0]
        rev["prio"] = 1
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
        for table in reversed(_BAR_TABLES):  # built bars last, so they replace the feed's own
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
    ) -> pd.DataFrame:
        """Bars with a `time` column in US/Eastern time."""
        sql = "SELECT ticker, ts, " + ", ".join(BAR_COLUMNS) + " FROM {table} WHERE timespan=? AND multiplier=?"
        params: list = [timespan, multiplier]
        if tickers:
            sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
            params += [t.upper() for t in tickers]
        if start:
            sql += " AND ts >= ?"
            params.append(_date_ms(start))
        if end:
            sql += " AND ts < ?"
            params.append(_date_ms(end) + 2 * 86_400_000)
        frames = []
        for feed in [self._feed(source)] if source else self._open_feeds():
            if feed is not None:
                for table in _BAR_TABLES:
                    frames.append(pd.read_sql_query(sql.format(table=table) + " ORDER BY ticker, ts", feed.conn,
                                                    params=params))
                    frames[-1].insert(0, "source", feed.name)
        frames = [f for f in frames if len(f)]  # an empty read has untyped columns
        if not frames:
            return pd.DataFrame(columns=["source", "ticker", "time", *BAR_COLUMNS])
        df = pd.concat(frames, ignore_index=True).drop_duplicates(["source", "ticker", "ts"])  # built first
        if len(frames) > 1:
            df = df.sort_values(["ticker", "ts"], kind="stable", ignore_index=True)
        df["time"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        return df[["source", "ticker", "time", *BAR_COLUMNS]].reset_index(drop=True)

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

    def bars_changed_since(self, source: str, ticker: str, timespans: Sequence[str], stored_after: int) -> dict:
        """Per timespan, (earliest bar time, newest stored_at) of the ticker's bars, its own or
        built, stored after `stored_after` (ms): what a cached series has to load again."""
        feed = self._feed(source)
        out: dict[str, tuple[int, int]] = {}
        if feed is None or not timespans:
            return out
        marks = ",".join("?" * len(timespans))
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
        feed = self._feed(source)
        if feed is None:
            return (0, None, 0, None)
        t = feed.conn.execute(
            "SELECT COUNT(*), MAX(ts_ns) FROM ticks.trades WHERE ticker=?", (ticker.upper(),)
        ).fetchone() if feed.has_ticks() else (0, None)
        b = [feed.conn.execute(f"SELECT COUNT(*), MAX(ts) FROM {table} WHERE ticker=?", (ticker.upper(),)).fetchone()
             for table in _BAR_TABLES]
        return tuple(t) + (sum(n for n, _ in b), max((m for _, m in b if m is not None), default=None))

    def stored_series(self) -> list[tuple]:
        """(source, ticker, kind) for every series with data, kind = trades | second | minute | ...
        Scans the tables only after the database has changed."""
        return self._cached("stored_series", self._stored_series)

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

    def newest_any(self, ticker: str, timespan: str) -> tuple[str, int] | None:
        """(feed, ms) of the newest bar of a series in any feed: one index seek per feed holding it."""
        ticker = ticker.upper()
        best = None
        for src in {s for s, t, k in self.stored_series() if t == ticker and k == timespan}:
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
        bars = [_bar_row(ticker, timespan, 1, {
            "t": int(t), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v,
            "vw": None if pd.isna(r.vw) else r.vw, "n": None if pd.isna(r.n) else int(r.n)}, stored) + (derivation,)
            for t, r in zip(comp.index, comp.itertuples())]
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

    def bar_edges(self, source: str, ticker: str, timespan: str = "minute", multiplier: int = 1) -> tuple | None:
        """(first, last) bar start in ms for one series. Two index lookups, however much is stored."""
        conn = self._bars(source)
        if conn is None:
            return None
        key = (ticker.upper(), timespan, multiplier)
        edges = []
        for table in _BAR_TABLES:
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
