"""Massive (massive.com, formerly Polygon.io) REST API source."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import requests

from ..errors import BudgetExhausted, NotEntitled, RateLimited, SourceError
from ..models import Exchange, Instrument, country, instrument_class
from ..ratelimit import RateLimiter
from .base import DataSource

TIMESPANS = ("second", "minute", "hour", "day", "week", "month", "quarter", "year")
PAGE_LIMIT = 50000


class MassiveClient:
    """Thin HTTP client: auth, rate limiting, request cap, retries and pagination."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        rate_limiter: RateLimiter | None = None,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        request_budget: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._host = urlparse(self.base_url).netloc
        self._limiter = rate_limiter or RateLimiter(None)
        self._session = session or requests.Session()
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        # Hard cap on requests this client may send, retries included.
        self.request_budget = request_budget
        self.requests_made = 0

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict:
        if path_or_url.startswith("http"):
            # Only follow pagination links on the API host, so the key is never sent elsewhere.
            if urlparse(path_or_url).netloc != self._host:
                raise SourceError(0, f"refusing to follow link to another host: {path_or_url}")
            url = path_or_url
        else:
            url = self.base_url + path_or_url

        for attempt in range(self._max_retries + 1):
            if self.request_budget is not None and self.requests_made >= self.request_budget:
                raise BudgetExhausted(f"request cap of {self.request_budget} reached")
            self._limiter.acquire()
            resp = self._session.get(url, params=params, headers=self._headers, timeout=self._timeout)
            self.requests_made += 1
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt == self._max_retries:
                break
            self._sleep(_retry_delay(resp, attempt))

        if resp.status_code >= 400:
            message = _error_message(resp)
            if resp.status_code in (401, 403):
                raise NotEntitled(resp.status_code, message)
            if resp.status_code == 429:
                raise RateLimited(resp.status_code, message)
            raise SourceError(resp.status_code, message)
        return resp.json()

    def pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[list[dict]]:
        """Yield each page of results, following next_url links."""
        data = self.get(path, params)
        yield data.get("results") or []
        while data.get("next_url"):
            data = self.get(data["next_url"])
            yield data.get("results") or []


class MassiveSource(DataSource):
    name = "massive"

    def __init__(self, client: MassiveClient):
        self.client = client

    @property
    def requests_made(self) -> int:
        return self.client.requests_made

    def is_entitled(self, resolution: str, ticker: str) -> bool:
        """One request with limit=1 over the last week."""
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=7)
        try:
            if resolution == "trades":
                ns = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()) * 10**9
                next(iter(self._trade_pages(ticker, gte_ns=ns, limit=1)), None)
            else:
                next(iter(self._bar_pages(ticker, start, end, resolution, 1, False, limit=1)), None)
        except NotEntitled:
            return False
        return True

    def trade_pages(self, ticker, gte_ns=None, lt_ns=None, descending=False):
        return self._trade_pages(ticker, gte_ns, lt_ns, descending)

    def _trade_pages(self, ticker, gte_ns=None, lt_ns=None, descending=False, limit=PAGE_LIMIT):
        params: dict[str, Any] = {"order": "desc" if descending else "asc", "sort": "timestamp", "limit": limit}
        if gte_ns is not None:
            params["timestamp.gte"] = gte_ns
        if lt_ns is not None:
            params["timestamp.lt"] = lt_ns
        pages = self.client.pages(f"/v3/trades/{ticker.upper()}", params)
        return pages if limit == PAGE_LIMIT else _first(pages)

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False):
        return self._bar_pages(ticker, start, end, timespan, multiplier, descending)

    def _bar_pages(self, ticker, start, end, timespan, multiplier, descending, limit=PAGE_LIMIT):
        if timespan not in TIMESPANS:
            raise ValueError(f"timespan must be one of {TIMESPANS}")
        path = f"/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}/{start}/{end}"
        params = {"adjusted": "true", "sort": "desc" if descending else "asc", "limit": limit}
        pages = self.client.pages(path, params)
        return pages if limit == PAGE_LIMIT else _first(pages)

    def exchanges(self) -> list[Exchange]:
        data = self.client.get("/v3/reference/exchanges", {"asset_class": "stocks"})
        return [
            Exchange(
                source=self.name, id=int(r["id"]), name=r.get("name") or "",
                country=country(r.get("locale")), mic=r.get("mic") or r.get("operating_mic"),
                acronym=r.get("acronym"), type=r.get("type"),
            )
            for r in data.get("results") or [] if r.get("id") is not None
        ]

    def instrument(self, ticker: str, resolve_exchange: bool = True) -> Instrument:
        r = self.client.get(f"/v3/reference/tickers/{ticker.upper()}").get("results") or {}
        mic = r.get("primary_exchange")
        exchange = None
        if mic:
            known = self.exchanges() if resolve_exchange else []
            exchange = next((e for e in known if e.mic == mic), None) or Exchange(
                self.name, -1, mic, country(r.get("locale")), mic=mic
            )
        list_date = r.get("list_date")
        cls = instrument_class(r.get("type"))
        return cls(
            ticker=r.get("ticker", ticker.upper()), name=r.get("name"),
            primary_exchange=exchange, currency=(r.get("currency_name") or "usd").upper(),
            list_date=date.fromisoformat(list_date) if list_date else None,
            figi=r.get("composite_figi"),
            active=bool(r.get("active", True)) and not r.get("delisted_utc"),
        )

    def market_daily(self, day: date) -> list[dict]:
        data = self.client.get(f"/v2/aggs/grouped/locale/us/market/stocks/{day}", {"adjusted": "true"})
        return data.get("results") or []


def _first(pages: Iterator[list[dict]]) -> Iterator[list[dict]]:
    """Only the first page (used by probes, so they cost exactly one request)."""
    yield next(pages)


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return min(60.0, 5.0 * 2**attempt)


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    return body.get("message") or body.get("error") or str(body)[:200]
