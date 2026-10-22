"""The interface every market-data source implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterator

from ..models import Exchange, Instrument

# Finest first. A source tries each in turn and keeps the first the plan allows.
RESOLUTIONS = ("trades", "second", "minute")


class DataSource(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def requests_made(self) -> int:
        """API requests sent so far by this instance."""

    @abstractmethod
    def is_entitled(self, resolution: str, ticker: str) -> bool:
        """Probe (with as few requests as possible) whether the plan allows a resolution."""

    @abstractmethod
    def trade_pages(
        self, ticker: str, gte_ns: int | None = None, lt_ns: int | None = None, descending: bool = False
    ) -> Iterator[list[dict]]:
        """Tick-level trades. Dicts have sip_timestamp (ns), price, size, exchange, conditions,
        sequence_number, id, tape."""

    @abstractmethod
    def bar_pages(
        self, ticker: str, start: date, end: date, timespan: str, multiplier: int = 1,
        descending: bool = False,
    ) -> Iterator[list[dict]]:
        """OHLCV bars. Dicts have t (ms bar start), o, h, l, c, v, vw, n."""

    @abstractmethod
    def exchanges(self) -> list[Exchange]:
        """Every exchange this source reports trades for."""

    @abstractmethod
    def instrument(self, ticker: str) -> Instrument:
        """Reference details for one ticker."""

    def market_daily(self, day: date) -> list[dict]:
        """Daily bars for the whole market on one date (dicts also carry T = ticker).

        Optional: sources without a whole-market endpoint leave this unimplemented.
        """
        raise NotImplementedError(f"{self.name} has no whole-market daily endpoint")
