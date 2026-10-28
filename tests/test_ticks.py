"""Ticks: the consolidated tape's bar rules, the tick window, bars built here from ticks, and no
bar downloads where ticks are (or will be) held."""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from massive_collate import collector
from massive_collate.collector import ET, build_from_ticks, sync
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


def test_every_stocks_minutes_come_first_then_the_one_second_windows_in_a_second_pass(store):
    for t in ("SPCX", "AAPL"):
        store.watch_spec("alpaca", resolve(t), "auto")
        store.set_listing("alpaca", t, None, None, date(2020, 1, 2), t, "stock")
    store.kv_set("watchlist:order", '{"AAPL": 0, "SPCX": 1}')  # most traded first
    src = Ticky(entitled=("trades", "second", "minute"), trades=_ticks(datetime(2026, 10, 21, 9, 30, tzinfo=ET), 150),
                bars={"minute": [], "second": [], "day": []})
    feeds = {"alpaca": FeedProfile("alpaca", ALPACA, frozenset({"stock"}), 0, second_days=30)}
    sync(src, store, ALPACA, NOW, log=_log, feeds=feeds)  # pass 1: minutes for both, no seconds yet
    assert {w["resolution"] for w in store.watchlist("alpaca")} == {"second"} and src.trade_calls == []
    first = [(t, ts, s) for t, s, _, ts, _ in src.bar_calls if ts in ("minute", "second")]
    assert {ts for _, ts, _ in first} == {"minute"}
    assert min(s for _, _, s in first) < datetime(2021, 1, 1, tzinfo=ET)  # back to the listing
    assert all(w["seconds_from"] is None for w in store.watchlist("alpaca"))
    src.bar_calls = []
    sync(src, store, ALPACA, NOW, log=_log, feeds=feeds)  # pass 2: every minute history is in
    second = [(t, s) for t, s, _, ts, _ in src.bar_calls if ts == "second"]
    assert [t for t, _ in second] == ["AAPL", "SPCX"]  # most traded first
    assert min(s for _, s in second) >= datetime(2026, 9, 21, tzinfo=ET)  # 30 days of seconds
    assert {w["seconds_from"] for w in store.watchlist("alpaca")} == {date(2026, 9, 21)}


def test_a_symbol_opened_in_the_chart_gets_its_seconds_in_the_minutes_pass(store):
    for t in ("SPCX", "AAPL"):
        store.watch_spec("alpaca", resolve(t), "auto")
        store.set_listing("alpaca", t, None, None, date(2020, 1, 2), t, "stock")
    collector.request_priority(store, "SPCX", NOW)
    src = Ticky(entitled=("second", "minute"), bars={"minute": [], "second": [], "day": []})
    feeds = {"alpaca": FeedProfile("alpaca", ALPACA, frozenset({"stock"}), 0, second_days=30)}
    sync(src, store, ALPACA, NOW, log=_log, feeds=feeds)
    assert {t for t, _, _, ts, _ in src.bar_calls if ts == "second"} == {"SPCX"}  # the open symbol only


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
    assert not headers and src.resolutions("crypto") == ("minute",)  # trades for live use only, none for history


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
    page = engine.bars(MERGED, "SPCX", "second", 30, session="extended", limit=15)
    assert len(page["bars"]) == 15 and page["more"] is True and page["fine"]
    older_page = engine.bars(MERGED, "SPCX", "second", 30, session="extended", limit=500,
                             to_ms=page["bars"][0]["timestamp"])
    assert len(older_page["bars"]) == 5 and older_page["more"] is False  # 600 s / 30 = 20 bars in all
    minutes = engine.bars(MERGED, "SPCX", "minute", 1, session="extended", limit=100)
    assert len(minutes["bars"]) == 15


def _reference(store, *rows):
    """Massive's condition reference (id, name, letter, high/low, open/close, volume), as the sync saves it."""
    store.save_conditions("massive", [{"id": i, "name": n, "cta": l, "utp": l, "high_low": hl, "open_close": oc,
                                       "volume": v} for i, n, l, hl, oc, v in rows])


def test_extended_hours_prints_build_extended_hours_bars(store):
    # consolidated rules say Form T sets no high/low/last; bars of extended hours are built from them
    _reference(store, (12, "Form T", "T", False, False, True), (13, "Extended hours, out of sequence", "U", False, False, True))
    rules = store.condition_rules("alpaca")  # the tapes' letters, the overrides folded in
    df = pd.DataFrame({"ts_ns": [0, 10 * S, 20 * S], "price": [50.0, 51.0, 52.0], "size": [10, 10, 10],
                       "conditions": ["T", "T", "U"], "tape": [3, 3, 3]})
    bars, _ = build_bars(df, 60 * S, rules)
    assert bars == [{"t": 0, "o": 50.0, "h": 52.0, "l": 50.0, "c": 51.0, "v": 30.0, "vw": 51.0, "n": 3}]


def test_prior_reference_and_derivatively_priced_prints_make_no_spikes(store):
    # the tape's rules let them set the high and low (as Massive's reference says); a minute's bar does not
    _reference(store, (4, "Bunched Trade", None, True, True, True), (10, "Derivatively Priced", "4", True, False, True),
               (22, "Prior Reference Price", "P", True, False, True))
    df = pd.DataFrame({"ts_ns": [0, 10 * S, 20 * S, 30 * S], "price": [147.0, 153.64, 147.2, 146.9],
                       "size": [100, 150, 100, 100], "conditions": ["@", "@,4,P", "@", "@,4"], "tape": [3, 3, 3, 1]})
    bars, unknown = build_bars(df, 60 * S, store.condition_rules("alpaca"))
    assert unknown == set()
    assert bars[0]["h"] == 147.2 and bars[0]["l"] == 147.0 and bars[0]["c"] == 147.2  # SPCX 2026-10-22, 13:34
    assert bars[0]["v"] == 450.0 and bars[0]["n"] == 4  # they still count in volume
    # Massive marks its trades with the reference's ids: its 4 is a bunched trade, which sets the high
    # (the letter '4' looked up first left it out); its 10 is the derivatively priced print
    massive = store.condition_rules("massive")
    assert massive[("utp", "4")] == (True, True, True) and massive[("utp", "10")] == (False, False, True)
    df = pd.DataFrame({"ts_ns": [0, 10 * S], "price": [10.0, 10.5], "size": [100, 100], "conditions": ["", "4"],
                       "tape": [3, 3]})
    (bar,), _ = build_bars(df, 60 * S, massive)
    assert bar["h"] == 10.5 and bar["c"] == 10.5


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
