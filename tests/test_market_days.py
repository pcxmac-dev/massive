"""The watched stocks' daily bars from the whole-market daily file (collector.market_days): one request a
trading day for every US stock, instead of one or more per stock (the user, 2026-10-28: "do any of the
subscriptions allow you to do bulk symbol daily's"; "without blowing through a whole bunch of subscription
calls/resource"; "Yes, switch it"; "every stock on the heat map needs to be updated daily")."""
from __future__ import annotations

import inspect
from datetime import date, datetime

import pytest

from massive_collate import collector
from massive_collate.collector import ET, MARKET_DAYS_BACK, fetch_market_moves, market_days, sync_bars
from massive_collate.config import TIERS
from massive_collate.coverage import FeedProfile, day_start_ms
from massive_collate.errors import NotEntitled
from massive_collate.storage import Store

NOW = datetime(2026, 10, 28, 21, 0, tzinfo=ET)  # a Wednesday, the session over
# the ten trading days back from it (no market holiday among them)
DAYS = [date(2026, 10, d) for d in (28, 27, 26, 23, 22, 21, 20, 19, 16, 15)]


def _feeds():
    adv = TIERS["advanced"]
    return {"massive": FeedProfile("massive", adv, frozenset({"stock"}), 0, venues={"stock": "sip"}),
            "alpaca": FeedProfile("alpaca", adv, frozenset({"stock"}), 1, venues={"stock": "sip"}),
            "iex": FeedProfile("iex", adv, frozenset({"stock"}), 2, venues={"stock": "iex"})}


class Src:
    """The whole-market file: every listed ticker's day, stamped as the file stamps it (not midnight)."""
    name = "massive"

    def __init__(self, missing=(), refuse=None):
        self.calls, self.bar_calls, self.missing, self.refuse = [], [], set(missing), refuse

    def market_daily(self, day):
        self.calls.append(day)
        if self.refuse and day in self.refuse:
            raise self.refuse[day]
        if day in self.missing:
            return []
        close = int(datetime(day.year, day.month, day.day, 16, tzinfo=ET).timestamp() * 1000)
        return [{"T": t, "o": p, "h": p + 1, "l": p - 1, "c": p + 0.5, "v": 1000.0, "vw": p, "n": 10, "t": close}
                for t, p in (("AAA", 10.0 + day.day), ("BBB", 20.0), ("CCC", 30.0), ("DDD", 40.0), ("EEE", 50.0),
                             ("ZZZ", 60.0), ("SPY", 500.0 + day.day))]

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
        self.bar_calls.append((ticker, timespan))
        yield []


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        s.watch("massive", ["AAA", "BBB", "CCC", "DDD", "EEE"], "minute", asset="stock")
        checked = date(2026, 10, 1)
        s.set_listing("massive", "AAA", None, None, checked)               # listed before the plan's history
        s.set_listing("massive", "BBB", date(2026, 10, 21), None, checked)  # a new listing: from the 21st
        s.set_listing("massive", "DDD", None, None, checked)
        s.set_listing("massive", "EEE", None, None, checked)
        s.set_retired("DDD", date(2026, 10, 20))  # retired: nothing after the 20th
        # CCC's listing never checked: left to its own pass, which checks it first
        s.add_coverage("alpaca", "EEE", "day", day_start_ms(date(2026, 10, 27)), day_start_ms(date(2026, 10, 28)))
        yield s


def _days(store, ticker):
    df = store.load_bars([ticker], "day", 1, source="massive")
    return [ts.date() for ts in df["time"].dt.tz_convert(ET)] if len(df) else []


def test_one_request_a_day_keeps_every_watched_stock_current(store):
    src = Src()
    used = market_days(src, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                       log=lambda *_: None)
    assert used == MARKET_DAYS_BACK == len(src.calls) and src.calls == DAYS  # newest first, one each
    assert sorted(_days(store, "AAA")) == sorted(DAYS)
    df = store.load_bars(["AAA"], "day", 1, source="massive")
    last = df.sort_values("time").iloc[-1]
    assert (last["open"], last["high"], last["low"], last["close"]) == (38.0, 39.0, 37.0, 38.5)  # the 28th's
    # stamped at the day's start, as a stock's own daily bars are (the file stamps the close)
    assert all(ts.hour == 0 and ts.minute == 0 for ts in df["time"].dt.tz_convert(ET))
    # asked again: nothing (every day asked once)
    assert market_days(src, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                       log=lambda *_: None) == 0


def test_only_watched_stocks_within_their_listing_and_days_no_feed_holds(store):
    market_days(Src(), store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                log=lambda *_: None)
    assert min(_days(store, "BBB")) == date(2026, 10, 21)  # from its listing date: before it, another company's
    assert _days(store, "CCC") == []  # listing not checked yet: its own pass checks it first
    assert max(_days(store, "DDD")) == date(2026, 10, 20)  # nothing after its last listed day
    assert date(2026, 10, 27) not in _days(store, "EEE") and date(2026, 10, 28) in _days(store, "EEE")  # Alpaca's day
    assert _days(store, "ZZZ") == []  # not watched: not kept


def test_the_stocks_own_pass_then_asks_nothing_for_those_days(store):
    feeds = _feeds()
    market_days(Src(), store, feeds["massive"], feeds, store.watchlist("massive"), NOW, ["SPY"], log=lambda *_: None)
    src = Src()
    sync_bars(src, store, "AAA", "day", DAYS[-1], NOW, me=feeds["massive"], feeds=feeds, log=lambda *_: None)
    assert src.bar_calls == []  # held in the ledger: no request
    sync_bars(src, store, "CCC", "day", DAYS[-1], NOW, me=feeds["massive"], feeds=feeds, log=lambda *_: None)
    assert src.bar_calls == [("CCC", "day")]  # one the file did not fill still asks


def test_the_market_moves_read_the_same_file(store):
    src = Src()
    market_days(src, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                log=lambda *_: None)
    src.calls.clear()
    assert fetch_market_moves(src, store, NOW, ["SPY"], log=lambda *_: None) == 0 and src.calls == []
    assert store.proxy_days("massive")[-1] == date(2026, 10, 28)


def test_a_day_not_out_yet_is_asked_again_and_a_session_under_way_is_not_asked(store):
    src = Src(missing={date(2026, 10, 28)})
    market_days(src, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                log=lambda *_: None)
    assert date(2026, 10, 28) not in store.grouped_days("massive")  # nothing in it yet: asked next run
    src = Src()
    assert market_days(src, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                       log=lambda *_: None) == 1 and src.calls == [date(2026, 10, 28)]
    midday = Src()
    market_days(midday, store, _feeds()["massive"], _feeds(), store.watchlist("massive"),
                datetime(2026, 10, 29, 12, 0, tzinfo=ET), ["SPY"], log=lambda *_: None)
    assert date(2026, 10, 29) not in midday.calls  # the session under way: its day is not final


def test_refusals(store):
    early = Src(refuse={date(2026, 10, 28): NotEntitled(403, "Attempted to request today's data before end of day.")})
    market_days(early, store, _feeds()["massive"], _feeds(), store.watchlist("massive"), NOW, ["SPY"],
                log=lambda *_: None)
    assert date(2026, 10, 28) not in store.grouped_days("massive") and len(early.calls) == MARKET_DAYS_BACK
    refused = Src(refuse={date(2026, 10, 27): NotEntitled(403, "Your plan doesn't include this data timeframe.")})
    with Store(":memory:") as s:
        s.watch("massive", ["AAA"], "minute", asset="stock")
        s.set_listing("massive", "AAA", None, None, date(2026, 10, 1))
        market_days(refused, s, _feeds()["massive"], _feeds(), s.watchlist("massive"), NOW, ["SPY"],
                    log=lambda *_: None)
        assert refused.calls == [date(2026, 10, 28), date(2026, 10, 27)]  # refused: not further back
        assert s.grouped_days("massive")[date(2026, 10, 27)] == "denied"


def test_the_run_takes_the_file_before_the_market_moves():
    body = inspect.getsource(collector.sync)
    assert body.index("market_days(source, store, me, feeds, store.watchlist(source.name), now, PROXIES, log)") \
        < body.index("fetch_market_moves(source, store, now, PROXIES, log)")
    assert "if source.serves(\"stock\") and gives_days(source, me):" in body
