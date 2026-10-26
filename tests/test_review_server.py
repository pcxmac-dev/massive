"""Fixes from the code review (CODE_REVIEW.md) in the chart server: a level checked before anything
is read (1), a stretch's read capped (7), requests from other sites refused (32), the status answer
kept once per feed (33), paper day orders and a partial feed's bars (34), slow work off the shared
lock (35), whole histories read outside the engine's lock (36), a past bar's level 2 from the whole
bar (37), a replay's view (39), and the low ones: crypto's hours on UTC hours, key statistics of a
market open every day, a scheme without the forming bar, the range-bar guard, errors answered, and
"All data" on the series path."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest

from massive_collate import baddata, derive
from massive_collate.gui import charttypes
from massive_collate.gui import server as server_module
from massive_collate.gui import window as window_module
from massive_collate.gui.bars import BarEngine
from massive_collate.gui.server import WRITE_HEADER, ChartApp, make_handler
from massive_collate.gui.stats import key_stats
from massive_collate.gui.window import WindowReader
from massive_collate.storage import Store
from massive_collate.trading import paper
from massive_collate.trading.paper import PaperBook

ET = "America/New_York"
MIN = 60_000
DAYS = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")


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
        s.upsert_bars("massive", "SPCX", "day", 1, [{"t": _ms(f"{d} 00:00"), "o": 10.0 + i, "h": 10.2 + i, "l": 9.9 + i,
                                                     "c": 10.1 + i, "v": 1e6, "vw": 10.05 + i, "n": 900}
                                                    for i, d in enumerate(DAYS[:4])])
        for feed in ("massive", "alpaca"):
            s.watch(feed, ["SPCX"], "minute")
        derive.run(s, log=lambda *_: None)
        yield s


@pytest.fixture
def served(store):
    app = ChartApp(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _call(port: int, path: str, method: str = "GET", headers: dict | None = None, body=None):
    c = HTTPConnection("127.0.0.1", port, timeout=30)
    c.request(method, path, body=json.dumps(body) if body is not None else None,
              headers={"Host": f"127.0.0.1:{port}", **({"Content-Type": "application/json"} if body is not None else {}),
                       **(headers or {})})
    r = c.getresponse()
    data = r.read()
    return r.status, json.loads(data) if r.getheader("Content-Type") == "application/json" else data


def _raise(err: Exception):
    def fail(*_a, **_k):
        raise err
    return fail


def _spiked(tmp_path) -> Store:
    """Two days of a stock's minutes, one of them bad data (pruned: left out of the filtered view)."""
    s = Store(tmp_path / "b.db", check_same_thread=False)
    s.watch("massive", ["SPCX"], "minute")
    for day in ("2026-10-19", "2026-10-20"):
        for start, end, volume in (("04:00", "09:29", 100), ("09:30", "15:59", 10_000), ("16:00", "19:59", 100)):
            rows = _minutes(day, 50.0, start, end, every=1)
            s.upsert_bars("massive", "SPCX", "minute", 1, [{**r, "v": float(volume)} for r in rows])
    t = _ms("2026-10-19 17:08")
    s.upsert_bars("massive", "SPCX", "minute", 1, [{"t": t, "o": 50.0, "h": 75.0, "l": 30.0, "c": 50.01, "v": 120.0,
                                                    "vw": 50.0, "n": 3}])  # a spike without volume: bad data
    assert baddata.scan_series(s, "massive", "SPCX", "minute", "stock") == 1
    return s


# -- 1: a level the chart does not draw is refused before anything is read -----------------------
@pytest.mark.parametrize("route", ["bars", "stream", "profile", "future", "range", "scheme", "replay"])
@pytest.mark.parametrize("span", ["0", "-1", "1000000", "15"])
def test_a_level_the_chart_does_not_draw_is_refused(served, route, span):
    _, port = served
    lo, hi = _ms(f"{DAYS[1]} 10:00"), _ms(f"{DAYS[2]} 15:00")
    status, body = _call(port, f"/api/{route}?source=*&ticker=SPCX&period=minute&span={span}&auto=1&since={lo}"
                               f"&from={lo}&to={hi}&scheme=cusum")
    assert status == 400 and f"no level {span} minute" in body["error"]  # span 0 paged forever: no answer at all


def test_a_window_of_no_width_is_refused_not_paged_forever(store):
    reader = WindowReader(BarEngine(store))
    done = []
    t = threading.Thread(target=lambda: done.append(pytest.raises(ValueError, reader.page, "*", "SPCX", "minute", 0,
                                                                  "extended", to_ms=_ms(f"{DAYS[2]} 12:00"))),
                         daemon=True)
    t.start()
    t.join(10)
    assert done and "no level" in str(done[0].value)


# -- 7: a stretch's read is estimated, and capped, before it is read -------------------------------
def test_a_stretch_too_long_for_its_level_is_refused_before_it_is_read(store, monkeypatch):
    app = ChartApp(store)
    monkeypatch.setattr(app.window, "_built", _raise(AssertionError("the stretch was read")))
    # a highlight drawn over ten years, then zoomed to seconds
    q = {"source": "*", "ticker": "SPCX", "period": "second", "span": "1", "session": "regular",
         "from": str(_ms("2016-10-12 09:30")), "to": str(_ms(f"{DAYS[-1]} 16:00"))}
    with pytest.raises(ValueError, match="the highlight is too long for 1 sec bars .* zoom out to a coarser level"):
        app.range_stats(q)
    with pytest.raises(ValueError, match="this stretch is too long for 1 sec bars"):
        app.profile(q)  # the highlight's profile
    with pytest.raises(ValueError, match="coarser level"):
        app.scheme({**q, "scheme": "cusum"})
    # 30-second bars read thirty times their number in seconds: refused at a thirtieth of the stretch
    days = window_module.STRETCH_READ / window_module.per_day("stock", "second", 1, "extended")
    lo = _ms(f"{DAYS[0]} 09:30")
    with pytest.raises(ValueError, match="30 sec"):
        app.range_stats({**q, "span": "30", "from": str(lo), "to": str(lo + int(1.2 * days * 86_400_000))})
    assert window_module.read_estimate("stock", "hour", 1, 0, 20 * 365 * 86_400_000) < window_module.STRETCH_READ


def test_a_stretch_within_the_cap_still_answers(served):
    _, port = served
    lo, hi = _ms(f"{DAYS[1]} 10:00"), _ms(f"{DAYS[2]} 15:00")
    status, body = _call(port, f"/api/range?source=*&ticker=SPCX&period=minute&span=5&session=regular&from={lo}&to={hi}")
    assert status == 200 and body["bars"]


# -- 32: requests from other sites; the row counts bounded -----------------------------------------
def test_requests_from_other_sites_are_refused_and_the_apps_own_pass(served):
    app, port = served
    own = f"http://127.0.0.1:{port}"
    for ok in ({}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},  # the address bar
               {"Sec-Fetch-Site": "same-origin", "Origin": own}):
        assert _call(port, "/api/watchlists", headers=ok)[0] == 200, ok
    for bad in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "http://evil.example"},
                {"Origin": "null"}, {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:1"}):
        assert _call(port, "/api/inventory?ticker=ZZZ", headers=bad)[0] == 403, bad  # an <img> on another site
    assert not app._counting and "ZZZ" not in app._row_counts  # nothing was started
    # writes: the header is still needed, and from another site not even with it
    # (refused without a body: one left unread can reset the connection before the answer is read)
    assert _call(port, "/api/watchlists", "PUT", {WRITE_HEADER: "1", "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _call(port, "/api/watchlists", "PUT", {"Origin": own})[0] == 403
    lists = {"lists": [{"name": "Mine", "tickers": ["SPCX"]}]}
    assert _call(port, "/api/watchlists", "PUT", {WRITE_HEADER: "1", "Origin": own}, lists)[0] == 200
    assert _call(port, "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 200  # the page itself is not data


def test_row_counts_are_made_a_few_at_a_time_and_a_bounded_number_kept(store, monkeypatch):
    from massive_collate import inventory

    monkeypatch.setattr(server_module, "ROW_COUNTS_KEPT", 3)
    release, started = threading.Event(), []

    def slow(reader, ticker):
        started.append(ticker)
        release.wait(10)
        return {}
    monkeypatch.setattr(inventory, "row_counts", slow)
    app = ChartApp(store)
    tickers = [f"T{i}" for i in range(8)]
    for t in tickers:
        app.inventory(t)  # a page asking for one ticker after another
    for _ in range(500):
        if len(started) == server_module.COUNTING_AT_ONCE:
            break
        time.sleep(0.01)
    time.sleep(0.1)
    assert len(app._counting) == len(started) == server_module.COUNTING_AT_ONCE  # the rest when asked again
    release.set()
    for t in tickers:  # each asked again until its count starts
        for _ in range(500):
            app.inventory(t)
            if t in started:
                break
            time.sleep(0.01)
    for _ in range(500):
        if not app._counting:
            break
        time.sleep(0.01)
    assert set(started) == set(tickers) and len(app._row_counts) == 3  # the symbols counted last


# -- 33: the status answer, once per feed ------------------------------------------------------------
def test_the_status_answer_is_kept_once_per_feed_for_known_feeds(store, monkeypatch):
    made = []

    def answer(store, settings, path, source, log_lines=200, focus=None, **_k):
        made.append((source, log_lines, focus))
        return {"source": source}
    monkeypatch.setattr(server_module.activity, "activity", answer)
    app = ChartApp(store)
    monkeypatch.setattr(app, "feeds", lambda: [])
    for source in ("massive", "x" * 60, "nope", "alpaca"):
        for lines in ("10", "-5", "999999"):
            assert app.activity({"source": source, "lines": lines})["source"] in ("massive", "alpaca")
    assert set(app._activity) == {"massive", "alpaca"}  # free text made no entries of its own
    assert {s for s, _, _ in made} == {"massive", "alpaca"} and all(1 <= n <= 1000 for _, n, _ in made)
    monkeypatch.setattr(server_module, "ACTIVITY_TTL_S", 0.0)
    time.sleep(0.01)
    app.activity({"source": "massive"})
    assert set(app._activity) == {"massive"}  # the expired one dropped


# -- low: an unexpected error is answered, not dropped ------------------------------------------------
def test_an_unexpected_error_is_answered_not_dropped(served, monkeypatch, capsys):
    app, port = served
    monkeypatch.setattr(app, "layouts", _raise(RuntimeError("boom")))
    status, body = _call(port, "/api/layouts")
    assert status == 500 and "RuntimeError: boom" in body["error"]
    monkeypatch.setattr(app, "layouts", _raise(sqlite3.OperationalError("database is locked")))
    status, body = _call(port, "/api/layouts")
    assert status == 503 and "busy" in body["error"]
    monkeypatch.setattr(app, "save_watchlists", _raise(RuntimeError("boom")))
    status, body = _call(port, "/api/watchlists", "PUT", {WRITE_HEADER: "1"}, {"lists": []})
    assert status == 500 and "boom" in body["error"]
    assert "RuntimeError: boom" in capsys.readouterr().err  # logged
