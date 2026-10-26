"""Candles a feed did not send at that interval are built here and marked derived; the feed's own
replace them when they arrive, and what was known when stays readable."""
from __future__ import annotations

import pandas as pd
import pytest

from massive_collate import derive
from massive_collate.gui.bars import BarEngine
from massive_collate.storage import Store, feeds

ET = "America/New_York"


def _ms(t: str, tz: str = ET) -> int:
    return int(pd.Timestamp(t, tz=tz).timestamp() * 1000)


def _bar(t: str, price: float, volume: float = 10.0, tz: str = ET) -> dict:
    return {"t": _ms(t, tz), "o": price, "h": price + 1, "l": price - 1, "c": price, "v": volume, "vw": price, "n": 1}


def _minutes(start: str, n: int, price0: float = 100.0, tz: str = ET) -> list[dict]:
    idx = pd.date_range(start, periods=n, freq="1min", tz=tz)
    return [{"t": int(t.timestamp() * 1000), "o": price0 + i, "h": price0 + i + 0.5, "l": price0 + i - 0.5,
             "c": price0 + i + 0.25, "v": 1.0, "vw": price0 + i, "n": 1} for i, t in enumerate(idx)]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db", check_same_thread=False)
    yield s
    s.close()


def _stored_at(monkeypatch, ms: int) -> None:
    monkeypatch.setattr(feeds, "_stored_ms", lambda: ms)


def test_the_feeds_own_bar_replaces_a_built_one_and_the_built_version_is_kept(store, monkeypatch):
    _stored_at(monkeypatch, 1_000)
    assert store.upsert_built_bars("alpaca", "BTCUSD", "minute", 1, [_bar("2026-10-20 10:00", 50.0)], 7) == 1
    _stored_at(monkeypatch, 2_000)
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, [_bar("2026-10-20 10:00", 51.0)])
    conn = store._bars("alpaca")
    assert conn.execute("SELECT COUNT(*) FROM built_bars").fetchone()[0] == 0
    assert conn.execute("SELECT close, stored_at, replaced_at, derivation FROM bar_revisions").fetchall() == [
        (50.0, 1_000, 2_000, 7)]
    loaded = store.load_bars(["BTCUSD"], "minute", source="alpaca")
    assert loaded["close"].tolist() == [51.0] and loaded["derived"].tolist() == [False]
    lo, hi = _ms("2026-10-20 10:00"), _ms("2026-10-20 10:01")
    assert store.bars_window(["alpaca"], "BTCUSD", "minute", lo, hi, as_of=1_500)["close"].tolist() == [50.0]
    assert store.bars_window(["alpaca"], "BTCUSD", "minute", lo, hi, as_of=2_500)["close"].tolist() == [51.0]


def test_nothing_is_built_where_the_feed_sent_its_own(store):
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, [_bar("2026-10-20 10:00", 51.0)])
    n = store.upsert_built_bars("alpaca", "BTCUSD", "minute", 1,
                                [_bar("2026-10-20 10:00", 50.0), _bar("2026-10-20 10:01", 52.0)], 7)
    assert n == 1
    window = store.bars_window(["alpaca"], "BTCUSD", "minute", 0, _ms("2026-10-21"))
    assert window["close"].tolist() == [51.0, 52.0] and window["derived"].tolist() == [0, 1]


def test_built_bars_from_before_the_rule_go_once(store):
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, [_bar("2026-10-20 10:00", 51.0)])
    conn = store._bars("alpaca")
    with conn:  # as an older build left it: both for the same minute
        conn.execute("INSERT INTO built_bars VALUES ('BTCUSD', 'minute', 1, ?, 50, 51, 49, 50, 10, 50, 1, 7, 5)",
                     (_ms("2026-10-20 10:00"),))
    assert store.drop_superseded_built("alpaca") == 1
    assert conn.execute("SELECT COUNT(*) FROM built_bars").fetchone()[0] == 0
    assert conn.execute("SELECT derivation FROM bar_revisions").fetchall() == [(7,)]


def test_frames_from_minutes_stock_days_are_the_regular_session():
    rows = pd.DataFrame(_minutes("2026-10-20 09:25", 12))  # 09:25 .. 09:36
    rows["ts"], rows["time"] = rows["t"], pd.to_datetime(rows["t"], unit="ms", utc=True).dt.tz_convert(ET)
    m = rows.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap",
                             "n": "transactions"})
    five = derive.aggregate(m, "minute", 5, "stock")
    assert [pd.Timestamp(b["t"], unit="ms", tz="UTC").tz_convert(ET).strftime("%H:%M") for b in five] == [
        "09:25", "09:30", "09:35"]
    assert five[1]["o"] == 105 and five[1]["c"] == 109.25 and five[1]["v"] == 5 and five[1]["n"] == 5
    (day,) = derive.aggregate(m, "day", 1, "stock")
    assert day["t"] == _ms("2026-10-20 00:00") and day["o"] == 105  # from the 09:30 open, stamped New York midnight
    (utc_day,) = derive.aggregate(m, "day", 1, "crypto")
    assert utc_day["t"] == _ms("2026-10-20 00:00", "UTC") and utc_day["o"] == 100  # the whole UTC day


def test_a_feeds_frames_are_built_once_marked_and_rebuilt_where_minutes_change(store, monkeypatch):
    with monkeypatch.context() as m:  # stored a while ago
        _stored_at(m, 1_000)
        store.upsert_bars("massive", "SPCX", "minute", 1,
                          _minutes("2026-10-19 09:30", 390) + _minutes("2026-10-20 09:30", 390))
        store.upsert_bars("massive", "SPCX", "day", 1, [_bar("2026-10-19 00:00", 99.0)])  # the feed sent day 1 only
    n = derive.build_series(store, "massive", "SPCX", "stock", log=lambda *_: None)
    conn = store._bars("massive")
    count = dict(conn.execute("SELECT timespan || multiplier, COUNT(*) FROM built_bars GROUP BY 1").fetchall())
    assert count == {"minute5": 156, "minute30": 26, "hour1": 14, "hour2": 8, "hour4": 4, "day1": 1} and n == 209
    assert derive.build_series(store, "massive", "SPCX", "stock", log=lambda *_: None) == 0  # nothing changed
    store.upsert_bars("massive", "SPCX", "minute", 1, [{**_minutes("2026-10-20 10:00", 1)[0], "h": 999.0}])
    assert derive.build_series(store, "massive", "SPCX", "stock", log=lambda *_: None) > 0
    high = conn.execute("SELECT high FROM built_bars WHERE timespan='minute' AND multiplier=5 AND ts=?",
                        (_ms("2026-10-20 10:00"),)).fetchone()[0]
    assert high == 999.0  # the correction reached the frames above it
    (d,) = store.derivations(kind="bars-from-bars")[:1]
    assert d["settings"]["days"] == "New York dates, regular session"


def test_the_chart_marks_what_was_built(store):
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, _minutes("2026-10-20 10:00", 3))
    store.upsert_built_bars("alpaca", "BTCUSD", "minute", 1, _minutes("2026-10-20 10:03", 2, 200.0), 7)
    eng = BarEngine(store)
    one = eng.bars("alpaca", "BTCUSD", "minute", 1, "extended")
    assert [b["derived"] for b in one["bars"]] == [False, False, False, True, True] and one["derivedFrom"] is None
    five = eng.bars("alpaca", "BTCUSD", "minute", 5, "extended")
    assert all(b["derived"] for b in five["bars"]) and five["derivedFrom"] == "1-minute bars"


# -- the feed's own candles are fetched where only built ones exist --------------------------------

def test_exchange_candles_are_published_as_each_minute_closes():
    from datetime import datetime as dt

    from massive_collate import freshness
    from massive_collate.config import PUBLIC_FEEDS

    now = dt(2026, 10, 24, 7, 30, 20, tzinfo=pd.Timestamp("2026-10-24", tz="UTC").tzinfo)
    exp = freshness.expected("crypto", PUBLIC_FEEDS["coinbase"][0], now)
    assert exp.latest == dt(2026, 10, 24, 7, 29, tzinfo=now.tzinfo)  # the newest closed minute, not yesterday


def test_a_feed_fetches_its_own_minutes_where_it_only_has_built_ones(store):
    from datetime import date, datetime as dt, timezone

    from massive_collate.config import PUBLIC_FEEDS
    from massive_collate.coverage import FeedProfile, plan

    cb = FeedProfile("coinbase", PUBLIC_FEEDS["coinbase"][0], frozenset({"crypto"}), 0, venues={"crypto": "coinbase"})
    feeds = {"coinbase": cb}
    store.watch("coinbase", ["BTCUSD"], "minute")
    now = dt(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
    t = lambda h: int(dt(2026, 10, 24, h, tzinfo=timezone.utc).timestamp() * 1000)
    store.add_coverage("coinbase", "BTCUSD", "minute", t(0), t(7))  # its own minutes to 07:00
    store.add_coverage("coinbase", "BTCUSD", "minute", t(7), t(8), origin="ticks")  # built live since
    store.add_coverage("coinbase", "BTCUSD", "trades", t(7), t(8))  # from its own stream
    gaps = plan(store, cb, feeds, "BTCUSD", "crypto", "minute", date(2026, 10, 24), now)
    assert (t(7), t(8)) in gaps or any(a <= t(7) and b >= t(8) for a, b in gaps)


def test_a_coarser_frame_is_fetched_only_as_far_as_its_bars_are_complete():
    from datetime import datetime as dt, timezone

    from massive_collate.config import PUBLIC_FEEDS
    from massive_collate.coverage import frontier_ms

    now = dt(2026, 10, 24, 7, 32, 20, tzinfo=timezone.utc)
    tier = PUBLIC_FEEDS["coinbase"][0]
    t = lambda h, m: int(dt(2026, 10, 24, h, m, tzinfo=timezone.utc).timestamp() * 1000)
    assert frontier_ms("crypto", tier, now, "minute") == t(7, 32)
    assert frontier_ms("crypto", tier, now, "minute:5") == t(7, 30)
    assert frontier_ms("crypto", tier, now, "hour") == t(7, 0)


def test_only_seconds_minutes_and_days_are_fetched_every_other_frame_is_built(store):
    from datetime import datetime as dt, timedelta

    from massive_collate.collector import ET, sync
    from massive_collate.config import TIERS

    from .conftest import FakeSource

    now = dt(2026, 10, 21, 12, 0, tzinfo=ET)
    day = (now - timedelta(days=1)).replace(hour=10, minute=0)
    t = lambda m: int((day + timedelta(minutes=m)).timestamp() * 1000)
    minutes = [{"t": t(i), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1.0} for i in range(10)]
    src = FakeSource(entitled=("minute",), bars={"minute": minutes, "day": [],
                                                 "minute:5": [{"t": t(0), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 5.0}],
                                                 "hour": [{"t": t(0), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 10.0}]})
    src.frames = (("minute", 5), ("hour", 1))
    store.watch("fake", ["SPCX"], "minute")
    store.upsert_built_bars("fake", "SPCX", "minute", 5, [{"t": t(0), "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0}], 7)
    sync(src, store, TIERS["free"], now, log=lambda m: None)
    conn = store._bars("fake")
    got = dict(conn.execute("SELECT timespan || multiplier, COUNT(*) FROM bars GROUP BY 1").fetchall())
    # only 1-second, 1-minute and daily bars are fetched: the feed's own 5-minute and hourly are not
    assert "minute5" not in got and "hour1" not in got
    assert {c[3] for c in src.bar_calls} <= {"minute", "day"}
    built = conn.execute("SELECT ts FROM built_bars WHERE timespan='minute' AND multiplier=5").fetchall()
    assert (t(0),) in built and (t(5),) in built  # built here from the minutes


def test_seconds_can_go_on_past_the_second_layer_interpolated_from_minutes(store, monkeypatch):
    from massive_collate.gui import bars as gui_bars
    from massive_collate.gui.bars import BarEngine
    from massive_collate.gui.profile_gaps import uncovered

    monkeypatch.setattr(gui_bars, "INTERPOLATED", frozenset({"BTCUSD"}))  # off for every symbol now

    assert uncovered((0, 100), [(10, 20), (15, 30), (50, 60)]) == [(0, 10), (30, 50), (60, 100)]
    t0 = _ms("2026-10-20 10:00", "UTC")
    mins = [{"t": t0 + i * 60_000, "o": 100.0 + i, "h": 100.5 + i, "l": 99.5 + i, "c": 100.0 + i, "v": 1.0,
             "vw": 100.0 + i} for i in range(10)]
    mins += [{"t": t0 + (20 + i) * 60_000, "o": 200.0, "h": 200.0, "l": 200.0, "c": 200.0, "v": 1.0, "vw": 200.0}
             for i in range(3)]  # after a 10-minute gap
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, mins)
    secs = [{"t": t0 + 22 * 60_000 + i * 1000, "o": 200.0, "h": 200.0, "l": 200.0, "c": 200.0, "v": 0.1, "n": 1}
            for i in range(60)]  # the last minute in seconds
    store.upsert_built_bars("coinbase", "BTCUSD", "second", 1, secs, 7)
    store.add_coverage("coinbase", "BTCUSD", "second", t0 + 22 * 60_000, t0 + 23 * 60_000, origin="ticks")
    page = BarEngine(store).bars("coinbase", "BTCUSD", "second", 1, "extended", limit=5000)
    bars = {b["timestamp"]: b for b in page["bars"]}
    real = [b for b in bars.values() if not b.get("interp")]
    assert len(real) == 60
    mid = bars[t0 + 60_000]  # half way between minute 0's middle (100 at 0:30) and minute 1's (101 at 1:30)
    assert mid["interp"] and mid["derived"] and mid["close"] == pytest.approx(100.5) and mid["volume"] == 0
    assert t0 + 15 * 60_000 not in bars  # nothing drawn across the 10-minute gap
    assert "interpolated" in page


def test_frames_are_only_built_upward_seconds_to_30s_and_days_to_weeks_months_years(store):
    secs = [{"t": _ms("2026-10-20 10:00") + i * 1000, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i,
             "v": 1, "vw": 100 + i, "n": 1} for i in range(65)]
    store.upsert_built_bars("alpaca", "BTCUSD", "second", 1, secs, 1)
    days = [_bar(f"2026-{m:02d}-{d:02d} 00:00", 100.0 + d, tz="UTC") for m, d in ((9, 28), (9, 30), (10, 1), (10, 2))]
    store.upsert_bars("alpaca", "BTCUSD", "day", 1, days)
    store.watch("alpaca", ["BTCUSD"], "trades", asset="crypto")
    store.upsert_bars("tiingo", "KO", "day", 1, [_bar("2026-10-19 00:00", 70.0)])  # a feed with daily bars only
    derive.run(store, log=lambda *_: None)
    conn = store._bars("alpaca")
    count = dict(conn.execute("SELECT timespan || multiplier, COUNT(*) FROM built_bars GROUP BY 1").fetchall())
    assert count["second30"] == 3 and count["week1"] == 1 and count["month1"] == 2 and count["year1"] == 1
    months = conn.execute("SELECT ts FROM built_bars WHERE timespan='month' ORDER BY ts").fetchall()
    assert [m for (m,) in months] == [_ms("2026-09-01 00:00", "UTC"), _ms("2026-10-01 00:00", "UTC")]
    kinds = {k for (k,) in store._bars("tiingo").execute("SELECT DISTINCT timespan FROM built_bars")}
    assert kinds == {"week", "month", "year"}  # nothing finer than what it sent: gaps stay gaps
    assert store.kv_get(derive.PROGRESS_KEY) == ""  # cleared when the run ends


def test_multi_hour_bins_start_at_midnight_in_the_day_zone():
    rows = pd.DataFrame(_minutes("2026-10-20 09:30", 300))  # 09:30 .. 14:29 New York
    rows["ts"], rows["time"] = rows["t"], pd.to_datetime(rows["t"], unit="ms", utc=True).dt.tz_convert(ET)
    m = rows.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap",
                             "n": "transactions"})
    four = derive.aggregate(m, "hour", 4, "stock")
    assert [pd.Timestamp(b["t"], unit="ms", tz="UTC").tz_convert(ET).strftime("%H:%M") for b in four] == ["08:00", "12:00"]
    utc = derive.aggregate(m, "hour", 4, "crypto")  # 13:30-18:29 UTC: bins at 12:00 and 16:00 UTC
    assert [pd.Timestamp(b["t"], unit="ms", tz="UTC").strftime("%H:%M") for b in utc] == ["12:00", "16:00"]


def test_bars_a_feed_sent_at_the_charts_interval_are_prime(store):
    """Alpaca sends 5-minute bars: those show unmarked; 5-minute bars built from minutes where it sent
    none stay derived; and a feed's clock hours never land on a stock chart's hours from the open."""
    store.upsert_bars("alpaca", "SPCX", "minute", 1, _minutes("2026-10-20 09:30", 15))  # 09:30 .. 09:44
    store.upsert_bars("alpaca", "SPCX", "minute", 5, [_bar("2026-10-20 09:35", 500.0, volume=99.0)])
    store.upsert_bars("alpaca", "SPCX", "hour", 1, [_bar("2026-10-20 09:00", 700.0)])
    eng = BarEngine(store)
    five = eng.bars("alpaca", "SPCX", "minute", 5, "regular")["bars"]
    assert [(b["close"], b["derived"]) for b in five] == [(104.25, True), (500.0, False), (114.25, True)]
    assert five[1]["volume"] == 99.0  # the feed's own bar, whole
    hour = eng.bars("alpaca", "SPCX", "hour", 1, "regular")["bars"]
    assert all(b["derived"] for b in hour) and hour[0]["close"] != 700.0  # 09:30-aligned: the 09:00 bar stays out


def test_the_status_bar_hears_what_is_being_built_while_it_runs(store):
    import json
    from datetime import datetime, timezone

    from massive_collate.gui.activity import derive_progress

    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    at = int(now.timestamp() * 1000)
    store.kv_set(derive.PROGRESS_KEY, json.dumps({"source": "alpaca", "ticker": "MRVL", "from": "1m",
                                                  "to": ["5m", "1h"], "n": 3, "of": 9, "at": at - 5_000}))
    assert derive_progress(store, now)["to"] == ["5m", "1h"]
    store.kv_set(derive.PROGRESS_KEY, json.dumps({"n": 3, "of": 9, "at": at - 600_000}))
    assert derive_progress(store, now) is None  # not heard from in minutes: over
    store.kv_set(derive.PROGRESS_KEY, "")
    assert derive_progress(store, now) is None
