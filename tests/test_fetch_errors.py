"""Fetching when things go wrong: one symbol's error, no answer at all, a pause during a wait,
plans that have not released a day yet, venues' own symbol rules,
pairs listed later than the history asked for, delisted symbols, per-month symbol limits."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from massive_collate import collector
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS, load_settings
from massive_collate.errors import BudgetExhausted, NotEntitled, Paused, SourceError
from massive_collate.models import Instrument
from massive_collate.sources import rate_window
from massive_collate.sources.massive import MassiveClient
from massive_collate.sources.public import CoinbaseSource, PublicClient, bitfinex_symbol
from massive_collate.storage import Store

from .conftest import FakeResponse, FakeSession, FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
FREE = TIERS["free"]


def test_one_symbols_http_error_skips_only_that_symbol(store):
    class OneBad(FakeSource):
        def bar_pages(self, ticker, *a, **k):
            if ticker == "BAD":
                self._spend()
                raise SourceError(404, "unknown symbol")
            yield from super().bar_pages(ticker, *a, **k)

    src = OneBad(entitled=("minute",))
    store.watch("fake", ["BAD", "GOOD"], "minute")
    logged = []
    result = sync(src, store, FREE, NOW, log=logged.append)
    assert result.stopped is None  # the run went on
    rows = {w["ticker"]: w for w in store.watchlist("fake")}
    assert rows["GOOD"]["synced_at"] is not None and rows["BAD"]["synced_at"] is None
    assert any("BAD: HTTP 404: unknown symbol; skipped this run" in m for m in logged)
    assert json.loads(store.kv_get("refusals:fake"))["BAD"]["n"] == 1  # three runs like it: left a day


def test_no_answer_is_tried_again_counted_and_then_an_error_of_that_request():
    class Down(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append((url, params, headers))
            raise requests.ConnectionError("network unreachable")

    session = Down([])
    sleeps = []
    client = MassiveClient("k", session=session, request_budget=10, sleep=sleeps.append)
    with pytest.raises(SourceError, match="no answer: ConnectionError"):
        client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
    assert client.requests_made == len(session.calls) == 5 and len(sleeps) == 4  # each try counted against the caps


def test_an_answer_that_is_not_json_is_that_requests_error():
    class Html(FakeResponse):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    client = MassiveClient("k", session=FakeSession([Html(200, {})]))
    with pytest.raises(SourceError, match="not JSON"):
        client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")


def test_no_request_goes_out_after_a_pause_that_came_during_the_limiters_wait():
    session = FakeSession([FakeResponse(body={"results": []})])
    client = MassiveClient("k", session=session)
    paused = [None]

    class Waits:
        def acquire(self):
            paused[0] = "fetching paused by you until 20:00 UTC"  # the user pressed pause meanwhile

    client._limiter = Waits()
    client.should_stop = lambda: paused[0]
    with pytest.raises(Paused):
        client.get("/v2/aggs/ticker/AAPL/range/1/minute/2026-10-01/2026-10-02")
    assert session.calls == [] and client.requests_made == 0


def test_too_many_pages_is_a_source_error_not_a_type_error():
    pages = [FakeResponse(body={"results": [1], "next_url": "https://api.massive.com/next"}) for _ in range(3)]
    client = MassiveClient("k", session=FakeSession(pages))
    with pytest.raises(SourceError, match="more than 2 pages"):
        list(client.pages("/v3/reference/tickers", {}, max_pages=2))


def test_a_day_the_plan_has_not_released_is_not_a_refusal_of_history(store):
    class Evening(FakeSource):
        def market_daily(self, day):
            self._spend()
            if day == NOW.date():
                raise NotEntitled(403, "Attempted to request today's data before end of day")
            return [{"T": "AAPL", "c": 1.0, "v": 1, "t": 0}]

    src = Evening()
    evening = datetime(2026, 10, 21, 21, 0, tzinfo=ET)
    collector.sync_market_daily(src, store, FREE, evening, log=lambda *_: None)
    days = store.grouped_days("fake")
    assert NOW.date() not in days and days.get(date(2026, 10, 20)) == "ok"


def test_bitfinex_symbols_longer_than_three_letters_take_a_colon():
    assert bitfinex_symbol("BTC", "USD") == "tBTCUSD"
    assert bitfinex_symbol("DOGE", "USD") == "tDOGE:USD" and bitfinex_symbol("LINK", "USD") == "tLINK:USD"
    assert bitfinex_symbol("BCH", "USD") == "tBCHN:USD"


def test_rate_windows_keep_inside_each_servers_published_limit():
    settings = load_settings(env={}, env_file=None)
    assert rate_window("coinbase", settings) == (8, 1.0)  # 10 a second per address
    assert rate_window("massive", settings) == (5, 62.0)  # 5 a minute, with a margin for jitter
    assert rate_window("alpaca", settings)[1] == 60.0


def test_coinbase_stops_going_back_after_a_long_run_of_empty_windows(monkeypatch):
    class Empty(PublicClient):
        def __init__(self):
            self.requests_made = 0

        def get(self, path, params=None):
            self.requests_made += 1
            return []

    src = CoinbaseSource(Empty())
    monkeypatch.setattr(CoinbaseSource, "EMPTY_RUN", 5)
    pages = list(src.bar_pages("SOLUSD", date(2015, 1, 26), date(2026, 10, 20), "minute", descending=True))
    assert src.client.requests_made == 5 and all(p == [] for p in pages)  # not ~20,000 empty windows


def test_a_delisted_symbol_is_checked_weekly_not_every_sync(store):
    class Gone(FakeSource):
        def instrument(self, ticker, resolve_exchange=True):
            self._spend()
            self.instrument_calls += 1
            # a record that says who the instrument is (a FIGI): a feed's own status alone retires nothing
            return Instrument(ticker, active=False, figi="BBG-OLD", details={})

    src = Gone(entitled=("minute",))
    store.watch("fake", ["OLD"], "minute")
    for day in range(5):
        sync(src, store, FREE, NOW + timedelta(days=day), log=lambda *_: None, refresh_hours=0)
    assert src.instrument_calls == 1
    assert store.watchlist("fake", "OLD")[0]["retired_on"] == NOW.date()  # kept, not moved forward daily


def test_forgetting_a_feeds_spans_after_its_rows_are_deleted(store):
    store.add_coverage("fake", "SPCX", "minute", 0, 1_000)
    store.add_coverage("fake", "SPCX", "minute", 2_000, 5_000)
    store.add_coverage("other", "SPCX", "minute", 0, 5_000)
    store.drop_coverage("fake", "SPCX", before_ms=3_000)
    assert store.coverage("SPCX", "minute")["fake"] == [(3_000, 5_000)]
    store.drop_coverage("fake", "SPCX")
    assert "fake" not in store.coverage("SPCX", "minute") and store.coverage("SPCX", "minute")["other"]


def test_a_feed_with_a_monthly_symbol_limit_takes_them_in_order(store, monkeypatch):
    class Archive(FakeSource):  # like Tiingo: a symbol's history once, then closed
        archival = True

        def covers(self, ticker):
            return True

    monkeypatch.setitem(collector.SYMBOLS_PER_MONTH, "fake", 2)
    store.watch("fake", ["AAA", "BBB", "CCC"], "minute")
    for t in ("AAA", "BBB", "CCC"):  # an archive fills a stock from its listing date only
        store.set_listing("fake", t, date(2020, 1, 2), None, NOW.date())
    src = Archive(entitled=("minute",))
    logged = []
    sync(src, store, FREE, NOW, log=logged.append)
    assert {call[0] for call in src.bar_calls} == {"AAA", "BBB"}
    assert any("1 symbols wait for next month" in m for m in logged)
    sync(src, store, FREE, NOW + timedelta(days=1), log=lambda *_: None)  # October still: CCC waits
    assert "CCC" not in {call[0] for call in src.bar_calls}
    for done in ("AAA", "BBB"):  # their histories in: closed, never asked again
        store.set_closed("fake", done, NOW + timedelta(days=1))
    sync(src, store, FREE, NOW + timedelta(days=12), log=lambda *_: None)  # November
    assert "CCC" in {call[0] for call in src.bar_calls}
