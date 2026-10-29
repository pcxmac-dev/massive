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
        # official days, not the last two; within their own minutes (a day beyond them is bad data, baddata.py)
        s.upsert_bars("massive", "SPCX", "day", 1, [{"t": _ms(f"{d} 00:00"), "o": 10.0 + i, "h": 10.2 + i, "l": 9.9 + i,
                                                     "c": 10.1 + i, "v": 1e6, "vw": 10.05 + i, "n": 900}
                                                    for i, d in enumerate(DAYS[:5])])
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
    assert minute["perDay"] == {"regular": pytest.approx(390 * 252 / 365), "extended": pytest.approx(960 * 252 / 365),
                                "all": pytest.approx(1440 * 252 / 365)}  # the overnight session too


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
    for auto in ("1", "0"):  # an offset past them answers nothing (past 2262 the timestamps overflowed)
        far = {"source": "*", "ticker": "SPCX", "period": "minute", "span": "1", "auto": auto, "offset": str(10**12)}
        assert app.future(far)["timestamps"] == []


@pytest.mark.parametrize("period, span", [("minute", 5), ("hour", 1), ("day", 1), ("week", 1)])
def test_the_highlights_statistics_from_its_window_are_the_whole_historys(store, period, span):
    engine = BarEngine(store)
    lo, hi = _ms(f"{DAYS[1]} 10:00"), _ms(f"{DAYS[5]} 15:00")
    whole = engine.range_stats("*", "SPCX", period, span, "regular", lo, hi)
    assert WindowReader(engine).range_stats("*", "SPCX", period, span, "regular", lo, hi) == whole and whole["bars"]


# -- chart types and models on the window -------------------------------------------------------
from massive_collate.calc.derivatives import Model  # noqa: E402
from massive_collate.gui import window as window_module  # noqa: E402
from massive_collate.gui.bars import Style  # noqa: E402


def _key(b):
    return (b["timestamp"], round(b["open"], 6), round(b["high"], 6), round(b["low"], 6), round(b["close"], 6),
            round(b["volume"], 3))


def _styled_pages(reader, source, period, span, session, limit, **kw):
    """Every page back from the newest, oldest first (a page's own oldest value is the next `to`)."""
    got, to = [], None
    for _ in range(500):
        page = reader.styled_page(source, "SPCX", period, span, session, to_ms=to, limit=limit, **kw)
        got = page["bars"] + got
        if not page["more"] or not page["bars"]:
            return got
        to = page["bars"][0]["timestamp"]
    raise AssertionError("paging did not end")


@pytest.mark.parametrize("source", ["massive", "*"])
@pytest.mark.parametrize("kind", ["renko", "kagi", "line_break", "point_figure", "range"])
@pytest.mark.parametrize("session", ["regular", "extended"])
def test_price_based_charts_are_the_series_paths_at_their_own_level(store, source, kind, session):
    engine = BarEngine(store)
    reader = WindowReader(engine)
    level = reader.brick_level(source, "SPCX", session)
    assert (level["type"], level["span"]) == ("minute", 1)  # a short history: its finest level
    whole = engine.bars(source, "SPCX", "minute", 1, session, limit=100_000, style=Style(type=kind))
    # asked at the chart's level (4 hours): built at their own
    paged = _styled_pages(reader, source, "hour", 4, session, 997, style=Style(type=kind))
    assert paged and [_key(b) for b in paged] == [_key(b) for b in whole["bars"]]
    first = reader.styled_page(source, "SPCX", "hour", 4, session, style=Style(type=kind), limit=5)
    assert first["style"] == whole["style"] and first["level"]["fixed"] and first["level"]["label"] == "1 min"
    assert first["total"] == len(whole["bars"])


def test_a_long_history_builds_its_bricks_from_days(store, monkeypatch):
    monkeypatch.setattr(window_module, "BRICK_READ", 100)  # the minutes are too many to walk
    engine = BarEngine(store)
    reader = WindowReader(engine)
    assert reader.brick_level("*", "SPCX")["type"] == "day"
    whole = engine.bars("*", "SPCX", "day", 1, "regular", style=Style(type="renko", box=0.5))
    page = reader.styled_page("*", "SPCX", "minute", 1, "regular", style=Style(type="renko", box=0.5))
    assert page["bars"] and [_key(b) for b in page["bars"]] == [_key(b) for b in whole["bars"]]
    assert page["level"]["label"] == "1 day"


def test_bricks_are_built_once_while_the_data_is_unchanged(store, monkeypatch):
    reader = WindowReader(BarEngine(store))
    builds = []
    level_bars = reader._level_bars
    monkeypatch.setattr(reader, "_level_bars", lambda *a: builds.append(a) or level_bars(*a))
    first = reader.bricks("*", "SPCX", "extended", Style(type="renko"), limit=5)
    reader.bricks("*", "SPCX", "extended", Style(type="renko"), to_ms=first["bars"][0]["timestamp"], limit=5)
    assert len(builds) == 1
    reader.bricks("*", "SPCX", "extended", Style(type="kagi"), limit=5)  # another chart: its own build
    assert len(builds) == 2


def test_newer_bricks_from_an_anchor_meet_the_older_ones(store):
    reader = WindowReader(BarEngine(store))
    every = reader.bricks("*", "SPCX", "extended", Style(type="line_break"), limit=100_000)["bars"]
    anchor = every[len(every) // 2]["timestamp"]
    newer = reader.bricks("*", "SPCX", "extended", Style(type="line_break"), after_ms=anchor - 1, limit=100_000)
    assert [b["timestamp"] for b in newer["bars"]] == [b["timestamp"] for b in every if b["timestamp"] >= anchor]
    assert not newer["newer"]


MODELS = [Model(kind="d1", window=9), Model(kind="d2", window=11), Model(kind="d1", window=9, causal=True),
          Model(kind="fourier", window=15), Model(kind="fourier", window=15, basis="log"),
          Model(kind="d1", window=5, reset_gaps=False)]


@pytest.mark.parametrize("source", ["massive", "*"])
@pytest.mark.parametrize("period, span", [("minute", 1), ("minute", 5), ("hour", 1), ("day", 1)])
@pytest.mark.parametrize("model", MODELS, ids=lambda m: f"{m.kind}-{m.window}-{m.basis}-{m.causal}-{m.reset_gaps}")
def test_models_paged_by_window_are_the_whole_historys(store, source, period, span, model):
    engine = BarEngine(store)
    whole = engine.bars(source, "SPCX", period, span, "extended", limit=100_000, model=model)
    paged = _styled_pages(WindowReader(engine), source, period, span, "extended", 23, model=model)
    want = [(b["timestamp"], b["close"]) for b in whole["bars"]]
    assert [b["timestamp"] for b in paged] == [t for t, _ in want]
    assert [b["close"] for b in paged] == pytest.approx([c for _, c in want], rel=1e-9, abs=1e-12)


def test_the_integral_is_drawn_from_a_window_at_days_and_longer_only(store):
    engine = BarEngine(store)
    reader = WindowReader(engine)
    model = Model(kind="integral")
    whole = engine.bars("*", "SPCX", "week", 1, "regular", model=model)
    assert reader.styled_page("*", "SPCX", "week", 1, "regular", model=model)["bars"] == whole["bars"]
    with pytest.raises(ValueError):
        reader.styled_page("*", "SPCX", "minute", 5, "regular", model=model)
    app = ChartApp(store)
    q = {"source": "*", "ticker": "SPCX", "auto": "1", "model": "integral"}
    assert not app._auto({**q, "period": "minute"}) and app._auto({**q, "period": "day"})
    assert app._auto({**q, "model": "price", "period": "minute"}, "renko")
    assert not app._auto({**q, "model": "d1", "period": "minute"}, "renko") and not app._auto(q, "box")


def test_a_fourier_fit_cut_short_says_so(store, monkeypatch):
    engine = BarEngine(store)
    exact = WindowReader(engine).styled_page("massive", "SPCX", "minute", 1, "extended", limit=30,
                                             model=Model(kind="fourier", window=15))
    assert exact["bars"] and "approximate" not in exact  # its sessions are read whole
    monkeypatch.setattr(window_module, "FOURIER_CONTEXT", 40)  # the history runs on without a gap
    model = Model(kind="fourier", window=15, reset_gaps=False)
    # fitted over the 40 bars before the newest 30 (none after them)
    page = WindowReader(engine).styled_page("massive", "SPCX", "minute", 1, "extended", model=model, limit=30)
    assert len(page["bars"]) == 30 and "Fourier fitted over the 70 bars" in page["approximate"]


@pytest.mark.parametrize("period, span", [("minute", 5), ("hour", 1), ("day", 1)])
def test_heikin_ashi_from_a_window_is_the_whole_historys(store, period, span):
    engine = BarEngine(store)
    whole = engine.bars("*", "SPCX", period, span, "extended", limit=100_000, style=Style(type="heikin_ashi"))["bars"]
    paged = _styled_pages(WindowReader(engine), "*", period, span, "extended", 19, style=Style(type="heikin_ashi"))
    assert [b["timestamp"] for b in paged] == [b["timestamp"] for b in whole]
    for k in ("open", "high", "low", "close"):
        assert [b[k] for b in paged] == pytest.approx([b[k] for b in whole], rel=1e-12), k


@pytest.mark.parametrize("period, span", [("minute", 5), ("day", 1)])
def test_a_models_highlight_statistics_from_its_window_are_the_whole_historys(store, period, span):
    engine = BarEngine(store)
    lo, hi = _ms(f"{DAYS[1]} 10:00"), _ms(f"{DAYS[5]} 15:00")
    model = Model(kind="d1", window=5)  # seven days: a window that fits them
    whole = engine.range_stats("*", "SPCX", period, span, "extended", lo, hi, model=model)
    windowed = WindowReader(engine).range_stats("*", "SPCX", period, span, "extended", lo, hi, model=model)
    assert whole["bars"] and windowed.keys() == whole.keys()
    for k, v in whole.items():
        assert windowed[k] == (pytest.approx(v, rel=1e-9, abs=1e-12) if isinstance(v, float) else v), k


def test_the_chart_draws_bricks_and_models_without_the_history(store, monkeypatch):
    app = ChartApp(store)
    monkeypatch.setattr(app.engine, "series", lambda *a, **k: pytest.fail("the whole history was loaded"))
    q = {"source": "*", "ticker": "SPCX", "period": "hour", "span": "1", "auto": "1", "limit": "10"}
    renko = app.bars({**q, "type": "renko"})
    assert renko["bars"] and renko["level"]["fixed"] and renko["style"]["type"] == "renko"
    assert app.future({**q, "type": "renko", "count": "5"})["timestamps"] == []  # bricks: no time to project
    slope = app.bars({**q, "model": "d1", "window": "5"})
    assert slope["model"]["kind"] == "d1" and slope["style"]["model"] == "d1"
    fut = app.future({**q, "model": "d1", "window": "5", "count": "5"})
    assert len(fut["timestamps"]) == 5 and fut["timestamps"][0] > fut["lastBar"]
    assert app.bars({**q, "type": "heikin_ashi"})["bars"]
    assert app.range_stats({**q, "model": "d1", "window": "5", "from": str(_ms(f"{DAYS[1]} 10:00")),
                            "to": str(_ms(f"{DAYS[2]} 15:00"))})["bars"]


def test_the_series_path_pages_newer_bars_too(store):
    engine = BarEngine(store)
    every = engine.bars("*", "SPCX", "minute", 30, "extended", limit=100_000)["bars"]
    anchor = every[len(every) // 2]["timestamp"]
    older = engine.bars("*", "SPCX", "minute", 30, "extended", to_ms=anchor, limit=10)
    assert older["newer"] and older["bars"][-1]["timestamp"] < anchor
    newer = engine.bars("*", "SPCX", "minute", 30, "extended", after_ms=older["bars"][-1]["timestamp"], limit=5)
    assert [b["timestamp"] for b in newer["bars"]] == [b["timestamp"] for b in every if b["timestamp"] >= anchor][:5]
    assert newer["newer"] and newer["more"]


def test_new_bars_build_the_newest_bricks_again_and_older_pages_keep_theirs(store, monkeypatch):
    reader = WindowReader(BarEngine(store))
    builds = []
    level_bars = reader._level_bars
    monkeypatch.setattr(reader, "_level_bars", lambda *a: builds.append(a) or level_bars(*a))
    monkeypatch.setattr(reader.engine, "fingerprint", lambda *a: pytest.fail("the fingerprint counts every row"))
    first = reader.bricks("massive", "SPCX", "extended", Style(type="renko"), limit=5)
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-10-21", 30.0))  # a new day: the bricks move on
    reader.bricks("massive", "SPCX", "extended", Style(type="renko"), to_ms=first["bars"][0]["timestamp"], limit=5)
    assert len(builds) == 1  # an older page of what the chart shows
    newest = reader.bricks("massive", "SPCX", "extended", Style(type="renko"), limit=5)
    assert len(builds) == 2 and newest["total"] > first["total"]
    monkeypatch.setattr(window_module, "BRICK_TTL_S", 0.0)  # kept no longer: built again
    reader.bricks("massive", "SPCX", "extended", Style(type="renko"), limit=5)
    assert len(builds) == 3


def test_hours_come_from_half_hours_read_once_a_month(store, monkeypatch):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minutes("2026-09-15", 9.0))  # a settled month before October
    engine = BarEngine(store)
    reader = WindowReader(engine)
    reads = []
    merged = reader._merged
    monkeypatch.setattr(reader, "_merged", lambda s, feeds, base, lo, hi: reads.append((base, lo)) or
                        merged(s, feeds, base, lo, hi))
    september, october = _ms("2026-09-01 00:00", "UTC"), _ms("2026-10-01 00:00", "UTC")
    reader.page("*", "SPCX", "hour", 4, "extended", limit=1000)
    assert sorted(reads) == [("minute", september), ("minute", october)]
    reads.clear()
    hourly = _all_pages(reader, "*", "hour", 1, "regular", limit=1000)  # another level: the same bins
    assert reads == [("minute", october)]  # the newest month is read again (live bars land there), not September
    whole = engine.bars("*", "SPCX", "hour", 1, "regular", limit=100_000)["bars"]
    assert [_key(b) for b in hourly] == [_key(b) for b in whole]
    monkeypatch.setattr(window_module, "ROLLUP_TTL_S", 0.0)  # kept no longer: read again
    reads.clear()
    reader.page("*", "SPCX", "minute", 30, "extended", limit=1000)
    assert sorted(reads) == [("minute", september), ("minute", october)]


def test_minutes_without_seconds_are_wide_candles_at_the_second_levels(store):
    """Seconds for two minutes of the last day's evening; the minutes after them (as Blue Ocean's night from
    Tiingo: minutes only) fill their slots at 1, 10 and 30 s: as wide as their time, the middle slot the
    minute itself, the others flat at its close, each carrying its prices. Minutes that hold seconds
    are not doubled."""
    day = DAYS[-1]
    secs = [{"t": _ms(f"{day} 19:50") + i * 1000, "o": 20.0, "h": 20.1, "l": 19.9, "c": 20.05, "v": 5.0, "vw": 20.0, "n": 1}
            for i in range(0, 120, 3)]  # 19:50:00 to 19:51:57, every third second
    store.upsert_bars("massive", "SPCX", "second", 1, secs)
    night = [{"t": _ms(f"{day} 20:00") + i * 60_000, "o": 21.0 + i, "h": 21.5 + i, "l": 20.5 + i, "c": 21.2 + i,
              "v": 600.0, "vw": 21.0 + i, "n": 6} for i in range(3)]  # 20:00 to 20:02: no seconds
    store.upsert_bars("tiingoboats", "SPCX", "minute", 1, night)
    store.watch("tiingoboats", ["SPCX"], "minute")
    reader = WindowReader(BarEngine(store))
    page = reader.page("*", "SPCX", "second", 1, "all", limit=5000)["bars"]
    wide = [b for b in page if b.get("wide") and b["timestamp"] >= _ms(f"{day} 20:00")]
    assert len(wide) == 180 and {b["wide"] for b in wide} == {60_000}  # three minutes, 60 slots each
    first = [b for b in wide if b["timestamp"] < _ms(f"{day} 20:01")]
    assert [b["timestamp"] for b in first] == [_ms(f"{day} 20:00") + i * 1000 for i in range(60)]
    assert {(b["wo"], b["wh"], b["wl"], b["wc"]) for b in first} == {(21.0, 21.5, 20.5, 21.2)}  # the minute, on each
    middle = first[30]
    assert (middle["open"], middle["high"], middle["low"], middle["close"], middle["volume"]) == (21.0, 21.5, 20.5, 21.2, 600.0)
    rest = first[:30] + first[31:]
    assert {(b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in rest} == {(21.2, 21.2, 21.2, 21.2, 0.0)}  # flat
    own = [b for b in page if not b.get("wide") and _ms(f"{day} 19:50") <= b["timestamp"] < _ms(f"{day} 19:52")]
    assert len(own) == 40 and all(b["wide"] == 0 for b in own)  # the minutes 19:50 and 19:51 hold seconds: not wide
    ten = reader.page("*", "SPCX", "second", 10, "all", limit=5000)["bars"]
    assert sum(1 for b in ten if b.get("wide") and b["timestamp"] >= _ms(f"{day} 20:00")) == 18  # 6 a minute at 10 s
    tail = reader.page("*", "SPCX", "second", 1, "all", since_ms=_ms(f"{day} 20:02"))["bars"]
    assert len(tail) == 60 and tail[0]["close"] == 23.2  # the live tail takes the newest minute's slots
    assert all(not b.get("wide") for b in reader.page("*", "SPCX", "minute", 1, "all", limit=5000)["bars"])
    stats_level = reader._built(reader.series("*", "SPCX"), reader.feeds("*", "SPCX"), "second", "second", 1, "all",
                                _ms(f"{day} 19:00"), _ms(f"{day} 21:00"))
    assert "wide" not in stats_level and len(stats_level) == 40  # stats and profiles read the seconds only
