"""One-off move from the single-file layout to files per feed (see storage.py).

Before: every feed's bars (and the composite's deviation stats) in the main file, every feed's
ticks in one ticks.db beside it. After: feeds/<feed>.db and feeds/<feed>-ticks.db, with the main
file keeping only what the feeds share.

The old files are backed up first (SQLite's online backup, checked page for page). Each copy is
checked against its original (row count, column totals, SQLite's quick_check) before anything is
removed; only then are the old tables dropped, the main file compacted and the old tick file
deleted. Copies are written to a `feeds.partial` folder and moved into `feeds` once all are
checked, so a run that stops part way leaves the old layout whole and can simply be run again.
Run it with no sync going (the CLI takes the sync lock) and the chart app closed.
"""
from __future__ import annotations

import math
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .storage import COMPOSITE, COMPOSITE_SCHEMA, FEED_SCHEMA, TICKS_SCHEMA, old_layout

# (columns copied, sums the copy must reproduce, key order). Both sides are summed in key order,
# so a faithful copy gives the very same totals.
BARS = ("ticker, timespan, multiplier, ts, open, high, low, close, volume, vwap, transactions",
        "COUNT(*), TOTAL(LENGTH(ticker)), TOTAL(LENGTH(timespan)), TOTAL(multiplier), TOTAL(ts), TOTAL(open), "
        "TOTAL(high), TOTAL(low), TOTAL(close), TOTAL(volume), TOTAL(vwap), TOTAL(transactions), COUNT(vwap), "
        "COUNT(transactions)",
        "ticker, timespan, multiplier, ts")
TRADES = ("ticker, ts_ns, seq, price, size, exchange, conditions, trade_id, tape",
          "COUNT(*), TOTAL(LENGTH(ticker)), TOTAL(ts_ns), TOTAL(seq), TOTAL(price), TOTAL(size), TOTAL(exchange), "
          "TOTAL(tape), TOTAL(LENGTH(conditions)), TOTAL(LENGTH(trade_id))",
          "ticker, ts_ns, seq")
STATS = ("ticker, timespan, ts, venues, sd_bps, range_bps, env_h, env_l, members, outliers",
         "COUNT(*), TOTAL(LENGTH(ticker)), TOTAL(LENGTH(timespan)), TOTAL(ts), TOTAL(venues), TOTAL(sd_bps), "
         "TOTAL(range_bps), TOTAL(env_h), TOTAL(env_l), TOTAL(LENGTH(members)), TOTAL(LENGTH(outliers))",
         "ticker, timespan, ts")


class SplitError(RuntimeError):
    """A copy or backup did not match its original; nothing was removed."""


def split(path: Path, log=print, feeds_dir: Path | None = None, backup_dir: Path | None = None) -> dict[str, int]:
    """Move every feed's data to files of its own. Returns {copied table: rows}."""
    path = Path(path)
    feeds_dir = Path(feeds_dir) if feeds_dir else path.with_name("feeds")
    backup_dir = Path(backup_dir) if backup_dir else path.parent / "backups"
    old_ticks = path.with_name("ticks.db")
    with closing(sqlite3.connect(str(path))) as conn:
        left = old_layout(conn, path)
        if not left:
            log("nothing to move: every feed already has files of its own")
            return {}
        log("moving: " + ", ".join(left))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        _backup(conn, "main", backup_dir / f"pre-split-{stamp}.db", log)
        if old_ticks.exists():
            conn.execute("ATTACH DATABASE ? AS old", (str(old_ticks),))
            _backup(conn, "old", backup_dir / f"pre-split-{stamp}-ticks.db", log)

        work = feeds_dir.with_name(feeds_dir.name + ".partial")
        if work.exists():
            shutil.rmtree(work)  # an earlier run that stopped part way: its copies are redone
        work.mkdir(parents=True)
        moved: dict[str, int] = {}
        if _has(conn, "main", "bars"):
            for src in _sources(conn, "main.bars"):
                schema = FEED_SCHEMA + (COMPOSITE_SCHEMA if src == COMPOSITE else "")
                moved[f"{src}.db bars"] = _copy(conn, work / f"{src}.db", schema, "bars", "main.bars", src, BARS, log)
        if _has(conn, "main", "composite_stats"):
            moved[f"{COMPOSITE}.db composite_stats"] = _copy(
                conn, work / f"{COMPOSITE}.db", FEED_SCHEMA + COMPOSITE_SCHEMA, "composite_stats",
                "main.composite_stats", None, STATS, log)
        origins = [t for t in ("main.trades", "old.trades") if _has(conn, *t.split(".")) and _sources(conn, t)]
        if len(origins) > 1:
            raise SplitError("ticks in both the main file and ticks.db: merge them by hand first; nothing removed")
        for origin in origins:
            for src in _sources(conn, origin):
                moved[f"{src}-ticks.db trades"] = _copy(conn, work / f"{src}-ticks.db", TICKS_SCHEMA, "trades",
                                                        origin, src, TRADES, log, ticks=True)

        feeds_dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(work.iterdir()):
            for stale in (feeds_dir / (f.name + "-wal"), feeds_dir / (f.name + "-shm")):
                stale.unlink(missing_ok=True)
            f.replace(feeds_dir / f.name)
        work.rmdir()
        log(f"feed files in place: {feeds_dir}")

        with conn:
            for table in ("bars", "composite_stats", "trades"):
                conn.execute(f"DROP TABLE IF EXISTS main.{table}")
        t0 = time.monotonic()
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log(f"main file compacted to {path.stat().st_size / 1e6:,.1f} MB ({time.monotonic() - t0:.0f} s)")
        if old_ticks.exists():
            conn.execute("DETACH DATABASE old")
    for f in (old_ticks, old_ticks.with_name("ticks.db-wal"), old_ticks.with_name("ticks.db-shm")):
        f.unlink(missing_ok=True)
    log("old tick file removed (its copy is in the backup and in the feed's tick file)")
    return moved


def _has(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    try:
        return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone() is not None
    except sqlite3.OperationalError:  # schema not attached
        return False


def _sources(conn: sqlite3.Connection, table: str) -> list[str]:
    """Distinct feeds in a table keyed by source first: one index seek per feed."""
    out = []
    row = conn.execute(f"SELECT source FROM {table} ORDER BY source LIMIT 1").fetchone()
    while row is not None:
        out.append(row[0])
        row = conn.execute(f"SELECT source FROM {table} WHERE source > ? ORDER BY source LIMIT 1", row).fetchone()
    return out


def _backup(conn: sqlite3.Connection, name: str, dest: Path, log) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with closing(sqlite3.connect(str(dest))) as target:
        conn.backup(target, name=name)
        pages = target.execute("PRAGMA page_count").fetchone()[0]
        check = target.execute("PRAGMA quick_check").fetchone()[0]
    want = conn.execute(f"PRAGMA {name}.page_count").fetchone()[0]
    if pages != want or check != "ok":
        raise SplitError(f"backup {dest} is not a whole copy ({pages} of {want} pages, check {check}); nothing moved")
    log(f"backed up to {dest} ({dest.stat().st_size / 1e9:.2f} GB, {time.monotonic() - t0:.0f} s)")


def _create(target: Path, schema: str, ticks: bool) -> None:
    with closing(sqlite3.connect(":memory:" if ticks else str(target))) as c:
        if ticks:
            c.execute("ATTACH DATABASE ? AS ticks", (str(target),))
        c.executescript(schema)


def _copy(conn: sqlite3.Connection, target: Path, schema: str, table: str, origin: str, source: str | None,
          spec: tuple[str, str, str], log, ticks: bool = False) -> int:
    """Copy one feed's rows of `origin` into `table` of `target` and check the copy."""
    cols, sums, order = spec
    where, args = ("WHERE source=?", (source,)) if source else ("", ())
    _create(target, schema, ticks)
    t0 = time.monotonic()
    conn.execute("ATTACH DATABASE ? AS dst", (str(target),))
    try:
        conn.execute("PRAGMA dst.journal_mode=OFF")  # a fresh file, checked below: no journal needed
        conn.execute("PRAGMA dst.synchronous=NORMAL")
        conn.execute("PRAGMA dst.cache_size=-262144")
        with conn:
            conn.execute(f"INSERT INTO dst.{table} ({cols}) SELECT {cols} FROM {origin} {where} ORDER BY {order}", args)
        want = conn.execute(f"SELECT {sums} FROM {origin} {where}", args).fetchone()
        got = conn.execute(f"SELECT {sums} FROM dst.{table}").fetchone()
        check = conn.execute("PRAGMA dst.quick_check").fetchone()[0]
    finally:
        conn.execute("DETACH DATABASE dst")
    label = f"{target.name} {table}"
    if not _same(want, got) or check != "ok":
        raise SplitError(f"{label}: the copy differs from the original ({want} vs {got}, check {check}); "
                         "nothing removed")
    with closing(sqlite3.connect(str(target))) as c:
        c.execute("PRAGMA journal_mode=WAL")
    log(f"{label}: {want[0]:,} rows copied and checked ({time.monotonic() - t0:.0f} s)")
    return want[0]


def _same(a: tuple, b: tuple) -> bool:
    """Counts equal; totals equal but for float rounding (they are summed in the same order)."""
    return len(a) == len(b) and a[0] == b[0] and all(
        math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-9) for x, y in zip(a[1:], b[1:]))
