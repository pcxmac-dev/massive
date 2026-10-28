"""Errors shared by every data source."""
from __future__ import annotations


class SourceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class NotEntitled(SourceError):
    """The request is outside what your plan with this source covers."""


class Unauthorized(SourceError):
    """The source refused the key itself (HTTP 401: revoked, mistyped, expired): not one symbol's
    plan, so not a NotEntitled. Every request would be refused alike, so it ends the run and kills
    the feed's fetching (killswitch.is_refusal); the client sends nothing more to that host."""


class RateLimited(SourceError):
    """Still rate limited after all retries. `retry_after`: the seconds the server asked for, if it
    said (the kill that follows lasts at least that long: killswitch.retry_after_s)."""

    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(status, message)
        self.retry_after = retry_after


class BudgetExhausted(Exception):
    """The request cap for this run or day has been reached; nothing more is sent."""


class Paused(BudgetExhausted):
    """Background fetching is paused (the chart's pause button): nothing more is sent this run."""
