"""Local SQLite store for ticks, bars, exchanges, the watchlist and an audit log of runs.

Every market-data row is tagged with the `source` it came from (e.g. "massive"),
so data from several providers can live side by side.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

import pandas as pd

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

CREATE TABLE IF NOT EXISTS layouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,        -- JSON: symbol, interval, chart type, indicators, drawings per ticker
    updated_at TEXT NOT NULL
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

    def upsert_contracts(self, source: str, product: str, contracts: Iterable[dict]) -> int:
        rows = [(source, product, c["ticker"], c.get("first_trade_date"), c.get("last_trade_date")) for c in contracts]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO futures_contracts VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def contracts(self, source: str, product: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ticker, first_trade, last_trade FROM futures_contracts WHERE source=? AND product=? "
            "ORDER BY last_trade", (source, product))
        return [{"ticker": t, "first_trade": date.fromisoformat(f) if f else None,
                 "last_trade": date.fromisoformat(l) if l else None} for t, f, l in cur]

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
        name: str | None = None, kind: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE watchlist SET listed_from=?, figi=?, checked_at=?, name=COALESCE(?, name), "
                "kind=COALESCE(?, kind) WHERE source=? AND ticker=?",
                (listed_from.isoformat() if listed_from else None, figi, checked.isoformat(), name, kind,
                 source, ticker.upper()),
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
        return n

    def unwatch(self, source: str, tickers: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "DELETE FROM watchlist WHERE source=? AND ticker=?", [(source, t.upper()) for t in tickers]
            )

    def watchlist(self, source: str | None = None) -> list[dict]:
        sql = (
            "SELECT source, ticker, resolution, backfilled_to, exchange, listed_from, figi, checked_at, "
            "name, kind, asset, source_ticker, invert, rank FROM watchlist"
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
             "asset": a or "stock", "source_ticker": st or t, "invert": bool(inv), "rank": rk or 1}
            for s, t, r, b, e, lf, f, c, n, k, a, st, inv, rk
            in self.conn.execute(sql + " ORDER BY source, ticker", params)
        ]

    def series_fingerprint(self, source: str, ticker: str) -> tuple:
        """Cheap value that changes whenever a ticker's stored data changes (for caching)."""
        t = self.conn.execute(
            "SELECT COUNT(*), MAX(ts_ns) FROM trades WHERE source=? AND ticker=?", (source, ticker.upper())
        ).fetchone()
        b = self.conn.execute(
            "SELECT COUNT(*), MAX(ts) FROM bars WHERE source=? AND ticker=?", (source, ticker.upper())
        ).fetchone()
        return tuple(t) + tuple(b)

    def stored_series(self) -> list[tuple]:
        """(source, ticker, kind) for every series with data, kind = trades | second | minute | ..."""
        rows = self.conn.execute("SELECT DISTINCT source, ticker, 'trades' FROM trades").fetchall()
        rows += self.conn.execute(
            "SELECT DISTINCT source, ticker, timespan FROM bars WHERE multiplier=1"
        ).fetchall()
        return rows

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
