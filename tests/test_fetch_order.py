"""The order a run takes symbols in (collector.fetch_order): after those opened in the chart, the
user's watchlists, then the last session's big movers (collector.big_movers: the largest moves
from the previous close among stocks that traded enough, from daily bars already held), then the
most traded (the initializer's order)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from massive_collate import collector
from massive_collate.collector import ET, sync
from massive_collate.config import TIERS
from massive_collate.initializer import ORDER_KEY
from massive_collate.live import WATCHLISTS_KEY

from .conftest import FakeSource

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)


def _day(d: date, close: float, volume: float) -> dict:
    t = int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)
    return {"t": t, "o": close, "h": close, "l": close, "c": close, "v": volume}


def _closes(store, ticker, prev, last, volume=1_000_000):
    store.upsert_bars("fake", ticker, "day", 1, [_day(date(2026, 10, 19), prev, volume), _day(date(2026, 10, 20), last, volume)])


def test_big_movers_are_the_largest_moves_among_stocks_that_traded_enough(store):
    _closes(store, "UP", 10, 15)      # +50%, $15M
    _closes(store, "DOWN", 20, 12)    # -40%, $12M
    _closes(store, "CALM", 50, 51)    # +2%
    _closes(store, "TINY", 1, 3, volume=100)  # +200% but $300 traded: not a mover
    store.upsert_bars("fake", "STALE", "day", 1, [_day(date(2026, 10, 15), 5, 1e6), _day(date(2026, 10, 16), 9, 1e6)])
    assert collector.big_movers(store, "fake", ["UP", "DOWN", "CALM", "TINY", "STALE"]) == ["UP", "DOWN", "CALM"]


def test_the_order_is_watchlists_then_movers_then_the_most_traded(store):
    _closes(store, "MOVER", 10, 20)
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["mine"]}]))
    store.kv_set(ORDER_KEY, json.dumps({"BIG": 0, "MOVER": 5, "MINE": 9, "SMALL": 7}))
    order = collector.fetch_order(store, "fake", ["BIG", "MOVER", "MINE", "SMALL"])
    assert sorted(order, key=order.get) == ["MINE", "MOVER", "BIG", "SMALL"]


def test_a_run_backfills_in_that_order(store):
    store.watch("fake", ["BIG", "MOVER", "MINE"], "minute")
    _closes(store, "MOVER", 10, 20)
    store.kv_set(WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["MINE"]}]))
    store.kv_set(ORDER_KEY, json.dumps({"BIG": 0, "MOVER": 1, "MINE": 2}))
    src = FakeSource(entitled=("minute",), bars={"day": [], "minute": []})
    sync(src, store, TIERS["free"], NOW, log=lambda *_: None)
    minutes = [c[0] for c in src.bar_calls if c[3] == "minute"]
    assert minutes[:3] == ["MINE", "MOVER", "BIG"]
