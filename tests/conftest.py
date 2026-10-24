from __future__ import annotations

from datetime import date
from typing import Iterator

import pytest

from massive_collate import live
from massive_collate.errors import NotEntitled
from massive_collate.models import Exchange, Instrument, country
from massive_collate.sources.base import DataSource
from massive_collate.storage import Store


@pytest.fixture(autouse=True)
def no_recorders(monkeypatch):
    """Tests never start the live recorder: `sync` would start a real detached process (with real
    exchange connections) pointed at the test's temporary folder."""
    monkeypatch.setenv("LIVE_CRYPTO", "off")
    monkeypatch.setattr(live, "start_detached", lambda db_path: False)


class FakeResponse:
    def __init__(self, status: int = 200, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Returns queued responses and records every request."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


class FakeSource(DataSource):
    """In-memory source: entitlements and data are configured per test."""

    name = "fake"

    def __init__(self, entitled=("trades", "second", "minute"), trades=None, bars=None, budget=None,
                 listing: Instrument | None = None):
        self.entitled = set(entitled)
        self.listing = listing
        self.instrument_calls = 0
        self.trades = sorted(trades or [], key=lambda t: t["sip_timestamp"])
        self.bars = bars or {}
        self.budget = budget
        self._requests = 0
        self.trade_calls: list[dict] = []

    def _spend(self):
        from massive_collate.errors import BudgetExhausted

        if self.budget is not None and self._requests >= self.budget:
            raise BudgetExhausted("fake cap")
        self._requests += 1

    @property
    def requests_made(self) -> int:
        return self._requests

    def is_entitled(self, resolution, ticker, asset="stock"):
        self._spend()
        self.probed = getattr(self, "probed", []) + [(resolution, ticker, asset)]
        return resolution in self.entitled

    def trade_pages(self, ticker, gte_ns=None, lt_ns=None, descending=False, page_size=2) -> Iterator[list[dict]]:
        if "trades" not in self.entitled:
            self._spend()
            raise NotEntitled(403, "no trades")
        self.trade_calls.append({"gte": gte_ns, "lt": lt_ns, "desc": descending})
        rows = [
            t for t in self.trades
            if (gte_ns is None or t["sip_timestamp"] >= gte_ns) and (lt_ns is None or t["sip_timestamp"] < lt_ns)
        ]
        if descending:
            rows = rows[::-1]
        for i in range(0, max(len(rows), 1), page_size):
            self._spend()
            yield rows[i:i + page_size]

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
        self._spend()
        self.bar_calls = getattr(self, "bar_calls", []) + [(ticker, start, end, timespan, asset)]
        if timespan not in self.entitled and timespan not in ("day", "hour"):
            raise NotEntitled(403, "no " + timespan)
        key = timespan if multiplier == 1 else f"{timespan}:{multiplier}"
        src = self.bars.get((ticker, key), self.bars.get(key, []))
        yield list(src)

    frames: tuple = ()  # the time frames above a minute it sends (native_frames)

    def native_frames(self, asset):
        return self.frames

    def exchanges(self):
        self._spend()
        return [Exchange("fake", 4, "Nasdaq", country("US"), mic="XNAS"), Exchange("fake", 12, "NYSE Arca", country("US"))]

    def instrument(self, ticker, resolve_exchange=True):
        self._spend()
        self.instrument_calls += 1
        return self.listing or Instrument(ticker)

    def market_daily(self, day: date):
        self._spend()
        return []


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
