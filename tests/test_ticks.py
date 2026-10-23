"""Ticks: the consolidated tape's bar rules, the tick window, bars built here from ticks, and no
bar downloads where ticks are (or will be) held."""
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from massive_collate import collector
from massive_collate.collector import ET, build_from_ticks, sync, tick_space
from massive_collate.config import ALPACA_TIERS, TIERS
from massive_collate.coverage import FeedProfile, ms, plan
from massive_collate.symbols import resolve
from massive_collate.ticks import build_bars, eligibility

from .conftest import FakeResponse, FakeSession, FakeSource

S = 1_000_000_000
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
ALPACA = ALPACA_TIERS["basic"]
RULES = {("utp", "I"): (False, False, True), ("utp", "W"): (False, False, True), ("cta", "I"): (False, False, True),
         ("utp", "T"): (True, True, True), ("utp", "Z"): (True, False, True)}


class Ticky(FakeSource):
    name = "alpaca"


def _log(_):
    pass


def _ticks(start: datetime, n: int, every_s: float = 1.0, price=100.0, cond="@", tape=3):
    t0 = int(start.timestamp() * S)
    return [{"sip_timestamp": t0 + int(i * every_s * S), "sequence_number": i, "price": price + i * 0.01, "size": 100,
             "exchange": 81, "conditions": [cond], "id": str(i), "tape": tape} for i in range(n)]


def test_rules_follow_the_tape():
    df = pd.DataFrame({"ts_ns": [0, 1, 2, 3], "price": [10, 12, 9, 11], "size": [100, 5, 100, 100],
                       "conditions": ["@", "@,I", "Z", "taker=B"], "tape": [3, 3, 3, None]})
    hl, oc, vol, unknown = eligibility(df, RULES)
    assert list(hl) == [True, False, True, True] and list(oc) == [True, False, False, True] and all(vol)
    assert unknown == set()  # crypto tags are not sale conditions
    bars, _ = build_bars(df, 60 * S, RULES)
    assert bars == [{"t": 0, "o": 10.0, "h": 11.0, "l": 9.0, "c": 11.0, "v": 305.0,
                     "vw": pytest.approx((1000 + 60 + 900 + 1100) / 305), "n": 4}]
    _, unknown = build_bars(df.assign(conditions=["@", "Q", "@", "@"]), 60 * S, RULES)
    assert unknown == {"utp:Q"}  # reported, counted as regular


def test_odd_lot_only_bar_carries_the_previous_close():
    df = pd.DataFrame({"ts_ns": [0, 70 * S], "price": [10.0, 55.0], "size": [100, 3], "conditions": ["@", "I"], "tape": [3, 3]})
    bars, _ = build_bars(df, 60 * S, RULES)
    assert bars[1] == {"t": 60_000, "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 3.0, "vw": 55.0, "n": 1}


def _feeds(tick_days=365):
    return {"massive": FeedProfile("massive", TIERS["free"], frozenset({"stock", "crypto"}), 0),
            "alpaca": FeedProfile("alpaca", ALPACA, frozenset({"stock", "crypto"}), 1, tick_days=tick_days)}


def test_tick_feed_takes_the_window_builds_bars_and_downloads_minutes_only_before_it(store, monkeypatch):
    monkeypatch.setattr(collector, "tick_space", lambda store, gb: "ok")
    store.watch_spec("alpaca", resolve("SPCX"), "trades")
    store.set_listing("alpaca", "SPCX", None, None, NOW.date(), "SpaceX", "stock")
    session = datetime(2026, 10, 21, 9, 30, tzinfo=ET)
    src = Ticky(entitled=("trades", "minute"), trades=_ticks(session, 150), bars={"minute": [], "day": []})
    sync(src, store, ALPACA, NOW, log=_log, feeds=_feeds(tick_days=30))
    # ticks: newest first, from 30 days back to 15 minutes ago
    first = src.trade_calls[0]
    assert first["desc"] and first["lt"] == ms(datetime(2026, 10, 21, 11, 45, tzinfo=ET)) * 1_000_000
    assert min(c["gte"] for c in src.trade_calls) == ms(datetime(2026, 9, 21, tzinfo=ET)) * 1_000_000
    # bars built here: 150 one-second bars, 3 minutes (09:30, 09:31, 09:32 partial-by-data)
    secs = store.load_bars(["SPCX"], "second", source="alpaca")
    mins = store.load_bars(["SPCX"], "minute", source="alpaca")
    assert len(secs) == 150 and list(mins["transactions"]) == [60, 60, 30]
    detail = store.coverage_detail("SPCX", "minute")
    assert any(o == "ticks" for _, o, _, _ in detail)
    # minute bars were downloaded only before the tick window
    minute_calls = [(s, e) for _, s, e, ts, _ in src.bar_calls if ts == "minute"]
    assert minute_calls and all(e <= datetime(2026, 9, 21, tzinfo=ET) for _, e in minute_calls)


def test_minute_feed_leaves_the_tick_window_to_the_tick_feed(store):
    store.watch("alpaca", ["SPCX"], "trades")
    store.watch("massive", ["SPCX"], "minute")
    gaps = plan(store, _feeds()["massive"], _feeds(), "SPCX", "stock", "minute", date(2024, 10, 22), NOW)
    assert gaps == []  # ticks rank first and Alpaca serves minutes from 2016: nothing for Massive


def test_a_minute_cut_by_a_stopped_run_is_built_when_its_ticks_are_complete(store):
    t0 = datetime(2026, 10, 20, 10, 0, tzinfo=ET)
    ticks = _ticks(t0, 120)  # 10:00:00 - 10:01:59
    store.upsert_trades("alpaca", "SPCX", ticks[90:])  # a stopped run got 10:01:30 onwards
    store.add_coverage("alpaca", "SPCX", "trades", ticks[90]["sip_timestamp"] // 1_000_000, ms(t0) + 120_000)
    build_from_ticks(store, "alpaca", "SPCX", ticks[90]["sip_timestamp"] // 1_000_000, ms(t0) + 120_000, {}, _log)
    assert len(store.load_bars(["SPCX"], "minute", source="alpaca")) == 0  # 10:01 is incomplete
    store.upsert_trades("alpaca", "SPCX", ticks[:90])  # the next run fills the rest
    store.add_coverage("alpaca", "SPCX", "trades", ms(t0), ticks[90]["sip_timestamp"] // 1_000_000)
    build_from_ticks(store, "alpaca", "SPCX", ms(t0), ticks[90]["sip_timestamp"] // 1_000_000, {}, _log)
    mins = store.load_bars(["SPCX"], "minute", source="alpaca")
    assert list(mins["transactions"]) == [60, 60]  # both minutes whole, nothing counted twice


def test_disk_guard(store, monkeypatch):
    import shutil

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 100e9))
    assert tick_space(store, 250) == "no-backfill" and store.kv_get("ticks:backfill-paused") == "100 GB free"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 40e9))
    assert tick_space(store, 250) == "stop"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 900e9))
    assert tick_space(store, 250) == "ok" and not store.kv_get("ticks:backfill-paused")


def test_alpaca_crypto_ticks_need_no_key():
    from massive_collate.sources.alpaca import AlpacaClient, AlpacaSource

    session = FakeSession([FakeResponse(200, {"trades": {"BTC/USD": [
        {"i": 7, "p": 81658, "s": 0.0004, "t": "2026-10-22T12:00:00.200261576Z", "tks": "B"}]}, "next_page_token": None})])
    src = AlpacaSource(AlpacaClient(None, None, session=session, sleep=lambda s: None),
                       now=lambda: datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc))
    pages = list(src.trade_pages("X:BTCUSD", gte_ns=1792670400 * S, lt_ns=1792674000 * S, descending=True))
    assert pages[0][0]["conditions"] == ["taker=B"] and pages[0][0]["tape"] is None
    assert pages[0][0]["sip_timestamp"] == 1792670400200261576
    url, params, headers = session.calls[0]
    assert url.endswith("/v1beta3/crypto/us-1/trades") and params["symbols"] == "BTC/USD" and "feed" not in params
    assert not headers and src.resolutions("crypto") == ("trades", "minute")


def test_charts_read_seconds_by_window_on_a_minute_base(store):
    from massive_collate.gui.bars import MERGED, BarEngine

    t0 = datetime(2026, 10, 20, 10, 0, tzinfo=ET)
    ticks = _ticks(t0, 600)  # ten minutes of one tick a second
    store.upsert_trades("alpaca", "SPCX", ticks)
    store.add_coverage("alpaca", "SPCX", "trades", ms(t0), ms(t0) + 600_000)
    build_from_ticks(store, "alpaca", "SPCX", ms(t0), ms(t0) + 600_000, {}, _log)
    older = [{"t": ms(t0) - 86_400_000 + i * 60_000, "o": 99, "h": 99, "l": 99, "c": 99, "v": 1, "n": 1} for i in range(5)]
    store.upsert_bars("massive", "SPCX", "minute", 1, older)
    engine = BarEngine(store, priority=["massive", "alpaca"])
    assert engine.sources_for("SPCX") == ["alpaca", "massive"]  # the feed with ticks first
    s = engine.series(MERGED, "SPCX")
    assert s.kind == "minute" and len(s.frame) == 15 and s.fine_sources == ["alpaca"]
    assert {"type": "second", "span": 1} in engine.periods(s)
    page = engine.bars(MERGED, "SPCX", "second", 5, session="extended", limit=50)
    assert len(page["bars"]) == 50 and page["more"] is True and page["fine"]
    older_page = engine.bars(MERGED, "SPCX", "second", 5, session="extended", limit=500,
                             to_ms=page["bars"][0]["timestamp"])
    assert len(older_page["bars"]) == 70 and older_page["more"] is False  # 600 s / 5 = 120 bars in all
    minutes = engine.bars(MERGED, "SPCX", "minute", 1, session="extended", limit=100)
    assert len(minutes["bars"]) == 15


def test_extended_hours_prints_build_extended_hours_bars():
    # consolidated rules say Form T sets no high/low/last; bars of extended hours are built from them
    rules = {("utp", "T"): (False, False, True), ("utp", "U"): (False, False, True)}
    df = pd.DataFrame({"ts_ns": [0, 10 * S, 20 * S], "price": [50.0, 51.0, 52.0], "size": [10, 10, 10],
                       "conditions": ["T", "T", "U"], "tape": [3, 3, 3]})
    bars, _ = build_bars(df, 60 * S, rules)
    assert bars == [{"t": 0, "o": 50.0, "h": 52.0, "l": 50.0, "c": 51.0, "v": 30.0, "vw": 51.0, "n": 3}]


def test_merged_daily_bars_combine_feeds_day_by_day(store):
    from massive_collate.gui.bars import MERGED, BarEngine

    day = lambda d: ms(datetime(2026, 10, d, tzinfo=ET))  # noqa: E731
    store.upsert_bars("alpaca", "SPCX", "minute", 1, [{"t": day(19) + 36_000_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    store.upsert_trades("alpaca", "SPCX", _ticks(datetime(2026, 10, 19, 10, tzinfo=ET), 3))
    store.upsert_bars("alpaca", "SPCX", "day", 1, [{"t": day(19), "o": 1, "h": 1, "l": 1, "c": 10, "v": 1}])
    store.upsert_bars("massive", "SPCX", "minute", 1, [{"t": day(20) + 36_000_000, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}])
    store.upsert_bars("massive", "SPCX", "day", 1, [{"t": day(20), "o": 2, "h": 2, "l": 2, "c": 20, "v": 1}])
    s = BarEngine(store, priority=["massive", "alpaca"]).series(MERGED, "SPCX")
    assert list(s.daily["close"]) == [10, 20]  # the tick feed ranks first, yet the newest day is kept
