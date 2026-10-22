"""Sliding-window rate limiter (the free plan allows 5 requests per minute)."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        max_calls: int | None,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        """Block until another call is allowed, then record it."""
        if not self.max_calls:
            return
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return
            self._sleep(self.period - (now - self._calls[0]))
