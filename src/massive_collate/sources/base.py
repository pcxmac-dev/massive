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

    def future_contracts(self, product: str, start: date, end: date) -> list[dict]:
        """Outright (non-spread) contracts of a futures product expiring between start and
        end: dicts with ticker, first_trade_date, last_trade_date. Optional."""
        raise NotImplementedError(f"{self.name} has no futures")

    def market_daily(self, day: date) -> list[dict]:
        """Daily bars for the whole market on one date (dicts also carry T = ticker).

        Optional: sources without a whole-market endpoint leave this unimplemented.
        """
        raise NotImplementedError(f"{self.name} has no whole-market daily endpoint")
