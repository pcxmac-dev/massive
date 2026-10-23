"""Local SQLite store for ticks, bars, exchanges, the watchlist and an audit log of runs.

Every market-data row is tagged with the `source` it came from (e.g. "massive"),
so data from several providers can live side by side.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")

if TYPE_CHECKING:
    from .models import Exchange

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    source     TEXT    NOT NULL,
    ticker     TEXT    NOT NULL,
    ts_ns      INTEGER NOT NULL,     -- SIP timestamp, ns since epoch (UTC)
    seq        INTEGER NOT NULL,     -- sequence number
    price      REAL    NOT NULL,
    size       REAL    NOT NULL,
    exchange   INTEGER,              -- exchange id, see the exchanges table
    conditions TEXT,                 -- comma-separated condition codes
    trade_id   TEXT,
    tape       INTEGER,
    PRIMARY KEY (source, ticker, ts_ns, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS bars (
    source       TEXT    NOT NULL,
    ticker       TEXT    NOT NULL,
    timespan     TEXT    NOT NULL,
    multiplier   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,   -- bar start, ms since epoch (UTC)
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, vwap REAL, transactions INTEGER,
    PRIMARY KEY (source, ticker, timespan, multiplier, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS exchanges (
    source      TEXT    NOT NULL,
    id          INTEGER NOT NULL,
    name        TEXT,
    acronym     TEXT,
    mic         TEXT,
    type        TEXT,
    country     TEXT,                -- ISO country code
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS watchlist (
    source        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    resolution    TEXT NOT NULL DEFAULT 'auto',  -- auto | trades | second | minute
    backfilled_to TEXT,                          -- oldest date fully backfilled, NULL until done
    added_at      TEXT NOT NULL,
    exchange      TEXT,                          -- expected primary exchange MIC, NULL = any
    listed_from   TEXT,                          -- listing date of the current instrument
    figi          TEXT,                          -- composite FIGI of the current instrument
    checked_at    TEXT,                          -- date the listing was last checked
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS grouped_days (
    source     TEXT NOT NULL,
    day        TEXT NOT NULL,        -- YYYY-MM-DD
    status     TEXT NOT NULL,        -- ok | empty | denied
    bar_count  INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, day)
);

CREATE TABLE IF NOT EXISTS futures_contracts (
    source      TEXT NOT NULL,
    product     TEXT NOT NULL,       -- e.g. NQ
    ticker      TEXT NOT NULL,       -- e.g. NQZ6
    first_trade TEXT,
    last_trade  TEXT,
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS futures_rolls (
    source   TEXT NOT NULL,
    product  TEXT NOT NULL,
    rank     INTEGER NOT NULL,       -- 1 = most active
    session  TEXT NOT NULL,          -- session (settlement) date
    ticker   TEXT NOT NULL,          -- contract held for that session
    PRIMARY KEY (source, product, rank, session)
);

CREATE TABLE IF NOT EXISTS entitlements (
    source     TEXT NOT NULL,
    asset      TEXT NOT NULL,        -- stock | index | fx | future
    resolution TEXT NOT NULL,        -- trades | second | minute
    allowed    INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (source, asset, resolution)
);

CREATE TABLE IF NOT EXISTS instrument_details (
    source     TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    data       TEXT NOT NULL,        -- JSON reference record from the source
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS layouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,        -- JSON: symbol, interval, chart type, indicators, drawings per ticker
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS icons (
    source     TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    url        TEXT,
    mime       TEXT,
    data       BLOB,                            -- NULL = the source had none (retried after 30 days)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, ticker)
);

-- Official calendars from a source (override the rule-based calendar in sessions.py).
CREATE TABLE IF NOT EXISTS market_holidays (
    source   TEXT NOT NULL,
    day      TEXT NOT NULL,
    exchange TEXT NOT NULL,
    status   TEXT NOT NULL,                     -- closed | early-close
    open_ms  INTEGER,
    close_ms INTEGER,
    name     TEXT,
    PRIMARY KEY (source, day, exchange)
);

CREATE TABLE IF NOT EXISTS futures_schedule (
    source  TEXT NOT NULL,
    product TEXT NOT NULL,
    day     TEXT NOT NULL,                      -- session end (trading) date
    event   TEXT NOT NULL,                      -- open | close | pre_open | ...
    ts_ms   INTEGER NOT NULL,
    PRIMARY KEY (source, product, day, event, ts_ms)
);

-- News headlines (announcements ticker on the Markets globe).
CREATE TABLE IF NOT EXISTS news (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    published_ms INTEGER NOT NULL,
    title        TEXT NOT NULL,
    publisher    TEXT,
    url          TEXT,
    tickers      TEXT,                      -- JSON list
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS news_time ON news (source, published_ms);

-- Daily closes of the market proxy funds (from the whole-market daily file), for the globe's colours.
CREATE TABLE IF NOT EXISTS proxy_daily (
    source TEXT NOT NULL,
    day    TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (source, day, ticker)
);

-- Every listing on the exchanges we cover (placeholders: no market data is collected for them
-- unless they are added to the watchlist), refreshed weekly.
CREATE TABLE IF NOT EXISTS directory (
    source   TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    kind     TEXT NOT NULL,                     -- stock | future
    name     TEXT,
    exchange TEXT,                              -- MIC (XNAS, XNYS) or futures venue
    type     TEXT,                              -- CS, ADRC, ETF... / futures sector
    figi     TEXT,
    seen     TEXT NOT NULL,                     -- date of the listing that included it
    PRIMARY KEY (source, ticker)
);

-- The whole market's latest daily bar (close, volume, VWAP) per ticker, from the market-wide
-- daily file: ranks the directory and fills placeholder tiles. Only the newest days are kept.
CREATE TABLE IF NOT EXISTS market_snapshot (
    source TEXT NOT NULL,
    day    TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close  REAL,
    volume REAL,
    vwap   REAL,
    PRIMARY KEY (source, day, ticker)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Which feed holds which span of a series. Each span [start, end) of a ticker's series was asked
-- for by exactly one feed, which stored whatever exists in it: other feeds never download it again
-- (only an audit may, without storing). Spans of one feed are kept merged.
CREATE TABLE IF NOT EXISTS coverage (
    source    TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    timespan  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    PRIMARY KEY (source, ticker, timespan, start_ms)
);
CREATE INDEX IF NOT EXISTS coverage_series ON coverage (ticker, timespan);

-- Audits: a second feed's copy of a stored span, compared in memory (see audit.py). Only the
-- comparison is kept, never the second copy.
CREATE TABLE IF NOT EXISTS audits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    asset     TEXT,
    timespan  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    owner     TEXT    NOT NULL,   -- the feed whose stored data was checked
    auditor   TEXT    NOT NULL,   -- the feed asked for a second copy
    reason    TEXT,               -- after-download | random | manual
    verdict   TEXT    NOT NULL,   -- pass | warn | fail | no-data
    matched   INTEGER, only_owner INTEGER, only_auditor INTEGER,
    report    TEXT                -- JSON: box stats and pass rates per field, offsets, worst bars
);

-- Spans waiting to be audited by another feed (queued after a download, done in that feed's run).
CREATE TABLE IF NOT EXISTS audit_queue (
    ticker    TEXT    NOT NULL,
    timespan  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    owner     TEXT    NOT NULL,
    queued_at TEXT    NOT NULL,
    PRIMARY KEY (ticker, timespan, start_ms, owner)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    requests    INTEGER NOT NULL DEFAULT 0,
    rows        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    message     TEXT
);
"""

# Columns added after the first release: (table, column, type). Applied to older databases on open.
MIGRATIONS = [
    ("watchlist", "exchange", "TEXT"),
    ("watchlist", "listed_from", "TEXT"),
    ("watchlist", "figi", "TEXT"),
    ("watchlist", "checked_at", "TEXT"),
    ("watchlist", "name", "TEXT"),
    ("watchlist", "kind", "TEXT"),
    ("watchlist", "asset", "TEXT NOT NULL DEFAULT 'stock'"),
    ("watchlist", "source_ticker", "TEXT"),
    ("watchlist", "invert", "INTEGER NOT NULL DEFAULT 0"),
    ("watchlist", "rank", "INTEGER NOT NULL DEFAULT 1"),
    # Save order: clocks can tie (Windows), a counter cannot.
    ("layouts", "touched", "INTEGER NOT NULL DEFAULT 0"),
    # The symbol a running sync is working on, for the on-screen activity view.
    ("runs", "current", "TEXT"),
    # Last scan: when the source was asked for its newest bar, and what it had (ms).
    ("watchlist", "probed_at", "TEXT"),
    ("watchlist", "source_through", "INTEGER"),
    # Futures contracts: live | ended | missing (no such contract month); checked = last probe date
    ("futures_contracts", "state", "TEXT"),
    ("futures_contracts", "checked", "TEXT"),
    # When a symbol last finished syncing completely (for the per-symbol refresh interval).
    ("watchlist", "synced_at", "TEXT"),
]

BAR_COLUMNS = ["open", "high", "low", "close", "volume", "vwap", "transactions"]

_UPSERT_BAR = """
INSERT INTO bars (source, ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (source, ticker, timespan, multiplier, ts) DO UPDATE SET
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    volume=excluded.volume, vwap=excluded.vwap, transactions=excluded.transactions
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class LayoutLimit(Exception):
    """The maximum number of saved layouts has been reached."""


def _layout_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("layout name must be 1-60 characters")
    return name


def _date_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _bar_row(source: str, ticker: str, timespan: str, multiplier: int, bar: dict) -> tuple:
    return (
        source, ticker, timespan, multiplier, int(bar["t"]),
        bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
        bar.get("v"), bar.get("vw"), bar.get("n"),
    )


class Store:
    def __init__(self, path: Path | str, check_same_thread: bool = True):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        # Data-version caches: SQLite's data_version changes when another connection (the
        # collector) commits; _writes counts this connection's own market-data writes.
        self._writes = 0
        self._cache: dict = {}

    def _version(self) -> tuple[int, int]:
        return self.conn.execute("PRAGMA data_version").fetchone()[0], self._writes

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
                source, ticker, int(t["sip_timestamp"]), int(t.get("sequence_number") or 0),
                t["price"], t["size"], t.get("exchange"),
                ",".join(str(c) for c in t.get("conditions") or []) or None,
                t.get("id"), t.get("tape"),
            )
            for t in trades
        ]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._wrote()
        return len(rows)

    def trade_range(self, source: str, ticker: str) -> tuple[int, int] | None:
        """(oldest ns, newest ns) stored for a ticker's trades, or None."""
        row = self.conn.execute(
            "SELECT MIN(ts_ns), MAX(ts_ns) FROM trades WHERE source=? AND ticker=?",
            (source, ticker.upper()),
        ).fetchone()
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
        sql = (
            "SELECT t.source, t.ts_ns, t.seq, t.price, t.size, t.exchange, e.name AS exchange_name, "
            "t.conditions FROM trades t LEFT JOIN exchanges e ON e.source=t.source AND e.id=t.exchange "
            "WHERE t.ticker=?"
        )
        params: list = [ticker.upper()]
        if source:
            sql += " AND t.source=?"
            params.append(source)
        if start:
            sql += " AND t.ts_ns >= ?"
            params.append(_date_ms(start) * 1_000_000)
        if end:
            sql += " AND t.ts_ns < ?"
            params.append((_date_ms(end) + 2 * 86_400_000) * 1_000_000)
        if exchanges:
            sql += f" AND t.exchange IN ({','.join('?' * len(exchanges))})"
            params += list(exchanges)
        df = pd.read_sql_query(sql + " ORDER BY t.ts_ns, t.seq", self.conn, params=params)
        df["time"] = pd.to_datetime(df.pop("ts_ns"), unit="ns", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        cols = ["source", "time", "price", "size", "exchange", "exchange_name", "conditions", "seq"]
        return df[cols].reset_index(drop=True)

    # -- bars --------------------------------------------------------------

    def upsert_bars(self, source: str, ticker: str, timespan: str, multiplier: int, bars: Iterable[dict]) -> int:
        ticker = ticker.upper()
        rows = [_bar_row(source, ticker, timespan, multiplier, b) for b in bars]
        with self.conn:
            self.conn.executemany(_UPSERT_BAR, rows)
        self._wrote()
        return len(rows)

    def bar_range(self, source: str, ticker: str, timespan: str, multiplier: int = 1) -> tuple[int, int] | None:
        """(oldest ts, newest ts) in ms stored for a series, or None if empty."""
        row = self.conn.execute(
            "SELECT MIN(ts), MAX(ts) FROM bars WHERE source=? AND ticker=? AND timespan=? AND multiplier=?",
            (source, ticker.upper(), timespan, multiplier),
        ).fetchone()
        return None if row[0] is None else (row[0], row[1])

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
        sql = (
            "SELECT source, ticker, ts, " + ", ".join(BAR_COLUMNS)
            + " FROM bars WHERE timespan=? AND multiplier=?"
        )
        params: list = [timespan, multiplier]
        if source:
            sql += " AND source=?"
            params.append(source)
        if tickers:
            sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
            params += [t.upper() for t in tickers]
        if start:
            sql += " AND ts >= ?"
            params.append(_date_ms(start))
        if end:
            sql += " AND ts < ?"
            params.append(_date_ms(end) + 2 * 86_400_000)
        df = pd.read_sql_query(sql + " ORDER BY ticker, ts", self.conn, params=params)
        df["time"] = pd.to_datetime(df.pop("ts"), unit="ms", utc=True).dt.tz_convert("America/New_York")
        if end:
            df = df[df["time"].dt.date <= end]
        return df[["source", "ticker", "time", *BAR_COLUMNS]].reset_index(drop=True)

    # -- exchanges ---------------------------------------------------------

    def upsert_exchanges(self, exchanges: Iterable["Exchange"]) -> int:
        data = [
            (e.source, e.id, e.name, e.acronym, e.mic, e.type, e.country.code if e.country else None)
            for e in exchanges
        ]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def exchanges(self, source: str | None = None) -> pd.DataFrame:
        sql = "SELECT source, id, name, acronym, mic, type, country FROM exchanges"
        params: list = []
        if source:
            sql += " WHERE source=?"
            params.append(source)
        return pd.read_sql_query(sql + " ORDER BY source, id", self.conn, params=params)

    # -- grouped (whole-market daily) --------------------------------------

    def save_grouped_day(self, source: str, day: date, bars: list[dict]) -> int:
        rows = [_bar_row(source, b["T"].upper(), "day", 1, b) for b in bars if b.get("T")]
        with self.conn:
            self.conn.executemany(_UPSERT_BAR, rows)
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

    # -- watchlist ---------------------------------------------------------

    def watch(
        self, source: str, tickers: Iterable[str], resolution: str = "auto", exchange: str | None = None,
        asset: str | None = None, source_ticker: str | None = None, invert: bool = False, rank: int = 1,
    ) -> None:
        """Add or update watchlist entries. `exchange` (a MIC) pins the expected primary exchange;
        `asset`/`source_ticker`/`invert`/`rank` describe non-stock symbols (see symbols.resolve)."""
        with self.conn:
            for t in tickers:
                self.conn.execute(
                    "INSERT INTO watchlist (source, ticker, resolution, added_at, exchange, asset, source_ticker, "
                    "invert, rank) VALUES (?, ?, ?, ?, ?, COALESCE(?, 'stock'), ?, ?, ?) "
                    "ON CONFLICT (source, ticker) DO UPDATE SET resolution=excluded.resolution, "
                    "exchange=COALESCE(excluded.exchange, watchlist.exchange), "
                    "asset=CASE WHEN ? IS NULL THEN watchlist.asset ELSE excluded.asset END, "
                    "source_ticker=COALESCE(excluded.source_ticker, watchlist.source_ticker), "
                    "invert=CASE WHEN ? IS NULL THEN watchlist.invert ELSE excluded.invert END, "
                    "rank=CASE WHEN ? IS NULL THEN watchlist.rank ELSE excluded.rank END, "
                    "backfilled_to=CASE WHEN watchlist.resolution=excluded.resolution "
                    "THEN watchlist.backfilled_to END",
                    (source, t.upper(), resolution, _now(), exchange, asset, source_ticker, int(invert), rank,
                     asset, asset, asset),
                )

    def watch_spec(self, source: str, spec, resolution: str = "auto") -> None:
        """Watch a symbols.SymbolSpec."""
        self.watch(source, [spec.ticker], resolution, exchange=spec.exchange, asset=spec.asset,
                   source_ticker=spec.source_ticker, invert=spec.invert, rank=spec.rank)

    # -- futures -----------------------------------------------------------

    def set_contract(
        self, source: str, product: str, ticker: str, month: date, state: str,
        last_trade: date | None, checked: date,
    ) -> None:
        """Record what is known about a contract: `month` is its contract month (stored in
        first_trade), `state` live | ended | missing, `last_trade` its last session once ended."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO futures_contracts (source, product, ticker, first_trade, last_trade, state, checked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, product, ticker, month.isoformat(), last_trade.isoformat() if last_trade else None,
                 state, checked.isoformat()))

    def contracts(self, source: str, product: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ticker, first_trade, last_trade, state, checked FROM futures_contracts "
            "WHERE source=? AND product=? ORDER BY first_trade", (source, product))
        d = lambda v: date.fromisoformat(v) if v else None  # noqa: E731
        return [{"ticker": t, "month": d(f), "last_trade": d(l), "state": s, "checked": d(c)}
                for t, f, l, s, c in cur]

    def set_rolls(self, source: str, product: str, rank: int, schedule: list[tuple[date, str]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM futures_rolls WHERE source=? AND product=? AND rank=?", (source, product, rank))
            self.conn.executemany(
                "INSERT INTO futures_rolls VALUES (?, ?, ?, ?, ?)",
                [(source, product, rank, d.isoformat(), t) for d, t in schedule])

    def rolls(self, source: str, product: str, rank: int = 1) -> list[tuple[date, str]]:
        cur = self.conn.execute(
            "SELECT session, ticker FROM futures_rolls WHERE source=? AND product=? AND rank=? ORDER BY session",
            (source, product, rank))
        return [(date.fromisoformat(d), t) for d, t in cur]

    def set_listing(
        self, source: str, ticker: str, listed_from: date | None, figi: str | None, checked: date,
        name: str | None = None, kind: str | None = None, exchange: str | None = None,
    ) -> None:
        """Record a listing check. `exchange` pins the primary exchange if none was set yet."""
        with self.conn:
            self.conn.execute(
                "UPDATE watchlist SET listed_from=?, figi=?, checked_at=?, name=COALESCE(?, name), "
                "kind=COALESCE(?, kind), exchange=COALESCE(exchange, ?) WHERE source=? AND ticker=?",
                (listed_from.isoformat() if listed_from else None, figi, checked.isoformat(), name, kind,
                 exchange, source, ticker.upper()),
            )

    def delete_before(self, source: str, ticker: str, cutoff_ns: int | None = None) -> int:
        """Delete a ticker's ticks and bars older than cutoff_ns (all of them if None)."""
        ticker = ticker.upper()
        cutoff_ns = cutoff_ns if cutoff_ns is not None else 2**62
        with self.conn:
            n = self.conn.execute(
                "DELETE FROM trades WHERE source=? AND ticker=? AND ts_ns < ?", (source, ticker, cutoff_ns)
            ).rowcount
            n += self.conn.execute(
                "DELETE FROM bars WHERE source=? AND ticker=? AND ts < ?", (source, ticker, cutoff_ns // 10**6)
            ).rowcount
        self._wrote()
        return n

    def unwatch(self, source: str, tickers: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "DELETE FROM watchlist WHERE source=? AND ticker=?", [(source, t.upper()) for t in tickers]
            )

    def watchlist(self, source: str | None = None) -> list[dict]:
        sql = (
            "SELECT source, ticker, resolution, backfilled_to, exchange, listed_from, figi, checked_at, "
            "name, kind, asset, source_ticker, invert, rank, synced_at FROM watchlist"
        )
        params: list = []
        if source:
            sql += " WHERE source=?"
            params.append(source)

        def d(v: str | None) -> date | None:
            return date.fromisoformat(v) if v else None

        return [
            {"source": s, "ticker": t, "resolution": r, "backfilled_to": d(b), "exchange": e,
             "listed_from": d(lf), "figi": f, "checked_at": d(c), "name": n, "kind": k,
             "asset": a or "stock", "source_ticker": st or t, "invert": bool(inv), "rank": rk or 1,
             "synced_at": datetime.fromisoformat(sy) if sy else None}
            for s, t, r, b, e, lf, f, c, n, k, a, st, inv, rk, sy
            in self.conn.execute(sql + " ORDER BY source, ticker", params)
        ]

    def series_fingerprint(self, source: str, ticker: str) -> tuple:
        """Value that changes whenever a ticker's stored data changes (for caching).
        Recomputed only after the database has changed."""
        return self._cached(("fp", source, ticker.upper()), lambda: self._fingerprint(source, ticker))

    def _fingerprint(self, source: str, ticker: str) -> tuple:
        t = self.conn.execute(
            "SELECT COUNT(*), MAX(ts_ns) FROM trades WHERE source=? AND ticker=?", (source, ticker.upper())
        ).fetchone()
        b = self.conn.execute(
            "SELECT COUNT(*), MAX(ts) FROM bars WHERE source=? AND ticker=?", (source, ticker.upper())
        ).fetchone()
        return tuple(t) + tuple(b)

    def stored_series(self) -> list[tuple]:
        """(source, ticker, kind) for every series with data, kind = trades | second | minute | ...
        Scans the tables only after the database has changed."""
        return self._cached("stored_series", self._stored_series)

    def _stored_series(self) -> list[tuple]:
        # Loose index scans: one primary-key seek per distinct series instead of reading every row.
        rows = [(s, t, "trades") for s, t in self._distinct("trades", ("source", "ticker"), ("ts_ns", "seq"))]
        rows += [(s, t, ts) for s, t, ts, m in self._distinct("bars", ("source", "ticker", "timespan", "multiplier"),
                                                                ("ts",)) if m == 1]
        return rows

    def _distinct(self, table: str, cols: tuple[str, ...], rest: tuple[str, ...]) -> list[tuple]:
        """Distinct values of a primary-key prefix `cols`. Each step seeks past the whole key
        (`rest` = the remaining integer key columns, set to their maximum), so rows sharing a
        prefix are skipped in the index rather than read."""
        names = ", ".join(cols)
        key = ", ".join(cols + rest)
        top = (2**63 - 1,) * len(rest)
        out: list[tuple] = []
        row = self.conn.execute(f"SELECT {names} FROM {table} ORDER BY {names} LIMIT 1").fetchone()
        while row is not None:
            out.append(tuple(row))
            row = self.conn.execute(
                f"SELECT {names} FROM {table} WHERE ({key}) > ({', '.join('?' * (len(cols) + len(rest)))}) "
                f"ORDER BY {key} LIMIT 1", tuple(row) + top).fetchone()
        return out

    def set_resolution(self, source: str, ticker: str, resolution: str) -> None:
        self.watch(source, [ticker], resolution)

    def reset_backfill(self, source: str, ticker: str) -> None:
        self.set_backfilled(source, ticker, None)

    def set_backfilled(self, source: str, ticker: str, day: date | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE watchlist SET backfilled_to=? WHERE source=? AND ticker=?",
                (day.isoformat() if day else None, source, ticker.upper()),
            )

    # -- coverage: which feed holds which span (one feed per span) ----------

    STEP_MS = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000, "week": 7 * 86_400_000}

    def add_coverage(self, source: str, ticker: str, timespan: str, start_ms: int, end_ms: int) -> None:
        """Record that `source` holds [start, end) of a series, merged with its touching spans."""
        if end_ms <= start_ms:
            return
        ticker = ticker.upper()
        key = (source, ticker, timespan)
        with self.conn:
            rows = self.conn.execute(
                "SELECT start_ms, end_ms FROM coverage WHERE source=? AND ticker=? AND timespan=? "
                "AND end_ms >= ? AND start_ms <= ?", (*key, start_ms, end_ms)).fetchall()
            lo = min([start_ms] + [r[0] for r in rows])
            hi = max([end_ms] + [r[1] for r in rows])
            self.conn.executemany("DELETE FROM coverage WHERE source=? AND ticker=? AND timespan=? AND start_ms=?",
                                  [(*key, r[0]) for r in rows])
            self.conn.execute("INSERT INTO coverage VALUES (?, ?, ?, ?, ?)", (*key, lo, hi))

    def coverage(self, ticker: str, timespan: str, source: str | None = None) -> dict[str, list[tuple[int, int]]]:
        """{feed: [(start, end), ...]} for a series, oldest first. Series stored before the ledger
        existed are entered from their stored range first (`_seed_coverage`)."""
        ticker = ticker.upper()
        self._seed_coverage(ticker, timespan)
        sql = "SELECT source, start_ms, end_ms FROM coverage WHERE ticker=? AND timespan=?"
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
            r = self.bar_edges(src, ticker, timespan)  # two index seeks
            if not r:
                continue
            start = r[0]
            row = self.conn.execute("SELECT backfilled_to FROM watchlist WHERE source=? AND ticker=?",
                                    (src, ticker)).fetchone()
            if row and row[0]:
                d = date.fromisoformat(row[0])
                start = min(start, int(datetime(d.year, d.month, d.day, tzinfo=_ET).timestamp() * 1000))
            self.add_coverage(src, ticker, timespan, start, r[1] + self.STEP_MS.get(timespan, 60_000))

    def newest_any(self, ticker: str, timespan: str) -> tuple[str, int] | None:
        """(feed, ms) of the newest bar of a series in any feed: one index seek per feed holding it
        (the bars key starts with the feed, so a query without it would read every bar)."""
        ticker = ticker.upper()
        best = None
        for src in {s for s, t, k in self.stored_series() if t == ticker and k == timespan}:
            newest = self.conn.execute(  # MAX alone is answered from the index's last entry
                "SELECT MAX(ts) FROM bars WHERE source=? AND ticker=? AND timespan=? AND multiplier=1",
                (src, ticker, timespan)).fetchone()[0]
            if newest is not None and (best is None or newest > best[1]):
                best = (src, newest)
        return best

    # -- audits -------------------------------------------------------------

    def save_audit(self, row: dict) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO audits (at, ticker, asset, timespan, start_ms, end_ms, owner, auditor, reason, verdict, "
                "matched, only_owner, only_auditor, report) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.get("at") or _now(), row["ticker"].upper(), row.get("asset"), row["timespan"], row["start_ms"],
                 row["end_ms"], row["owner"], row["auditor"], row.get("reason"), row["verdict"], row.get("matched"),
                 row.get("only_owner"), row.get("only_auditor"), json.dumps(row.get("report") or {})))
            return cur.lastrowid

    def audits(self, limit: int = 50, ticker: str | None = None) -> list[dict]:
        sql = ("SELECT id, at, ticker, asset, timespan, start_ms, end_ms, owner, auditor, reason, verdict, matched, "
               "only_owner, only_auditor, report FROM audits")
        args: list = []
        if ticker:
            sql += " WHERE ticker=?"
            args.append(ticker.upper())
        cols = ("id", "at", "ticker", "asset", "timespan", "start_ms", "end_ms", "owner", "auditor", "reason",
                "verdict", "matched", "only_owner", "only_auditor", "report")
        out = []
        for r in self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)):
            d = dict(zip(cols, r))
            d["report"] = json.loads(d["report"] or "{}")
            out.append(d)
        return out

    def audit_scores(self, days: int = 90) -> dict[str, dict]:
        """Per feed, over recent audits it took part in: how many and the share that passed.
        Two feeds that disagree score alike (an audit cannot say which is right); a third feed
        makes the scores differ."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        out: dict[str, dict] = {}
        for owner, auditor, verdict in self.conn.execute(
                "SELECT owner, auditor, verdict FROM audits WHERE at >= ? AND verdict != 'no-data'", (since,)):
            for feed in (owner, auditor):
                s = out.setdefault(feed, {"audits": 0, "passed": 0})
                s["audits"] += 1
                s["passed"] += verdict == "pass"
        for s in out.values():
            s["accuracy"] = s["passed"] / s["audits"] if s["audits"] else None
        return out

    def queue_audit(self, ticker: str, timespan: str, start_ms: int, end_ms: int, owner: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO audit_queue VALUES (?, ?, ?, ?, ?, ?)",
                              (ticker.upper(), timespan, start_ms, end_ms, owner, _now()))

    def audit_queue(self, exclude_owner: str | None = None) -> list[dict]:
        rows = self.conn.execute("SELECT ticker, timespan, start_ms, end_ms, owner, queued_at FROM audit_queue "
                                 "ORDER BY queued_at").fetchall()
        cols = ("ticker", "timespan", "start_ms", "end_ms", "owner", "queued_at")
        return [dict(zip(cols, r)) for r in rows if r[4] != exclude_owner]

    def dequeue_audit(self, ticker: str, timespan: str, start_ms: int, owner: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM audit_queue WHERE ticker=? AND timespan=? AND start_ms=? AND owner=?",
                              (ticker.upper(), timespan, start_ms, owner))

    # -- plan entitlements (per asset class) -------------------------------

    def set_entitlement(self, source: str, asset: str, resolution: str, allowed: bool) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO entitlements VALUES (?, ?, ?, ?, ?)",
                              (source, asset, resolution, int(allowed), _now()))

    def entitlement(self, source: str, asset: str, resolution: str, max_age_days: int = 7) -> bool | None:
        """Known answer for this plan, or None if never checked or older than max_age_days."""
        row = self.conn.execute(
            "SELECT allowed, checked_at FROM entitlements WHERE source=? AND asset=? AND resolution=?",
            (source, asset, resolution)).fetchone()
        if row is None:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row[1])
        return bool(row[0]) if age.days < max_age_days else None

    def clear_entitlements(self, source: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entitlements WHERE source=?", (source,))

    def set_probe(self, source: str, ticker: str, when: datetime, newest_ms: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE watchlist SET probed_at=?, source_through=? WHERE source=? AND ticker=?",
                              (when.astimezone(timezone.utc).isoformat(timespec="seconds"), newest_ms,
                               source, ticker.upper()))

    def probe(self, source: str, ticker: str) -> tuple[datetime, int | None] | None:
        row = self.conn.execute("SELECT probed_at, source_through FROM watchlist WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        return (datetime.fromisoformat(row[0]), row[1]) if row and row[0] else None

    def save_icon(self, source: str, ticker: str, url: str | None, mime: str | None, data: bytes | None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO icons VALUES (?, ?, ?, ?, ?, ?)",
                              (source, ticker.upper(), url, mime, data, _now()))

    def icon(self, ticker: str, source: str | None = None) -> tuple[str, bytes] | None:
        """(content type, bytes) of a stored company icon, from any source unless one is given."""
        sql = "SELECT mime, data FROM icons WHERE ticker=? AND data IS NOT NULL"
        params: list = [ticker.upper()]
        if source:
            sql += " AND source=?"
            params.append(source)
        row = self.conn.execute(sql + " ORDER BY fetched_at DESC LIMIT 1", params).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def icon_checked(self, source: str, ticker: str, max_age_days: int = 30, url: str | None = None) -> bool:
        """Whether an icon was fetched, or found missing recently enough not to ask again. With
        `url` (the logo link in the company's current details), a stored icon from a different
        link does not count: the company has a new logo."""
        row = self.conn.execute("SELECT data IS NOT NULL, fetched_at, url FROM icons WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        if row is None:
            return False
        if row[0]:
            return url is None or row[2] == url
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row[1])).days < max_age_days

    def stale_details(self, source: str, tickers_types: tuple[str, ...], max_age_days: int, limit: int) -> list[str]:
        """Directory companies whose stored details (and so logo link) are older than max_age_days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(tickers_types))
        return [r[0] for r in self.conn.execute(
            f"""SELECT d.ticker FROM directory d JOIN instrument_details x ON x.source=d.source AND x.ticker=d.ticker
                WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks}) AND x.updated_at < ?
                ORDER BY x.updated_at LIMIT ?""", (source, *tickers_types, cutoff, limit))]

    def icon_link_changes(self, source: str) -> list[str]:
        """Tickers whose stored icon came from a different logo link than their details now give."""
        out = []
        for ticker, url, data in self.conn.execute(
                "SELECT i.ticker, i.url, x.data FROM icons i JOIN instrument_details x "
                "ON x.source=i.source AND x.ticker=i.ticker WHERE i.source=? AND i.data IS NOT NULL", (source,)):
            current = ((json.loads(data).get("branding") or {}).get("icon_url"))
            if current and current != url:
                out.append(ticker)
        return out

    def icon_tickers(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT ticker FROM icons WHERE data IS NOT NULL")}

    # -- official calendars --------------------------------------------------

    def save_market_holidays(self, source: str, rows: Iterable[dict]) -> int:
        data = [(source, r["date"], r["exchange"], r["status"], r.get("open_ms"), r.get("close_ms"), r.get("name"))
                for r in rows]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_holidays VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def market_holidays(self, source: str) -> list[tuple[date, str, int | None, int | None]]:
        """NYSE closures and early closes (Nasdaq where NYSE is not listed for a day)."""
        rows = self.conn.execute(
            "SELECT day, exchange, status, open_ms, close_ms FROM market_holidays WHERE source=? "
            "AND exchange IN ('NYSE', 'NASDAQ') ORDER BY day, exchange DESC", (source,)).fetchall()
        out: dict[str, tuple] = {}
        for day, exchange, status, o, c in rows:  # NYSE sorts after NASDAQ descending: NYSE wins
            if day not in out or exchange == "NYSE":
                out[day] = (date.fromisoformat(day), status, o, c)
        return list(out.values())

    def save_futures_schedule(self, source: str, product: str, rows: Iterable[tuple[str, str, int]]) -> int:
        """rows: (session date, event, ms). Replaces what was stored for those dates."""
        rows = list(rows)
        days = sorted({d for d, _, _ in rows})
        with self.conn:
            self.conn.executemany("DELETE FROM futures_schedule WHERE source=? AND product=? AND day=?",
                                  [(source, product.upper(), d) for d in days])
            self.conn.executemany("INSERT OR REPLACE INTO futures_schedule VALUES (?, ?, ?, ?, ?)",
                                  [(source, product.upper(), d, e, ms) for d, e, ms in rows])
        return len(rows)

    def futures_schedules(self, source: str) -> list[tuple[str, date, list[tuple[str, int]]]]:
        out: dict[tuple, list] = {}
        for product, day, event, ms in self.conn.execute(
                "SELECT product, day, event, ts_ms FROM futures_schedule WHERE source=? ORDER BY product, day, ts_ms",
                (source,)):
            out.setdefault((product, day), []).append((event, ms))
        return [(p, date.fromisoformat(d), ev) for (p, d), ev in out.items()]

    def save_news(self, source: str, items: Iterable[dict]) -> int:
        rows = [(source, str(i["id"]), int(i["published_ms"]), i["title"], i.get("publisher"), i.get("url"),
                 json.dumps(i.get("tickers") or [])) for i in items if i.get("id") and i.get("title")]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO news VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def news(self, source: str | None = None, since_ms: int = 0, limit: int = 60) -> list[dict]:
        sql = "SELECT source, id, published_ms, title, publisher, url, tickers FROM news WHERE published_ms >= ?"
        params: list = [since_ms]
        if source:
            sql += " AND source=?"
            params.append(source)
        rows = self.conn.execute(sql + " ORDER BY published_ms DESC LIMIT ?", params + [limit]).fetchall()
        return [{"source": s, "id": i, "published_ms": t, "title": ti, "publisher": p, "url": u,
                 "tickers": json.loads(tk or "[]")} for s, i, t, ti, p, u, tk in rows]

    def latest_news_ms(self, source: str) -> int | None:
        return self.conn.execute("SELECT MAX(published_ms) FROM news WHERE source=?", (source,)).fetchone()[0]

    def save_proxy_closes(self, source: str, day: date, closes: dict[str, float]) -> int:
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO proxy_daily VALUES (?, ?, ?, ?)",
                                  [(source, day.isoformat(), t, c) for t, c in closes.items()])
        return len(closes)

    def proxy_closes(self, source: str, day: date) -> dict[str, float]:
        return dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                      (source, day.isoformat())).fetchall())

    def snapshot_closes(self, source: str, day: date, tickers: list[str]) -> dict[str, float]:
        marks = ",".join("?" * len(tickers))
        return dict(self.conn.execute(
            f"SELECT ticker, close FROM market_snapshot WHERE source=? AND day=? AND ticker IN ({marks}) AND close IS NOT NULL",
            (source, day.isoformat(), *tickers)).fetchall())

    def proxy_days(self, source: str) -> list[date]:
        return [date.fromisoformat(d) for (d,) in self.conn.execute(
            "SELECT DISTINCT day FROM proxy_daily WHERE source=? ORDER BY day", (source,))]

    def proxy_moves(self, source: str = "massive") -> dict[str, dict]:
        """Latest daily change of each proxy: {ticker: {day, close, changePct}}."""
        days = self.proxy_days(source)[-2:]
        if len(days) < 2:
            return {}
        prev, last = (dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                             (source, d.isoformat())).fetchall()) for d in days)
        return {t: {"day": days[1].isoformat(), "close": c, "changePct": (c / prev[t] - 1) * 100}
                for t, c in last.items() if prev.get(t)}

    def series_moves(self, source: str, tickers: Iterable[str]) -> dict[str, dict]:
        """Latest daily change of stored series from their own day bars: {ticker: {day (UTC date
        of the newest bar), close, prevClose, changePct}}. Tickers without two day bars are left out."""
        out = {}
        for t in tickers:
            rows = self.conn.execute(
                "SELECT ts, close FROM bars WHERE source=? AND ticker=? AND timespan='day' AND multiplier=1 "
                "ORDER BY ts DESC LIMIT 2", (source, t.upper())).fetchall()
            if len(rows) == 2 and rows[0][1] and rows[1][1]:
                day = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).date().isoformat()
                out[t] = {"day": day, "close": rows[0][1], "prevClose": rows[1][1],
                          "changePct": (rows[0][1] / rows[1][1] - 1) * 100}
        return out

    # -- directory ------------------------------------------------------------

    def save_directory(self, source: str, rows: Iterable[dict], seen: date) -> int:
        data = [(source, r["ticker"].upper(), r.get("kind", "stock"), r.get("name"), r.get("exchange"),
                 r.get("type"), r.get("figi"), seen.isoformat()) for r in rows if r.get("ticker")]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO directory VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data)
        self._wrote()
        return len(data)

    def prune_directory(self, source: str, kind: str, before: date) -> int:
        """Drop listings not seen in the latest listing (delisted, or symbol reused)."""
        with self.conn:
            return self.conn.execute("DELETE FROM directory WHERE source=? AND kind=? AND seen<?",
                                     (source, kind, before.isoformat())).rowcount

    def directory(self, source: str = "massive", kind: str | None = None) -> list[dict]:
        sql = """SELECT d.ticker, d.kind, d.name, d.exchange, d.type, s.close, s.volume, s.vwap,
                        i.data IS NOT NULL AS has_icon
                 FROM directory d
                 LEFT JOIN market_snapshot s ON s.source=d.source AND s.ticker=d.ticker
                      AND s.day=(SELECT MAX(day) FROM market_snapshot WHERE source=d.source)
                 LEFT JOIN icons i ON i.source=d.source AND i.ticker=d.ticker
                 WHERE d.source=?""" + (" AND d.kind=?" if kind else "") + " ORDER BY d.ticker"
        cols = ["ticker", "kind", "name", "exchange", "type", "close", "volume", "vwap", "icon"]
        return [dict(zip(cols, r)) for r in self.conn.execute(sql, [source] + ([kind] if kind else []))]

    def directory_icon_queue(self, source: str, types: tuple[str, ...], limit: int) -> list[str]:
        """Companies still without an icon decision, biggest daily dollar volume first."""
        marks = ",".join("?" * len(types))
        sql = f"""SELECT d.ticker FROM directory d
                  LEFT JOIN icons i ON i.source=d.source AND i.ticker=d.ticker
                  LEFT JOIN market_snapshot s ON s.source=d.source AND s.ticker=d.ticker
                       AND s.day=(SELECT MAX(day) FROM market_snapshot WHERE source=d.source)
                  WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks}) AND i.ticker IS NULL
                  ORDER BY COALESCE(s.close * s.volume, 0) DESC, d.ticker LIMIT ?"""
        return [r[0] for r in self.conn.execute(sql, [source, *types, limit])]

    def directory_stats(self, source: str, types: tuple[str, ...]) -> dict:
        marks = ",".join("?" * len(types))
        q = self.conn.execute
        listed = q("SELECT kind, COUNT(*) FROM directory WHERE source=? GROUP BY kind", (source,)).fetchall()
        companies = q(f"SELECT COUNT(*) FROM directory WHERE source=? AND kind='stock' AND type IN ({marks})",
                      (source, *types)).fetchone()[0]
        decided = q(f"""SELECT COUNT(*), SUM(i.data IS NOT NULL) FROM directory d JOIN icons i
                        ON i.source=d.source AND i.ticker=d.ticker
                        WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks})""", (source, *types)).fetchone()
        return {"listed": dict(listed), "companies": companies, "iconsChecked": decided[0] or 0,
                "icons": int(decided[1] or 0)}

    def save_market_snapshot(self, source: str, day: date, bars: list[dict]) -> int:
        rows = [(source, day.isoformat(), b["T"].upper(), b.get("c"), b.get("v"), b.get("vw")) for b in bars if b.get("T")]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_snapshot VALUES (?, ?, ?, ?, ?, ?)", rows)
            keep = [d for (d,) in self.conn.execute(
                "SELECT DISTINCT day FROM market_snapshot WHERE source=? ORDER BY day DESC LIMIT 3", (source,))]
            if keep:
                self.conn.execute(f"DELETE FROM market_snapshot WHERE source=? AND day NOT IN ({','.join('?' * len(keep))})",
                                  (source, *keep))
        return len(rows)

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self._wrote()

    def set_synced(self, source: str, ticker: str, when: datetime) -> None:
        with self.conn:
            self.conn.execute("UPDATE watchlist SET synced_at=? WHERE source=? AND ticker=?",
                              (when.astimezone(timezone.utc).isoformat(timespec="seconds"), source, ticker.upper()))

    # -- reference details -------------------------------------------------

    def set_details(self, source: str, ticker: str, data: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO instrument_details VALUES (?, ?, ?, ?)",
                              (source, ticker.upper(), json.dumps(data), _now()))

    def details(self, source: str, ticker: str) -> dict | None:
        row = self.conn.execute("SELECT data, updated_at FROM instrument_details WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        return {**json.loads(row[0]), "_updated_at": row[1]} if row else None

    # -- chart layouts (saved markups) --------------------------------------

    MAX_LAYOUTS = 5

    def list_layouts(self) -> list[dict]:
        cur = self.conn.execute("SELECT id, name, updated_at FROM layouts ORDER BY touched DESC, id DESC")
        return [{"id": i, "name": n, "updated_at": u} for i, n, u in cur]

    def get_layout(self, layout_id: int) -> dict | None:
        row = self.conn.execute("SELECT id, name, data, updated_at FROM layouts WHERE id=?", (layout_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "data": json.loads(row[2]), "updated_at": row[3]}

    def create_layout(self, name: str, data: dict) -> dict:
        name = _layout_name(name)
        with self.conn:
            count = self.conn.execute("SELECT COUNT(*) FROM layouts").fetchone()[0]
            if count >= self.MAX_LAYOUTS:
                raise LayoutLimit(f"at most {self.MAX_LAYOUTS} layouts; delete one first")
            cur = self.conn.execute(
                "INSERT INTO layouts (name, data, updated_at, touched) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts))",
                (name, json.dumps(data), _now_precise()))
        return self.get_layout(cur.lastrowid)

    def update_layout(self, layout_id: int, name: str | None = None, data: dict | None = None) -> dict | None:
        sets = ["updated_at=?", "touched=(SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts)"]
        params: list = [_now_precise()]
        if name is not None:
            sets.append("name=?")
            params.append(_layout_name(name))
        if data is not None:
            sets.append("data=?")
            params.append(json.dumps(data))
        with self.conn:
            n = self.conn.execute(f"UPDATE layouts SET {', '.join(sets)} WHERE id=?", (*params, layout_id)).rowcount
        return self.get_layout(layout_id) if n else None

    def delete_layout(self, layout_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM layouts WHERE id=?", (layout_id,)).rowcount > 0

    # -- runs / audit ------------------------------------------------------

    def start_run(self, source: str | None, command: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (source, command, started_at) VALUES (?, ?, ?)", (source, command, _now())
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, requests: int, rows: int, status: str, message: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, requests=?, rows=?, status=?, message=? WHERE id=?",
                (_now(), requests, rows, status, message, run_id),
            )

    def set_run_requests(self, run_id: int, requests: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET requests=? WHERE id=?", (requests, run_id))

    def set_run_current(self, run_id: int, ticker: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET current=? WHERE id=?", (ticker, run_id))

    def close_abandoned_runs(self) -> int:
        """Runs still marked 'running' (crashed or killed). Their recorded requests are kept;
        nothing is lowered, so the daily cap stays conservative."""
        with self.conn:
            return self.conn.execute(
                "UPDATE runs SET status='abandoned', finished_at=COALESCE(finished_at, ?), "
                "message=COALESCE(message, '') || ' (run did not finish)' WHERE status='running'", (_now(),)
            ).rowcount

    def requests_today(self, source: str) -> int:
        """API requests recorded today (UTC) for a source, across all runs."""
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM runs WHERE source=? AND substr(started_at, 1, 10)=?",
            (source, today),
        ).fetchone()
        return int(row[0])

    def runs(self, limit: int = 25) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message, current "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]

    def daily_snapshot(self, source: str | None = None, sessions: int = 30) -> dict[str, dict]:
        """Per ticker from local daily bars: average dollar volume over the last `sessions`
        sessions (volume x VWAP, close when VWAP is missing), last close and the one before."""
        def compute():
            out: dict[str, dict] = {}
            for src, ticker, kind in self.stored_series():
                if kind != "day" or (source and src != source) or ticker in out:
                    continue
                rows = self.conn.execute(
                    "SELECT close, volume * COALESCE(vwap, close) FROM bars WHERE source=? AND ticker=? "
                    "AND timespan='day' AND multiplier=1 ORDER BY ts DESC LIMIT ?", (src, ticker, sessions)).fetchall()
                if rows:
                    out[ticker] = {"close": rows[0][0], "prevClose": rows[1][0] if len(rows) > 1 else None,
                                   "dollarVolume": sum(v or 0.0 for _, v in rows) / len(rows)}
            return out
        return self._cached(("daily_snapshot", source, sessions), compute)

    def bar_edges(self, source: str, ticker: str, timespan: str = "minute", multiplier: int = 1) -> tuple | None:
        """(first, last) bar start in ms for one series. Two index lookups, however much is stored."""
        key = (source, ticker.upper(), timespan, multiplier)
        where = "FROM bars WHERE source=? AND ticker=? AND timespan=? AND multiplier=?"
        first = self.conn.execute(f"SELECT MIN(ts) {where}", key).fetchone()[0]
        if first is None:
            return None
        return first, self.conn.execute(f"SELECT MAX(ts) {where}", key).fetchone()[0]

    def recent_runs(self, limit: int = 10) -> list[tuple]:
        return self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()

    # -- admin -------------------------------------------------------------

    def summary(self) -> dict:
        q = self.conn.execute
        trades = q(
            "SELECT source, ticker, COUNT(*), MIN(ts_ns), MAX(ts_ns) FROM trades GROUP BY source, ticker"
        ).fetchall()
        bars = q(
            "SELECT source, timespan, multiplier, COUNT(DISTINCT ticker), COUNT(*), MIN(ts), MAX(ts) "
            "FROM bars GROUP BY source, timespan, multiplier"
        ).fetchall()
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"db_path": str(self.path), "size_bytes": size, "trades": trades, "bars": bars}

    def integrity_check(self) -> str:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0]

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def backup(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(dest))) as target:
            self.conn.backup(target)
        return dest
