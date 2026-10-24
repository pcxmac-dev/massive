"""Maintenance: summary of what is stored, integrity checks, compaction and backups."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .datasets import datasets_backup_dir
from .feeds import feeds_backup_dir


class AdminStore:
    """Store methods: maintenance: summary of what is stored, integrity checks, compaction and backups."""

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
            for table, label in (("bars", feed.name), ("built_bars", f"{feed.name} (built from ticks)")):
                bars += [(label, *r) for r in feed.conn.execute(
                    "SELECT timespan, multiplier, COUNT(DISTINCT ticker), COUNT(*), MIN(ts), MAX(ts) "
                    f"FROM {table} GROUP BY timespan, multiplier")]
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
