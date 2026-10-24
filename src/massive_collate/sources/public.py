"""Public historical sources, used as closed feeds.

Each fills the history nobody else holds for a symbol and is then closed for it: never asked again
(no updates, scans, probes or audits), unless reopened with `feeds reopen`. They are free to use
for personal research; each keeps to its published limits and identifies itself.

- Bitstamp (bitstamp.net): crypto minute bars, no key; BTC/USD from August 2011. Minutes without
  trades come back as flat zero-volume bars and are dropped, as the other feeds have no bar then.
  An exchange of its own, so a live feed (its data is not a copy of another exchange's).
- Coinbase (coinbase.com) and Bitfinex (bitfinex.com): crypto minute and day bars, no key; live
  exchange feeds for the composite and reference models (Coinbase is the reference for crypto).
- Cboe (cboe.com): daily OHLC of Cboe's indices (VIX from 1990 ...), one file per index, no key.
- Tiingo (tiingo.com): US stock daily bars from 1962 with a free account key (TIINGO_API_KEY).
  Raw prices are adjusted here for splits only, from Tiingo's split factors, so they continue the
  other feeds' split-adjusted bars without a dividend seam.

Not used: Stooq (scripts now meet a browser check), Yahoo Finance and Nasdaq.com (their terms bar
automated access), Binance (not for US users). Alpha Vantage gives free weekly/monthly history
but unadjusted; Coinbase's minute bars start in 2015, after Bitstamp's.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

import requests

from ..errors import NotEntitled, SourceError
from ..models import Instrument, Stock
from ..ratelimit import RateLimiter
from .base import DataSource
from .massive import MassiveClient

ET = ZoneInfo("America/New_York")
USER_AGENT = "massive-collate/1 (personal research; historical archive)"


class PublicClient(MassiveClient):
    """Caps, rate limiting and retries as for the other feeds; no auth unless a header is given."""

    def __init__(self, base_url: str, headers: dict | None = None, rate_limiter: RateLimiter | None = None,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 request_budget: int | None = None):
        super().__init__("", base_url=base_url, rate_limiter=rate_limiter, session=session, sleep=sleep,
                         request_budget=request_budget)
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}

    def text(self, path_or_url: str, params: dict[str, Any] | None = None) -> str:
        return self._request(path_or_url, params).text


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _bounds(start, end, tz=timezone.utc) -> tuple[int, int]:
    """[start, end) in ms from dates (whole days, end inclusive) or datetimes."""
    lo = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time(), tz)
    hi = end if isinstance(end, datetime) else datetime.combine(end + timedelta(days=1), datetime.min.time(), tz)
    return _ms(lo), _ms(hi)


class _Archive(DataSource):
    """Shared by the public sources: no ticks, no venues, reference from the other feeds."""

    archival = True  # a closed feed: fills history once per symbol, then is not asked again
    assets: tuple[str, ...] = ()

    def __init__(self, client: PublicClient):
        self.client = client

    @property
    def requests_made(self) -> int:
        return self.client.requests_made

    def covers(self, ticker: str) -> bool:
        return True

    def serves(self, asset: str) -> bool:
        return asset in self.assets

    def is_entitled(self, resolution: str, ticker: str, asset: str = "stock") -> bool:
        return asset in self.assets and resolution in self.resolutions(asset)  # public: no plan to probe

    def trade_pages(self, ticker, gte_ns=None, lt_ns=None, descending=False):
        raise NotEntitled(0, f"{self.name} has no ticks")

    def exchanges(self):
        return []

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        raise NotImplementedError(f"{self.name} has no reference data")


# -- Bitstamp -----------------------------------------------------------------------------------

BITSTAMP_PAIRS = {"BTCUSD", "BTCEUR", "ETHUSD", "ETHEUR", "ETHBTC", "LTCUSD", "XRPUSD", "BCHUSD", "LINKUSD",
                  "SOLUSD", "ADAUSD", "DOGEUSD", "AVAXUSD", "DOTUSD", "XLMUSD", "UNIUSD", "USDCUSD", "USDTUSD"}
BITSTAMP_STEPS = {("minute", 1): 60, ("minute", 5): 300, ("hour", 1): 3600, ("day", 1): 86400}
EXCHANGE_FRAMES = (("minute", 5), ("hour", 1))  # the crypto exchanges' own candles above a minute


class BitstampSource(_Archive):
    name = "bitstamp"
    assets = ("crypto",)
    archival = False  # an exchange: its own market, still trading
    PAGE = 1000  # bars per request (the API's maximum)

    def resolutions(self, asset: str) -> tuple[str, ...]:
        return ("minute",) if asset == "crypto" else ()

    def native_frames(self, asset: str) -> tuple[tuple[str, int], ...]:
        return EXCHANGE_FRAMES if asset == "crypto" else ()

    def covers(self, ticker: str) -> bool:
        return ticker.upper().removeprefix("X:") in BITSTAMP_PAIRS

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="crypto",
                  limit: int | None = None) -> Iterator[list[dict]]:
        if (timespan, multiplier) not in BITSTAMP_STEPS:
            raise ValueError(f"Bitstamp steps: {tuple(BITSTAMP_STEPS)}")
        pair = ticker.upper().removeprefix("X:").lower()
        step = BITSTAMP_STEPS[(timespan, multiplier)]
        a, b = _bounds(start, end)
        a_s, b_s = -(-a // 1000), b // 1000
        cursor = b_s - step if descending else a_s
        while (cursor >= a_s) if descending else (cursor < b_s):
            params = {"step": step, "limit": limit or self.PAGE}
            params.update({"end": cursor} if descending else {"start": cursor})
            rows = (self.client.get(f"/ohlc/{pair}/", params).get("data") or {}).get("ohlc") or []
            stamps = [int(r["timestamp"]) for r in rows]
            bars = [{"t": ts * 1000, "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                     "c": float(r["close"]), "v": float(r["volume"]), "vw": None, "n": None}
                    for ts, r in zip(stamps, rows) if a_s <= ts < b_s and float(r["volume"]) > 0]
            yield sorted(bars, key=lambda x: -x["t"]) if descending else bars
            if not stamps or limit:
                return
            nxt = min(stamps) - step if descending else max(stamps) + step
            if (descending and nxt >= cursor) or (not descending and nxt <= cursor):
                return  # no progress: the start of the pair's history
            cursor = nxt

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        pair = ticker.upper().removeprefix("X:")
        return Instrument(ticker=pair, name=f"{pair[:-3]}/{pair[-3:]} on Bitstamp", details={"venue": "Bitstamp"})


# -- Coinbase and Bitfinex ------------------------------------------------------------------------

CRYPTO_PAIRS = {"BTCUSD", "ETHUSD", "LTCUSD", "BCHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "ADAUSD", "AVAXUSD",
                "LINKUSD", "DOTUSD", "XLMUSD", "UNIUSD", "BTCEUR", "ETHEUR", "ETHBTC"}


def _pair(ticker: str) -> tuple[str, str]:
    t = ticker.upper().removeprefix("X:")
    for quote in ("USD", "EUR", "BTC"):
        if t.endswith(quote) and len(t) > len(quote):
            return t[:-len(quote)], quote
    return t, ""


class CoinbaseSource(_Archive):
    """Coinbase Exchange public candles: 300 per request, newest first (time, low, high, open, close,
    volume). Minutes without trades have no candle."""

    name = "coinbase"
    assets = ("crypto",)
    archival = False
    PAGE = 300
    STEPS = {("minute", 1): 60, ("minute", 5): 300, ("minute", 15): 900, ("hour", 1): 3600, ("hour", 6): 21600,
             ("day", 1): 86400}

    def resolutions(self, asset: str) -> tuple[str, ...]:
        return ("minute",) if asset == "crypto" else ()

    def native_frames(self, asset: str) -> tuple[tuple[str, int], ...]:
        return EXCHANGE_FRAMES if asset == "crypto" else ()

    def covers(self, ticker: str) -> bool:
        return ticker.upper().removeprefix("X:") in CRYPTO_PAIRS

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="crypto",
                  limit: int | None = None) -> Iterator[list[dict]]:
        if (timespan, multiplier) not in self.STEPS:
            raise ValueError(f"Coinbase granularities: {tuple(self.STEPS)}")
        base, quote = _pair(ticker)
        step = self.STEPS[(timespan, multiplier)]
        a, b = _bounds(start, end)
        a_s, b_s = -(-a // 1000) // step * step, b // 1000
        windows = []
        t = a_s
        while t < b_s:
            windows.append((t, min(b_s, t + self.PAGE * step)))
            t += self.PAGE * step
        for lo, hi in (reversed(windows) if descending else windows):
            iso = lambda s: datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
            rows = self.client.get(f"/products/{base}-{quote}/candles",
                                   {"granularity": step, "start": iso(lo), "end": iso(hi - step)})
            bars = [{"t": int(r[0]) * 1000, "o": float(r[3]), "h": float(r[2]), "l": float(r[1]), "c": float(r[4]),
                     "v": float(r[5]), "vw": None, "n": None} for r in rows or [] if lo <= int(r[0]) < hi]
            bars.sort(key=lambda x: -x["t"] if descending else x["t"])
            yield bars[:limit] if limit else bars
            if limit:
                return

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        base, quote = _pair(ticker)
        return Instrument(ticker=base + quote, name=f"{base}/{quote} on Coinbase", details={"venue": "Coinbase"})


class BitfinexSource(_Archive):
    """Bitfinex public candles: up to 10,000 per request ([ms, open, close, high, low, volume]).
    Prices carry 5 significant digits (whole dollars for Bitcoin)."""

    name = "bitfinex"
    assets = ("crypto",)
    archival = False
    PAGE = 10000
    FRAMES = {("minute", 1): ("1m", 60_000), ("minute", 5): ("5m", 300_000), ("hour", 1): ("1h", 3_600_000),
              ("day", 1): ("1D", 86_400_000)}

    def resolutions(self, asset: str) -> tuple[str, ...]:
        return ("minute",) if asset == "crypto" else ()

    def native_frames(self, asset: str) -> tuple[tuple[str, int], ...]:
        return EXCHANGE_FRAMES if asset == "crypto" else ()

    def covers(self, ticker: str) -> bool:
        return ticker.upper().removeprefix("X:") in CRYPTO_PAIRS

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="crypto",
                  limit: int | None = None) -> Iterator[list[dict]]:
        if (timespan, multiplier) not in self.FRAMES:
            raise ValueError(f"Bitfinex frames: {tuple(self.FRAMES)}")
        base, quote = _pair(ticker)
        frame, step = self.FRAMES[(timespan, multiplier)]
        a, b = _bounds(start, end)
        hi = b - 1
        lo = a
        while lo <= hi:
            params = {"start": lo, "end": hi, "limit": limit or self.PAGE, "sort": -1 if descending else 1}
            rows = self.client.get(f"/candles/trade:{frame}:t{base}{quote}/hist", params) or []
            bars = [{"t": int(r[0]), "o": float(r[1]), "c": float(r[2]), "h": float(r[3]), "l": float(r[4]),
                     "v": float(r[5]), "vw": None, "n": None} for r in rows if a <= int(r[0]) < b]
            yield bars
            if limit or len(rows) < (limit or self.PAGE):
                return
            stamps = [int(r[0]) for r in rows]
            if descending:
                hi = min(stamps) - 1
            else:
                lo = max(stamps) + 1

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        base, quote = _pair(ticker)
        return Instrument(ticker=base + quote, name=f"{base}/{quote} on Bitfinex", details={"venue": "Bitfinex"})


# -- Cboe ---------------------------------------------------------------------------------------

CBOE_INDICES = {"VIX", "VIX9D", "VIX3M", "VIX6M", "VIX1Y", "VVIX", "SKEW", "VXN", "VXD", "RVX", "OVX", "GVZ",
                "VXEEM", "VXSLV", "VXAZN", "VXAPL", "VXGOG", "VXGS", "VXIBM", "BXM", "PUT"}


class CboeSource(_Archive):
    """Cboe's daily history files: one request returns an index's whole history."""

    name = "cboe"
    assets = ("index",)

    def __init__(self, client: PublicClient):
        super().__init__(client)
        self._files: dict[str, list[dict]] = {}

    def resolutions(self, asset: str) -> tuple[str, ...]:
        return ("day",) if asset == "index" else ()

    def covers(self, ticker: str) -> bool:
        return ticker.upper().removeprefix("I:") in CBOE_INDICES

    def _history(self, code: str) -> list[dict]:
        if code not in self._files:
            text = self.client.text(f"/{code}_History.csv")
            bars = []
            for row in csv.DictReader(io.StringIO(text)):
                try:
                    d = datetime.strptime(row["DATE"], "%m/%d/%Y")
                    o, h, l, c = (float(row[k]) for k in ("OPEN", "HIGH", "LOW", "CLOSE"))
                except (KeyError, ValueError):
                    continue
                # day bars are stamped at midnight US/Eastern of the session date, like the other feeds'
                bars.append({"t": _ms(datetime(d.year, d.month, d.day, tzinfo=ET)), "o": o, "h": h, "l": l, "c": c,
                             "v": None, "vw": None, "n": None})
            self._files[code] = bars
        return self._files[code]

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="index",
                  limit: int | None = None) -> Iterator[list[dict]]:
        if timespan != "day":
            raise NotEntitled(0, "Cboe publishes daily history only")
        code = ticker.upper().removeprefix("I:")
        if code not in CBOE_INDICES:
            raise NotEntitled(0, f"Cboe has no history file for {code}")
        a, b = _bounds(start, end, ET)
        bars = [x for x in self._history(code) if a <= x["t"] < b]
        bars.sort(key=lambda x: -x["t"] if descending else x["t"])
        yield bars[:limit] if limit else bars

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        code = ticker.upper().removeprefix("I:")
        return Instrument(ticker=code, name=f"Cboe {code} index", details={"source": "Cboe daily history"})


# -- Tiingo -------------------------------------------------------------------------------------

def split_adjust(rows: list[dict]) -> list[dict]:
    """Tiingo's raw daily rows (oldest first, with splitFactor on split days) as split-adjusted bars:
    prices divided, volume multiplied, by the product of every later split. Dividends are not
    adjusted (the other feeds' bars are split-adjusted only)."""
    out = []
    factor = 1.0
    for r in reversed(rows):
        d = datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date()
        p = lambda k: (float(r[k]) / factor) if r.get(k) is not None else None  # noqa: E731
        out.append({"t": _ms(datetime(d.year, d.month, d.day, tzinfo=ET)), "o": p("open"), "h": p("high"),
                    "l": p("low"), "c": p("close"), "v": float(r["volume"]) * factor if r.get("volume") is not None else None,
                    "vw": None, "n": None})
        split = float(r.get("splitFactor") or 1.0)
        if split and split != 1.0:
            factor *= split  # this day's split applies to every earlier day
    return out[::-1]


class TiingoSource(_Archive):
    """US stock daily history with a free Tiingo key. One request returns a symbol's whole history
    (needed anyway: a split today changes the adjustment of every earlier day)."""

    name = "tiingo"
    assets = ("stock",)

    def __init__(self, client: PublicClient, has_key: bool):
        super().__init__(client)
        self.has_key = has_key
        self._history: dict[str, list[dict]] = {}

    def serves(self, asset: str) -> bool:
        return asset == "stock" and self.has_key

    def resolutions(self, asset: str) -> tuple[str, ...]:
        return ("day",) if asset == "stock" and self.has_key else ()

    def _all(self, ticker: str) -> list[dict]:
        sym = ticker.upper().replace(".", "-")
        if sym not in self._history:
            rows = self.client.get(f"/tiingo/daily/{sym}/prices", {"startDate": "1950-01-01", "format": "json",
                                                                   "resampleFreq": "daily"})
            if not isinstance(rows, list):
                raise SourceError(0, f"unexpected Tiingo answer for {sym}")
            self._history[sym] = split_adjust(rows)
        return self._history[sym]

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock",
                  limit: int | None = None) -> Iterator[list[dict]]:
        if not self.has_key:
            raise NotEntitled(401, "TIINGO_API_KEY not set; nothing sent")
        if timespan != "day":
            raise NotEntitled(0, "Tiingo serves daily bars here")
        a, b = _bounds(start, end, ET)
        bars = [x for x in self._all(ticker) if a <= x["t"] < b]
        bars.sort(key=lambda x: -x["t"] if descending else x["t"])
        yield bars[:limit] if limit else bars


__all__ = ["PublicClient", "BitstampSource", "CoinbaseSource", "BitfinexSource", "CboeSource", "TiingoSource",
           "split_adjust", "BITSTAMP_PAIRS", "CRYPTO_PAIRS", "CBOE_INDICES"]
