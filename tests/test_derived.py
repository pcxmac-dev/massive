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
    assert count == {"minute5": 156, "hour1": 14, "day1": 1} and n == 171
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
