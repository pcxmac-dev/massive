from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from massive_collate.gui import charttypes as ct

ET = "America/New_York"


def _bars(closes, opens=None, highs=None, lows=None, start="2026-10-20 09:30"):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz=ET)
    c = np.asarray(closes, float)
    o = np.asarray(opens if opens is not None else np.r_[c[:1], c[:-1]], float)
    h = np.asarray(highs if highs is not None else np.maximum(o, c), float)
    l = np.asarray(lows if lows is not None else np.minimum(o, c), float)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1.0, "turnover": c, "trades": 1},
                        index=idx)


def _strictly_increasing(df):
    return df.index.is_monotonic_increasing and df.index.is_unique


def test_heikin_ashi_formula():
    b = _bars([10, 11, 12], opens=[9, 10, 11], highs=[10.5, 11.5, 12.5], lows=[8.5, 9.5, 10.5])
    ha = ct.heikin_ashi(b)
    assert ha["close"].iloc[0] == pytest.approx((9 + 10.5 + 8.5 + 10) / 4)
    assert ha["open"].iloc[0] == pytest.approx((9 + 10) / 2)
    assert ha["open"].iloc[1] == pytest.approx((ha["open"].iloc[0] + ha["close"].iloc[0]) / 2)
    assert (ha["high"] >= ha[["open", "close"]].max(axis=1)).all()
    assert (ha["low"] <= ha[["open", "close"]].min(axis=1)).all()


def test_renko_bricks_and_two_box_reversal():
    r = ct.renko(_bars([10.0, 10.4, 11.05, 12.3, 11.6, 10.9, 9.95]), box=1.0)
    bricks = list(zip(r["open"], r["close"]))
    # up 10->11, 11->12; 11.6 is not a 2-box reversal from 12 (needs <= 10); 9.95 is
    assert bricks == [(10.0, 11.0), (11.0, 12.0), (11.0, 10.0)]
    assert _strictly_increasing(r)
    assert r["volume"].sum() <= 7


def test_renko_one_bar_many_bricks_get_distinct_times():
    r = ct.renko(_bars([10.0, 13.2]), box=1.0)
    assert list(zip(r["open"], r["close"])) == [(10.0, 11.0), (11.0, 12.0), (12.0, 13.0)]
    assert _strictly_increasing(r)
    assert (r["src_time"] == r["src_time"].iloc[0]).all()


def test_line_break_three_lines():
    lb = ct.line_break(_bars([10, 11, 12, 13, 12.5, 11.5, 10.5, 9.5]), lines=3)
    lines = list(zip(lb["open"], lb["close"]))
    # up lines to 13; the last 3 lines span 10-13, so 12.5, 11.5 and 10.5 do not reverse;
    # 9.5 breaks below 10 and the down line starts at the last line's open (12)
    assert lines == [(10, 11), (11, 12), (12, 13), (12, 9.5)]


def test_kagi_reversal_and_thickness():
    k = ct.kagi(_bars([10, 12, 11.5, 10.8, 13, 9]), reversal=1.0)
    segs = list(zip(k["open"], k["close"]))
    assert segs == [(10, 12), (12, 10.8), (10.8, 13), (13, 9)]
    assert k["yang"].tolist()[0] is True or k["yang"].iloc[0]
    third = k.iloc[2]  # rises above the 12 shoulder: thickness stays/turns yang
    assert bool(third["yang"]) is True
    last = k.iloc[3]  # falls below the 10.8 waist: turns yin at 10.8
    assert bool(last["yang"]) is False and last["switch"] == pytest.approx(10.8)


def test_point_figure_columns():
    b = _bars([10, 12, 14, 13.4, 11, 12.2, 15],
              highs=[10, 12, 14, 13.4, 11, 12.2, 15], lows=[10, 12, 14, 13.4, 11, 12.2, 15], opens=[10] * 7)
    pf = ct.point_figure(b, box=1.0, reversal=3)
    cols = [(bool(u), lo, hi) for u, lo, hi in zip(pf["up"], pf["low"], pf["high"])]
    assert cols == [(True, 10, 14), (False, 11, 13), (True, 12, 15)]


def test_range_bars_span_exactly_the_size():
    ticks = _bars([10.0, 10.3, 10.6, 10.2, 9.9, 10.75])
    rb = ct.range_bars(ticks, size=0.5)
    done = rb.iloc[:-1]
    assert np.allclose(done["high"] - done["low"], 0.5)
    assert (rb["open"].iloc[1:].to_numpy() == rb["close"].iloc[:-1].to_numpy()).all()
    assert _strictly_increasing(rb)


def test_atr_and_box_rounding():
    b = _bars([10, 11, 10, 11, 10], highs=[10.5, 11.5, 10.5, 11.5, 10.5], lows=[9.5, 10.5, 9.5, 10.5, 9.5])
    assert ct.atr(b) > 0
    assert ct.round_box(0.12345, 2) == pytest.approx(0.12)
    assert ct.round_box(0.0001, 2) == pytest.approx(0.01)


@pytest.mark.parametrize("fn, args", [(ct.renko, (0,)), (ct.kagi, (-1,)), (ct.point_figure, (0, 3)), (ct.range_bars, (0,))])
def test_invalid_parameters(fn, args):
    with pytest.raises(ValueError):
        fn(_bars([1, 2]), *args)


def test_empty_input():
    empty = _bars([])
    for out in (ct.renko(empty, 1), ct.line_break(empty), ct.kagi(empty, 1), ct.point_figure(empty, 1), ct.range_bars(empty, 1)):
        assert out.empty
    assert math.isclose(ct.atr(empty), 0.0)
