"""Massive (massive.com, formerly Polygon.io) REST API source."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
        # Called with the running total after every request, so usage is recorded even if
        # the run is interrupted (the daily cap counts it).
        self.on_request: Callable[[int], None] | None = None
        # Called with (seconds, HTTP status) before waiting to retry a refused or failed request.
        self.on_retry: Callable[[float, int], None] | None = None

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict:
        return self._request(path_or_url, params).json()

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """A file on the API host (e.g. a company icon): (content, content type). Counts as a request."""
        resp = self._request(url, None)
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

    def _request(self, path_or_url: str, params: dict[str, Any] | None) -> requests.Response:
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
            if self.on_request:
                self.on_request(self.requests_made)
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt == self._max_retries:
                break
            delay = _retry_delay(resp, attempt)
            if self.on_retry:
                self.on_retry(delay, resp.status_code)
            self._sleep(delay)

        if resp.status_code >= 400:
            message = _error_message(resp)
            if resp.status_code in (401, 403):
                raise NotEntitled(resp.status_code, message)
            if resp.status_code == 429:
                raise RateLimited(resp.status_code, message)
            raise SourceError(resp.status_code, message)
        return resp

    def pages(self, path: str, params: dict[str, Any] | None = None, max_pages: int | None = None) -> Iterator[list[dict]]:
        """Yield each page of results, following next_url links. With `max_pages`, a list longer than
        expected stops with an error instead of spending the run's requests on it."""
        data = self.get(path, params)
        yield data.get("results") or []
        n = 1
        while data.get("next_url"):
            if max_pages is not None and n >= max_pages:
                raise SourceError(f"{path}: more than {max_pages} pages; stopped (the list is longer than expected)")
            data = self.get(data["next_url"])
            n += 1
            yield data.get("results") or []


class MassiveSource(DataSource):
    name = "massive"

    def __init__(self, client: MassiveClient):
        self.client = client

    @property
    def requests_made(self) -> int:
        return self.client.requests_made

    def is_entitled(self, resolution: str, ticker: str, asset: str = "stock") -> bool:
        """One request with limit=1 over the last week."""
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=7)
        try:
            if resolution == "trades":
                ns = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()) * 10**9
                next(iter(self._trade_pages(ticker, gte_ns=ns, limit=1)), None)
            elif asset == "future":
                next(iter(self._future_pages(ticker, start, end, resolution, False, limit=1)), None)
            else:
                next(iter(self._bar_pages(ticker, start, end, resolution, 1, False, limit=1)), None)
        except NotEntitled:
            return False
        return True

    PLAN_DEPTHS = (3, 6, 11, 21)  # years back the plan probe asks for daily bars

    def plan_reach(self, ticker: str = "AAPL", today: date | None = None) -> dict[str, bool]:
        """What the key's plan may ask for, one small request each: a week of daily bars at
        PLAN_DEPTHS years back (`day_<n>y`: data returned), then second bars, trades and quotes of
        the last week (`config.tier_for` names the plan)."""
        today = today or datetime.now(timezone.utc).date()
        out: dict[str, bool] = {}
        for years in self.PLAN_DEPTHS:
            start = today - timedelta(days=round(years * 365.25))
            try:
                out[f"day_{years}y"] = bool(next(iter(self._bar_pages(ticker, start, start + timedelta(days=7), "day",
                                                                      1, False, limit=1)), []))
            except NotEntitled:
                out[f"day_{years}y"] = False
        out["second"] = self.is_entitled("second", ticker)
        out["trades"] = self.is_entitled("trades", ticker)
        try:
            self.client.get(f"/v3/quotes/{ticker.upper()}", {"limit": 1})
            out["quotes"] = True
        except NotEntitled:
            out["quotes"] = False
        return out

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

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
        if asset == "future":
            if multiplier != 1:
                raise ValueError("futures bars are fetched at multiplier 1")
            return self._future_pages(ticker, start, end, timespan, descending)
        # Stocks, indices (I:...), currencies (C:...) and crypto (X:...) share the aggregates endpoint.
        return self._bar_pages(ticker, start, end, timespan, multiplier, descending)

    # -- futures ---------------------------------------------------------------

    def _future_pages(self, contract, start, end, timespan, descending, limit=PAGE_LIMIT):
        if timespan not in FUTURE_RESOLUTIONS:
            raise ValueError(f"futures timespan must be one of {tuple(FUTURE_RESOLUTIONS)}")
        params = {
            "resolution": FUTURE_RESOLUTIONS[timespan],
            # Sessions open the evening before they settle: start a day early so `start`'s session is included.
            "window_start.gte": str(start - timedelta(days=1) if timespan == "day" else start),
            "window_start.lt": str(end + timedelta(days=1)),
            "sort": f"window_start.{'desc' if descending else 'asc'}",
            "limit": limit,
        }
        pages = (
            [normalize_future_bar(b, timespan) for b in page]
            for page in self.client.pages(f"/futures/v1/aggs/{contract.upper()}", params)
        )
        return pages if limit == PAGE_LIMIT else _first(pages)


    def _bar_pages(self, ticker, start, end, timespan, multiplier, descending, limit=PAGE_LIMIT):
        if timespan not in TIMESPANS:
            raise ValueError(f"timespan must be one of {TIMESPANS}")
        # dates are whole days; datetimes are sent as ms timestamps (the endpoint takes either)
        start, end = (int(x.timestamp() * 1000) if isinstance(x, datetime) else x for x in (start, end))
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
            details=r,
        )

    def latest_bar(self, ticker: str, since: date, asset: str = "stock", timespan: str = "minute") -> dict | None:
        """The newest bar from `since` to today, or None: one request (limit 1, newest first)."""
        end = datetime.now(timezone.utc).date() + timedelta(days=1)
        if asset == "future":
            page = next(iter(self._future_pages(ticker, since, end, timespan, True, limit=1)), [])
        else:
            page = next(iter(self._bar_pages(ticker, since, end, timespan, 1, True, limit=1)), [])
        return page[0] if page else None

    def market_holidays(self) -> list[dict]:
        data = self.client.get("/v1/marketstatus/upcoming")
        rows = data if isinstance(data, list) else data.get("results") or []
        ms = lambda v: int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000) if v else None  # noqa: E731
        return [{"date": r["date"], "exchange": r.get("exchange", ""), "status": r.get("status", "closed"),
                 "name": r.get("name"), "open_ms": ms(r.get("open")), "close_ms": ms(r.get("close"))}
                for r in rows if r.get("date")]

    def futures_schedule(self, product: str, since: date) -> list[tuple[str, str, int]]:
        out = []
        # Near term only: official schedules matter for holidays and special sessions ahead; the
        # calendar's rules cover everything further out.
        params = {"product_code": product.upper(), "session_end_date.gte": str(since),
                  "session_end_date.lte": str(since + timedelta(days=60)), "limit": 1000,
                  "sort": "session_end_date.asc"}
        for page in self.client.pages("/futures/v1/schedules", params):
            for r in page:
                if r.get("product_code", product).upper() != product.upper():
                    continue
                t = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                out.append((r["session_end_date"], r["event"], int(t.timestamp() * 1000)))
        return out

    def news(self, since: datetime | None = None, limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"order": "desc", "sort": "published_utc", "limit": limit}
        if since:
            params["published_utc.gte"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.client.get("/v2/reference/news", params).get("results") or []
        out = []
        for r in rows:
            try:
                t = datetime.fromisoformat(r["published_utc"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            out.append({"id": r.get("id"), "published_ms": int(t.timestamp() * 1000), "title": r.get("title"),
                        "publisher": (r.get("publisher") or {}).get("name"), "url": r.get("article_url"),
                        "tickers": r.get("tickers") or []})
        return out

    def listings(self, exchange: str) -> list[dict]:
        params = {"market": "stocks", "exchange": exchange.upper(), "active": "true", "limit": 1000, "order": "asc",
                  "sort": "ticker"}
        out = []
        for page in self.client.pages("/v3/reference/tickers", params, max_pages=20):  # ~6 pages for Nasdaq
            out += [{"ticker": r["ticker"], "name": r.get("name"), "exchange": r.get("primary_exchange") or exchange,
                     "type": r.get("type"), "figi": r.get("composite_figi"), "kind": "stock"} for r in page if r.get("ticker")]
        return out

    def futures_products(self, day: date | None = None) -> list[dict]:
        """The futures products as of one day (single contracts, not spreads), in one request. The
        endpoint keeps a dated record of every product for every day since 2017: asked without a
        date it pages through all of them. A day without a record (weekend, holiday, not yet
        published) steps back a day, at most a week."""
        day = day or datetime.now(timezone.utc).date()
        for back in range(8):
            d = day - timedelta(days=back)
            params = {"date": d.isoformat(), "type": "single", "limit": 50000}
            rows = [r for page in self.client.pages("/futures/v1/products", params, max_pages=3) for r in page]
            if not rows:
                continue
            out = {}
            for r in rows:
                code = r.get("product_code")
                if code and code not in out:
                    out[code] = {"ticker": code, "name": r.get("name"), "exchange": r.get("trading_venue"),
                                 "type": r.get("sector") or r.get("asset_class"), "figi": None, "kind": "future"}
            return list(out.values())
        return []

    def icon(self, url: str) -> tuple[bytes, str]:
        return self.client.get_bytes(url)

    def trade_conditions(self) -> list[dict]:
        out = []
        params = {"asset_class": "stocks", "data_type": "trade", "limit": 1000}
        for page in self.client.pages("/v3/reference/conditions", params):
            for r in page:
                sip = r.get("sip_mapping") or {}
                rules = (r.get("update_rules") or {}).get("consolidated") or {}
                if r.get("id") is None:
                    continue
                out.append({"id": int(r["id"]), "name": r.get("name"), "cta": sip.get("CTA"), "utp": sip.get("UTP"),
                            "high_low": rules.get("updates_high_low", True),
                            "open_close": rules.get("updates_open_close", True),
                            "volume": rules.get("updates_volume", True)})
        return out

    def market_daily(self, day: date) -> list[dict]:
        data = self.client.get(f"/v2/aggs/grouped/locale/us/market/stocks/{day}", {"adjusted": "true"})
        return data.get("results") or []


FUTURE_RESOLUTIONS = {"second": "1sec", "minute": "1min", "hour": "1hour", "day": "1session"}
_ET = ZoneInfo("America/New_York")


def normalize_future_bar(b: dict, timespan: str) -> dict:
    """Massive futures bar -> the common bar shape (t ms, o, h, l, c, v, vw, n).

    Session ("day") bars are stamped at midnight US/Eastern of their settlement date,
    like stock daily bars, so the date shown is the trading date.
    """
    if timespan == "day" and b.get("session_end_date"):
        d = date.fromisoformat(b["session_end_date"])
        t = int(datetime(d.year, d.month, d.day, tzinfo=_ET).timestamp() * 1000)
    else:
        t = int(b["window_start"]) // 10**6
    volume = b.get("volume")
    dollar = b.get("dollar_volume")
    return {
        "t": t, "o": b.get("open"), "h": b.get("high"), "l": b.get("low"), "c": b.get("close"),
        "v": volume, "vw": dollar / volume if dollar is not None and volume else None,
        "n": b.get("transactions"),
    }


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
