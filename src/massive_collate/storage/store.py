"""The Store: one object over the main file and the feed and dataset files. Its methods come from
one part per responsibility, the other modules of this package."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from .appstate import AppStateStore
from .audits import AuditStore
from .common import _date_ms, _distinct, _feed_name, _now
from .directory import DirectoryStore
from .ledger import LedgerStore
from .markets import MarketsStore
from .schema import MIGRATIONS, SCHEMA
from .watchlist import WatchlistStore


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
    PRIMARY KEY (ticker, timespan, multiplier, ts)
) WITHOUT ROWID;
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
# A research dataset provider's file (datasets/<provider>.db, see datasets.py): series of dated
# values (factor returns, prices, positions), and index membership.
DATASET_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    dataset   TEXT    NOT NULL,
    series    TEXT    NOT NULL,
    title     TEXT,
    unit      TEXT,
    freq      TEXT,              -- day | week | month
    first_ts  INTEGER,           -- first and last value (ms, the period's start, US/Eastern midnight)
    last_ts   INTEGER,
    n         INTEGER,
    PRIMARY KEY (dataset, series)
);
CREATE TABLE IF NOT EXISTS observations (
    dataset   TEXT    NOT NULL,
    series    TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    value     REAL    NOT NULL,
    PRIMARY KEY (dataset, series, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS constituents (
    idx       TEXT    NOT NULL,  -- e.g. S&P 500
    ticker    TEXT    NOT NULL,
    name      TEXT,
    sector    TEXT,
    industry  TEXT,
    added     TEXT,
    cik       TEXT,
    seen      TEXT    NOT NULL,  -- when the list was fetched
    PRIMARY KEY (idx, ticker)
);
"""

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
    PRIMARY KEY (ticker, timespan, ts)
) WITHOUT ROWID;
"""

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "vwap", "transactions"]

_UPSERT_BAR = """
INSERT INTO bars (ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (ticker, timespan, multiplier, ts) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, vwap=excluded.vwap, transactions=excluded.transactions
"""


def _bar_row(ticker: str, timespan: str, multiplier: int, bar: dict) -> tuple:
    return (
        ticker, timespan, multiplier, int(bar["t"]),
        bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
        bar.get("v"), bar.get("vw"), bar.get("n"),
    )


def feeds_backup_dir(dest: Path) -> Path:
    """Where a backup's feed files go: a folder beside it, named after it (open the backup with
    Store(dest, feeds_dir=feeds_backup_dir(dest), datasets_dir=datasets_backup_dir(dest)))."""
    return dest.with_name(dest.stem + "-feeds")


def datasets_backup_dir(dest: Path) -> Path:
    return dest.with_name(dest.stem + "-datasets")


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
        self.conn.executescript(FEED_SCHEMA + (COMPOSITE_SCHEMA if name == COMPOSITE else ""))
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


class Store(DirectoryStore, MarketsStore, WatchlistStore, LedgerStore, AuditStore, AppStateStore):
    def __init__(self, path: Path | str, check_same_thread: bool = True, feeds_dir: Path | str | None = None,
                 datasets_dir: Path | str | None = None):
        self.path = Path(path)
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.feeds_dir = Path(feeds_dir) if feeds_dir else (None if memory else self.path.with_name("feeds"))
        self.datasets_dir = Path(datasets_dir) if datasets_dir else (None if memory else self.path.with_name("datasets"))
        self._dataset_conns: dict[str, sqlite3.Connection] = {}
        self._check_same_thread = check_same_thread
        self.conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
        old = old_layout(self.conn, None if memory else self.path)
        if old:
            self.conn.close()
            raise OldLayout(f"{self.path} still holds every feed's data in one place ({', '.join(old)}): "
                            "run `python -m massive_collate admin split-feeds` to move each feed to its own "
                            "files (it backs up first)")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._feeds: dict[str, _Feed] = {}
        self._feeds_lock = threading.Lock()
        self._feeds_seen = None
        # Data-version caches: SQLite's data_version changes when another connection (the
        # collector) commits, per file; _writes counts this store's own market-data writes.
        self._writes = 0
        self._cache: dict = {}
        self._scan_feeds()

    def _version(self) -> tuple:
        self._scan_feeds()
        return (self.conn.execute("PRAGMA data_version").fetchone()[0],
                tuple(f.version() for f in self._open_feeds(scan=False)), self._writes)

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

    def _cached(self, key, compute):
        version = self._version()
        hit = self._cache.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]
        value = compute()
        self._cache[key] = (version, value)
        return value

    def _wrote(self) -> None:
        self._writes += 1

    def _migrate(self) -> None:
        with self.conn:
            for table, column, kind in MIGRATIONS:
                existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        for feed in self._feeds.values():
            feed.conn.close()
        for conn in self._dataset_conns.values():
            conn.close()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

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
        rows = [_bar_row(ticker, timespan, multiplier, b) for b in bars]
        conn = self._feed(source, create=True).conn
        with conn:
            conn.executemany(_UPSERT_BAR, rows)
        self._wrote()
        return len(rows)

    def bars_window(self, sources: Sequence[str], ticker: str, timespan: str, lo_ms: int, hi_ms: int) -> pd.DataFrame:
        """Bars of a series in [lo, hi) ms from the given feeds (an index range scan per feed), with
        a `time` column in US/Eastern like load_bars."""
        frames = [pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume, vwap, transactions FROM bars WHERE ticker=? "
            "AND timespan=? AND multiplier=1 AND ts >= ? AND ts < ? ORDER BY ts", conn,
            params=(ticker.upper(), timespan, int(lo_ms), int(hi_ms)))
            for conn in (self._bars(src) for src in sources) if conn is not None]
        df = pd.concat(frames) if frames else pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "vwap", "transactions"])
        df = df.sort_values("ts", kind="stable").drop_duplicates("ts")  # one feed per span: no real duplicates
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("America/New_York")  # ts kept: ms
        return df.reset_index(drop=True)

    def bar_rows(self, source: str, ticker: str, timespan: str, lo_ms: int, hi_ms: int) -> list[tuple]:
        """(ts, open, high, low, close, volume, vwap, transactions) of one feed's series in [lo, hi) ms."""
        conn = self._bars(source)
        if conn is None:
            return []
        return conn.execute(
            "SELECT ts, open, high, low, close, volume, vwap, transactions FROM bars WHERE ticker=? AND timespan=? "
            "AND multiplier=1 AND ts >= ? AND ts < ? ORDER BY ts", (ticker.upper(), timespan, lo_ms, hi_ms)).fetchall()

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
        sql = "SELECT ticker, ts, " + ", ".join(BAR_COLUMNS) + " FROM bars WHERE timespan=? AND multiplier=?"
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
                frames.append(pd.read_sql_query(sql + " ORDER BY ticker, ts", feed.conn, params=params))
                frames[-1].insert(0, "source", feed.name)
        if not frames:
            return pd.DataFrame(columns=["source", "ticker", "time", *BAR_COLUMNS])
        df = pd.concat(frames, ignore_index=True)
        if len(frames) > 1:
            df = df.sort_values(["ticker", "ts"], kind="stable", ignore_index=True)
        df["time"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        return df[["source", "ticker", "time", *BAR_COLUMNS]].reset_index(drop=True)

    # -- grouped (whole-market daily) --------------------------------------

    def save_grouped_day(self, source: str, day: date, bars: list[dict]) -> int:
        rows = [_bar_row(b["T"].upper(), "day", 1, b) for b in bars if b.get("T")]
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
            n += feed.conn.execute("DELETE FROM bars WHERE ticker=? AND ts < ?", (ticker, cutoff_ns // 10**6)).rowcount
        self._wrote()
        return n

    def series_fingerprint(self, source: str, ticker: str) -> tuple:
        """Value that changes whenever a ticker's stored data changes (for caching).
        Recomputed only after the database has changed."""
        return self._cached(("fp", source, ticker.upper()), lambda: self._fingerprint(source, ticker))

    def _fingerprint(self, source: str, ticker: str) -> tuple:
        feed = self._feed(source)
        if feed is None:
            return (0, None, 0, None)
        t = feed.conn.execute(
            "SELECT COUNT(*), MAX(ts_ns) FROM ticks.trades WHERE ticker=?", (ticker.upper(),)
        ).fetchone() if feed.has_ticks() else (0, None)
        b = feed.conn.execute("SELECT COUNT(*), MAX(ts) FROM bars WHERE ticker=?", (ticker.upper(),)).fetchone()
        return tuple(t) + tuple(b)

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
            bars += [(feed.name, t, ts) for t, ts, m in _distinct(feed.conn, "bars", ("ticker", "timespan", "multiplier"),
                                                                  ("ts",)) if m == 1]
        return trades + bars

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
            newest = self._bars(src).execute(  # MAX alone is answered from the index's last entry
                "SELECT MAX(ts) FROM bars WHERE ticker=? AND timespan=? AND multiplier=1",
                (ticker, timespan)).fetchone()[0]
            if newest is not None and (best is None or newest > best[1]):
                best = (src, newest)
        return best

    # -- composite model and quality -----------------------------------------

    def save_composite(self, ticker: str, timespan: str, comp: pd.DataFrame) -> int:
        """Composite bars (the 'composite' feed) and their deviation across exchanges, together."""
        if comp.empty:
            return 0
        ticker = ticker.upper()
        bars = [_bar_row(ticker, timespan, 1, {
            "t": int(t), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v,
            "vw": None if pd.isna(r.vw) else r.vw, "n": None if pd.isna(r.n) else int(r.n)})
            for t, r in zip(comp.index, comp.itertuples())]
        rows = [(ticker, timespan, int(t), int(r.venues), float(r.sd_bps), float(r.range_bps), float(r.env_h),
                 float(r.env_l), r.members, r.outliers) for t, r in zip(comp.index, comp.itertuples())]
        conn = self._feed(COMPOSITE, create=True).conn
        with conn:
            conn.executemany(_UPSERT_BAR, bars)
            conn.executemany("INSERT OR REPLACE INTO composite_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
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

    # -- research datasets (datasets.py) ---------------------------------------

    def _dataset_db(self, provider: str, create: bool = False) -> sqlite3.Connection | None:
        """A dataset provider's file, opened on first use; None if it has none yet unless `create`."""
        conn = self._dataset_conns.get(provider)
        if conn is not None:
            return conn
        if not _feed_name(provider):
            raise ValueError(f"not a provider name: {provider!r}")
        path = self.datasets_dir / f"{provider}.db" if self.datasets_dir else None
        if not create and (path is None or not path.exists()):
            return None
        with self._feeds_lock:
            conn = self._dataset_conns.get(provider)
            if conn is None:
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=self._check_same_thread)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(DATASET_SCHEMA)
                self._dataset_conns[provider] = conn
        return conn

    def dataset_files(self, name: str) -> dict[str, dict]:
        """{url: {bytes, values, fetched_at, closed}} for the files of a dataset fetched so far."""
        return {url: {"bytes": b, "values": n, "fetched_at": datetime.fromisoformat(f), "closed": bool(c)}
                for url, b, n, f, c in self.conn.execute(
                    "SELECT url, bytes, values_n, fetched_at, closed FROM dataset_files WHERE dataset=? ORDER BY url",
                    (name,))}

    def set_dataset_file(self, name: str, url: str, size: int, values: int, closed: bool = False) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO dataset_files VALUES (?, ?, ?, ?, ?, ?)",
                              (name, url, size, values, _now(), int(closed)))

    def save_dataset(self, provider: str, name: str, parsed, replace: bool = False) -> int:
        """A parsed dataset file's values (replacing revised ones), its series' descriptions and,
        for an index list, the index's members (the list replaces the previous one). With
        `replace`, the dataset's earlier values and series go first. Returns rows."""
        conn = self._dataset_db(provider, create=True)
        touched = sorted({s for s, _, _ in parsed.values})
        with conn:
            if replace:
                conn.execute("DELETE FROM observations WHERE dataset=?", (name,))
                conn.execute("DELETE FROM series WHERE dataset=?", (name,))
            conn.executemany("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?)",
                             [(name, s, ts, v) for s, ts, v in parsed.values])
            conn.executemany(
                "INSERT INTO series (dataset, series, title, unit, freq) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (dataset, series) DO UPDATE SET title=excluded.title, unit=excluded.unit, freq=excluded.freq",
                [(name, s, *meta) for s, meta in parsed.series.items()])
            conn.executemany(
                "UPDATE series SET first_ts=(SELECT MIN(ts) FROM observations o WHERE o.dataset=?1 AND o.series=?2), "
                "last_ts=(SELECT MAX(ts) FROM observations o WHERE o.dataset=?1 AND o.series=?2), "
                "n=(SELECT COUNT(*) FROM observations o WHERE o.dataset=?1 AND o.series=?2) "
                "WHERE dataset=?1 AND series=?2", [(name, s) for s in touched])
            for index in sorted({c["index"] for c in parsed.constituents}):
                conn.execute("DELETE FROM constituents WHERE idx=?", (index,))
            conn.executemany("INSERT OR REPLACE INTO constituents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                (c["index"], c["ticker"], c.get("name"), c.get("sector"), c.get("industry"), c.get("added"),
                 c.get("cik"), _now()) for c in parsed.constituents])
        return len(parsed.values) + len(parsed.constituents)

    def dataset_series(self, provider: str, name: str) -> list[dict]:
        """A dataset's series: {series, title, unit, freq, first_ts, last_ts, n}, by name."""
        conn = self._dataset_db(provider)
        if conn is None:
            return []
        cur = conn.execute("SELECT series, title, unit, freq, first_ts, last_ts, n FROM series WHERE dataset=? "
                           "ORDER BY series", (name,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def dataset_last_values(self, provider: str, name: str) -> dict[str, float]:
        """{series: its newest value} for every series of a dataset (one index seek each)."""
        conn = self._dataset_db(provider)
        if conn is None:
            return {}
        return dict(conn.execute(
            "SELECT o.series, o.value FROM series s JOIN observations o ON o.dataset=s.dataset AND o.series=s.series "
            "AND o.ts=s.last_ts WHERE s.dataset=?", (name,)))

    def dataset_values(self, provider: str, name: str, series: str, lo_ms: int | None = None,
                       hi_ms: int | None = None) -> pd.DataFrame:
        """ts (ms) and value of one series, oldest first."""
        conn = self._dataset_db(provider)
        if conn is None:
            return pd.DataFrame(columns=["ts", "value"])
        sql, args = "SELECT ts, value FROM observations WHERE dataset=? AND series=?", [name, series]
        if lo_ms is not None:
            sql += " AND ts >= ?"
            args.append(lo_ms)
        if hi_ms is not None:
            sql += " AND ts < ?"
            args.append(hi_ms)
        return pd.read_sql_query(sql + " ORDER BY ts", conn, params=args)

    def constituents(self, provider: str, index: str) -> dict[str, dict]:
        """{ticker: {name, sector, industry, added, cik}} of an index's members as last fetched."""
        conn = self._dataset_db(provider)
        if conn is None:
            return {}
        return {t: {"name": n, "sector": s, "industry": i, "added": a, "cik": c}
                for t, n, s, i, a, c in conn.execute(
                    "SELECT ticker, name, sector, industry, added, cik FROM constituents WHERE idx=?", (index,))}

    def dataset_bytes(self) -> dict[str, int]:
        """{provider: bytes of its file and write-ahead log}."""
        if self.datasets_dir is None or not self.datasets_dir.is_dir():
            return {}
        out: dict[str, int] = {}
        for f in self.datasets_dir.iterdir():
            if f.name.endswith((".db", ".db-wal")):
                key = f.name.split(".db")[0]
                out[key] = out.get(key, 0) + f.stat().st_size
        return out

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
        where = "FROM bars WHERE ticker=? AND timespan=? AND multiplier=?"
        first = conn.execute(f"SELECT MIN(ts) {where}", key).fetchone()[0]
        if first is None:
            return None
        return first, conn.execute(f"SELECT MAX(ts) {where}", key).fetchone()[0]

    # -- admin -------------------------------------------------------------

    def summary(self) -> dict:
        # No counts over the tick files (billions of rows): first and last tick per series by index.
        trades = []
        for src, ticker, kind in self.stored_series():
            if kind == "trades":
                e = self.trade_edges(src, ticker)
                trades.append((src, ticker, None, e[0], e[1]))
        bars = []
        for feed in self._open_feeds():
            bars += [(feed.name, *r) for r in feed.conn.execute(
                "SELECT timespan, multiplier, COUNT(DISTINCT ticker), COUNT(*), MIN(ts), MAX(ts) "
                "FROM bars GROUP BY timespan, multiplier")]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"db_path": str(self.path), "size_bytes": size, "feeds_dir": str(self.feeds_dir or ""),
                "feed_bytes": self.feed_bytes(), "trades": trades, "bars": bars}

    def integrity_check(self) -> str:
        """'ok', or what is wrong in which file (the main file, then each feed's bars and ticks)."""
        found = {"main": self.conn.execute("PRAGMA integrity_check").fetchone()[0]}
        for feed in self._open_feeds():
            found[feed.name] = feed.conn.execute("PRAGMA integrity_check").fetchone()[0]
        bad = {k: v for k, v in found.items() if v != "ok"}
        return "ok" if not bad else "; ".join(f"{k}: {v}" for k, v in bad.items())

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
        for feed in self._open_feeds():
            feed.conn.execute("VACUUM")

    def backup(self, dest: Path, ticks: bool = False) -> Path:
        """Copy the main file to `dest`, each feed's bars to `feeds_backup_dir(dest)` and the dataset
        files to `datasets_backup_dir(dest)`; with `ticks`, the feeds' tick files too (they can be
        hundreds of GB, so they are not copied by default)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(dest))) as target:
            self.conn.backup(target)
        if self.datasets_dir is not None and self.datasets_dir.is_dir():
            for f in sorted(self.datasets_dir.glob("*.db")):
                datasets_backup_dir(dest).mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(str(f))) as src, closing(
                        sqlite3.connect(str(datasets_backup_dir(dest) / f.name))) as target:
                    src.backup(target)
        folder = feeds_backup_dir(dest)
        for feed in self._open_feeds():
            folder.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(str(folder / f"{feed.name}.db"))) as target:
                feed.conn.backup(target)
            if ticks and feed.has_ticks():
                with closing(sqlite3.connect(str(folder / f"{feed.name}-ticks.db"))) as target:
                    feed.conn.backup(target, name="ticks")
        return dest
