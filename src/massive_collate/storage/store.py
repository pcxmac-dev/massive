"""The Store: one object over the main file and the feed and dataset files. Its methods come from
one part per responsibility, the other modules of this package."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .admin import AdminStore
from .appstate import AppStateStore
from .audits import AuditStore
from .common import BUSY_TIMEOUT_S, add_column, write_transaction
from .datasets import DatasetStore
from .directory import DirectoryStore
from .feeds import FeedStore, OldLayout, _Feed, old_layout, tick_access
from .ledger import LedgerStore, key_coverage_by_origin
from .markets import MarketsStore
from .provenance import ProvenanceStore
from .pruned import PrunedStore
from .schema import MIGRATIONS, SCHEMA
from .watchlist import WatchlistStore


class Store(ProvenanceStore, AdminStore, FeedStore, PrunedStore, DatasetStore, DirectoryStore, MarketsStore,
            WatchlistStore, LedgerStore, AuditStore, AppStateStore):
    def __init__(self, path: Path | str, check_same_thread: bool = True, feeds_dir: Path | str | None = None,
                 datasets_dir: Path | str | None = None, ticks: bool | None = None):
        self.path = Path(path)
        # Whether the feeds' tick files are opened at all (bars-only mode: not; feeds.set_tick_access)
        self.ticks_allowed = tick_access() if ticks is None else ticks
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.feeds_dir = Path(feeds_dir) if feeds_dir else (None if memory else self.path.with_name("feeds"))
        self.datasets_dir = Path(datasets_dir) if datasets_dir else (None if memory else self.path.with_name("datasets"))
        self._dataset_conns: dict[str, sqlite3.Connection] = {}
        self._check_same_thread = check_same_thread
        self.conn = sqlite3.connect(str(path), check_same_thread=check_same_thread, timeout=BUSY_TIMEOUT_S)
        old = old_layout(self.conn, None if memory else self.path)
        if old:
            self.conn.close()
            raise OldLayout(f"{self.path} still holds every feed's data in one place ({', '.join(old)}): "
                            "run `python -m massive_collate admin split-feeds` to move each feed to its own "
                            "files (it backs up first)")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA journal_size_limit=67108864")  # cut back to 64 MB once checkpointed
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._feeds: dict[str, _Feed] = {}
        self._feeds_lock = threading.Lock()
        self._missing: dict[str, float] = {}  # feed -> when it was found to have no files (monotonic s)
        self._feeds_seen = None
        # Data-version caches: SQLite's data_version changes when another connection (the
        # collector) commits, per file; _writes counts this store's own market-data writes.
        self._writes = 0
        self.rows_saved = 0  # bars and trades a feed sent, stored by this instance (a run's count)
        self._cache: dict = {}
        self._scan_feeds()

    def reader(self) -> "Store":
        """Another store on the same files, with connections of its own: SQLite's WAL lets it read
        while this one is busy (the chart engine loading a long history does not hold up the
        rest of the chart app). An in-memory store has no files to share: itself."""
        if str(self.path) == ":memory:":
            return self
        return Store(self.path, check_same_thread=self._check_same_thread, feeds_dir=self.feeds_dir,
                     datasets_dir=self.datasets_dir, ticks=self.ticks_allowed)

    def _version(self) -> tuple:
        self._scan_feeds()
        return (self.conn.execute("PRAGMA data_version").fetchone()[0],
                tuple(f.version() for f in self._open_feeds(scan=False)), self._writes)

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
        # Looked at without a lock (every store opened takes this path; almost always nothing is
        # missing), then changed under the write lock and looked at again there: on the day of an
        # upgrade the chart app, the sync and the live recorder open the file at once.
        existing = {t: {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")} for t, _, _ in MIGRATIONS}
        missing = [m for m in MIGRATIONS if m[1] not in existing[m[0]]]
        if missing:
            with write_transaction(self.conn):
                for table, column, kind in missing:
                    add_column(self.conn, table, column, kind)
        key_coverage_by_origin(self.conn)

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
