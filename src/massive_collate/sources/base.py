"""The interface every market-data source implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterator

from ..models import Exchange, Instrument

# Finest first, per asset class. A source tries each in turn and keeps the first the plan allows.
RESOLUTIONS_BY_ASSET = {
    "stock": ("trades", "second", "minute"),
    "index": ("second", "minute"),
    "fx": ("second", "minute"),
    "future": ("second", "minute"),
}
RESOLUTIONS = RESOLUTIONS_BY_ASSET["stock"]


class DataSource(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def requests_made(self) -> int:
        """API requests sent so far by this instance."""

    @abstractmethod
    def is_entitled(self, resolution: str, ticker: str, asset: str = "stock") -> bool:
        """Probe (with as few requests as possible) whether the plan allows a resolution.
        `ticker` is the source's own ticker (e.g. I:COMP, C:EURUSD, NQZ6)."""

    @abstractmethod
    def trade_pages(
        self, ticker: str, gte_ns: int | None = None, lt_ns: int | None = None, descending: bool = False
    ) -> Iterator[list[dict]]:
        """Tick-level trades. Dicts have sip_timestamp (ns), price, size, exchange, conditions,
        sequence_number, id, tape."""

    @abstractmethod
    def bar_pages(
        self, ticker: str, start: date, end: date, timespan: str, multiplier: int = 1,
        descending: bool = False, asset: str = "stock",
    ) -> Iterator[list[dict]]:
        """OHLCV bars in one shape for every asset class: t (ms bar start; for "day" bars
        midnight US/Eastern of the trading/settlement date), o, h, l, c, v, vw, n."""

    @abstractmethod
    def exchanges(self) -> list[Exchange]:
        """Every exchange this source reports trades for."""

    @abstractmethod
    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        """Reference details for the instrument currently using a ticker.

        With resolve_exchange=False the primary exchange is not looked up, so the
        call costs as little as possible (used by the scheduled listing check).
        """

    def latest_bar(self, ticker: str, since: date, asset: str = "stock", timespan: str = "minute") -> dict | None:
        """The newest bar the source has from `since` on (one cheap request), or None.
        Used by `scan` to confirm new data exists before fetching it."""
        raise NotImplementedError(f"{self.name} cannot report its newest bar")

    def market_holidays(self) -> list[dict]:
        """Upcoming exchange closures / early closes: dicts with date, exchange, status, name,
        open_ms and close_ms (early closes)."""
        raise NotImplementedError(f"{self.name} has no market holiday calendar")

    def futures_schedule(self, product: str, since: date) -> list[tuple[str, str, int]]:
        """Official futures session events from `since` on: (session date, event, ms)."""
        raise NotImplementedError(f"{self.name} has no futures schedules")

    def icon(self, url: str) -> tuple[bytes, str]:
        """Download a company icon from the source: (content, content type)."""
        raise NotImplementedError(f"{self.name} has no company icons")

    def market_daily(self, day: date) -> list[dict]:
        """Daily bars for the whole market on one date (dicts also carry T = ticker).

        Optional: sources without a whole-market endpoint leave this unimplemented.
        """
        raise NotImplementedError(f"{self.name} has no whole-market daily endpoint")
