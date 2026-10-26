"""Schemes over a stretch of the chart, and replay (gui/window.py: scheme, replay; the chart app's
/api/scheme, /api/schemes and /api/replay): the stretch and its warm-up only, never the history;
replayed bars that become the chart's own bars exactly."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pandas as pd
import pytest

from massive_collate import derive
from massive_collate.calc import schemes as sc
from massive_collate.gui.server import ChartApp, make_handler
from massive_collate.storage import Store

ET = "America/New_York"
DAYS = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20")


def _ms(t: str) -> int:
    return int(pd.Timestamp(t, tz=ET).timestamp() * 1000)


def _minutes(day: str, price: float, every: int = 3) -> list[dict]:
    out = []
    for i, t in enumerate(pd.date_range(pd.Timestamp(f"{day} 04:00", tz=ET), pd.Timestamp(f"{day} 19:59", tz=ET),
                                        freq="1min")):
        if i % every:
            continue
        p = price + ((i * 7) % 23) * 0.03 + (i % 200) * 0.01  # swings, so the schemes decide
        out.append({"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.06, "l": p - 0.05, "c": p + 0.02,
                    "v": 100.0 + i, "vw": p, "n": 3})
    return out


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        for i, d in enumerate(DAYS):
            s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(d, 10.0 + i * 0.3))
        s.upsert_bars("massive", "SPCX", "day", 1, [{"t": _ms(f"{d} 00:00"), "o": 10.0 + i, "h": 13.0 + i, "l": 9.0 + i,
                                                     "c": 10.5 + i, "v": 1e6, "vw": 10.2 + i, "n": 900}
                                                    for i, d in enumerate(DAYS[:5])])
        s.watch("massive", ["SPCX"], "minute")
        derive.run(s, log=lambda *_: None)
        yield s


def test_a_scheme_reads_its_stretch_and_warm_up_and_matches_a_run_over_the_whole_level(store, monkeypatch):
    app = ChartApp(store)
    monkeypatch.setattr(app.engine, "series", lambda *a, **k: pytest.fail("the whole history was loaded"))
    lo, hi = _ms(f"{DAYS[3]} 09:30"), _ms(f"{DAYS[5]} 15:55")
    q = {"source": "*", "ticker": "SPCX", "period": "minute", "span": "5", "session": "regular", "from": str(lo),
         "to": str(hi), "scheme": "atr_swing", "params": json.dumps({"mult": 1.5}), "fee": "1"}
    out = app.scheme(q)
    assert out["equity"][0][0] == lo and out["equity"][-1][0] == hi and out["warmup"] == 200  # ten ATRs, at least 200
    assert out["params"] == {"atr": 14, "mult": 1.5} and out["view"] == "filtered" and out["trades"]
    # the same as the scheme over the level's bars read whole
    every = app.window._frame_page(app.window.series("*", "SPCX"), ["massive"], "minute", 5, "regular", hi + 300_000,
                                   None, None, 100_000)["frame"]  # to the last bar's end: the whole of it
    ts = every.index.as_unit("ms").asi8
    first = int(ts.searchsorted(lo))
    whole = sc.run("atr_swing", every[ts <= hi].iloc[first - 200:], 200, {"mult": 1.5}, sc.Costs(fee_bps=1))
    assert out["trades"] == whole["trades"] and out["stats"] == whole["stats"]


def test_a_scheme_refuses_bad_settings_and_too_many_bars(store):
    app = ChartApp(store)
    q = {"source": "*", "ticker": "SPCX", "period": "minute", "span": "1", "from": str(_ms(f"{DAYS[0]} 09:30")),
         "to": str(_ms(f"{DAYS[-1]} 16:00")), "scheme": "cusum"}
    with pytest.raises(ValueError):
        app.scheme({**q, "params": json.dumps({"h": 0})})
    with pytest.raises(ValueError):
        app.scheme({**q, "fee": "-1"})
    with pytest.raises(ValueError):
        app.scheme({**q, "scheme": "martingale"})
    from massive_collate.gui import window

    old = window.SCHEME_MAX_BARS
    window.SCHEME_MAX_BARS = 100
    try:
        with pytest.raises(ValueError, match="coarser level"):
            app.scheme(q)
    finally:
        window.SCHEME_MAX_BARS = old
    assert {s["key"] for s in app.schemes()["schemes"]} == set(sc.SCHEMES)


def _replayed(app, period, span, session, start, limit=97):
    """Every replay page from `start`: the finer bars, and the chart bars the pages completed."""
    fine, complete, at = [], [], start
    for _ in range(1000):
        page = app.replay({"source": "*", "ticker": "SPCX", "period": period, "span": str(span), "session": session,
                           "from": str(at), "limit": str(limit)})
        fine += page["bars"]
        complete += page["complete"]
        if not page["more"]:
            return page, fine, complete
        at = page["next"] + 1
    raise AssertionError("replay did not end")


@pytest.mark.parametrize("period, span, session", [("minute", 5, "regular"), ("hour", 1, "extended"),
                                                   ("day", 1, "regular")])
def test_replayed_bars_build_the_charts_bars_exactly(store, period, span, session):
    app = ChartApp(store)
    start = _ms(f"{DAYS[2]} 00:00")
    last, fine, complete = _replayed(app, period, span, session, start)
    assert last["fine"]["type"] == "minute" and fine[0]["timestamp"] >= start
    assert [b["timestamp"] for b in fine] == sorted({b["timestamp"] for b in fine})  # each once, in order
    chart = app.bars({"source": "*", "ticker": "SPCX", "period": period, "span": str(span), "session": session,
                      "auto": "1", "limit": "5000"})["bars"]
    expect = [b for b in chart if b["timestamp"] >= min(f["bin"] for f in fine)]
    assert [b["timestamp"] for b in complete] == [b["timestamp"] for b in expect]  # every bin, once
    assert complete == expect  # the chart's own bars (the feed's official days where it sent them)
    if period != "day":  # built from the same minutes: the replayed forming bars close as the chart's
        groups = {}
        for f in fine:
            groups.setdefault(f["bin"], []).append(f)
        for b in expect:
            g = groups[b["timestamp"]]
            assert (g[0]["open"], max(x["high"] for x in g), min(x["low"] for x in g), g[-1]["close"]) == \
                   (b["open"], b["high"], b["low"], b["close"])
            assert sum(x["volume"] for x in g) == pytest.approx(b["volume"])


def test_replay_builds_from_seconds_where_they_are_held(store):
    t0 = _ms(f"{DAYS[4]} 10:00")
    store.upsert_bars("massive", "SPCX", "second", 1, [{"t": t0 + i * 1000, "o": 20.0, "h": 20.1, "l": 19.9, "c": 20.0,
                                                        "v": 5.0, "n": 1} for i in range(600)])
    store.upsert_bars("massive", "SPCX", "minute", 1, [{"t": t0 + i * 60_000, "o": 20.0, "h": 20.1, "l": 19.9, "c": 20.0,
                                                        "v": 300.0, "n": 60} for i in range(10)])  # as its seconds
    app = ChartApp(store)
    app.window._levels.clear()
    assert app.window.replay_fine("*", "SPCX", "minute", t0 + 60_000) == "second"
    assert app.window.replay_fine("*", "SPCX", "minute", t0 - 86_400_000) == "minute"  # before the seconds
    assert app.window.replay_fine("*", "SPCX", "day", t0) == "minute"
    page = app.replay({"source": "*", "ticker": "SPCX", "period": "minute", "span": "1", "session": "extended",
                       "from": str(t0), "limit": "150"})
    assert page["fine"]["label"] == "1 sec" and len(page["bars"]) == 150
    assert {b["bin"] for b in page["bars"]} == {t0, t0 + 60_000, t0 + 120_000}
    assert [b["timestamp"] for b in page["complete"]] == [t0, t0 + 60_000]  # the third minute goes on next page


def test_the_routes(store):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ChartApp(store), [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        def get(path):
            c = HTTPConnection("127.0.0.1", port)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            return r.status, json.loads(r.read())

        status, body = get("/api/schemes")
        assert status == 200 and [s["key"] for s in body["schemes"]] == list(sc.SCHEMES)
        lo, hi = _ms(f"{DAYS[1]} 09:30"), _ms(f"{DAYS[4]} 15:00")
        status, body = get(f"/api/scheme?source=*&ticker=SPCX&period=minute&span=30&from={lo}&to={hi}&scheme=cusum")
        assert status == 200 and body["level"]["label"] == "30 min" and body["equity"]
        status, body = get(f"/api/scheme?source=*&ticker=SPCX&period=minute&span=30&from={lo}&to={hi}&scheme=nope")
        assert status == 400
        status, body = get(f"/api/scheme?source=*&ticker=SPCX&period=minute&span=15&from={lo}&to={hi}&scheme=cusum")
        assert status == 400 and "no level 15 minute" in body["error"]  # not a level the chart draws
        status, body = get(f"/api/replay?source=*&ticker=SPCX&period=minute&span=5&from={lo}&limit=10&unfiltered=1")
        assert status == 200 and len(body["bars"]) == 10 and body["more"]
    finally:
        httpd.shutdown()
