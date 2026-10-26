"""Maintenance: summary of what is stored, integrity checks, compaction and backups."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .common import BUSY_TIMEOUT_S, _distinct, _feed_name
from .datasets import datasets_backup_dir
from .feeds import feeds_backup_dir

# In-place VACUUM rebuilds a file in a temporary copy and writes it back through the write-ahead
# log, holding the write lock all the while: every writer waits (the live recorder's trades pile
# up, the sync fails), for hours on a file of hundreds of GB. Larger files are left to `admin
# compact`, which copies the file instead and blocks no one.
VACUUM_IN_PLACE_MAX = 4 * 2**30
SPACE_MARGIN = 2**30  # left free on a drive after a compaction, for everything else writing to it


def _free(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def _vacuum_room(path: Path) -> str | None:
    """Why an in-place VACUUM of `path` should not run (None: it may): the file too large, or too
    little free space for its temporary copy (in the temporary folder) and the write-ahead log it
    is written back through (beside it), each about the file's size."""
    size = path.stat().st_size
    if size > VACUUM_IN_PLACE_MAX:
        return f"{size / 1e9:.1f} GB: too large to compact in place (every writer would wait); `admin compact` copies it"
    temp = Path(tempfile.gettempdir())
    same = os.stat(temp).st_dev == os.stat(path.parent).st_dev
    need = {path.parent: (2 if same else 1) * size + SPACE_MARGIN}
    if not same:
        need[temp] = size + SPACE_MARGIN
    for folder, n in need.items():
        if _free(folder) < n:
            return f"needs {n / 1e9:.1f} GB free in {folder}, {_free(folder) / 1e9:.1f} GB is"
    return None


def _vacuum(path: Path | None, conn: sqlite3.Connection, schema: str, log) -> bool:
    """VACUUM one file in place after the checks above, then checkpoint its write-ahead log and
    cut it back (the copy written back left it as large as the database). False: skipped."""
    if path is not None and path.exists():
        why = _vacuum_room(path)
        if why:
            log(f"vacuum {path.name}: skipped ({why})")
            return False
    conn.execute(f"VACUUM {schema}")
    busy = conn.execute(f"PRAGMA {schema}.wal_checkpoint(TRUNCATE)").fetchone()
    if busy and busy[0]:
        log(f"vacuum {path.name if path else schema}: the write-ahead log is cut back at the next checkpoint "
            "(another connection is reading)")
    return True


def _vacuum_into(conn: sqlite3.Connection, copy: Path) -> None:
    conn.execute("VACUUM INTO ?", (str(copy),))


def _sharing_violation(path: Path) -> bool:
    """Windows: whether another handle is open on the file (one that shares nothing cannot be
    had while any other is open). Elsewhere: not known (False)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, no sharing, OPEN_EXISTING
    if handle in (None, wintypes.HANDLE(-1).value):  # INVALID_HANDLE_VALUE
        return ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
    k32.CloseHandle(handle)
    return False


def _in_use(path: Path) -> bool:
    """Whether another connection has a database file open, once this process has closed its own:
    its write-ahead log and shared-memory files exist while any connection does (the last to close
    removes them), and on Windows the file cannot be had without sharing."""
    return any(Path(f"{path}{s}").exists() for s in ("-wal", "-shm")) or _sharing_violation(path)


def _objects(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)) as conn:
        return sorted(conn.execute("SELECT type, name FROM sqlite_master"))


def compact_file(path: Path | str, swap: bool = False, log=print) -> Path | None:
    """Compact one database file by copying it: `VACUUM INTO` a new file beside it,
    <name>.compact.db (not a feed's or a dataset's name: nothing opens it). It needs free space
    for the live data only (checked first) and blocks no writer; the copy is the file as it was
    when the copy began.

    With `swap`, the copy then takes the file's place and the file is kept beside it as
    <name>.pre-compact-<time>.db (deleting it is the user's decision), only if nothing was written
    to the file during the copy and no other process has it open: the chart app, the sync and the
    live recorder stopped (on Windows a file open elsewhere cannot be renamed either). Otherwise
    nothing is swapped and the copy is removed (it is, or may soon be, out of date).

    Returns the file now in place (swapped), the copy (not swapped), or None when nothing was done."""
    path = Path(path)
    if not path.is_file():
        log(f"compact {path}: no such file")
        return None
    copy = path.with_name(f"{path.stem}.compact{path.suffix}")
    if copy.exists():
        log(f"compact {path.name}: {copy.name} is there from an earlier compaction: swap it in or remove it first")
        return None
    if swap and _sharing_violation(path):  # before hours of copying, where it can be known
        log(f"compact {path.name}: open in another process (stop the chart app, the sync and the live recorder "
            "first): nothing done")
        return None
    with closing(sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S)) as conn:
        page, pages, unused = (conn.execute(f"PRAGMA {p}").fetchone()[0] for p in
                               ("page_size", "page_count", "freelist_count"))
        live = page * (pages - unused)
        if _free(path.parent) < live + SPACE_MARGIN:
            log(f"compact {path.name}: needs {(live + SPACE_MARGIN) / 1e9:.1f} GB free in {path.parent}, "
                f"{_free(path.parent) / 1e9:.1f} GB is: nothing done")
            return None
        log(f"compact {path.name}: copying {live / 1e9:.2f} GB of live data to {copy.name}")
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        _vacuum_into(conn, copy)
        written = conn.execute("PRAGMA data_version").fetchone()[0] != version  # another connection committed
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before, after = path.stat().st_size, copy.stat().st_size
    log(f"compact {path.name}: {before / 1e9:.2f} GB -> {after / 1e9:.2f} GB ({copy.name})")
    if not swap:
        return copy

    def give_up(why: str) -> None:
        copy.unlink()
        log(f"compact {path.name}: {why}: not swapped, the copy removed; the file is as it was")

    if written:
        give_up("written to during the copy (the copy is out of date)")
        return None
    if _in_use(path):
        give_up("open in another process (stop the chart app, the sync and the live recorder first)")
        return None
    if _objects(copy) != _objects(path):
        give_up("the copy's tables differ from the file's")
        return None
    with open(copy, "rb+") as f:  # VACUUM INTO does not flush its file to disk
        os.fsync(f.fileno())
    kept = path.with_name(f"{path.stem}.pre-compact-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
    try:
        os.rename(path, kept)
    except OSError as e:  # on Windows: opened by another process since the check
        give_up(f"cannot be renamed ({e.strerror or e})")
        return None
    try:
        os.rename(copy, path)
    except OSError as e:
        os.rename(kept, path)
        give_up(f"the copy cannot take its place ({e.strerror or e})")
        return None
    log(f"compact {path.name}: swapped in; the old file is kept as {kept.name} (remove it once the new one "
        "has served well)")
    return path


class AdminStore:
    """Store methods: maintenance: summary of what is stored, integrity checks, compaction and backups."""

    # -- admin -------------------------------------------------------------

    def summary(self, counts: bool = False) -> dict:
        """What is stored, from index seeks: per feed (its own bars and those built here) and
        frame, the symbols and the first and last bar (a loose scan of each file's keys, then two
        seeks a series; a GROUP BY read every bar, hours on a file of hundreds of GB). With
        `counts`, the bars of each too (a count runs through every bar of a series: that long)."""
        # No counts over the tick files (billions of rows): first and last tick per series by index.
        trades = []
        for src, ticker, kind in self.stored_series():
            if kind == "trades":
                e = self.trade_edges(src, ticker)
                trades.append((src, ticker, None, e[0], e[1]))
        bars = []
        for feed in self._open_feeds():
            for table, label in (("bars", feed.name), ("built_bars", f"{feed.name} (built from ticks)")):
                frames: dict[tuple, list] = {}
                for ticker, timespan, multiplier in _distinct(feed.conn, table, ("ticker", "timespan", "multiplier"),
                                                              ("ts",)):
                    key, where = (ticker, timespan, multiplier), f"FROM {table} WHERE ticker=? AND timespan=? AND multiplier=?"
                    first = feed.conn.execute(f"SELECT MIN(ts) {where}", key).fetchone()[0]
                    if first is None:  # removed since the scan saw it
                        continue
                    last = feed.conn.execute(f"SELECT MAX(ts) {where}", key).fetchone()[0]
                    n = feed.conn.execute(f"SELECT COUNT(*) {where}", key).fetchone()[0] if counts else None
                    held = frames.setdefault((timespan, multiplier), [0, 0 if counts else None, first, last])
                    held[0] += 1
                    held[1] = held[1] + n if counts else None
                    held[2], held[3] = min(held[2], first), max(held[3], last)
                bars += [(label, timespan, multiplier, *held) for (timespan, multiplier), held in sorted(frames.items())]
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

    def _dataset_files(self) -> list[Path]:
        """The dataset providers' files (not a compaction's copy or the file it replaced)."""
        if self.datasets_dir is None or not self.datasets_dir.is_dir():
            return []
        return [f for f in sorted(self.datasets_dir.glob("*.db")) if _feed_name(f.stem)]

    def data_files(self) -> list[Path]:
        """Every database file of the store on disk: the main one, each feed's bars and (where this
        process may open tick files) its ticks, and each dataset provider's."""
        if str(self.path) == ":memory:":
            return []
        files = [self.path]
        for feed in self._open_feeds():
            files.append(feed.path)
            if self.ticks_allowed and feed.ticks_path.exists():
                files.append(feed.ticks_path)
        return files + self._dataset_files()

    def vacuum(self, log=print) -> list[str]:
        """Compact every file in place: the main one, each feed's bars and its ticks (attached as
        `ticks`), and each dataset's. Each needs about twice its size free (its temporary copy
        and the write-ahead log it is written back through; checked first) and holds every writer
        off while it runs, so a file over VACUUM_IN_PLACE_MAX is skipped (`admin compact` copies
        it instead); the log is cut back after. Returns the files compacted."""
        done = []
        targets = [(None if str(self.path) == ":memory:" else self.path, self.conn, "main")]
        for feed in self._open_feeds():
            targets.append((feed.path, feed.conn, "main"))
            if feed.has_ticks():
                targets.append((feed.ticks_path, feed.conn, "ticks"))
        for path, conn, schema in targets:
            if path is not None:
                log(f"vacuum {path.name}")
            if _vacuum(path, conn, schema, log):
                done.append(str(path) if path is not None else ":memory:")
        for f in self._dataset_files():
            with closing(sqlite3.connect(str(f), timeout=BUSY_TIMEOUT_S)) as conn:
                if _vacuum(f, conn, "main", log):
                    done.append(str(f))
        return done

    def backup(self, dest: Path, ticks: bool = False) -> Path:
        """Copy the main file to `dest`, each feed's bars to `feeds_backup_dir(dest)` and the dataset
        files to `datasets_backup_dir(dest)`; with `ticks`, the feeds' tick files too (they can be
        hundreds of GB, so they are not copied by default)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(dest))) as target:
            self.conn.backup(target)
        for f in self._dataset_files():
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
