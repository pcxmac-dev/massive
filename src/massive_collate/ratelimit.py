"""Sliding-window rate limiter (the free plan allows 5 requests per minute).

Every run is a new process with a new limiter. Without a shared record, a run starting right
after another would begin with an empty window and could send up to twice the allowed rate
within one window, so a limiter shares its window through ledgers: the machine's
(`MachineLedger`: every process and every database on this machine, a test copy too, under the
feed's name and a fingerprint of its key, as the server counts them) and the database's
(`KvLedger`, which the app shows). A call is taken inside one write transaction of the first
ledger, so two processes cannot both take the window's last place. Before it waits it tells
`on_wait` how long, so the app can show the wait and the limit behind it.

The machine's ledger also counts each key's requests per UTC day (`MachineLedger.take_day`): the
daily cap holds for the machine, not for each database apart.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter, deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol


class Ledger(Protocol):
    def load(self) -> list[float]: ...

    def update(self, fn: Callable[[list[float]], list[float]]) -> list[float]:
        """Read, change and write the times in one write transaction; returns the new times."""
        ...


class RateLimiter:
    def __init__(
        self,
        max_calls: int | None,
        period: float = 60.0,
        clock: Callable[[], float] = time.time,  # wall clock: windows are shared between processes
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._ledgers: list[Ledger] = []
        self.on_wait: Callable[[float], None] | None = None  # told the seconds before each wait

    def share(self, ledger: Ledger) -> None:
        """Keep this limiter's window in `ledger` too, continuing what earlier runs left there.
        The first ledger shared decides each call (in one transaction); the others are told."""
        self._ledgers.append(ledger)

    def acquire(self) -> None:
        """Block until another call is allowed, then record it."""
        if not self.max_calls:
            return
        while True:
            wait = self._take()
            if wait <= 0:
                return
            if self.on_wait:
                self.on_wait(wait)
            self._sleep(wait)

    def _take(self) -> float:
        """Take a place in the window now (0), or say how many seconds until one frees. Every
        ledger's calls count (a multiset union: calls made in the same millisecond are separate
        calls); the decision is made inside the first ledger's transaction."""
        if not self._ledgers:
            return self._place(Counter())
        first, *others = self._ledgers
        seen = Counter()
        for other in others:
            seen |= Counter(other.load())
        wait = 0.0

        def take(times: list[float]) -> list[float]:
            nonlocal wait
            wait = self._place(seen | Counter(times))
            return list(self._calls)

        first.update(take)
        if wait <= 0:
            for other in others:
                other.update(lambda times: self._inside(Counter(times) | Counter(self._calls)))
        return wait

    def _place(self, known: Counter) -> float:
        """With `known` calls merged into this limiter's own: a place taken now (0), or the wait."""
        now = self._clock()
        self._calls = deque(self._inside(Counter(self._calls) | known, now))
        if len(self._calls) < self.max_calls:
            self._calls.append(round(now, 3))
            return 0.0
        return min(self.period, self.period - (now - self._calls[0]))

    def _inside(self, calls: Counter, now: float | None = None) -> list[float]:
        """The calls still inside the window, oldest first."""
        now = self._clock() if now is None else now
        return sorted(t for t in calls.elements() if now - t < self.period)

    def describe(self) -> str:
        return describe(self.max_calls, self.period)


def describe(max_calls: int | None, period: float) -> str:
    """'5 requests per minute', '45 requests per hour', 'no limit'."""
    if not max_calls:
        return "no limit"
    unit = {1.0: "second", 60.0: "minute", 3600.0: "hour", 86400.0: "day"}.get(float(period), f"{period:g} s")
    return f"{max_calls:,} requests per {unit}"


def window_state(times: list[float], max_calls: int | None, period: float, now: float | None = None) -> dict:
    """How full a source's window is: calls in it, and seconds until the next call is allowed."""
    now = time.time() if now is None else now
    inside = sorted(t for t in times if now - t < period)
    free_in = 0.0
    if max_calls and len(inside) >= max_calls:
        free_in = max(0.0, min(period, period - (now - inside[-max_calls])))
    return {"used": len(inside), "allowed": max_calls, "periodSeconds": period, "freeIn": round(free_in, 1)}


def ledger_key(source: str) -> str:
    return f"ratelimit:{source}"


def wait_key(source: str) -> str:
    """Where a run records a wait in progress: {until, seconds, reason}."""
    return f"fetch:wait:{source}"


def _floats(value) -> list[float]:
    try:
        return [float(t) for t in value or []]
    except (ValueError, TypeError):
        return []


def load_times(store, source: str) -> list[float]:
    return _floats(store.kv_json(ledger_key(source), []))


class KvLedger:
    """A source's recent request times in the app's key-value table, shared by every run on
    that database (and shown by the app)."""

    def __init__(self, store, source: str):
        self.store, self.source = store, source

    def load(self) -> list[float]:
        return load_times(self.store, self.source)

    def update(self, fn: Callable[[list[float]], list[float]]) -> list[float]:
        return self.store.kv_update(ledger_key(self.source), lambda v: fn(_floats(v)), [])


def fingerprint(key: str) -> str:
    """What names a key in the machine's ledger: a short digest, never the key."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def machine_path() -> Path:
    """The machine's request windows and day counts: beside the live recorder's machine-wide lock
    (live.machine_lock; MASSIVE_LIVE_LOCK moves both, as the tests do)."""
    from .live import machine_lock

    return machine_lock().with_name("request-windows.db")


DAYS_KEPT = 7  # day counts older than this are dropped as new ones are taken


class MachineLedger:
    """A feed's request window and daily count for the whole machine, under the feed's name (an
    asset class's own window: `massive:crypto`) and a fingerprint of its key (none for the public
    feeds, whose limits are per address): two databases on one key, a test copy say, share them
    as the server does. A small SQLite file; every change is one write transaction."""

    def __init__(self, name: str, key: str | None = None, path: Path | None = None):
        self.name = f"{name}:{fingerprint(key)}" if key else name
        self.path = path

    def _in_transaction(self, fn):
        """`fn(conn)` inside one write transaction (BEGIN IMMEDIATE: another process waits its
        turn, so neither loses the other's change, reads included); the tables made first."""
        path = self.path or machine_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=60, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS windows (name TEXT PRIMARY KEY, times TEXT NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS days (name TEXT, day TEXT, n INTEGER NOT NULL, "
                             "PRIMARY KEY (name, day))")
                conn.execute("CREATE TABLE IF NOT EXISTS symbols (name TEXT, month TEXT, symbol TEXT, "
                             "PRIMARY KEY (name, month, symbol))")
                out = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return out

    def _times(self, conn) -> list[float]:
        row = conn.execute("SELECT times FROM windows WHERE name=?", (self.name,)).fetchone()
        return _floats(json.loads(row[0])) if row else []

    def load(self) -> list[float]:
        return self._in_transaction(self._times)

    def update(self, fn: Callable[[list[float]], list[float]]) -> list[float]:
        def change(conn):
            times = fn(self._times(conn))
            conn.execute("INSERT OR REPLACE INTO windows VALUES (?, ?)", (self.name, json.dumps(times)))
            return times

        return self._in_transaction(change)

    def take_day(self, cap: int, now: datetime | None = None) -> bool:
        """Count one request on today's (UTC) count, unless it has reached `cap`: whether the
        request may go."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = now.date().isoformat()

        def take(conn) -> bool:
            row = conn.execute("SELECT n FROM days WHERE name=? AND day=?", (self.name, day)).fetchone()
            if row and row[0] >= cap:
                return False
            conn.execute("INSERT INTO days VALUES (?, ?, 1) ON CONFLICT (name, day) DO UPDATE SET n = n + 1",
                         (self.name, day))
            conn.execute("DELETE FROM days WHERE day < ?", ((now - timedelta(days=DAYS_KEPT)).date().isoformat(),))
            return True

        return self._in_transaction(take)

    def take_symbol(self, symbol: str, cap: int, now: datetime | None = None) -> bool:
        """Whether `symbol` may be asked for this month (UTC) under a plan's cap of different symbols
        a month, counted for the key on the whole machine, every database and every feed on it
        (Tiingo's 500: its stocks and its forex alike): taken when it may, in one transaction."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        month = f"{now:%Y-%m}"

        def take(conn) -> bool:
            if conn.execute("SELECT 1 FROM symbols WHERE name=? AND month=? AND symbol=?",
                            (self.name, month, symbol)).fetchone():
                return True
            if conn.execute("SELECT COUNT(*) FROM symbols WHERE name=? AND month=?", (self.name, month)).fetchone()[0] >= cap:
                return False
            conn.execute("INSERT INTO symbols VALUES (?, ?, ?)", (self.name, month, symbol))
            conn.execute("DELETE FROM symbols WHERE month < ?", (f"{now - timedelta(days=62):%Y-%m}",))
            return True

        return self._in_transaction(take)

    def today(self, now: datetime | None = None) -> int:
        """Requests counted today (UTC) on this machine for this feed and key."""
        day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        row = self._in_transaction(
            lambda conn: conn.execute("SELECT n FROM days WHERE name=? AND day=?", (self.name, day)).fetchone())
        return row[0] if row else 0
