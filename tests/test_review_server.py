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


# -- 34: paper day orders wait for their session; a partial feed's bars are looked at again ------
class Tape:
    """Minute bars from 09:30 a test lays down, each settled or not (only a feed of one exchange's
    share holds it); the book reads them as it would the store's."""

    T0 = _ms("2026-10-14 09:30")

    def __init__(self):
        self.bars: dict[int, tuple] = {}

    def add(self, i: int, low: float, settled: bool = True):
        self.bars[self.T0 + i * MIN] = (10.0, 10.2, low, 10.0, settled)

    def after(self, source, ticker, at, until):
        rows = [(t, *b) for t, b in sorted(self.bars.items()) if at <= t < until]
        return pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "settled"]), MIN

    def last(self, source, ticker):
        return 10.0


@pytest.fixture
def tape_book():
    tape = Tape()
    book = PaperBook(sqlite3.connect(":memory:"), tape.after, tape.last)  # orders good till cancelled: no close
    book.tape = tape
    return book


def test_a_partial_feeds_bars_are_looked_at_again_when_the_whole_market_replaces_them(tape_book):
    book, tape, t0 = tape_book, tape_book.tape, Tape.T0
    o = book.place("SPCX", "*", "buy", 1, "limit", limit=9.5, tif="gtc", now_ms=t0)
    for i in range(3):
        tape.add(i, 9.8)
    for i in (3, 4):
        tape.add(i, 9.7, settled=False)  # IEX's share of the session, until a sync stores the whole market's
    book.match(t0 + 10 * MIN)
    row = book.order(o["id"])
    assert row["status"] == "working" and row["looked_to"] == t0 + 3 * MIN  # not past IEX's bars
    tape.add(3, 9.8)
    tape.add(4, 9.4)  # the whole market traded lower than IEX did
    book.match(t0 + 10 * MIN)
    row = book.order(o["id"])
    assert (row["status"], row["done_at"], row["fill_px"]) == ("filled", t0 + 4 * MIN, 9.5)


def test_a_partial_feeds_bar_that_reaches_the_order_fills_it(tape_book):
    book, tape, t0 = tape_book, tape_book.tape, Tape.T0
    o = book.place("SPCX", "*", "buy", 1, "limit", limit=9.5, tif="gtc", now_ms=t0)
    tape.add(0, 9.8, settled=False)
    tape.add(1, 9.4, settled=False)  # the read goes on past bars it may look at again
    book.match(t0 + 10 * MIN)
    assert book.order(o["id"])["status"] == "filled" and book.order(o["id"])["done_at"] == t0 + MIN


def test_a_day_order_waits_for_the_whole_markets_session_before_it_expires(tmp_path, monkeypatch):
    day1, day2 = "2026-10-19", "2026-10-20"
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day1, 10.0, "09:30", "19:59", every=1))
        s.upsert_bars("iex", "SPCX", "minute", 1, _minutes(day2, 10.0, "09:30", "11:00", every=1))  # the live recorder's
        s.watch("massive", ["SPCX"], "minute")
        app = ChartApp(s)
        clock = {"now": _ms(f"{day2} 11:00")}
        monkeypatch.setattr(paper, "_now", lambda: clock["now"])
        bars, step = app._fill_bars("*", "SPCX", _ms(f"{day1} 15:00"), clock["now"])
        settled = dict(zip(bars["t"].tolist(), bars["settled"].tolist()))
        assert step == MIN and settled[_ms(f"{day1} 15:59")] and not settled[_ms(f"{day2} 10:00")]
        assert app._stored_to("*", "SPCX") == _ms(f"{day1} 19:59") + MIN  # the whole market's, not IEX's
        o = app.trade_place({"ticker": "SPCX", "side": "buy", "qty": 1, "type": "limit", "limit": 1.0})
        clock["now"] = _ms(f"{day2} 18:00")  # past the close; the sync has not stored the session
        assert next(r for r in app.trading()["orders"] if r["id"] == o["id"])["status"] == "working"
        s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day2, 10.0, "09:30", "16:30", every=1))
        view = app.trading()
        row = next(r for r in view["orders"] if r["id"] == o["id"])
        assert (row["status"], row["done_at"]) == ("expired", _ms(f"{day2} 16:00"))


# -- 35: slow work off the shared lock -------------------------------------------------------------
def test_paper_and_the_markets_do_their_slow_work_off_the_shared_lock(store, monkeypatch):
    from massive_collate.gui import world

    app = ChartApp(store)

    def lock_free() -> bool:  # whether another thread could take the shared lock now
        got = []

        def take():
            if app._lock.acquire(timeout=1):
                app._lock.release()
                got.append(True)
        t = threading.Thread(target=take)
        t.start()
        t.join()
        return got == [True]
    seen = []
    monkeypatch.setattr(world, "world_markets", lambda *a, **k: seen.append(lock_free()) or [])
    app.markets()
    app.announcements()
    assert seen == [True, True]  # ~70 calendars built with the lock free
    answered = []
    with app._lock:  # the layouts, watchlists or Fetcher busy with it
        t = threading.Thread(target=lambda: answered.append(app.trading()))
        t.start()
        t.join(20)
    assert answered and answered[0]["broker"] == "paper"


# -- 36: a whole history is read outside the engine's lock, once -------------------------------------
def test_a_whole_history_is_read_outside_the_engines_lock_and_once(store, monkeypatch):
    eng = BarEngine(store)
    started, release, loads, got = threading.Event(), threading.Event(), [], []
    real = eng._load

    def slow(*a, **k):
        loads.append(a[:2])
        started.set()
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(eng, "_load", slow)
    readers = [threading.Thread(target=lambda: got.append(eng.series("massive", "SPCX"))) for _ in range(2)]
    readers[0].start()
    assert started.wait(10)
    readers[1].start()  # the same series asked for meanwhile: waits for that read
    assert eng.lock.acquire(timeout=5)  # the engine's lock is free while the history is read
    eng.lock.release()
    assert eng.fingerprint("massive", "SPCX")  # the live stream's check answers
    release.set()
    for t in readers:
        t.join(10)
    assert loads == [("massive", "SPCX")] and len(got) == 2 and got[0] is got[1] and len(got[0].frame)
    assert eng.series("*", "SPCX").ticker == "SPCX"  # a merged chart from parts read that way


# -- 37: the level 2 profile of a past bar ------------------------------------------------------------
@pytest.mark.parametrize("period, span, at", [("hour", 1, f"{DAYS[2]} 10:30"), ("hour", 1, f"{DAYS[2]} 11:47"),
                                              ("minute", 5, f"{DAYS[2]} 12:35"), ("hour", 4, f"{DAYS[1]} 13:30"),
                                              ("minute", 30, f"{DAYS[3]} 09:44"), ("day", 1, f"{DAYS[1]} 00:00")])
def test_the_level_2_profile_of_a_past_bar_is_the_series_paths(store, period, span, at):
    engine = BarEngine(store)
    whole = engine.profile("*", "SPCX", period, span, "extended", at_ms=_ms(at))
    windowed = WindowReader(engine).profile("*", "SPCX", period, span, "extended", at_ms=_ms(at))
    assert not whole["newest"] and whole["rows"]
    for k in ("bar", "day", "last", "newest", "rows", "step", "basis"):
        assert windowed[k] == whole[k], k


# -- 39: a replay reads the view it is asked for ------------------------------------------------------
def test_a_replay_reads_the_view_it_is_asked_for(tmp_path):
    s = _spiked(tmp_path)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ChartApp(s), [0]))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            for view, name in (("0", "filtered"), ("1", "unfiltered")):
                status, body = _call(port, f"/api/replay?source=massive&ticker=SPCX&period=minute&span=5&session=extended"
                                           f"&from={_ms('2026-10-19 17:00')}&limit=30&unfiltered={view}")
                assert status == 200 and body["view"] == name
                assert (max(b["high"] for b in body["bars"]) == 75.0) == (view == "1")
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        s.close()


# -- low: "All data" on the series path -----------------------------------------------------------------
def test_all_data_reaches_the_series_path_and_its_caches_keep_the_views_apart(tmp_path):
    s = _spiked(tmp_path)
    try:
        app = ChartApp(s)
        highs: dict[str, list] = {}
        for view in ("1", "0", "1", "0"):
            for span in ("1", "5"):
                q = {"source": "massive", "ticker": "SPCX", "period": "minute", "span": span, "session": "extended",
                     "to": str(_ms("2026-10-19 17:30")), "limit": "40"}  # no auto: the whole-history series path
                with app._view({"unfiltered": view}):
                    highs.setdefault(view, []).append(max(b["high"] for b in app.bars(q)["bars"]))
        assert highs["1"] == [75.0] * 4 and max(highs["0"]) < 51
    finally:
        s.close()


# -- low: crypto and forex hours on UTC hours ---------------------------------------------------------
def test_crypto_hours_are_binned_on_utc_hours_as_derive_builds_them(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        t0 = _ms("2026-01-15 00:00", tz="UTC")  # winter: New York's midnight is 05:00 UTC
        s.upsert_bars("coinbase", "BTCUSD", "minute", 1, [
            {"t": t0 + i * 10 * MIN, "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.5 + i, "v": 1.0, "n": 1}
            for i in range(2 * 144)])
        s.watch("coinbase", ["BTCUSD"], "minute", asset="crypto")
        derive.run(s, log=lambda *_: None)
        engine = BarEngine(s)
        app = ChartApp(s)
        for span in (2, 4):
            step = span * 3_600_000
            whole = engine.bars("coinbase", "BTCUSD", "hour", span, "extended", limit=1000)["bars"]
            paged = WindowReader(engine).page("coinbase", "BTCUSD", "hour", span, "extended", limit=1000)["bars"]
            built = s.load_bars(["BTCUSD"], "hour", span, source="coinbase")
            assert whole and all(b["timestamp"] % step == 0 for b in whole)  # from UTC midnight, as its days
            assert [b["timestamp"] for b in paged] == [b["timestamp"] for b in whole]
            built = sorted(pd.DatetimeIndex(built["time"]).as_unit("ms").asi8.tolist())
            assert built == [b["timestamp"] for b in whole]  # derive.py's, on the same times
            fut = app.future({"source": "coinbase", "ticker": "BTCUSD", "period": "hour", "span": str(span),
                              "auto": "1", "count": "20"})
            assert len(fut["timestamps"]) == 20 and all(t % step == 0 for t in fut["timestamps"])


# -- low: key statistics of a market open every day -------------------------------------------------
def test_key_statistics_of_crypto_count_calendar_days_and_utc_years():
    idx = pd.date_range("2025-01-01", periods=400, freq="D", tz="UTC").tz_convert(ET)  # UTC days, as crypto's are
    c = np.arange(400, dtype=float) + 100
    d = pd.DataFrame({"open": c - 1, "high": c + 2, "low": c - 2, "close": c, "volume": 10.0}, index=idx)
    s = key_stats(d, "crypto")
    assert s["range52w"]["sessions"] == 365 and s["range52w"]["low"] == c[-365] - 2
    assert s["performance"]["1W"] == pytest.approx((c[-1] / c[-8] - 1) * 100)
    assert s["performance"]["1Y"] == pytest.approx((c[-1] / c[-366] - 1) * 100)
    # 2026-01-01 00:00 UTC is 2025-12-31 19:00 in New York: still the new year's first day
    assert s["performance"]["YTD"] == pytest.approx((c[-1] / c[364] - 1) * 100)
    rets = np.diff(np.log(c[-31:]))
    assert s["volatility30"] == pytest.approx(np.std(rets, ddof=1) * math.sqrt(365) * 100)
    assert key_stats(d)["range52w"]["sessions"] == 252  # a stock's year as before


# -- low: a scheme leaves out the bar still forming ---------------------------------------------------
def test_a_scheme_leaves_out_the_bar_still_forming(tmp_path, monkeypatch):
    day = "2026-10-20"
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, 10.0, "09:30", "10:57", every=1))
        s.watch("massive", ["SPCX"], "minute")
        app = ChartApp(s)
        q = {"source": "massive", "ticker": "SPCX", "period": "minute", "span": "5", "session": "regular",
             "from": str(_ms(f"{day} 09:30")), "to": str(_ms(f"{day} 10:55")), "scheme": "cusum"}
        monkeypatch.setattr(window_module, "_now_ms", lambda: _ms(f"{day} 10:57") + 30_000)
        assert app.scheme(q)["equity"][-1][0] == _ms(f"{day} 10:50")  # 10:55's bar runs to 11:00: not closed
        s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, 10.0, "10:58", "10:59", every=1))
        monkeypatch.setattr(window_module, "_now_ms", lambda: _ms(f"{day} 11:00") + 5_000)
        assert app.scheme(q)["equity"][-1][0] == _ms(f"{day} 10:55")
        derive.run(s, log=lambda *_: None)  # the day so far, built from its minutes
        today = {**q, "period": "day", "span": "1", "from": str(_ms(f"{day} 00:00")), "to": str(_ms(f"{day} 00:00"))}
        assert app.scheme(today)["equity"] == []  # the day is not over
        monkeypatch.setattr(window_module, "_now_ms", lambda: _ms("2026-10-21 05:00"))
        assert [e[0] for e in app.scheme(today)["equity"]] == [_ms(f"{day} 00:00")]  # over


# -- low: the range-bar guard follows the path inside each bar ----------------------------------------
def test_range_bars_count_the_path_inside_each_bar_not_only_the_closes():
    idx = pd.date_range("2026-10-20 09:30", periods=1000, freq="1min", tz=ET)
    flat = pd.DataFrame({"open": 100.0, "high": 110.0, "low": 90.0, "close": 100.0, "volume": 1.0, "turnover": 100.0,
                         "trades": 1}, index=idx)  # wide bars whose closes never move
    assert charttypes._path_length(flat.iloc[:2]) == pytest.approx(2 * (10 + 20 + 10))
    with pytest.raises(ValueError, match="too small"):
        charttypes.range_bars(flat, 0.0001)  # four hundred million bars from closes that "never move"
    assert len(charttypes.range_bars(flat.iloc[:3], 5.0))


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
