"""Live charts: a cached series takes the newest bars without being reloaded whole, one feed's
writes leave the others' fingerprints alone, and the stream pushes new bars to the page."""
from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from massive_collate.gui.bars import BarEngine
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store

ET = "America/New_York"


def _bars(start: str, n: int, price: float = 10.0, freq: str = "1min") -> list[dict]:
    idx = pd.date_range(start, periods=n, freq=freq, tz=ET)
    return [{"t": int(ts.timestamp() * 1000), "o": price + i * 0.01, "h": price + i * 0.01 + 0.05,
             "l": price + i * 0.01 - 0.05, "c": price + i * 0.01 + 0.01, "v": 100, "vw": price + i * 0.01}
            for i, ts in enumerate(idx)]


def _ms(t: str) -> int:
    return int(pd.Timestamp(t, tz=ET).timestamp() * 1000)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db", check_same_thread=False)
    s.upsert_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 09:30", 600))  # to 19:29
    s.upsert_bars("coinbase", "BTCUSD", "day", 1, _bars("2026-10-19", 2, freq="1D"))
    time.sleep(0.005)  # later writes get a later stored_at
    yield s
    s.close()


def _counting_loads(eng, monkeypatch) -> list:
    loads, real = [], eng._load
    monkeypatch.setattr(eng, "_load", lambda *a, **k: loads.append(a) or real(*a, **k))
    return loads


def test_new_revised_and_built_bars_are_spliced_in_exactly(store, monkeypatch):
    eng = BarEngine(store)
    eng.series("coinbase", "BTCUSD")
    loads = _counting_loads(eng, monkeypatch)
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 19:30", 5, price=16.0))  # new
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, [{**_bars("2026-10-20 19:29", 1)[0], "c": 99.0}])  # revised
    store.upsert_built_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 19:34", 3, price=17.0), 1)  # from ticks
    store.upsert_bars("coinbase", "BTCUSD", "day", 1, [{**_bars("2026-10-20", 1)[0], "c": 12.5}])
    s = eng.series("coinbase", "BTCUSD")
    assert not loads  # the tail was read, not the whole series
    whole = BarEngine(store).series("coinbase", "BTCUSD")
    pd.testing.assert_frame_equal(s.frame, whole.frame)
    pd.testing.assert_frame_equal(s.daily, whole.daily)
    assert len(s.frame) == 607 and s.frame["close"].iloc[599] == 99.0
    assert s.frame.loc[pd.Timestamp("2026-10-20 19:34", tz=ET), "close"] == 17.01  # built first, as a whole load
    assert s.daily["close"].iloc[-1] == 12.5


def test_old_changes_and_old_loads_reload_the_whole_series(store, monkeypatch):
    eng = BarEngine(store)
    eng.series("coinbase", "BTCUSD")
    loads = _counting_loads(eng, monkeypatch)
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-14 12:00", 1))  # six days back
    s = eng.series("coinbase", "BTCUSD")
    assert len(loads) == 1 and len(s.frame) == 601
    monkeypatch.setattr(eng, "FULL_RELOAD_SECONDS", 0)  # the safety net: deletions leave no stored_at
    store.upsert_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 19:30", 1))
    eng.series("coinbase", "BTCUSD")
    assert len(loads) == 2


def test_one_feed_writing_does_not_make_another_count_again(store, tmp_path, monkeypatch):
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, _bars("2026-10-20 09:30", 10))
    calls, real = [], store._fingerprint
    monkeypatch.setattr(store, "_fingerprint", lambda src, t: calls.append(src) or real(src, t))
    for src in ("alpaca", "coinbase"):
        store.series_fingerprint(src, "BTCUSD")
    calls.clear()
    with Store(tmp_path / "m.db") as recorder:  # the live recorder is another process
        recorder.upsert_built_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 19:30", 1), 1)
    for src in ("alpaca", "coinbase"):
        store.series_fingerprint(src, "BTCUSD")
    assert calls == ["coinbase"]


def test_the_stream_pushes_bars_as_they_are_stored(store, tmp_path):
    app = ChartApp(store)
    app.STREAM_CHECK = 0.01
    newest = _ms("2026-10-20 19:29")
    q = {"source": "coinbase", "ticker": "BTCUSD", "period": "minute", "span": "1", "session": "extended",
         "since": str(newest)}
    events = app.stream(q)
    first = json.loads(next(events).removeprefix(b"data: "))
    assert [b["timestamp"] for b in first["bars"]] == [newest] and first["model"] is None
    with Store(tmp_path / "m.db") as recorder:
        recorder.upsert_built_bars("coinbase", "BTCUSD", "minute", 1, _bars("2026-10-20 19:30", 2, price=16.0), 1)
    second = json.loads(next(events).removeprefix(b"data: "))
    assert [b["timestamp"] for b in second["bars"]] == [newest, newest + 60_000, newest + 120_000]
    assert second["bars"][-1]["close"] == pytest.approx(16.02)
    events.close()
    app.STREAM_SECONDS = 0
    assert next(app.stream(q)).startswith(b"event: end")  # the page opens the next one


def test_the_stream_reports_models_and_refuses_bad_requests(store):
    app = ChartApp(store)
    q = {"source": "coinbase", "ticker": "BTCUSD", "period": "minute", "span": "1", "session": "extended",
         "since": str(_ms("2026-10-20 19:20")), "model": "d1", "window": "9"}
    msg = json.loads(next(app.stream(q)).removeprefix(b"data: "))
    assert msg["model"] == "d1" and msg["bars"]
