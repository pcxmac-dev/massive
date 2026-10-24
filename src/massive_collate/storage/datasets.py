"""Research datasets: each provider's file (datasets/<provider>.db) and the ledger of fetched files."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .common import _feed_name, _now


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


def datasets_backup_dir(dest: Path) -> Path:
    return dest.with_name(dest.stem + "-datasets")


class DatasetStore:
    """Store methods: research datasets: each provider's file (datasets/<provider>.db) and the ledger of fetched files."""

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
