"""Research datasets: each provider's file (datasets/<provider>.db) and the ledger of fetched files.

Point in time: every value is kept in every version its source published. A value is stored again
only when it changes (a revision) or disappears (withdrawn: stored as NULL), and each version
carries when the source made it public (`available_at`), when it was stored here (`stored_at`) and
the file version it came from (`origin`: a git commit, or the sha256 of a downloaded file). Reads
give the latest version by default, or what was public at a given moment (`as_of`), so a backtest
sees the data as it was then: late reports, revisions and withdrawals included.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .common import _ET, BUSY_TIMEOUT_S, _feed_name, _now


# A research dataset provider's file (datasets/<provider>.db, see datasets.py): series of dated
# values (factor returns, prices, positions) and index membership, both versioned.
DATASET_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    dataset   TEXT    NOT NULL,
    series    TEXT    NOT NULL,
    title     TEXT,
    unit      TEXT,
    freq      TEXT,              -- day | week | month
    first_ts  INTEGER,           -- first and last value of the latest version (ms, the period's start,
    last_ts   INTEGER,           -- US/Eastern midnight) and how many values it has
    n         INTEGER,
    PRIMARY KEY (dataset, series)
);
CREATE TABLE IF NOT EXISTS observations (
    dataset      TEXT    NOT NULL,
    series       TEXT    NOT NULL,
    ts           INTEGER NOT NULL,   -- the period's start, ms (US/Eastern midnight)
    available_at INTEGER NOT NULL,   -- ms: when the source made this version public
    value        REAL,               -- NULL: withdrawn in this version
    stored_at    INTEGER NOT NULL,   -- ms: when it was stored here
    origin       TEXT,               -- the file version: git commit, or sha256 of the downloaded file
    PRIMARY KEY (dataset, series, ts, available_at)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS constituents (
    idx          TEXT    NOT NULL,   -- e.g. S&P 500
    ticker       TEXT    NOT NULL,
    available_at INTEGER NOT NULL,   -- ms: when this membership state became public
    member       INTEGER NOT NULL,   -- 1 in the index, 0 left it
    name         TEXT,
    sector       TEXT,
    industry     TEXT,
    added        TEXT,
    cik          TEXT,
    stored_at    INTEGER NOT NULL,
    origin       TEXT,
    PRIMARY KEY (idx, ticker, available_at)
);
"""

_MISSING = object()


def datasets_backup_dir(dest: Path) -> Path:
    return dest.with_name(dest.stem + "-datasets")


def _ms_now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _added_ms(added: str | None) -> int | None:
    """An index's 'date added' (YYYY-MM-DD somewhere in the text) as US/Eastern midnight, ms."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", added or "")
    if not m:
        return None
    d = date.fromisoformat(m.group(0))
    return int(datetime(d.year, d.month, d.day, tzinfo=_ET).timestamp() * 1000)


class DatasetStore:
    """Store methods: research datasets, point in time: each provider's file (datasets/<provider>.db),
    every version of every value, and the ledger of fetched files."""

    def _dataset_db(self, provider: str, create: bool = False) -> sqlite3.Connection | None:
        """A dataset provider's file, opened on first use; None if it has none yet unless `create`.
        A file from before versioning keeps its old tables as observations_v1 / constituents_v1
        (datasets.upgrade_legacy carries them over)."""
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
                conn = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=self._check_same_thread,
                                       timeout=BUSY_TIMEOUT_S)
                conn.execute("PRAGMA journal_mode=WAL")
                with conn:
                    for table, column in (("observations", "available_at"), ("constituents", "member")):
                        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                        if cols and column not in cols:
                            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
                conn.executescript(DATASET_SCHEMA)
                self._dataset_conns[provider] = conn
        return conn

    # -- the ledger of fetched file versions (main file) ----------------------------------

    def dataset_files(self, name: str) -> dict[str, dict]:
        """{url: {bytes, values, fetched_at, closed, sha256, version_at, origin, local_path}} for the
        file versions of a dataset fetched so far (a git-backed dataset: one per commit)."""
        cur = self.conn.execute(
            "SELECT url, bytes, values_n, fetched_at, closed, sha256, version_at, origin, local_path FROM dataset_files "
            "WHERE dataset=? ORDER BY url", (name,))
        return {url: {"bytes": b, "values": n, "fetched_at": datetime.fromisoformat(f), "closed": bool(c),
                      "sha256": sha, "version_at": datetime.fromisoformat(v) if v else None, "origin": o,
                      "local_path": lp}
                for url, b, n, f, c, sha, v, o, lp in cur}

    def set_dataset_file(self, name: str, url: str, size: int, values: int, closed: bool = False,
                         sha256: str | None = None, version_at: datetime | None = None, origin: str | None = None,
                         local_path: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO dataset_files (dataset, url, bytes, values_n, fetched_at, closed, sha256, "
                "version_at, origin, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, url, size, values, _now(), int(closed), sha256,
                 version_at.isoformat() if version_at else None, origin, local_path))

    def clear_dataset_files(self, name: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM dataset_files WHERE dataset=?", (name,))

    # -- saving a version ----------------------------------------------------------------

    def save_dataset_version(self, provider: str, name: str, parsed, version_ms: int, origin: str,
                             first_rule=None, withdraw: bool = False) -> dict[str, int]:
        """One published version of a dataset file. Values new to the dataset are available from
        the version's time, except in a series' first version, where `first_rule(ts)` estimates when
        each value was first published (capped at the version's time): history known before this
        source was followed. Changed values become revisions, at the version's time; with
        `withdraw` (a file holding the whole dataset) values missing from it are withdrawn. Index
        members join, change or leave the same way (a first list dates members from their 'date
        added'). Returns counts: new, revised, withdrawn, unchanged, joined, changed, left."""
        conn = self._dataset_db(provider, create=True)
        stored = _ms_now()
        by_series: dict[str, dict[int, float]] = {}
        for s, ts, v in parsed.values:
            by_series.setdefault(s, {})[int(ts)] = v
        counts = dict(new=0, revised=0, withdrawn=0, unchanged=0, joined=0, changed=0, left=0)
        with conn:
            known = {r[0] for r in conn.execute("SELECT DISTINCT series FROM observations WHERE dataset=?", (name,))}
            for s in sorted(set(by_series) | (known if withdraw else set())):
                new = by_series.get(s, {})
                latest = self._latest(conn, name, s)
                first = not latest
                rows = []
                for ts, v in new.items():
                    old = latest.get(ts, _MISSING)
                    if old is _MISSING:
                        at = min(int(first_rule(ts)), version_ms) if first and first_rule else version_ms
                        rows.append((name, s, ts, at, v, stored, origin))
                        counts["new"] += 1
                    elif old != v:
                        rows.append((name, s, ts, version_ms, v, stored, origin))
                        counts["revised"] += 1
                    else:
                        counts["unchanged"] += 1
                if withdraw:
                    for ts, old in latest.items():
                        if old is not None and ts not in new:
                            rows.append((name, s, ts, version_ms, None, stored, origin))
                            counts["withdrawn"] += 1
                conn.executemany("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                merged = dict(latest)
                merged.update({r[2]: r[4] for r in rows})
                live = [t for t, v in merged.items() if v is not None]
                title, unit, freq = parsed.series.get(s, (None, None, None))
                conn.execute(
                    "INSERT INTO series (dataset, series, title, unit, freq, first_ts, last_ts, n) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (dataset, series) DO UPDATE SET "
                    "title=COALESCE(excluded.title, title), unit=COALESCE(excluded.unit, unit), "
                    "freq=COALESCE(excluded.freq, freq), first_ts=excluded.first_ts, last_ts=excluded.last_ts, "
                    "n=excluded.n",
                    (name, s, title or s, unit, freq, min(live) if live else None, max(live) if live else None,
                     len(live)))
            for idx in sorted({c["index"] for c in parsed.constituents}):
                self._save_members(conn, idx, [c for c in parsed.constituents if c["index"] == idx], version_ms,
                                   stored, origin, counts)
        return counts

    def _save_members(self, conn, idx: str, members: list[dict], version_ms: int, stored: int, origin: str,
                      counts: dict) -> None:
        latest = {t: tuple(rest) for t, *rest, _ in conn.execute(
            "SELECT ticker, member, name, sector, industry, added, cik, MAX(available_at) FROM constituents "
            "WHERE idx=? GROUP BY ticker", (idx,))}
        first = not latest
        now = {c["ticker"]: (1, c.get("name"), c.get("sector"), c.get("industry"), c.get("added"), c.get("cik"))
               for c in members}
        rows = []
        for t, fields in now.items():
            old = latest.get(t)
            if old == fields:
                continue
            at = version_ms
            if old is None and first:
                added = _added_ms(fields[4])
                at = min(added, version_ms) if added else version_ms
            counts["joined" if old is None or old[0] == 0 else "changed"] += 1
            rows.append((idx, t, at, *fields, stored, origin))
        for t, old in latest.items():
            if old[0] == 1 and t not in now:
                rows.append((idx, t, version_ms, 0, *old[1:], stored, origin))
                counts["left"] += 1
        conn.executemany("INSERT OR REPLACE INTO constituents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    @staticmethod
    def _latest(conn, name: str, series: str, as_of: int | None = None) -> dict[int, float | None]:
        """{ts: value} of a series' newest version (as of `as_of` ms), withdrawn values as None."""
        sql = "SELECT ts, value, MAX(available_at) FROM observations WHERE dataset=? AND series=?"
        args: list = [name, series]
        if as_of is not None:
            sql += " AND available_at <= ?"
            args.append(as_of)
        return {ts: v for ts, v, _ in conn.execute(sql + " GROUP BY ts", args)}

    # -- reading --------------------------------------------------------------------------

    def dataset_series(self, provider: str, name: str) -> list[dict]:
        """A dataset's series: {series, title, unit, freq, first_ts, last_ts, n} (latest version), by name."""
        conn = self._dataset_db(provider)
        if conn is None:
            return []
        cur = conn.execute("SELECT series, title, unit, freq, first_ts, last_ts, n FROM series WHERE dataset=? "
                           "ORDER BY series", (name,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def dataset_last_values(self, provider: str, name: str) -> dict[str, float]:
        """{series: its newest value} for every series of a dataset (latest version)."""
        conn = self._dataset_db(provider)
        if conn is None:
            return {}
        return {s: v for s, v in conn.execute(
            "SELECT s.series, (SELECT o.value FROM observations o WHERE o.dataset=s.dataset AND o.series=s.series "
            "AND o.ts=s.last_ts ORDER BY o.available_at DESC LIMIT 1) FROM series s WHERE s.dataset=?", (name,))
            if v is not None}

    def dataset_values(self, provider: str, name: str, series: str, lo_ms: int | None = None,
                       hi_ms: int | None = None, as_of: int | None = None) -> pd.DataFrame:
        """ts (ms) and value of one series, oldest first: the latest version, or with `as_of` (ms)
        what was public then (no value published later, no revision made later)."""
        conn = self._dataset_db(provider)
        if conn is None:
            return pd.DataFrame(columns=["ts", "value"])
        sql, args = "SELECT ts, value, MAX(available_at) FROM observations WHERE dataset=? AND series=?", [name, series]
        if as_of is not None:
            sql += " AND available_at <= ?"
            args.append(as_of)
        if lo_ms is not None:
            sql += " AND ts >= ?"
            args.append(lo_ms)
        if hi_ms is not None:
            sql += " AND ts < ?"
            args.append(hi_ms)
        rows = [(ts, v) for ts, v, _ in conn.execute(sql + " GROUP BY ts ORDER BY ts", args) if v is not None]
        return pd.DataFrame(rows, columns=["ts", "value"])

    def dataset_versions(self, provider: str, name: str, series: str, ts: int) -> list[dict]:
        """Every version of one value, oldest first: {available_at, value, stored_at, origin}."""
        conn = self._dataset_db(provider)
        if conn is None:
            return []
        return [{"available_at": a, "value": v, "stored_at": s, "origin": o} for a, v, s, o in conn.execute(
            "SELECT available_at, value, stored_at, origin FROM observations WHERE dataset=? AND series=? AND ts=? "
            "ORDER BY available_at", (name, series, ts))]

    def constituents(self, provider: str, index: str, as_of: int | None = None) -> dict[str, dict]:
        """{ticker: {name, sector, industry, added, cik}} of an index's members: now, or as public at
        `as_of` (ms)."""
        conn = self._dataset_db(provider)
        if conn is None:
            return {}
        sql, args = ("SELECT ticker, member, name, sector, industry, added, cik, MAX(available_at) FROM constituents "
                     "WHERE idx=?"), [index]
        if as_of is not None:
            sql += " AND available_at <= ?"
            args.append(as_of)
        return {t: {"name": n, "sector": s, "industry": i, "added": a, "cik": c}
                for t, member, n, s, i, a, c, _ in conn.execute(sql + " GROUP BY ticker", args) if member}

    # -- files from before versioning -----------------------------------------------------

    def dataset_legacy(self, provider: str) -> tuple[list[tuple], list[tuple]]:
        """Rows of a provider file's tables from before versioning: (observations, constituents)."""
        conn = self._dataset_db(provider)
        if conn is None:
            return [], []
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        obs = conn.execute("SELECT dataset, series, ts, value FROM observations_v1").fetchall() \
            if "observations_v1" in tables else []
        members = conn.execute("SELECT idx, ticker, name, sector, industry, added, cik FROM constituents_v1").fetchall() \
            if "constituents_v1" in tables else []
        return obs, members

    def drop_dataset_legacy(self, provider: str) -> None:
        conn = self._dataset_db(provider)
        if conn is not None:
            with conn:
                conn.execute("DROP TABLE IF EXISTS observations_v1")
                conn.execute("DROP TABLE IF EXISTS constituents_v1")

    def dataset_providers(self) -> list[str]:
        """Providers with a file."""
        if self.datasets_dir is None or not self.datasets_dir.is_dir():
            return sorted(self._dataset_conns)
        return sorted({f.stem for f in self.datasets_dir.glob("*.db")} | set(self._dataset_conns))

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
