from __future__ import annotations

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
