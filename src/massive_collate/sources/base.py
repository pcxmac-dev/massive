"""The interface every market-data source implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Iterator

from ..models import Exchange, Instrument

# Finest first, per asset class. A source tries each in turn and keeps the first the plan allows.
RESOLUTIONS_BY_ASSET = {
    "stock": ("trades", "second", "minute"),
    "index": ("second", "minute"),
    "fx": ("second", "minute"),
    "crypto": ("second", "minute"),
    "future": ("second", "minute"),
}
RESOLUTIONS = RESOLUTIONS_BY_ASSET["stock"]


class DataSource(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def requests_made(self) -> int:
        """API requests sent so far by this instance."""

    def serves(self, asset: str) -> bool:
        """Whether this feed collects an asset class on this machine (e.g. keys present)."""
        return True

    def native_frames(self, asset: str) -> tuple[tuple[str, int], ...]:
        """Time frames above the base resolution that the source sends itself (daily bars aside):
        (timespan, multiplier). Collected so its own candles replace the ones built here."""
        return ()

    def resolutions(self, asset: str) -> tuple[str, ...]:
        """Resolutions to try for an asset class, finest first (the first the plan allows is kept)."""
        return RESOLUTIONS_BY_ASSET.get(asset, RESOLUTIONS)

    @abstractmethod
    def is_entitled(self, resolution: str, ticker: str, asset: str = "stock") -> bool:
        """Probe (with as few requests as possible) whether the plan allows a resolution.
        `ticker` is the source's own ticker (e.g. I:COMP, C:EURUSD, X:BTCUSD, NQZ6)."""

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
        midnight US/Eastern of the trading/settlement date), o, h, l, c, v, vw, n.
        `start`/`end` are whole days (dates, inclusive) or exact times (datetimes)."""

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

    def news(self, since: datetime | None = None, limit: int = 100) -> list[dict]:
        """Recent news, newest first: dicts with id, published_ms, title, publisher, url, tickers."""
        raise NotImplementedError(f"{self.name} has no news")

    def listings(self, exchange: str) -> list[dict]:
        """Every active listing on an exchange (MIC): dicts with ticker, name, exchange, type, figi."""
        raise NotImplementedError(f"{self.name} has no listings directory")

    def futures_products(self, day=None) -> list[dict]:
        """Futures products as of `day` (default today): dicts with ticker (product code), name,
        exchange (venue), type (sector)."""
        raise NotImplementedError(f"{self.name} has no futures products")

    def icon(self, url: str) -> tuple[bytes, str]:
        """Download a company icon from the source: (content, content type)."""
        raise NotImplementedError(f"{self.name} has no company icons")

    def trade_conditions(self) -> list[dict]:
        """SIP trade conditions with their consolidated update rules: dicts with id, name, cta and
        utp (the condition's letter on each tape family), high_low, open_close, volume (bools)."""
        raise NotImplementedError(f"{self.name} has no trade condition reference")

    def market_daily(self, day: date) -> list[dict]:
        """Daily bars for the whole market on one date (dicts also carry T = ticker).

        Optional: sources without a whole-market endpoint leave this unimplemented.
        """
        raise NotImplementedError(f"{self.name} has no whole-market daily endpoint")
