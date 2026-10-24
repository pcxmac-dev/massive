"""Errors shared by every data source."""
from __future__ import annotations


class SourceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class NotEntitled(SourceError):
    """The request is outside what your plan with this source covers."""


class RateLimited(SourceError):
    """Still rate limited after all retries."""


class BudgetExhausted(Exception):
    """The request cap for this run or day has been reached; nothing more is sent."""


class Paused(BudgetExhausted):
    """Background fetching is paused (the chart's pause button): nothing more is sent this run."""
