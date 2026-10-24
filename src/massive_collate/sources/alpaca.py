"""Alpaca (alpaca.markets) market data source: US stocks and crypto.

What it adds next to Massive on the free plans (Alpaca Basic, Oct 2026):
- stocks: every US exchange (SIP) back to 2016, minute bars and tick-level trades, up to
  15 minutes ago (Massive free: 2 years, end of day, no ticks);
- crypto: minute bars from 2021 with no key at all, from a real exchange's book (Kraken, `us-1`)
  rather than a consolidation.

Alpaca has no seconds, indices, currencies or futures; those stay with Massive.

Stocks need keys (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY, environment only). Without them a
stock request is refused here, before anything is sent, so it costs nothing.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from ..errors import NotEntitled
from ..models import Exchange, Instrument, Stock, country, exchange_mic
from ..ratelimit import RateLimiter
from .base import DataSource
from .massive import MassiveClient

DATA_URL = "https://data.alpaca.markets"
TRADING_URLS = {True: "https://paper-api.alpaca.markets", False: "https://api.alpaca.markets"}
PAGE_LIMIT = 10000  # Alpaca's maximum per page (across all symbols asked for)
TIMEFRAMES = {"minute": "1Min", "hour": "1Hour", "day": "1Day", "week": "1Week", "month": "1Month"}
# The free plan may not ask for the latest 15 minutes of SIP data; a little margin.
RECENT_SIP_BLOCK = timedelta(minutes=16)
ET = ZoneInfo("America/New_York")

# Alpaca's (CTA/UTP) exchange letters -> ISO MIC and type. Trades carry the letter; it is stored
# as its character code so trades join the exchanges table like Massive's numeric ids.
EXCHANGE_CODES: dict[str, tuple[str | None, str]] = {
    "A": ("XASE", "exchange"), "B": ("XBOS", "exchange"), "C": ("XCIS", "exchange"),
    "D": ("FINR", "TRF"), "E": (None, "SIP"), "H": ("EPRL", "exchange"), "I": ("XISX", "exchange"),
    "J": ("EDGA", "exchange"), "K": ("EDGX", "exchange"), "L": ("LTSE", "exchange"),
    "M": ("XCHI", "exchange"), "N": ("XNYS", "exchange"), "P": ("ARCX", "exchange"),
    "Q": ("XNAS", "exchange"), "S": (None, "SIP"), "T": ("XNAS", "exchange"), "U": ("MEMX", "exchange"),
    "V": ("IEXG", "exchange"), "W": ("XCBO", "exchange"), "X": ("XPHL", "exchange"),
    "Y": ("BATY", "exchange"), "Z": ("BATS", "exchange"),
}
TAPES = {"A": 1, "B": 2, "C": 3}
DROPPED_UPDATES = {"canceled", "incorrect"}  # trades later withdrawn by the exchange


class AlpacaClient(MassiveClient):
    """Same caps, rate limiting and retries as Massive's client; Alpaca's key headers and
    page tokens. Links are only followed to Alpaca's own hosts."""

    def __init__(self, key_id: str | None, secret_key: str | None, trading_url: str = TRADING_URLS[True],
                 base_url: str = DATA_URL, rate_limiter: RateLimiter | None = None,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 request_budget: int | None = None):
        super().__init__("", base_url=base_url, rate_limiter=rate_limiter, session=session, sleep=sleep,
                         request_budget=request_budget)
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key} if key_id and secret_key else {}
        self.has_keys = bool(self._headers)
        self.trading_url = trading_url.rstrip("/")
        self._hosts = {self._host, urlparse(self.trading_url).netloc}

    def _request(self, path_or_url: str, params: dict[str, Any] | None) -> requests.Response:
        if path_or_url.startswith("http") and urlparse(path_or_url).netloc in self._hosts:
            host, self._host = self._host, urlparse(path_or_url).netloc
            try:
                return super()._request(path_or_url, params)
            finally:
                self._host = host
        return super()._request(path_or_url, params)

    def pages(self, path: str, params: dict[str, Any], key: str) -> Iterator[dict[str, list[dict]]]:
        """Each page's `key` object ({symbol: [rows]}), following next_page_token."""
        params = dict(params)
        while True:
            data = self.get(path, params)
            yield data.get(key) or {}
            token = data.get("next_page_token")
            if not token:
                return
            params["page_token"] = token


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def _ns(ts: str) -> int:
    """RFC 3339 with up to nanosecond digits -> ns since epoch, exactly (no float rounding)."""
    whole, _, frac = ts.rstrip("Z").partition(".")
    seconds = int(datetime.fromisoformat(whole + "+00:00").timestamp())
    return seconds * 10**9 + int((frac + "000000000")[:9])


def normalize_bar(b: dict) -> dict:
    return {"t": _ms(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v"),
            "vw": b.get("vw"), "n": b.get("n")}


def normalize_trade(t: dict) -> dict | None:
    """Alpaca trade -> the common trade shape, or None for a withdrawn trade. Alpaca has no
    sequence number; the exchange's trade id (unique per exchange) and the exchange make one.
    Crypto trades have no exchange, conditions or tape; their taker side (who crossed the spread)
    is kept as the condition `taker=B` / `taker=S`."""
    if t.get("u") in DROPPED_UPDATES:
        return None
    trade_id = int(t.get("i") or 0)
    if "x" not in t and "tks" in t:
        return {"sip_timestamp": _ns(t["t"]), "price": t["p"], "size": t["s"], "exchange": None,
                "conditions": [f"taker={t['tks'].upper()}"] if t.get("tks") else [], "id": str(trade_id),
                "sequence_number": trade_id & ((1 << 62) - 1), "tape": None}
    x = t.get("x") or "?"
    return {"sip_timestamp": _ns(t["t"]), "price": t["p"], "size": t["s"], "exchange": ord(x[0]),
            "conditions": [c for c in t.get("c") or [] if c.strip()], "id": str(trade_id),
            "sequence_number": ((trade_id & ((1 << 56) - 1)) << 6) | (ord(x[0]) & 63),
            "tape": TAPES.get(t.get("z") or "")}


class AlpacaSource(DataSource):
    name = "alpaca"

    def __init__(self, client: AlpacaClient, feed: str = "sip", crypto_loc: str = "us-1",
                 realtime: bool = False, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.feed = feed
        self.crypto_loc = crypto_loc
        self.realtime = realtime  # Algo Trader Plus: no 15-minute block
        self._now = now

    @property
    def requests_made(self) -> int:
        return self.client.requests_made

    def serves(self, asset: str) -> bool:
        return asset == "crypto" or (asset == "stock" and self.client.has_keys)

    def native_frames(self, asset: str) -> tuple[tuple[str, int], ...]:
        """Alpaca's own 5-minute and hourly bars (stocks with keys, crypto without)."""
        return (("minute", 5), ("hour", 1)) if asset in ("stock", "crypto") else ()

    def resolutions(self, asset: str) -> tuple[str, ...]:
        """Finest first: ticks (kept for a recent window, see TICK_HISTORY_DAYS), with minute bars
        for the history before it. Alpaca has no second bars."""
        if asset in ("stock", "crypto"):
            return ("trades", "minute")
        return ()

    # -- requests --------------------------------------------------------------

    def _need_keys(self, asset: str) -> None:
        if asset == "stock" and not self.client.has_keys:
            raise NotEntitled(401, "Alpaca keys not set (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY); nothing sent")
        if asset not in ("stock", "crypto"):
            raise NotEntitled(0, f"Alpaca has no {asset} data")

    def _window(self, start: date, end: date, asset: str) -> tuple[str, str] | None:
        """Whole days start..end (US/Eastern for stocks; UTC for crypto, whose day bars are UTC
        days), clipped to what the plan may ask for."""
        tz = ET if asset == "stock" else timezone.utc
        lo = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time(), tz)
        hi = end if isinstance(end, datetime) else datetime.combine(end + timedelta(days=1), datetime.min.time(), tz)
        if asset == "stock" and not self.realtime:
            hi = min(hi, self._now() - RECENT_SIP_BLOCK)
        hi = min(hi, self._now())
        return (_rfc3339(lo), _rfc3339(hi)) if lo < hi else None

    def _path(self, asset: str, kind: str) -> str:
        return f"/v2/stocks/{kind}" if asset == "stock" else f"/v1beta3/crypto/{self.crypto_loc}/{kind}"

    def _symbol(self, ticker: str, asset: str) -> str:
        """Our ticker (or Massive's X:BTCUSD) -> Alpaca's (BTC/USD for crypto)."""
        t = ticker.upper().removeprefix("X:")
        if asset != "crypto" or "/" in t:
            return t
        for quote in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
            if t.endswith(quote) and len(t) > len(quote):
                return f"{t[:-len(quote)]}/{quote}"
        return t

    def _bar_params(self, ticker, start, end, timespan, multiplier, descending, asset, limit):
        if timespan not in TIMEFRAMES or (timespan != "minute" and multiplier != 1):
            raise ValueError(f"Alpaca timespan must be one of {tuple(TIMEFRAMES)}")
        window = self._window(start, end, asset)
        if window is None:
            return None
        frame = f"{multiplier}Min" if timespan == "minute" else TIMEFRAMES[timespan]
        params = {"symbols": self._symbol(ticker, asset), "timeframe": frame, "start": window[0], "end": window[1],
                  "limit": limit, "sort": "desc" if descending else "asc"}
        if asset == "stock":
            # split-adjusted like Massive's adjusted=true, so the feeds agree; `asof` follows renames
            params |= {"feed": self.feed, "adjustment": "split"}
        return params

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock",
                  limit=PAGE_LIMIT) -> Iterator[list[dict]]:
        self._need_keys(asset)
        params = self._bar_params(ticker, start, end, timespan, multiplier, descending, asset, limit)
        if params is None:
            return
        symbol = params["symbols"]
        for page in self.client.pages(self._path(asset, "bars"), params, "bars"):
            yield [normalize_bar(b) for b in page.get(symbol, [])]
            if limit != PAGE_LIMIT:
                return  # probes cost exactly one request

    def trade_pages(self, ticker, gte_ns=None, lt_ns=None, descending=False, limit=PAGE_LIMIT) -> Iterator[list[dict]]:
        """Ticks in [gte, lt). A crypto pair (X:BTCUSD, BTC/USD) is read from the crypto venue and
        needs no key; stocks need keys and stop 15 minutes ago on the free plan."""
        crypto = ticker.upper().startswith("X:") or "/" in ticker
        asset = "crypto" if crypto else "stock"
        self._need_keys(asset)
        blocked = timedelta(0) if self.realtime or crypto else RECENT_SIP_BLOCK
        now = self._now() - blocked
        start = datetime.fromtimestamp(gte_ns / 1e9, timezone.utc) if gte_ns is not None else now - timedelta(days=1)
        end = datetime.fromtimestamp(lt_ns / 1e9, timezone.utc) if lt_ns is not None else now
        end = min(end, now)
        if start >= end:
            return
        symbol = self._symbol(ticker, asset)
        params = {"symbols": symbol, "start": _rfc3339(start), "end": _rfc3339(end), "limit": limit,
                  "sort": "desc" if descending else "asc"}
        if not crypto:
            params["feed"] = self.feed
        for page in self.client.pages(self._path(asset, "trades"), params, "trades"):
            rows = [normalize_trade(t) for t in page.get(symbol, [])]
            yield [r for r in rows if r and (lt_ns is None or r["sip_timestamp"] < lt_ns)]
            if limit != PAGE_LIMIT:
                return

    def is_entitled(self, resolution: str, ticker: str, asset: str = "stock") -> bool:
        """One request with limit=1 over the last week; resolutions Alpaca does not offer
        (seconds) are answered without a request."""
        if resolution not in ("trades", *TIMEFRAMES) or asset not in ("stock", "crypto"):
            return False
        if asset == "stock" and not self.client.has_keys:
            return False
        end = self._now().date()
        try:
            if resolution == "trades":
                fetch = self._symbol(ticker, asset) if asset == "crypto" else ticker
                next(iter(self.trade_pages(fetch, gte_ns=int((self._now() - timedelta(days=7)).timestamp() * 1e9),
                                           limit=1)), None)
            else:
                next(iter(self.bar_pages(ticker, end - timedelta(days=7), end, resolution, asset=asset, limit=1)), None)
        except NotEntitled:
            return False
        return True

    def latest_bar(self, ticker: str, since: date, asset: str = "stock", timespan: str = "minute") -> dict | None:
        page = next(iter(self.bar_pages(ticker, since, self._now().date(), timespan, descending=True,
                                        asset=asset, limit=1)), [])
        return page[0] if page else None

    # -- reference ---------------------------------------------------------------

    def exchanges(self) -> list[Exchange]:
        self._need_keys("stock")
        names = self.client.get("/v2/stocks/meta/exchanges")
        return [Exchange(source=self.name, id=ord(code[0]), name=name, country=country("US"),
                         mic=EXCHANGE_CODES.get(code, (None, None))[0], acronym=code,
                         type=EXCHANGE_CODES.get(code, (None, "exchange"))[1])
                for code, name in sorted(names.items()) if code]

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        """The trading API's asset record: name, primary exchange, whether it is active. Alpaca
        publishes no listing date or FIGI; its `asof` mapping follows ticker renames instead.
        A crypto pair is described without a request."""
        if ticker.upper().startswith("X:") or "/" in ticker:
            pair = self._symbol(ticker, "crypto")
            venue = {"us-1": "Kraken (US)", "eu-1": "Kraken (EU)", "us": "Alpaca"}.get(self.crypto_loc, self.crypto_loc)
            return Instrument(ticker=pair, name=f"{pair} on {venue}", details={"pair": pair, "venue": venue})
        self._need_keys("stock")
        r = self.client.get(f"{self.client.trading_url}/v2/assets/{ticker.upper()}")
        exch = r.get("exchange")
        primary = Exchange(self.name, -1, exch, country("US"), mic=exchange_mic(exch)) if exch else None
        return Stock(ticker=r.get("symbol", ticker.upper()), name=r.get("name"), primary_exchange=primary,
                     active=r.get("status") == "active", details={**r, "source": "alpaca assets"})


__all__ = ["AlpacaClient", "AlpacaSource"]
