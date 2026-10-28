"""The fourth session: US stocks overnight, 20:00-04:00 ET on Blue Ocean ATS (the user, 2026-10-27),
in the calendar, from its own feed (Alpaca's `feed=boats`, within Alpaca's free limits), and on
the chart with all four sessions on ("all")."""
from __future__ import annotations

import json
from datetime import date, datetime, time

import pandas as pd
import pytest

from massive_collate import collector
from massive_collate.config import load_settings
from massive_collate.coverage import FeedProfile
from massive_collate.errors import NotEntitled
from massive_collate.gui.bars import BarEngine, MERGED, calendar_key, in_session, session_hours
from massive_collate.gui.markets import symbol_sessions
from massive_collate.sessions import ET, MarketCalendar
from massive_collate.sources import build_source, feed_profiles, providers, rate_window
from massive_collate.sources.alpaca import BoatsSource
from massive_collate.storage import Store

KEYS = {"MASSIVE_API_KEY": "k", "ALPACA_API_KEY_ID": "i", "ALPACA_API_SECRET_KEY": "s"}


def _et(day: str, hm: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(hm), ET)


def test_the_overnight_session_opens_at_20_the_evening_before_a_trading_day():
    cal = MarketCalendar()
    assert cal.overnight_open(date(2026, 10, 27)) == _et("2026-10-26", "20:00")  # Monday night, for Tuesday
    assert cal.overnight_open(date(2026, 10, 26)) == _et("2026-10-25", "20:00")  # Sunday night, for Monday
    assert cal.overnight_open(date(2026, 10, 31)) is None  # Saturday: no session
    assert cal.overnight_open(date(2026, 11, 26)) is None  # Thanksgiving: none the night before either
    week = cal.sessions("stock", date(2026, 10, 26), date(2026, 11, 1), overnight=True)
    assert [(s.open, s.close) for s in week[:2]] == [(_et("2026-10-25", "20:00"), _et("2026-10-26", "20:00")),
                                                      (_et("2026-10-26", "20:00"), _et("2026-10-27", "20:00"))]
    assert week[-1].day == date(2026, 10, 30) and week[-1].open == _et("2026-10-29", "20:00")  # Thursday night: Friday's
    plain = cal.sessions("stock", date(2026, 10, 26), date(2026, 10, 26))
    assert plain[0].open == _et("2026-10-26", "04:00")  # the collector's checks keep to 04:00-20:00
    assert cal.sessions("index", date(2026, 10, 26), date(2026, 10, 26), overnight=True)[0].open == _et("2026-10-26", "09:30")
    minutes = cal.open_minutes("stock", _et("2026-10-26", "19:58"), _et("2026-10-26", "20:03"), overnight=True)
    assert len(minutes) == 5  # through 20:00 into the night


def test_the_chart_annotates_all_four_sessions():
    rows = symbol_sessions(MarketCalendar(), "stock", None, _et("2026-10-27", "00:00"), _et("2026-10-27", "23:00"), "all")
    tue = next(r for r in rows if r["day"] == "2026-10-27")
    ms = lambda d, hm: int(_et(d, hm).timestamp() * 1000)  # noqa: E731
    assert (tue["open"], tue["extendedOpen"], tue["regularOpen"], tue["regularClose"], tue["close"]) == (
        ms("2026-10-26", "20:00"), ms("2026-10-27", "04:00"), ms("2026-10-27", "09:30"), ms("2026-10-27", "16:00"),
        ms("2026-10-27", "20:00"))
    ext = symbol_sessions(MarketCalendar(), "stock", None, _et("2026-10-27", "00:00"), _et("2026-10-27", "23:00"))
    assert "extendedOpen" not in ext[0] and ext[-1]["open"] == ms("2026-10-27", "04:00")


def test_each_session_shows_its_bars():
    idx = pd.DatetimeIndex([_et("2026-10-26", hm) for hm in ("21:00", "03:59", "04:00", "10:00", "19:59", "20:00")])
    frame = pd.DataFrame({"close": range(6)}, index=idx)
    assert len(in_session(frame, "all", "stock")) == 6
    assert [t.strftime("%H:%M") for t in in_session(frame, "extended", "stock").index] == ["04:00", "10:00", "19:59"]
    assert [t.strftime("%H:%M") for t in in_session(frame, "regular", "stock").index] == ["10:00"]
    assert len(in_session(frame, "extended", "crypto")) == 6  # around the clock: drawn whole
    # a night belongs to the next day: Monday 21:00 is in Tuesday's daily bar
    assert session_hours(type("S", (), {"asset": "stock"})()) == 4
    days = calendar_key(pd.DatetimeIndex([_et("2026-10-26", "21:00"), _et("2026-10-26", "19:00")]), "day", session_hours=4)
    assert [d.date() for d in days] == [date(2026, 10, 27), date(2026, 10, 26)]


def test_a_merged_chart_takes_blue_ocean_overnight_and_never_for_its_days(tmp_path):
    with Store(tmp_path / "m.db") as s:
        day = int(_et("2026-10-26", "00:00").timestamp() * 1000)
        s.upsert_bars("massive", "AAPL", "minute", 1, [
            {"t": int(_et("2026-10-26", "10:00").timestamp() * 1000), "o": 2, "h": 2, "l": 2, "c": 2, "v": 9}])
        s.upsert_bars("massive", "AAPL", "day", 1, [{"t": day, "o": 2, "h": 2, "l": 2, "c": 2, "v": 9}])
        s.upsert_bars("boats", "AAPL", "minute", 1, [
            {"t": int(_et("2026-10-26", "21:00").timestamp() * 1000), "o": 3, "h": 3, "l": 3, "c": 3, "v": 4}])
        s.upsert_bars("boats", "AAPL", "day", 1, [  # one venue's nights must never stand in for a day
            {"t": int(_et("2026-10-27", "00:00").timestamp() * 1000), "o": 99, "h": 99, "l": 99, "c": 99, "v": 4}])
        eng = BarEngine(s, priority=["massive"])
        assert eng.sources_for("AAPL") == ["massive", "boats"]  # one venue's feed ranks last
        every = eng.bars(MERGED, "AAPL", "minute", 1, session="all", limit=100)["bars"]
        assert [b["close"] for b in every] == [2, 3]
        assert [b["close"] for b in eng.bars(MERGED, "AAPL", "minute", 1, session="extended", limit=100)["bars"]] == [2]
        # Monday's official day; Tuesday's forming from its overnight minutes (Monday 21:00 is Tuesday's),
        # not Blue Ocean's stored "day"
        days = eng.bars(MERGED, "AAPL", "day", 1, session="all", limit=100)["bars"]
        assert [(b["close"], b.get("derived")) for b in days] == [(2, False), (3, True)]
        assert [b["close"] for b in eng.bars(MERGED, "AAPL", "day", 1, session="extended", limit=100)["bars"]] == [2]


def test_blue_ocean_is_a_feed_of_its_own_on_alpacas_keys_and_limits():
    s = load_settings(env=KEYS, env_file=None)
    p = providers()["boats"]
    assert p.order == 15 and p.key_names == providers()["alpaca"].key_names
    assert rate_window("boats", s) == rate_window("alpaca", s)  # one account: one window, kept under Alpaca's name
    assert s.for_source("boats").max_requests_per_day == s.for_source("alpaca").max_requests_per_day
    profiles = feed_profiles(s)
    assert profiles["boats"].assets == {"stock"} and profiles["boats"].venues == {"stock": "boats"}
    assert "boats" not in feed_profiles(load_settings(env={}, env_file=None)) or \
        feed_profiles(load_settings(env={}, env_file=None))["boats"].assets == frozenset()  # no keys: nothing
    source = build_source("boats", s)
    assert isinstance(source, BoatsSource) and source.name == "boats"
    assert source.resolutions("stock") == ("minute",) and source.native_frames("stock") == ()
    assert source.serves("stock") and not source.serves("crypto")
    params = source._bar_params("AAPL", date(2026, 10, 26), date(2026, 10, 27), "minute", 1, False, "stock", 10)
    assert params["feed"] == "boats" and params["timeframe"] == "1Min"
    with pytest.raises(NotEntitled):
        next(iter(source.bar_pages("BTCUSD", date(2026, 10, 26), date(2026, 10, 27), "minute", asset="crypto")))


def test_only_feeds_with_daily_bars_are_asked_for_them():
    s = load_settings(env=KEYS, env_file=None)
    profiles = feed_profiles(s)
    assert not collector.gives_days(build_source("boats", s), profiles["boats"])
    assert collector.gives_days(build_source("alpaca", s), profiles["alpaca"])
    fake = type("Fake", (), {"name": "fake"})()
    assert collector.gives_days(fake, FeedProfile("fake", profiles["alpaca"].tier, frozenset({"stock"})))


def test_tiingo_scans_its_plan_and_reports_the_blue_ocean_add_on(store):
    from massive_collate import cli, sourcing
    from massive_collate.sources.plugins import tiingo

    class Client:
        def __init__(self):
            self.paths = []

        def get(self, path, params):
            self.paths.append(path)
            if path.startswith("/boats/"):
                raise NotEntitled(403, "You do not have the BOATS entitlement ($9/month add-on)")
            if path == "/tiingo/crypto/prices":
                return [{"ticker": "btcusd", "priceData": [{"close": 1}, {"close": 2}]}]
            if path == "/tiingo/news":
                return [{"id": 1, "title": "Headline"}]
            return [{"close": 1}] * 3

    source = type("Source", (), {"client": Client()})()
    found = tiingo.scan(source, today=date(2026, 10, 27)).results
    assert [ok for _, ok, _ in found] == [True, True, False, True, True, True, True]
    assert "BOATS entitlement" in found[2][2] and found[4][2] == "2 bars for 2026-10-26"
    assert source.client.paths[2] == "/boats/AAPL/prices"

    cli._scan("tiingo", source, store, lambda *_: None)
    kept = store.kv_json(sourcing.SCAN_KEY.format("tiingo"))
    assert [r[1] for r in kept["results"]][:-1] == [True, True, False, True, True, True, True]  # then its limits
    calls = len(source.client.paths)
    cli._scan("tiingo", source, store, lambda *_: None, due_days=7)  # scanned a moment ago: not again
    assert len(source.client.paths) == calls
    s = load_settings(env={**KEYS, "TIINGO_API_KEY": "t"}, env_file=None)
    report = sourcing.reports(feed_profiles(s), date(2026, 10, 27), s, {"tiingo": kept})["tiingo"]
    lines = "\n".join(report.lines())
    assert "no  stock overnight bars from Blue Ocean (20:00-04:00)" in lines and report.as_dict()["scanned"][2]["available"] is False


def test_a_stock_opened_in_the_chart_goes_on_the_overnight_feed(tmp_path):
    from massive_collate.gui.server import ChartApp
    from massive_collate.symbols import resolve

    with Store(tmp_path / "m.db") as store:
        store.watch_spec("massive", resolve("AAPL::XNAS"))
        app = ChartApp(store, settings=load_settings(env=KEYS, env_file=None))
        profiles = feed_profiles(app.settings)
        assert app._watch_everywhere(store.watchlist(ticker="AAPL")[0], profiles)
        assert {w["source"] for w in store.watchlist(ticker="AAPL")} >= {"massive", "alpaca", "boats"}
        assert not app._watch_everywhere(store.watchlist(ticker="AAPL")[0], profiles)  # nothing more to add
        store.watch_spec("coinbase", resolve("BTCUSD"))
        assert not app._watch_everywhere(store.watchlist(ticker="BTCUSD")[0], profiles)  # stocks only


def test_a_sync_scans_a_feed_weekly_and_only_with_something_to_collect(store):
    from massive_collate import cli, sourcing

    asked = []
    source = type("Source", (), {})()
    plugin = providers()["alpaca"]
    original = plugin.scan
    object.__setattr__(plugin, "scan", lambda src: asked.append(1) or [("x", True, "")])
    try:
        cli._scan("alpaca", source, store, lambda *_: None, due_days=7)
        assert asked == [] and store.kv_get(sourcing.SCAN_KEY.format("alpaca")) is None  # nothing watched: no request
        store.watch("alpaca", ["AAPL"], "minute")
        cli._scan("alpaca", source, store, lambda *_: None, due_days=7)
        cli._scan("alpaca", source, store, lambda *_: None, due_days=7)  # a week has not passed
        assert asked == [1]
    finally:
        object.__setattr__(plugin, "scan", original)
