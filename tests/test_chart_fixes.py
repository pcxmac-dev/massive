"""The chart engine and server, from a bug sweep: daily-only feeds, merged charts with them, built
bars marked as built, futures sessions, the repeated autumn hour, UTC periods' ends, limits on
what a request may ask, and a read that must not write."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pandas as pd
import pytest

from massive_collate import live
from massive_collate.gui import charttypes
from massive_collate.gui.bars import BarEngine, bar_end, calendar_key, merge_frames
from massive_collate.gui.server import ChartApp, make_handler
from massive_collate.storage import Store

ET = "America/New_York"


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _bar(t: str, price: float, volume: float = 1.0, tz: str = ET) -> dict:
    return {"t": _ms(t, tz), "o": price, "h": price, "l": price, "c": price, "v": volume, "vw": price, "n": 1}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        yield s


def test_a_feed_of_daily_bars_only_charts_its_days(store):
    days = pd.bdate_range("2026-01-02", periods=100)
    store.upsert_bars("cboe", "VIX", "day", 1, [_bar(f"{d:%Y-%m-%d} 00:00", 15.0 + i % 3) for i, d in enumerate(days)])
    store.watch("cboe", ["VIX"], "day", asset="index")
    eng = BarEngine(store)
    for session in ("regular", "extended"):
        assert len(eng.bars("cboe", "VIX", "day", 1, session)["bars"]) == 97  # 100 weekdays, 3 NYSE holidays
        assert len(eng.bars("cboe", "VIX", "week", 1, session)["bars"]) > 0


def test_a_merged_chart_with_a_daily_only_feed_keeps_intraday_and_that_feeds_history(store):
    store.upsert_bars("tiingo", "KO", "day", 1, [_bar(f"{d:%Y-%m-%d} 00:00", 50.0) for d in pd.bdate_range("2015-01-02", periods=30)])
    store.upsert_bars("massive", "KO", "minute", 1, [_bar("2025-01-02 10:00", 60.0), _bar("2025-01-02 10:01", 61.0)])
    store.upsert_bars("massive", "KO", "day", 1, [_bar("2025-01-02 00:00", 61.0)])
    for feed in ("tiingo", "massive"):
        store.watch(feed, ["KO"], "auto")
    eng = BarEngine(store)
    s = eng.series("*", "KO")
    assert s.kind == "minute" and {"type": "minute", "span": 1} in eng.periods(s)
    days = eng.bars("*", "KO", "day", 1, "regular")["bars"]  # Tiingo's 29 sessions (MLK Day had none) + Massive's
    assert len(days) == 30 and pd.Timestamp(days[0]["timestamp"], unit="ms", tz="UTC").tz_convert(ET).date().isoformat() == "2015-01-02"


def test_bars_built_here_are_marked_derived(store):
    store.upsert_bars("composite", "BTCUSD", "day", 1, [_bar("2026-10-19 00:00", 1.0, tz="UTC")])
    store.upsert_bars("composite", "BTCUSD", "minute", 1, [_bar("2026-10-19 10:00", 1.0, tz="UTC")])
    eng = BarEngine(store)
    (day,) = eng.bars("composite", "BTCUSD", "day", 1, "extended")["bars"]
    assert day["derived"]  # a model's days are not a feed's


def test_futures_days_follow_cme_sessions():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-18 18:00", tz=ET), pd.Timestamp("2026-10-19 16:59", tz=ET)])
    keys = calendar_key(idx, "day", session_hours=6)
    assert keys[0] == keys[1] == pd.Timestamp("2026-10-19", tz=ET)  # one session, dated by the day it ends


def test_merging_ticks_in_the_repeated_autumn_hour_works():
    first = pd.Timestamp("2026-11-01 05:30", tz="UTC").tz_convert(ET)  # 01:30 EDT
    second = pd.Timestamp("2026-11-01 06:30", tz="UTC").tz_convert(ET)  # 01:30 EST, an hour later
    a = pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex([first]))
    b = pd.DataFrame({"close": [2.0]}, index=pd.DatetimeIndex([second]))
    out = merge_frames([a, b], "1min")
    assert len(out) == 2


def test_utc_periods_end_in_utc():
    march = pd.Timestamp("2026-03-01 00:00", tz="UTC").tz_convert(ET)
    assert bar_end(march, "month", 1, utc=True) == pd.Timestamp("2026-04-01 00:00", tz="UTC")
    dst_day = pd.Timestamp("2026-03-08 00:00", tz="UTC").tz_convert(ET)
    assert bar_end(dst_day, "day", 1, utc=True) == pd.Timestamp("2026-03-09 00:00", tz="UTC")


def test_requests_for_impossible_pages_are_refused(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, [_bar("2026-10-20 10:00", 10.0), _bar("2026-10-20 10:01", 10.5)])
    app = ChartApp(store)
    with pytest.raises(ValueError, match="limit"):
        app.bars({"source": "massive", "ticker": "SPCX", "period": "minute", "limit": "-1"})
    closes = pd.DataFrame({"close": [100.0, 200.0, 100.0]})
    with pytest.raises(ValueError, match="too small"):
        charttypes.renko(closes, 0.0001)


def test_the_series_cache_is_bounded(store):
    assert len(BarEngine(store)._series) == 0 and BarEngine(store)._series._size == 48


def test_a_read_does_not_write_unless_the_app_itself_asks(store):
    app = ChartApp(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        def get(headers):
            c = HTTPConnection("127.0.0.1", port)
            c.request("GET", "/api/activity?source=massive&open=AAPL", headers={"Host": f"127.0.0.1:{port}", **headers})
            return c.getresponse().status

        assert get({}) == 200 and store.kv_get(live.OPEN_KEY) is None  # any web page can send a plain GET
        assert get({"X-Massive-Charts": "1"}) == 200 and "AAPL" in json.loads(store.kv_get(live.OPEN_KEY))
    finally:
        httpd.shutdown()


def test_a_feed_holding_a_symbol_it_does_not_watch_charts_it_as_the_symbols_class(store):
    sat = [_bar(f"2026-10-24 {h:02d}:00", 1.0, tz="UTC") for h in range(24)]  # a Saturday: crypto trades
    store.upsert_built_bars("bitstamp", "BTCUSD", "minute", 1, sat, 1)  # its live trades, never watched there
    store.watch("coinbase", ["BTCUSD"], "minute", asset="crypto")
    eng = BarEngine(store)
    assert eng.series("bitstamp", "BTCUSD").asset == "crypto"
    assert len(eng.bars("bitstamp", "BTCUSD", "minute", 1, "regular")["bars"]) == 24  # not a stock's 9:30-16:00


def test_the_symbol_list_shows_the_state_of_the_feed_holding_the_data():
    from massive_collate.gui.server import _symbol_state

    rows = {"massive": {"state": "backfilling"}, "alpaca": {"state": "current"}, "tiingo": {"state": "blocked"}}
    assert _symbol_state(rows, ["massive"])["state"] == "backfilling"  # not whichever feed came last
    assert _symbol_state(rows, [])["state"] == "backfilling"  # none holds data: the most pressing that collects it
    assert _symbol_state({"tiingo": {"state": "blocked"}}, [])["state"] == "blocked"
    assert _symbol_state({}, []) is None


def test_a_zoomed_out_one_second_chart_pages_back_through_every_day(store):
    days = ("2026-10-20", "2026-10-21", "2026-10-22")
    secs, mins = [], []
    for d in days:
        t0 = _ms(f"{d} 10:00")
        secs += [{"t": t0 + i * 1000, "o": 10.0, "h": 10.0 + i % 7, "l": 9.0, "c": 10.0, "v": 1.0, "vw": 10.0, "n": 1}
                 for i in range(320)]
        mins += [{"t": t0 + i * 60_000, "o": 10.0, "h": 16.0, "l": 9.0, "c": 10.0, "v": 60.0, "vw": 10.0, "n": 60}
                 for i in range(6)]
        store.add_coverage("massive", "SPCX", "second", t0, t0 + 320_000)
    store.upsert_bars("massive", "SPCX", "second", 1, secs)
    store.upsert_bars("massive", "SPCX", "minute", 1, mins)
    store.watch("massive", ["SPCX"], "minute")
    eng = BarEngine(store)
    got, to = [], None
    for _ in range(20):
        page = eng.bars("massive", "SPCX", "second", 1, "extended", to_ms=to, limit=10, lod=16)
        got = page["bars"] + got
        if not page["more"]:
            break
        to = page["bars"][0]["timestamp"]
    stamps = [b["timestamp"] for b in got]
    assert stamps == sorted(set(stamps))  # pages meet without overlap
    assert pd.Timestamp(stamps[0], unit="ms", tz="UTC").tz_convert(ET).date().isoformat() == "2026-10-20"  # the first day too
    assert sum(b["volume"] for b in got) == len(secs)  # every second counted once
    assert all(b["timestamp"] % 16_000 == 0 for b in got)  # buckets on the clock


def test_the_series_cache_keeps_to_a_memory_budget(store, monkeypatch):
    from massive_collate.gui import bars as gui_bars

    for i, t in enumerate(("AAA", "BBB", "CCC")):
        store.upsert_bars("massive", t, "minute", 1, [_bar(f"2026-10-20 10:{m:02d}", 10.0 + i) for m in range(50)])
    one = gui_bars._weight(BarEngine(store).series("massive", "AAA"))
    monkeypatch.setattr(gui_bars, "SERIES_CACHE_BYTES", int(one * 1.5))  # room for one history
    eng = BarEngine(store)
    for t in ("AAA", "BBB", "CCC"):
        assert eng.series("massive", t) is not None
    assert list(k[1] for k in eng._series) == ["CCC"] and 0 < eng._series.nbytes <= one * 1.5
    assert eng.series("*", "AAA").ticker == "AAA"  # a merged chart whose part was let go still loads
    monkeypatch.setattr(gui_bars, "SERIES_CACHE_BYTES", 1)  # a history over the budget on its own
    small = BarEngine(store)
    assert small.series("massive", "AAA") is not None and len(small._series) == 0  # served, not kept


def test_open_symbol_goes_to_the_symbol_page_without_waiting_for_the_whole_list():
    from pathlib import Path

    from massive_collate.gui import server

    app = (Path(server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    opener = app.split("function openSymbol(", 1)[1].split("\n}\n", 1)[0]
    assert "showPane?.('stock'" in opener and "known ?" in opener
    assert "selectSymbol(t).catch" not in app and "selectSymbol(t.dataset.ticker).catch" not in app  # every pick opens the page


def test_a_long_history_load_does_not_hold_up_the_rest_of_the_app(store):
    store.watch("massive", ["SPCX"], "minute")
    app = ChartApp(store)
    assert app.engine.store is not app.store and app._lock is not app.engine.lock  # connections and locks of its own
    answered = []
    with app.engine.lock:  # the engine busy loading twenty years of minutes
        t = threading.Thread(target=lambda: answered.append((app.watchlists(), len(app.symbols()))))
        t.start()
        t.join(timeout=20)
    assert answered and answered[0][1] == 1  # the lists answered meanwhile
