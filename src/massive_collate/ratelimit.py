"""Sliding-window rate limiter (the free plan allows 5 requests per minute).

Every run is a new process with a new limiter. Without a shared record, a run starting right
after another would begin with an empty window and could send up to twice the allowed rate
within one window, so a limiter can share its window through a ledger (each source's recent
request times, kept in the database: `KvLedger`). Before it waits it tells `on_wait` how long,
so the app can show the wait and the limit behind it.
"""
from __future__ import annotations

import json
import time
from collections import Counter, deque
from typing import Callable, Protocol


class Ledger(Protocol):
    def load(self) -> list[float]: ...

    def save(self, times: list[float]) -> None: ...


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
        self._ledger: Ledger | None = None
        self.on_wait: Callable[[float], None] | None = None  # told the seconds before each wait

    def share(self, ledger: Ledger) -> None:
        """Continue the window earlier runs left in `ledger`, and keep this one's calls there."""
        self._ledger = ledger
        self._merge()

    def _merge(self) -> None:
        # Another process may have called since (the run lock normally prevents it). A multiset
        # union: calls made in the same millisecond are separate calls.
        if self._ledger is not None:
            self._calls = deque(sorted((Counter(self._calls) | Counter(self._ledger.load())).elements()))

    def acquire(self) -> None:
        """Block until another call is allowed, then record it."""
        if not self.max_calls:
            return
        self._merge()
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(round(now, 3))
                if self._ledger is not None:
                    self._ledger.save(list(self._calls))
                return
            wait = min(self.period, self.period - (now - self._calls[0]))
            if self.on_wait:
                self.on_wait(wait)
            self._sleep(wait)

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


def load_times(store, source: str) -> list[float]:
    try:
        return [float(t) for t in store.kv_json(ledger_key(source), [])]
    except (ValueError, TypeError):
        return []


class KvLedger:
    """A source's recent request times in the app's key-value table, shared by every run."""

    def __init__(self, store, source: str):
        self.store, self.source = store, source

    def load(self) -> list[float]:
        return load_times(self.store, self.source)

    def save(self, times: list[float]) -> None:
        self.store.kv_set(ledger_key(self.source), json.dumps(times))
