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
def no_saved_keys(monkeypatch):
    """Tests never read the user's real API keys: neither the saved user environment variables nor
    the process's own (a shell started after the keys were saved has them), for every provider
    plugin's keys and plans. A test that wants one sets it."""
    from massive_collate import config
    from massive_collate.sources.registry import variables

    monkeypatch.setattr(config, "saved_user_keys", lambda: {})
    for name in variables():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_agency_requests(monkeypatch):
    """Tests never read the agencies' real feeds: a full `sync` ends with the Gov fetch
    (cli._fetch_gov); test_gov.py drives gov.fetch with fake answers instead."""
    from massive_collate import cli

    monkeypatch.setattr(cli, "_fetch_gov", lambda settings, log, minutes=None: None)


@pytest.fixture(autouse=True)
def no_cached_movers():
    """The big movers are cached per process for a few minutes (collector.big_movers): each test
    starts without another test's."""
    from massive_collate import collector

    collector._movers_cache.clear()


@pytest.fixture(autouse=True)
def tick_access_restored():
    """cli.main sets the process's tick access from BARS_ONLY (off by default): a test that runs a
    command must not leave the stores of the tests after it unable to open tick files."""
    from massive_collate.storage import set_tick_access, tick_access

    was = tick_access()
    yield
    set_tick_access(was)


@pytest.fixture(autouse=True)
def no_recorders(monkeypatch, tmp_path):
    """Tests never start the live recorder: `sync` would start a real detached process (with real
    exchange connections) pointed at the test's temporary folder."""
    monkeypatch.setenv("LIVE_CRYPTO", "off")
    monkeypatch.setenv("LIVE_STOCKS", "off")
    monkeypatch.setenv("MASSIVE_LIVE_LOCK", str(tmp_path / "machine-live.lock"))  # never the real machine's
    monkeypatch.setattr(live, "start_detached", lambda db_path: False)
    # Nor see or end the machine's processes: a test recorder holding its locks reaps "other"
    # recorders (live.reap_strays), and once ended the user's real one.
    from massive_collate import proc

    monkeypatch.setattr(proc, "processes", lambda: [])

    def no_terminate(pid):
        raise AssertionError(f"a test tried to end process {pid}")

    monkeypatch.setattr(proc, "terminate", no_terminate)
    # Nor start one from the chart app (resuming fetching starts a sync, a scan, a build): a test
    # that resumed once started a real sync on the user's database. A test that wants to see what
    # would start replaces Popen on this stand-in.
    import subprocess
    from types import SimpleNamespace

    from massive_collate.gui import server

    def no_spawn(args, **kw):
        raise AssertionError(f"a test tried to start a process: {args}")

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(
        Popen=no_spawn, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE, CREATE_NO_WINDOW=0))
    # Nor from the command line: the hourly sync starts each feed's run in a process of its own
    # (cli.start_feed), and a test's `sync` once started five, which sent Alpaca two requests on the
    # user's keys. A test that wants to see which would start replaces start_feed.
    from massive_collate import cli

    monkeypatch.setattr(cli, "start_feed", lambda name: no_spawn(["--source", name, "sync", "--only"]))


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
