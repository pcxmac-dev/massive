"""Trading schemes (calc/schemes.py): each causal, its decisions filled as the spec says."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from massive_collate.calc import schemes as sc
from massive_collate.calc.schemes import LONG, SHORT, Bars, Costs, Decision, Decisions, backtest

T0 = 1_790_000_000_000
MIN = 60_000


def _bars(o, h, l, c) -> Bars:  # noqa: E741
    n = len(c)
    return Bars(np.arange(n, dtype=np.int64) * MIN + T0, *(np.asarray(x, dtype=float) for x in (o, h, l, c)))


def _walk(n: int = 3000, seed: int = 7) -> Bars:
    """A random walk with swings and gaps: every scheme decides often on it."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.002, n) + 0.004 * np.sin(np.arange(n) / 90)  # slow swings
    r[rng.integers(0, n, 30)] += rng.normal(0, 0.02, 30)  # jumps
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0]], c[:-1]]) * (1 + rng.normal(0, 0.0005, n))
    wick = np.abs(rng.normal(0, 0.0015, n)) * c
    return _bars(o, np.maximum(o, c) + wick, np.minimum(o, c) - wick, c)


def _flat(n: int, price: float = 100.0, half: float = 1.0) -> list[list[float]]:
    return [[price] * n, [price + half] * n, [price - half] * n, [price] * n]


@pytest.mark.parametrize("key", list(sc.SCHEMES))
def test_every_scheme_decides_from_the_past_only(key):
    b = _walk()
    scheme = sc.SCHEMES[key]
    p = scheme.settings({"breakout": "wick"} if key == "failed_breakout" else {})
    full = scheme.run(b, p)
    assert len(full.events) >= 5, "the walk should make the scheme decide"
    for k in (400, 1111, 2048, 2999):
        part = scheme.run(b.head(k), p)
        assert [(e.i, e.side, e.stop, e.target) for e in part.events] == \
               [(e.i, e.side, e.stop, e.target) for e in full.events if e.i < k], k
        for name, vals in part.lines.items():
            np.testing.assert_array_equal(vals, full.lines[name][:k])
        assert part.points == [pt for pt in full.points if pt["known"] < k]


def test_atr_swing_reverses_on_a_close_three_atrs_back():
    n = 30
    o, h, l, c = _flat(n)
    for i in range(14, 20):  # a rise to 112 ...
        c[i] = o[i] = 100 + 2 * (i - 13)
        h[i], l[i] = c[i] + 1, c[i] - 1
    for i in range(20, n):  # ... and a fall
        c[i] = o[i] = 112 - 3 * (i - 19)
        h[i], l[i] = c[i] + 1, c[i] - 1
    b = _bars(o, h, l, c)
    a = sc.atr(b, 14)
    assert a[13] == pytest.approx(2.0) and math.isnan(a[12])  # the first 14 true ranges
    d = sc.atr_swing(b, {"atr": 14, "mult": 3.0})
    first_long = d.events[0]
    assert first_long.side == LONG and b.c[first_long.i] > 99 + 3 * a[first_long.i]  # up from the low
    short = next(e for e in d.events if e.side == SHORT)
    top = max(b.h[:short.i + 1])
    assert b.c[short.i] < top - 3 * a[short.i] and b.c[short.i - 1] >= top - 3 * a[short.i - 1]
    assert {"kind": "high", "price": top, "known": short.i} == {k: d.points[-1][k] for k in ("kind", "price", "known")}


def test_a_failed_breakdown_goes_long_with_its_stop_and_target():
    n = 40
    o, h, l, c = _flat(30, 100.0, 1.0)  # the range: 99 to 101
    o += [99.0, 98.5, 99.6, 100.4, 101.0, 102.0, 102.0, 102.0, 102.0, 102.0]
    c += [98.5, 99.6, 100.4, 101.0, 102.0, 102.0, 102.0, 102.0, 102.0, 102.0]
    h += [99.2, 99.8, 100.6, 101.2, 102.2, 102.2, 102.2, 102.2, 102.2, 102.2]
    l += [98.2, 98.4, 99.5, 100.3, 100.9, 101.8, 101.8, 101.8, 101.8, 101.8]
    b = _bars(o, h, l, c)
    p = sc.SCHEMES["failed_breakout"].settings({})
    d = sc.failed_breakout(b, p)
    (e,) = d.events
    a = sc.atr(b, 14)
    assert e.i == 31 and e.side == LONG  # closed below 99 at bar 30, back above it at 31
    assert e.stop == pytest.approx(98.2 - 0.25 * a[31]) and e.target == 101.0
    res = backtest(b, d)
    (t,) = res.trades
    assert (t["entry_i"], t["entry"], t["exit_i"], t["exit"], t["why"]) == (32, 99.6, 33, 101.0, "target")


def test_a_breakdown_that_holds_is_not_a_signal():
    o, h, l, c = _flat(30)
    o += [99.0, 98.5, 98.0, 97.5, 99.5]
    c += [98.5, 98.0, 97.5, 97.0, 99.5]  # back above 99 only after the 3-bar window
    h += [99.2, 98.7, 98.2, 97.7, 99.6]
    l += [98.2, 97.8, 97.3, 96.8, 99.0]
    assert sc.failed_breakout(_bars(o, h, l, c), sc.SCHEMES["failed_breakout"].settings({})).events == []


def test_cusum_finds_a_shift_and_starts_again():
    rng = np.random.default_rng(3)
    r = np.concatenate([rng.normal(0, 0.001, 300), rng.normal(0.003, 0.001, 30)])  # a drift up begins at 300
    c = 100 * np.exp(np.cumsum(r))
    b = _bars(c, c * 1.0005, c * 0.9995, c)
    d = sc.cusum(b, {"k": 0.5, "h": 5.0, "vol": 50})
    ups = [e.i for e in d.events if e.side == LONG and e.i >= 300]
    assert ups and ups[0] <= 306  # found within a few bars
    assert d.lines["s_up"][ups[0]] == 0.0 and d.lines["s_down"][ups[0]] == 0.0  # both start again
    assert not any(e.i < 300 and e.side == LONG and d.lines["s_up"][e.i - 1] > 5 for e in d.events)


def _steps(*closes: float) -> Bars:
    c = list(closes)
    return _bars(c, [x + 0.5 for x in c], [x - 0.5 for x in c], c)


def test_fills_are_at_the_next_open_and_costs_are_charged_each_way():
    b = _bars([10, 10, 11, 12, 13], [10.5, 10.5, 11.5, 12.5, 13.5], [9.5, 9.5, 10.5, 11.5, 12.5], [10, 11, 12, 13, 14])
    d = Decisions(events=[Decision(1, LONG), Decision(3, sc.FLAT)])
    (t,) = backtest(b, d, costs=Costs(fee_bps=10, slippage_bps=5)).trades
    assert (t["entry_i"], t["entry"], t["exit_i"], t["exit"]) == (2, 11.0, 4, 13.0)  # the next bars' opens
    assert t["ret"] == pytest.approx((13 / 11) * (1 - 0.0015) ** 2 - 1)


def test_a_reversal_closes_and_opens_and_a_short_gains_as_price_falls():
    b = _steps(10, 10, 12, 12, 9, 9)
    res = backtest(b, Decisions(events=[Decision(1, LONG), Decision(3, SHORT)]))
    long, short = res.trades
    assert (long["exit_i"], long["why"], short["entry_i"]) == (4, "reversed", 4)
    assert long["ret"] == pytest.approx(9 / 12 - 1) and short["why"] == "open" and short["ret"] == 0.0
    assert res.equity[-1] == pytest.approx(9 / 12)
    assert res.stats["max_drawdown"] == pytest.approx(9 / 12 - 1)
    res2 = backtest(_steps(10, 10, 12, 12, 9, 6), Decisions(events=[Decision(3, SHORT)]))
    assert res2.trades[0]["ret"] == pytest.approx(1 - 6 / 9)  # a fixed quantity: side times exit over entry


def test_the_stop_wins_when_a_bar_reaches_both_and_a_gap_fills_at_the_open():
    b = _bars([10, 10, 10, 10], [10.2, 10.2, 12, 10.2], [9.8, 9.8, 8, 9.8], [10, 10, 10, 10])
    (t,) = backtest(b, Decisions(events=[Decision(0, LONG, stop=9, target=11)])).trades
    assert (t["entry_i"], t["exit_i"], t["exit"], t["why"]) == (1, 2, 9.0, "stop")  # bar 2 reached 12 and 8: the stop
    gap = _bars([10, 10, 8.5], [10.2, 10.2, 8.7], [9.8, 9.8, 8.3], [10, 10, 8.5])
    (g,) = backtest(gap, Decisions(events=[Decision(0, LONG, stop=9)])).trades
    assert (g["exit_i"], g["exit"], g["why"]) == (2, 8.5, "stop")  # opened below the stop: out at the open


def test_the_run_starts_flat_at_the_stretch_and_long_only_turns_shorts_into_exits():
    b = _steps(10, 10, 11, 12, 13, 12, 11)
    d = Decisions(events=[Decision(0, LONG), Decision(4, SHORT)])
    res = backtest(b, d, start=2)  # the long was decided in the warm-up
    assert [t["side"] for t in res.trades] == ["short"] and res.equity[0] == 1.0 and len(res.equity) == 5
    only = backtest(b, Decisions(events=[Decision(1, LONG), Decision(4, SHORT)]), long_only=True)
    (t,) = only.trades
    assert (t["side"], t["exit_i"], t["why"]) == ("long", 5, "exit signal")
    assert only.stats["buy_hold"] == pytest.approx(11 / 10 - 1) and only.stats["exposure"] == pytest.approx(3 / 7)


def test_settings_are_checked():
    s = sc.SCHEMES["failed_breakout"]
    assert s.settings({"lookback": "30", "depth": "0.5"})["lookback"] == 30
    with pytest.raises(ValueError):
        s.settings({"lookback": "1"})
    with pytest.raises(ValueError):
        s.settings({"breakout": "body"})
    assert isinstance(sc.SCHEMES["atr_swing"].settings({"mult": "2.5"})["mult"], float)


def test_a_run_reports_times_trades_and_statistics_for_the_chart():
    idx = pd.date_range("2026-10-01 09:30", periods=3000, freq="1min", tz="America/New_York")
    w = _walk()
    frame = pd.DataFrame({"open": w.o, "high": w.h, "low": w.l, "close": w.c}, index=idx)
    out = sc.run("atr_swing", frame, start=500, given={"mult": "2"}, costs=Costs(fee_bps=1))
    first = int(idx[500].value // 1_000_000)
    assert out["params"] == {"atr": 14, "mult": 2.0}
    assert out["equity"][0][0] == first and len(out["equity"]) == 2500
    assert all(s["time"] >= first for s in out["signals"]) and out["trades"]
    tr = out["trades"][0]
    assert tr["signalTime"] < tr["entryTime"] and tr["entryTime"] - tr["signalTime"] == 60_000
    assert out["stats"]["trades"] == len(out["trades"]) and -1 <= out["stats"]["max_drawdown"] <= 0
    assert out["lines"]["reversal"][0][0] >= first
    with pytest.raises(ValueError):
        sc.run("nope", frame, 0)


@pytest.mark.parametrize("key, given", [("directional_change", {"mode": "contrarian"}), ("fractals", {"alligator": "off"}),
                                        ("zigzag", {"basis": "close"}), ("pivots", {"left": 3, "right": 8})])
def test_the_other_settings_decide_from_the_past_only_too(key, given):
    b = _walk(2000, seed=11)
    scheme = sc.SCHEMES[key]
    p = scheme.settings(given)
    full = scheme.run(b, p)
    assert len(full.events) >= 5
    for k in (300, 977, 1999):
        part = scheme.run(b.head(k), p)
        assert [(e.i, e.side) for e in part.events] == [(e.i, e.side) for e in full.events if e.i < k]
        assert part.points == [pt for pt in full.points if pt["known"] < k]


def test_zigzag_points_are_known_when_price_has_come_back_far_enough():
    c = [100, 104, 108, 110, 107, 104.4, 103, 106, 109.7, 110.5]
    b = _bars(c, c, c, c)
    d = sc.zigzag(b, {"pct": 5.0, "basis": "high_low"})
    assert [(pt["i"], pt["known"], pt["kind"]) for pt in d.points] == [(0, 2, "low"), (3, 5, "high"), (6, 8, "low")]
    assert [(e.i, e.side) for e in d.events] == [(2, LONG), (5, SHORT), (8, LONG)]  # 108 >= 105; 104.4 <= 104.5


def test_a_fractal_is_known_two_bars_after_it_and_traded_on_a_close_beyond_it():
    h = [10, 11, 15, 12, 11, 12, 13, 14, 15.5, 16]
    l = [x - 2 for x in h]  # noqa: E741
    c = [x - 0.5 for x in h]
    b = _bars(c, h, l, c)
    d = sc.fractals(b, {"n": 2, "alligator": "off"})
    (top,) = [pt for pt in d.points if pt["kind"] == "fractal_high"]
    assert (top["i"], top["known"], top["price"]) == (2, 4, 15.0)
    (e,) = d.events
    assert (e.i, e.side) == (9, LONG) and b.c[9] > 15 >= b.c[8]  # 15.0 is not beyond it
    gated = sc.fractals(b, {"n": 2, "alligator": "on"})  # no teeth yet that early: the fractal does not count
    assert gated.events == [] and {pt["kind"] for pt in gated.points} <= {"fractal_high_out", "fractal_low_out"}
    assert set(gated.lines) >= {"jaw", "teeth", "lips"}


def test_alligator_lines_are_moved_on_and_read_only_the_past():
    b = _walk(200)
    a = sc.alligator(b)
    med = (b.h + b.l) / 2
    assert a["teeth"][7 + 5] == pytest.approx(med[:8].mean())  # SMMA 8's first value, 5 bars on
    assert np.isnan(a["jaw"][12 + 7])
    np.testing.assert_allclose(sc.alligator(b.head(120))["lips"], a["lips"][:120])


def test_pivots_use_their_own_left_and_right():
    h = [5, 6, 7, 9, 8, 7, 6, 5, 4, 10]
    b = _bars(h, h, [x - 1 for x in h], h)
    d = sc.pivots(b, {"left": 3, "right": 4})
    (pt,) = [x for x in d.points if x["kind"] == "pivot_high"]
    assert (pt["i"], pt["known"]) == (3, 7)
    assert [(e.i, e.side) for e in d.events] == [(9, LONG)]
    assert d.lines["pivot_high"][7] == 9 and np.isnan(d.lines["pivot_high"][9])  # used once


def test_directional_change_trades_events_or_fades_their_overshoot():
    c = [100, 101, 102, 101, 100.9, 103, 104, 105.1, 104, 103.9]  # 1 %: up at 101, down at 100.9 ...
    b = _bars(c, c, c, c)
    trend = sc.directional_change(b, {"theta": 1.0, "mode": "trend", "overshoot": 1.0})
    assert [(e.i, e.side) for e in trend.events] == [(1, LONG), (4, SHORT), (5, LONG), (8, SHORT)]
    fade = sc.directional_change(b, {"theta": 1.0, "mode": "contrarian", "overshoot": 2.0})
    # flat at each event; short once the upturn from 103 (at bar 5) runs 2 % past it (105.1 >= 105.06, bar 7)
    assert [(e.i, e.side) for e in fade.events] == [(1, 0), (4, 0), (5, 0), (7, SHORT), (8, 0)]


def test_hurst_crosses_its_fld_and_times_the_next_trough():
    n = 2000
    x = np.arange(n)
    c = 100 + 5 * np.sin(2 * np.pi * x / 40)
    b = _bars(c, c + 0.2, c - 0.2, c)
    d = sc.hurst(b, {"cycle": 40})
    np.testing.assert_allclose(d.lines["fld"][20:], ((b.h + b.l) / 2)[:-20])
    troughs = [pt for pt in d.points if pt["kind"] == "trough"]
    assert len(troughs) >= 40 and all(pt["known"] == pt["i"] + 20 for pt in troughs)
    gaps = np.diff([pt["i"] for pt in troughs])
    assert set(gaps) == {40}  # a 40-bar cycle: a trough every 40 bars, as each projected the next
    assert d.events and {e.side for e in d.events} == {LONG, SHORT}
    idx = pd.date_range("2026-10-01", periods=n, freq="1min", tz="UTC")
    out = sc.run("hurst", pd.DataFrame({"open": b.o, "high": b.h, "low": b.l, "close": b.c}, index=idx), 100)
    last = out["points"][-1]
    assert last["due"] - last["time"] == 40 * 60_000  # due a cycle after it, counted on past the data
