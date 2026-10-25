from __future__ import annotations

import time

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest

from massive_collate.gui.bars import MERGED, BarEngine, decimals, merge_frames, resample
from massive_collate.gui.server import ChartApp, make_handler
from massive_collate.storage import Store

ET = "America/New_York"


def _minutes(start: str, n: int, price0: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz=ET)
    p = price0 + np.arange(n) * 0.01
    return pd.DataFrame({"open": p, "high": p + 0.05, "low": p - 0.05, "close": p + 0.01,
                         "volume": 100.0, "turnover": p * 100, "trades": 1}, index=idx)


def test_decimals():
    assert decimals([1.5, 2.25, 3.125]) == 3
    assert decimals([10, 20]) == 0
    assert decimals([172.3875], minimum=2) == 4
    assert decimals([1.0], minimum=2) == 2


def test_resample_is_exact():
    f = _minutes("2026-10-20 09:30", 10)
    bars = resample(f, "minute", 5, "regular")
    assert len(bars) == 2
    first = bars.iloc[0]
    assert first["open"] == f["open"].iloc[0]
    assert first["close"] == f["close"].iloc[4]
    assert first["high"] == f["high"].iloc[:5].max() and first["low"] == f["low"].iloc[:5].min()
    assert first["volume"] == 500 and first["trades"] == 5
    assert bars.index[0] == pd.Timestamp("2026-10-20 09:30", tz=ET)


def test_regular_session_filters_extended_hours():
    f = pd.concat([_minutes("2026-10-20 08:00", 30), _minutes("2026-10-20 09:30", 30), _minutes("2026-10-20 16:00", 30)])
    assert resample(f, "minute", 1, "regular").index.min() == pd.Timestamp("2026-10-20 09:30", tz=ET)
    assert len(resample(f, "minute", 1, "regular")) == 30
    assert len(resample(f, "minute", 1, "extended")) == 90


def test_hour_bars_align_to_the_open():
    f = _minutes("2026-10-20 09:30", 120)
    bars = resample(f, "hour", 1, "regular")
    assert list(bars.index.strftime("%H:%M")) == ["09:30", "10:30"]


def test_week_and_month_bars_start_monday_and_first():
    f = pd.concat([_minutes(f"2026-10-{d} 10:00", 1) for d in (19, 21, 23, 26)])  # Mon, Wed, Fri, Mon
    weeks = resample(f, "week", 1, "regular")
    assert list(weeks.index.strftime("%Y-%m-%d")) == ["2026-10-19", "2026-10-26"]
    months = resample(f, "month", 1, "regular")
    assert list(months.index.strftime("%Y-%m-%d")) == ["2026-10-01"] and months["volume"].iloc[0] == 400


def test_empty_intervals_are_dropped():
    f = pd.concat([_minutes("2026-10-20 09:30", 1), _minutes("2026-10-20 09:45", 1)])
    assert len(resample(f, "minute", 5, "regular")) == 2


def test_merge_takes_each_slot_from_one_feed():
    a = _minutes("2026-10-20 09:30", 3, 100)
    b = _minutes("2026-10-20 09:31", 3, 200)  # overlaps a at 09:31-09:32, adds 09:33
    m = merge_frames([a, b])
    assert len(m) == 4
    assert m.loc["2026-10-20 09:31"]["open"] == a.loc["2026-10-20 09:31"]["open"]  # a has priority
    assert m.loc["2026-10-20 09:33"]["open"] == b.loc["2026-10-20 09:33"]["open"]
    assert m["volume"].sum() == 400  # nothing double counted


def test_merge_keeps_same_timestamp_ticks_and_avoids_double_count():
    t = pd.Timestamp("2026-10-20 10:00:00.5", tz=ET)
    ticks = lambda price, n, at: pd.DataFrame(  # noqa: E731
        {"open": price, "high": price, "low": price, "close": price, "volume": 1.0, "turnover": price, "trades": 1},
        index=pd.DatetimeIndex([at] * n))
    a = ticks(10.0, 3, t)  # three trades, same nanosecond
    b = pd.concat([ticks(10.0, 3, t), ticks(11.0, 1, t + pd.Timedelta("2min"))])  # same trades + one later
    m = merge_frames([a, b], bucket="1min")
    assert len(m) == 4  # 3 from a (kept, not de-duplicated), b only where a has no data


def _store_with_bars(tmp_path):
    s = Store(tmp_path / "m.db", check_same_thread=False)
    idx = pd.date_range("2026-10-20 09:30", periods=390, freq="1min", tz=ET)
    bars = [{"t": int(ts.timestamp() * 1000), "o": 10 + i * 0.001, "h": 10.5, "l": 9.5, "c": 10.25, "v": 100, "vw": 10.1}
            for i, ts in enumerate(idx)]
    s.upsert_bars("massive", "SPCX", "minute", 1, bars)
    s.watch("massive", ["SPCX"], "minute", exchange="XNAS")
    return s


def test_engine_paging(tmp_path):
    s = _store_with_bars(tmp_path)
    eng = BarEngine(s)
    first = eng.bars("massive", "SPCX", "minute", 1, limit=100)
    assert len(first["bars"]) == 100 and first["more"] is True
    older = eng.bars("massive", "SPCX", "minute", 1, to_ms=first["bars"][0]["timestamp"], limit=1000)
    assert len(older["bars"]) == 290 and older["more"] is False
    assert older["bars"][-1]["timestamp"] < first["bars"][0]["timestamp"]
    since = eng.bars("massive", "SPCX", "minute", 1, since_ms=first["bars"][-1]["timestamp"])
    assert len(since["bars"]) == 1
    assert eng.series("massive", "SPCX").price_precision == 3


def test_engine_refuses_intervals_finer_than_data(tmp_path):
    eng = BarEngine(_store_with_bars(tmp_path))
    with pytest.raises(ValueError):
        eng.bars("massive", "SPCX", "second", 1)


def test_engine_merged_feed(tmp_path):
    s = _store_with_bars(tmp_path)
    s.upsert_bars("overnight", "SPCX", "minute", 1, [
        {"t": int(pd.Timestamp("2026-10-20 21:00", tz=ET).timestamp() * 1000), "o": 1, "h": 1, "l": 1, "c": 1, "v": 5}])
    eng = BarEngine(s, priority=["massive"])
    assert eng.sources_for("SPCX") == ["massive", "overnight"]
    ext = eng.bars(MERGED, "SPCX", "minute", 1, session="extended", limit=10000)["bars"]
    assert len(ext) == 391
    reg = eng.bars(MERGED, "SPCX", "minute", 1, session="regular", limit=10000)["bars"]
    assert len(reg) == 390


def test_range_stats_are_exact(tmp_path):
    s = _store_with_bars(tmp_path)
    eng = BarEngine(s)
    bars = eng.bars("massive", "SPCX", "minute", 1, limit=10000)["bars"]
    a, b = bars[10], bars[19]  # 10 bars inclusive
    st = eng.range_stats("massive", "SPCX", "minute", 1, "regular", b["timestamp"], a["timestamp"])  # either order
    sel = bars[10:20]
    assert st["bars"] == 10
    assert st["open"] == sel[0]["open"] and st["close"] == sel[-1]["close"]
    assert st["high"] == max(x["high"] for x in sel) and st["low"] == min(x["low"] for x in sel)
    assert st["highTime"] == sel[0]["timestamp"]  # first bar reaching the high
    assert st["volume"] == sum(x["volume"] for x in sel)
    assert st["vwap"] == pytest.approx(sum(x["turnover"] for x in sel) / st["volume"])
    assert st["change"] == pytest.approx(sel[-1]["close"] - sel[0]["open"])
    assert st["changePct"] == pytest.approx((sel[-1]["close"] / sel[0]["open"] - 1) * 100)


def test_range_stats_follow_the_interval(tmp_path):
    eng = BarEngine(_store_with_bars(tmp_path))
    five = eng.bars("massive", "SPCX", "minute", 5, limit=10000)["bars"]
    st = eng.range_stats("massive", "SPCX", "minute", 5, "regular", five[0]["timestamp"], five[1]["timestamp"])
    assert st["bars"] == 2 and st["volume"] == 1000  # 10 one-minute bars of 100


def test_range_stats_empty_window(tmp_path):
    eng = BarEngine(_store_with_bars(tmp_path))
    assert eng.range_stats("massive", "SPCX", "minute", 1, "regular", 0, 1) == {"bars": 0}


@pytest.mark.parametrize("kind", ["heikin_ashi", "renko", "line_break", "kagi", "point_figure", "range"])
def test_engine_chart_types(tmp_path, kind):
    from massive_collate.gui.bars import Style

    eng = BarEngine(_store_with_bars(tmp_path))
    reversal = 3 if kind == "point_figure" else 0.01
    res = eng.bars("massive", "SPCX", "minute", 1, limit=100000, style=Style(type=kind, box=0.01, reversal=reversal))
    assert res["style"]["type"] == kind
    ts = [b["timestamp"] for b in res["bars"]]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert ("approximate" in res) == (kind == "range")


def test_server_rejects_bad_chart_parameters(tmp_path):
    app = ChartApp(_store_with_bars(tmp_path))
    with pytest.raises(ValueError):
        app.bars({"source": "massive", "ticker": "SPCX", "type": "renko", "box": "-1"})
    with pytest.raises(ValueError):
        app.bars({"source": "massive", "ticker": "SPCX", "type": "nope"})
    with pytest.raises(ValueError):
        app.bars({"source": "massive", "ticker": "SPCX", "type": "point_figure", "reversal": "2.5"})


def _future_store(tmp_path):
    from datetime import date

    from massive_collate.symbols import resolve

    s = Store(tmp_path / "f.db", check_same_thread=False)
    s.watch_spec("massive", resolve("NQ1!"), "minute")
    ms = lambda t: int(pd.Timestamp(t, tz=ET).timestamp() * 1000)  # noqa: E731
    bar = lambda t, c: {"t": ms(t), "o": c, "h": c, "l": c, "c": c, "v": 1}  # noqa: E731
    # NQU6 held for the 17th session (opens 16th 18:00); NQZ6 from the 18th session
    s.upsert_bars("massive", "NQU6", "minute", 1, [bar("2026-09-16 19:00", 100.0), bar("2026-09-17 10:00", 101.0)])
    s.upsert_bars("massive", "NQZ6", "minute", 1, [bar("2026-09-17 10:00", 111.0), bar("2026-09-17 20:00", 112.0),
                                                     bar("2026-09-18 10:00", 113.0)])
    s.set_rolls("massive", "NQ", 1, [(date(2026, 9, 17), "NQU6"), (date(2026, 9, 18), "NQZ6")])
    return s


def test_continuous_future_series(tmp_path):
    eng = BarEngine(_future_store(tmp_path))
    raw = eng.bars("massive", "NQ1!", "minute", 1, session="regular", limit=100)["bars"]
    # around the clock: the 19:00 / 20:00 bars are kept even though "regular" was asked for
    assert [b["close"] for b in raw] == [100.0, 101.0, 112.0, 113.0]
    s = eng.series("massive", "NQ1!")
    assert s.asset == "future" and s.around_the_clock
    assert s.rolls[0]["from"] == "NQU6" and s.rolls[0]["to"] == "NQZ6" and s.rolls[0]["gap"] == pytest.approx(10.0)
    adj = eng.bars("massive", "NQ1!", "minute", 1, adjust="difference", limit=100)["bars"]
    assert [b["close"] for b in adj] == [110.0, 111.0, 112.0, 113.0]


def test_meta_reports_asset_and_rolls(tmp_path):
    app = ChartApp(_future_store(tmp_path))
    m = app.meta("massive", "NQ1!")
    assert m["asset"] == "future" and m["aroundTheClock"] and len(m["rolls"]) == 1
    # roll time is epoch milliseconds of the first bar on the new contract (the chart's unit)
    first_new = app.bars({"source": "massive", "ticker": "NQ1!", "period": "minute", "limit": "100"})["bars"][2]
    assert m["rolls"][0]["time"] == first_new["timestamp"] == int(pd.Timestamp("2026-09-17 20:00", tz=ET).timestamp() * 1000)
    with pytest.raises(ValueError):
        app.meta("massive", "NQ1!", "bogus")


def test_lod_aggregate_is_exact_and_anchored():
    from massive_collate.gui.bars import lod_aggregate

    f = _minutes("2026-10-20 09:30", 10)
    agg = lod_aggregate(f, 4)
    assert len(agg) == 3  # 4 + 4 + 2
    first = agg.iloc[0]
    assert first["open"] == f["open"].iloc[0] and first["close"] == f["close"].iloc[3]
    assert first["high"] == f["high"].iloc[:4].max() and first["low"] == f["low"].iloc[:4].min()
    assert first["volume"] == f["volume"].iloc[:4].sum()
    assert list(agg.index) == [f.index[0], f.index[4], f.index[8]]
    # anchored to the series start: appending bars never moves existing groups
    longer = lod_aggregate(_minutes("2026-10-20 09:30", 13), 4)
    assert longer.iloc[:2].equals(agg.iloc[:2])
    assert lod_aggregate(f, 1) is f


def test_engine_lod_paging_and_limits(tmp_path):
    eng = BarEngine(_store_with_bars(tmp_path))
    full = eng.bars("massive", "SPCX", "minute", 1, limit=100000)
    lod = eng.bars("massive", "SPCX", "minute", 1, limit=100000, lod=8)
    assert full["total"] == 390 and lod["total"] == 49 and lod["style"]["lod"] == 8
    assert max(b["high"] for b in lod["bars"]) == max(b["high"] for b in full["bars"])
    assert sum(b["volume"] for b in lod["bars"]) == sum(b["volume"] for b in full["bars"])
    page = eng.bars("massive", "SPCX", "minute", 1, limit=10, lod=8)
    older = eng.bars("massive", "SPCX", "minute", 1, limit=100, lod=8, to_ms=page["bars"][0]["timestamp"])
    assert len(page["bars"]) + len(older["bars"]) == 49
    with pytest.raises(ValueError):
        eng.bars("massive", "SPCX", "minute", 1, lod=0)


def test_lod_cache_is_per_chart_type(tmp_path):
    from massive_collate.gui.bars import Style

    eng = BarEngine(_store_with_bars(tmp_path))
    q = dict(limit=100000, lod=2)
    rng = eng.bars("massive", "SPCX", "minute", 1, style=Style(type="range", box=0.25), **q)
    candles = eng.bars("massive", "SPCX", "minute", 1, **q)
    rng_small = eng.bars("massive", "SPCX", "minute", 1, style=Style(type="range", box=0.1), **q)
    assert candles["total"] == 195 and rng["total"] != candles["total"]
    assert rng_small["total"] != rng["total"]  # parameters matter too
    assert eng.bars("massive", "SPCX", "minute", 1, style=Style(type="range", box=0.25), **q)["total"] == rng["total"]


def test_lod_applies_to_bricks_but_not_kagi(tmp_path):
    from massive_collate.gui.bars import Style

    eng = BarEngine(_store_with_bars(tmp_path))
    rng = eng.bars("massive", "SPCX", "minute", 1, limit=100000, style=Style(type="range", box=0.25))
    rng4 = eng.bars("massive", "SPCX", "minute", 1, limit=100000, style=Style(type="range", box=0.25), lod=4)
    assert rng["total"] > 8
    assert rng4["style"]["lod"] == 4 and rng4["total"] == -(-rng["total"] // 4)
    assert max(b["high"] for b in rng4["bars"]) == max(b["high"] for b in rng["bars"])
    assert min(b["low"] for b in rng4["bars"]) == min(b["low"] for b in rng["bars"])
    kagi = eng.bars("massive", "SPCX", "minute", 1, limit=100000, style=Style(type="kagi", reversal=0.01), lod=4)
    assert "lod" not in kagi["style"]  # drawn from per-segment fields: never aggregated


def test_info_combines_symbol_details_stats_and_coverage(tmp_path):
    s = _store_with_bars(tmp_path)
    s.set_details("massive", "SPCX", {"name": "Space Exploration Technologies Corp.", "market_cap": 1.9e12,
                                      "description": "Rockets.", "branding": {"logo_url": "x"}})
    day = int(pd.Timestamp("2026-10-20", tz=ET).timestamp() * 1000)
    s.upsert_bars("massive", "SPCX", "day", 1, [{"t": day, "o": 10, "h": 11, "l": 9, "c": 10.77, "v": 123456}])
    app = ChartApp(s)
    info = app.info("*", "SPCX")
    assert info["data"]["rows"] is None  # counted in the background (a count blocks nothing): the next read has it
    for _ in range(200):
        if not app._counting:
            break
        time.sleep(0.01)
    info = app.info("*", "SPCX")
    assert info["symbol"]["ticker"] == "SPCX" and info["symbol"]["exchange"] == "XNAS"
    assert info["details"] == {"name": "Space Exploration Technologies Corp.", "market_cap": 1.9e12,
                               "description": "Rockets."}
    assert info["stats"]["last"] == 10.77 and info["stats"]["day"]["volume"] == 123456
    assert info["data"]["rows"] == 390 and info["data"]["officialDaily"] is True
    with pytest.raises(LookupError):
        ChartApp(s).info("massive", "NOPE")


def test_info_for_futures_reports_contract(tmp_path):
    info = ChartApp(_future_store(tmp_path)).info("massive", "NQ1!")
    assert info["futures"]["contract"] == "NQZ6" and info["futures"]["rolls"] == 1
    assert info["symbol"]["asset"] == "future" and info["stats"]["last"] == 113.0


def test_models_through_the_engine_and_api(tmp_path):
    from massive_collate.gui.bars import Style
    from massive_collate.calc.derivatives import Model

    s = _store_with_bars(tmp_path)  # opens rise 0.001 per minute, close constant 10.25
    eng = BarEngine(s)
    d1 = eng.bars("massive", "SPCX", "minute", 1, limit=1000, model=Model("d1", per="bar"))
    assert d1["model"]["kind"] == "d1" and d1["model"]["unit"] == "price/bar"
    assert all(abs(b["close"]) < 1e-9 for b in d1["bars"])  # constant close: zero slope
    integ = eng.bars("massive", "SPCX", "minute", 1, limit=1000, model=Model("integral", per="bar"))
    assert integ["bars"][-1]["close"] == pytest.approx(0.0)
    # zoomed-out aggregates of a model are its envelope; statistics are in model units
    lod = eng.bars("massive", "SPCX", "minute", 1, limit=1000, lod=8, model=Model("d1", per="bar"))
    assert lod["style"]["lod"] == 8 and lod["style"]["model"] == "d1"
    st = eng.range_stats("massive", "SPCX", "minute", 1, "regular", d1["bars"][0]["timestamp"],
                         d1["bars"][9]["timestamp"], model=Model("d1", per="bar"))
    assert st["bars"] == 10 and st["vwap"] is None and st["unit"] == "price/bar"
    with pytest.raises(ValueError):  # price-based chart types have no derivative
        eng.bars("massive", "SPCX", "minute", 1, style=Style(type="renko", box=0.1), model=Model("d1"))
    app = ChartApp(s)
    assert app.bars({"source": "massive", "ticker": "SPCX", "model": "d2", "per": "minute", "window": "21"})["model"]["unit"] == "price/minute²"
    with pytest.raises(ValueError):
        app.bars({"source": "massive", "ticker": "SPCX", "model": "d1", "window": "4"})


def test_daily_uses_official_bars(tmp_path):
    s = _store_with_bars(tmp_path)
    day = int(pd.Timestamp("2026-10-20 00:00", tz=ET).timestamp() * 1000)
    s.upsert_bars("massive", "SPCX", "day", 1, [{"t": day, "o": 10, "h": 11, "l": 9, "c": 10.77, "v": 123456}])
    d = BarEngine(s).bars("massive", "SPCX", "day", 1)["bars"]
    assert len(d) == 1 and d[0]["close"] == 10.77 and d[0]["volume"] == 123456


@pytest.fixture
def server(tmp_path):
    store = _store_with_bars(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ChartApp(store), [0]))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    store.close()


def _get(port, path, host="127.0.0.1"):
    c = HTTPConnection("127.0.0.1", port)
    c.request("GET", path, headers={"Host": f"{host}:{port}"})
    r = c.getresponse()
    return r.status, r.read()


def test_server_endpoints(server):
    status, body = _get(server, "/api/symbols")
    sym = json.loads(body)[0]
    assert status == 200 and sym["ticker"] == "SPCX" and sym["exchange"] == "XNAS"
    assert sym["feeds"] == [{"source": "massive", "data": ["minute"]}]
    status, body = _get(server, "/api/meta?source=*&ticker=SPCX")
    meta = json.loads(body)
    assert status == 200 and meta["base"] == "minute" and {"type": "second", "span": 1} not in meta["periods"]
    status, body = _get(server, "/api/bars?source=*&ticker=SPCX&period=minute&span=5&limit=3")
    assert status == 200 and len(json.loads(body)["bars"]) == 3
    bars = json.loads(body)["bars"]
    status, body = _get(server, f"/api/range?source=*&ticker=SPCX&period=minute&span=5"
                                f"&from={bars[0]['timestamp']}&to={bars[-1]['timestamp']}")
    assert status == 200 and json.loads(body)["bars"] == 3
    status, _ = _get(server, "/")
    assert status == 200


def test_server_rejects_other_hosts_and_bad_paths(server):
    assert _get(server, "/api/symbols", host="evil.example")[0] == 403
    assert _get(server, "/../../etc/passwd")[0] == 404
    assert _get(server, "/api/bars?source=massive&ticker=SPCX&period=second")[0] == 400
    assert _get(server, "/api/meta?source=massive&ticker=NOPE")[0] == 404


def test_every_interval_is_offered_and_those_the_data_cannot_build_are_marked(tmp_path):
    eng = BarEngine(_store_with_bars(tmp_path))
    s = eng.series("massive", "SPCX")
    every = {(p["type"], p["span"]): p["available"] for p in eng.all_periods(s)}
    assert list(every) == [("second", 1), ("second", 30), ("minute", 1), ("minute", 5), ("minute", 30), ("hour", 1),
                           ("hour", 2), ("hour", 4), ("day", 1), ("week", 1), ("month", 1), ("year", 1)]
    assert not every[("second", 1)] and every[("minute", 1)] and every[("day", 1)]  # minute data: no seconds
    assert {"type": "second", "span": 1} not in eng.periods(s) and {"type": "minute", "span": 5} in eng.periods(s)


def test_exchanges_without_a_mic_never_break_the_markets_endpoint(tmp_path):
    """Alpaca's SIP rows have no MIC: NaN in the frame (and NaN is true), which made /api/markets
    fail with 'Out of range float values are not JSON compliant' and the page retry it endlessly."""
    import pandas as pd

    from massive_collate.gui.markets import markets
    from massive_collate.gui.server import finite
    from massive_collate.sessions import MarketCalendar

    ex = pd.DataFrame([{"source": "alpaca", "id": 83, "name": "Securities Information Processor", "mic": float("nan"), "country": "US",
                        "acronym": "S", "type": "exchange"},
                       {"source": "massive", "id": 10, "name": "New York Stock Exchange", "mic": "XNYS",
                        "country": "US", "acronym": "NYSE", "type": "exchange"}])
    out = markets(MarketCalendar(), ex, [])
    stocks = next(m for m in out["markets"] if m["exchanges"] and m["exchanges"][0]["mic"] == "XNYS")
    assert stocks["exchanges"] == [{"name": "New York Stock Exchange", "mic": "XNYS"}]
    json.dumps(out, allow_nan=False)
    assert finite({"a": [1.0, float("nan"), {"b": float("inf")}], "c": "x"}) == {"a": [1.0, None, {"b": None}], "c": "x"}


def test_a_merged_crypto_chart_takes_the_reference_exchange_first_and_others_where_it_has_nothing(tmp_path):
    from massive_collate.gui.bars import BarEngine
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db") as store:
        t0 = 1_760_000_000_000
        store.upsert_bars("coinbase", "BTCUSD", "minute", 1, [{"t": t0 + 60_000, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}])
        store.upsert_bars("bitstamp", "BTCUSD", "minute", 1, [{"t": t0 + i * 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
                                                              for i in range(2)])
        for feed in ("coinbase", "bitstamp"):
            store.watch(feed, ["BTCUSD"], "minute", asset="crypto")
        eng = BarEngine(store)
        assert eng.sources_for("BTCUSD") == ["coinbase", "bitstamp"]  # alphabetically bitstamp would lead
        closes = [b["close"] for b in eng.bars("*", "BTCUSD", "minute", 1, "extended")["bars"]]
        assert closes == [1.0, 2.0]  # Bitstamp's older minute, then Coinbase's where it has one


def test_the_data_panel_answers_while_the_shared_lock_is_held_and_counts_later(tmp_path):
    import threading

    app = ChartApp(_store_with_bars(tmp_path))
    held, release = threading.Event(), threading.Event()

    def hold():  # something slow under the app's lock (a count of millions of bars took 20 s)
        with app._lock:
            held.set()
            release.wait(5)
    threading.Thread(target=hold, daemon=True).start()
    held.wait(5)
    try:
        inv = app.inventory("SPCX")  # its own connections and lock: not behind the held one
        minute = next(f for f in inv["feeds"][0]["frames"] if f["frame"] == "1m")
        assert minute["sent"]["rows"] is None and minute["sent"]["first"]
    finally:
        release.set()
    for _ in range(200):
        if not app._counting:
            break
        time.sleep(0.01)
    minute = next(f for f in app.inventory("SPCX")["feeds"][0]["frames"] if f["frame"] == "1m")
    assert minute["sent"]["rows"] == 390
