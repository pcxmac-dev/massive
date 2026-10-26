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
