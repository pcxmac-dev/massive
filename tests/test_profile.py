"""Traded volume by price (the virtual level 2), box plots, and daily bars that reach today."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from massive_collate.calc.profile import from_bars, from_trades, nice_step, weighted_quartiles
from massive_collate.gui.bars import BarEngine, Style, calendar_key
from massive_collate.storage import Store

ET = "America/New_York"


def test_nice_steps_are_round():
    assert nice_step(4.0) == 0.1 and nice_step(40) == 1 and nice_step(90) == 2.5 and nice_step(170) == 5
    assert nice_step(0) == 0.01


def test_trades_at_their_own_prices_are_exact_and_summed_segments_are_derived():
    rows, derived = from_trades([10.0, 10.0, 10.05, 9.95], [1, 2, 3, 4])
    assert not derived and rows.to_dict("records") == [
        {"price": 10.05, "volume": 3, "trades": 1}, {"price": 10.0, "volume": 3, "trades": 2},
        {"price": 9.95, "volume": 4, "trades": 1}]
    rows, derived = from_trades([10.01, 10.04, 10.12], [1, 1, 1], step=0.1)
    assert derived and rows.to_dict("records") == [{"price": 10.1, "volume": 1, "trades": 1},
                                                   {"price": 10.0, "volume": 2, "trades": 2}]


def test_bars_spread_their_volume_evenly_across_their_range():
    rows, step = from_bars([10.0, 12.0], [12.0, 12.0], [100.0, 7.0], [10.0, 1.0], step=1.0)
    by = dict(zip(rows["price"], rows["volume"]))
    assert step == 1.0 and by == {10.0: 50.0, 11.0: 50.0, 12.0: 7.0}  # [10, 12) halves; 12 has no range
    assert rows["volume"].sum() == pytest.approx(107) and rows["trades"].sum() == pytest.approx(11)


def test_weighted_quartiles_per_group():
    group = np.array([0, 0, 0, 0, 1, 1, 2])
    price = np.array([4.0, 1.0, 3.0, 2.0, 5.0, 9.0, 7.0])
    weight = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 3.0, 0.0])
    q = weighted_quartiles(group, price, weight, 4)
    assert q[0].tolist() == [1.0, 2.0, 3.0]
    assert q[1].tolist() == [5.0, 9.0, 9.0]  # three quarters of the volume traded at 9
    assert q[2].tolist() == [7.0, 7.0, 7.0]  # no volume recorded: the price counts once
    assert np.isnan(q[3]).all()


def _minute_bars(start: str, prices: list[float], volume: float = 1.0) -> list[dict]:
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz=ET)
    return [{"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.5, "l": p - 0.5, "c": p, "v": volume, "vw": p}
            for t, p in zip(idx, prices)]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db", check_same_thread=False)
    yield s
    s.close()


def test_box_plots_are_the_quartiles_of_the_finer_bars_inside(store):
    store.upsert_bars("massive", "SPCX", "minute", 1, _minute_bars("2026-10-20 09:30", list(range(1, 61))))  # one hour bar
    page = BarEngine(store).bars("massive", "SPCX", "hour", 1, "extended", style=Style(type="box"))
    (bar,) = [b for b in page["bars"] if b["volume"]]
    assert (bar["low"], bar["q1"], bar["med"], bar["q3"], bar["high"]) == (0.5, 15, 30, 45, 60.5)
    assert page["approximate"].startswith("* derived")
    same = BarEngine(store).bars("massive", "SPCX", "minute", 1, "extended", style=Style(type="box"))
    assert same["bars"][0]["q1"] is None and "finer" in same["approximate"]  # nothing finer inside a minute


def test_level2_lists_the_trades_of_the_bar_and_the_days_range(store):
    ns = int(pd.Timestamp("2026-10-20 10:00:05", tz=ET).timestamp()) * 10**9
    store.upsert_bars("massive", "SPCX", "minute", 1, _minute_bars("2026-10-20 09:59", [10.0, 10.2, 10.4]))
    store.upsert_trades("massive", "SPCX", [
        {"sip_timestamp": ns + i * 10**9, "sequence_number": i, "price": p, "size": s, "exchange": 4}
        for i, (p, s) in enumerate([(10.2, 100), (10.25, 50), (10.2, 25)])])
    eng = BarEngine(store)
    at = int(pd.Timestamp("2026-10-20 10:00:30", tz=ET).timestamp() * 1000)
    p = eng.profile("massive", "SPCX", "minute", 1, "extended", at_ms=at)
    assert p["derived"] is None and p["basis"].startswith("3 trades")
    assert p["rows"] == [{"price": 10.25, "volume": 50.0, "trades": 1}, {"price": 10.2, "volume": 125.0, "trades": 2}]
    assert p["day"]["high"] == 10.9 and p["day"]["low"] == 9.5 and p["last"] == 10.4 and not p["newest"]
    p = eng.profile("massive", "SPCX", "minute", 1, "extended", at_ms=at, step=0.5)
    assert p["derived"] == "summed into $0.50 price segments" and p["rows"] == [{"price": 10.0, "volume": 175.0, "trades": 3}]
    p = eng.profile("massive", "SPCX", "minute", 1, "extended")  # the newest bar: no ticks in it
    assert p["newest"] and p["derived"].startswith("estimated from 1-minute bars") and p["rows"]


def test_daily_bars_reach_today_from_the_finer_data(store):
    store.upsert_bars("massive", "SPCX", "day", 1, [{"t": int(pd.Timestamp("2026-10-19", tz=ET).timestamp() * 1000),
                                                      "o": 9, "h": 9.5, "l": 8.5, "c": 9.2, "v": 1000}])
    store.upsert_bars("massive", "SPCX", "minute", 1, _minute_bars("2026-10-20 10:00", [10.0, 11.0]))
    days = BarEngine(store).bars("massive", "SPCX", "day", 1, "regular")["bars"]
    assert [d["close"] for d in days] == [9.2, 11.0]  # the official day, then today built from its minutes


def test_crypto_days_are_utc_days():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-22 00:00", tz="UTC"), pd.Timestamp("2026-10-22 23:59", tz="UTC")])
    keys = calendar_key(idx, "day", utc=True)
    assert (keys == pd.Timestamp("2026-10-22 00:00", tz="UTC")).all()  # one UTC day, not split across New York dates
    assert calendar_key(idx[:1], "day")[0] == pd.Timestamp("2026-10-21", tz=ET)  # what New York days would say
