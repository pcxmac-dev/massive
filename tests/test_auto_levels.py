"""The auto-ranging chart's server side (gui/window.py): every level of a series for any window,
read page by page from the store, the same bars the interval charts drew from whole histories."""
from __future__ import annotations

import pandas as pd
import pytest

from massive_collate import derive
from massive_collate.gui.bars import BarEngine
from massive_collate.gui.server import ChartApp
from massive_collate.gui.window import WindowReader
from massive_collate.storage import Store

ET = "America/New_York"
DAYS = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20")


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _minutes(day: str, price: float, start: str = "04:00", end: str = "19:59", every: int = 7) -> list[dict]:
    t0, t1 = pd.Timestamp(f"{day} {start}", tz=ET), pd.Timestamp(f"{day} {end}", tz=ET)
    out = []
    for i, t in enumerate(pd.date_range(t0, t1, freq="1min")):
        if i % every:  # not every minute trades: gaps like a real tape
            continue
        p = price + (i % 13) * 0.01
        out.append({"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.05, "l": p - 0.05, "c": p + 0.01,
                    "v": 100.0 + i, "vw": p, "n": 3})
    return out


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        for i, d in enumerate(DAYS):
            s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(d, 10.0 + i))
        s.upsert_bars("alpaca", "SPCX", "minute", 1, _minutes(DAYS[2], 50.0, every=5))  # a second feed, one day
        s.upsert_bars("massive", "SPCX", "day", 1, [{"t": _ms(f"{d} 00:00"), "o": 10.0 + i, "h": 11.0 + i, "l": 9.0 + i,
                                                     "c": 10.5 + i, "v": 1e6, "vw": 10.2 + i, "n": 900}
                                                    for i, d in enumerate(DAYS[:5])])  # official days, not the last two
        for feed in ("massive", "alpaca"):
            s.watch(feed, ["SPCX"], "minute")
        derive.run(s, log=lambda *_: None)
        yield s


def _all_pages(reader, source, period, span, session, limit):
    got, to = [], None
    for _ in range(500):
        page = reader.page(source, "SPCX", period, span, session, to_ms=to, limit=limit)
        got = page["bars"] + got
        if not page["more"] or not page["bars"]:
            return got
        to = page["bars"][0]["timestamp"]
    raise AssertionError("paging did not end")


@pytest.mark.parametrize("source", ["massive", "*"])
@pytest.mark.parametrize("period, span", [("minute", 1), ("minute", 5), ("minute", 30), ("hour", 1), ("hour", 4),
                                          ("day", 1), ("week", 1)])
@pytest.mark.parametrize("session", ["regular", "extended"])
def test_every_level_paged_by_window_is_what_the_whole_history_gave(store, source, period, span, session):
    engine = BarEngine(store)
    whole = engine.bars(source, "SPCX", period, span, session, limit=100_000)["bars"]
    paged = _all_pages(WindowReader(engine), source, period, span, session, limit=37)
    key = lambda b: (b["timestamp"], round(b["open"], 6), round(b["high"], 6), round(b["low"], 6),
                     round(b["close"], 6), round(b["volume"], 3), bool(b.get("derived")))
    assert [key(b) for b in paged] == [key(b) for b in whole]


def test_newer_pages_from_an_anchor_meet_the_older_ones(store):
    reader = WindowReader(BarEngine(store))
    every = _all_pages(reader, "*", "minute", 5, "extended", limit=50)
    anchor = every[len(every) // 2]["timestamp"]
    older = reader.page("*", "SPCX", "minute", 5, "extended", to_ms=anchor, limit=40)
    newer, after = [], anchor - 1
    while True:
        page = reader.page("*", "SPCX", "minute", 5, "extended", after_ms=after, limit=40)
        newer += page["bars"]
        if not page["newer"] or not page["bars"]:
            break
        after = page["bars"][-1]["timestamp"]
    assert older["newer"] and older["bars"][-1]["timestamp"] < anchor
    assert [b["timestamp"] for b in newer] == [b["timestamp"] for b in every if b["timestamp"] >= anchor]


def test_the_live_tail_is_the_bars_from_the_newest_the_chart_has(store):
    reader = WindowReader(BarEngine(store))
    last = reader.page("massive", "SPCX", "minute", 30, "extended", limit=3)["bars"]
    tail = reader.page("massive", "SPCX", "minute", 30, "extended", since_ms=last[-2]["timestamp"])["bars"]
    assert [b["timestamp"] for b in tail] == [b["timestamp"] for b in last[-2:]]


def test_the_levels_say_where_each_starts_and_how_dense_it_is(store):
    levels = WindowReader(BarEngine(store)).levels("*", "SPCX")
    names = [(lv["type"], lv["span"]) for lv in levels]
    assert names[0] == ("minute", 1) and ("year", 1) in names and ("second", 1) not in names  # no seconds held
    minute = levels[0]
    assert minute["first"] == _ms(f"{DAYS[0]} 04:00") and minute["label"] == "1 min"
    assert minute["perDay"] == {"regular": pytest.approx(390 * 252 / 365), "extended": pytest.approx(960 * 252 / 365)}


def test_the_chart_opens_a_symbol_on_the_auto_path_without_its_history(store, monkeypatch):
    app = ChartApp(store)
    monkeypatch.setattr(app.engine, "series", lambda *a, **k: pytest.fail("the whole history was loaded"))
    meta = app.meta("*", "SPCX", auto=True)
    assert meta["auto"] and meta["levels"][0]["type"] == "minute" and meta["pricePrecision"] == 2
    page = app.bars({"source": "*", "ticker": "SPCX", "period": "hour", "span": "1", "auto": "1", "limit": "10"})
    assert page["level"]["label"] == "1 hour" and len(page["bars"]) == 10
    fut = app.future({"source": "*", "ticker": "SPCX", "period": "hour", "span": "1", "auto": "1", "count": "5"})
    assert len(fut["timestamps"]) == 5 and fut["timestamps"][0] > fut["lastBar"]
    info = app.info("*", "SPCX")
    assert info["data"]["levels"] and info["stats"]
    assert app.calendar({"source": "*", "ticker": "SPCX", "from": str(_ms(DAYS[0] + " 00:00")),
                         "to": str(_ms(DAYS[-1] + " 00:00"))})["asset"] == "stock"


@pytest.mark.parametrize("period, span", [("minute", 1), ("hour", 1), ("day", 1)])
def test_the_level_2_profile_from_one_bars_window_is_the_series_paths(store, period, span):
    engine = BarEngine(store)
    whole = engine.profile("*", "SPCX", period, span, "extended")
    windowed = WindowReader(engine).profile("*", "SPCX", period, span, "extended")
    for k in ("bar", "day", "last", "newest", "rows", "step", "basis"):
        assert windowed[k] == whole[k], k


def test_a_far_future_projection_stops_before_timestamps_end(store):
    app = ChartApp(store)
    fut = app.future({"source": "*", "ticker": "SPCX", "period": "month", "span": "1", "auto": "1", "count": "4000"})
    assert 0 < len(fut["timestamps"]) <= 600  # fifty years of months, not four thousand
