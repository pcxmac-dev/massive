"""Domain model: an instrument trades on an exchange, which is in a country.

These are "belongs to" relationships, so they are composed rather than
inherited. Inheritance is used where behaviour genuinely differs: kinds of
instrument here, and data sources in `sources/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar


@dataclass(frozen=True)
class Country:
    code: str  # ISO 3166-1 alpha-2, e.g. "US"
    name: str
    timezone: str  # IANA zone of its main market, e.g. "America/New_York"


COUNTRIES: dict[str, Country] = {
    "US": Country("US", "United States", "America/New_York"),
    "CA": Country("CA", "Canada", "America/Toronto"),
    "GB": Country("GB", "United Kingdom", "Europe/London"),
    "DE": Country("DE", "Germany", "Europe/Berlin"),
    "JP": Country("JP", "Japan", "Asia/Tokyo"),
    "AU": Country("AU", "Australia", "Australia/Sydney"),
}


def country(code: str | None) -> Country | None:
    return COUNTRIES.get(code.upper()) if code else None


# Friendly exchange names -> ISO 10383 MIC
EXCHANGE_ALIASES: dict[str, str] = {
    "NASDAQ": "XNAS", "NYSE": "XNYS", "ARCA": "ARCX", "NYSEARCA": "ARCX",
    "AMEX": "XASE", "NYSEAMERICAN": "XASE", "CBOE": "BATS", "BATS": "BATS", "IEX": "IEXG",
    "TSX": "XTSE", "LSE": "XLON", "XETRA": "XETR", "TSE": "XJPX", "ASX": "XASX",
}


def exchange_mic(name: str) -> str:
    """'NASDAQ' -> 'XNAS'; a MIC is returned unchanged."""
    key = name.strip().upper().replace(" ", "")
    return EXCHANGE_ALIASES.get(key, key)


def parse_symbol(text: str) -> tuple[str, str | None]:
    """'SPCX::NASDAQ' or 'NASDAQ:SPCX' -> ('SPCX', 'XNAS'); 'SPCX' -> ('SPCX', None)."""
    text = text.strip()
    if "::" in text:
        ticker, exch = text.split("::", 1)
    elif ":" in text:
        exch, ticker = text.split(":", 1)
    else:
        return text.upper(), None
    return ticker.strip().upper(), exchange_mic(exch)


@dataclass(frozen=True)
class Exchange:
    source: str
    id: int  # the source's own exchange id (appears on every trade)
    name: str
    country: Country | None
    mic: str | None = None  # ISO 10383 market identifier, e.g. "XNAS"
    acronym: str | None = None
    type: str | None = None  # exchange | TRF | SIP ...

    @property
    def timezone(self) -> str:
        return self.country.timezone if self.country else "UTC"


@dataclass(frozen=True)
class Instrument:
    """Something that trades. Subclasses describe specific kinds."""

    kind: ClassVar[str] = "instrument"
    ticker: str
    name: str | None = None
    primary_exchange: Exchange | None = None
    currency: str = "USD"
    # Symbols get reused: the listing date and FIGI identify *this* instrument.
    list_date: date | None = None
    figi: str | None = None
    active: bool = True
    # The source's reference record as received (description, market cap, address, ...).
    details: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def country(self) -> Country | None:
        return self.primary_exchange.country if self.primary_exchange else None

    def describe(self) -> str:
        where = self.primary_exchange.name if self.primary_exchange else "unknown exchange"
        return f"{self.ticker} ({self.kind}, {self.name or 'unnamed'}) on {where}"


@dataclass(frozen=True)
class Stock(Instrument):
    kind: ClassVar[str] = "stock"


@dataclass(frozen=True)
class ETF(Instrument):
    kind: ClassVar[str] = "etf"
    holdings: tuple[str, ...] = field(default=())


# Source type codes -> Instrument subclass
_KINDS: dict[str, type[Instrument]] = {
    "CS": Stock, "ADRC": Stock, "PFD": Stock,
    "ETF": ETF, "ETN": ETF, "ETV": ETF, "ETS": ETF,
}


def instrument_class(type_code: str | None) -> type[Instrument]:
    return _KINDS.get((type_code or "").upper(), Instrument)
