import numpy as np
import pandas as pd
import pytest

from massive_collate.calc.derivatives import (Model, apply_model, savgol_derivative, savgol_derivative_causal,
                                              segments)

ET = "America/New_York"


def _bars(values, start="2026-10-20 09:30", freq="1min"):
    idx = pd.date_range(start, periods=len(values), freq=freq, tz=ET)
    v = np.asarray(values, float)
    return pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "volume": 1.0, "turnover": v, "trades": 1}, index=idx)


def test_savgol_is_exact_for_polynomials_including_edges():
    x = np.arange(50, dtype=float)
    y = 2 + 3 * x - 0.5 * x**2 + 0.01 * x**3
    d1 = savgol_derivative(y, 9, 3, 1)
    d2 = savgol_derivative(y, 9, 3, 2)
    assert np.allclose(d1, 3 - x + 0.03 * x**2)
    assert np.allclose(d2, -1 + 0.06 * x)


def test_derivative_units_per_hour_and_per_bar():
    b = _bars(100 + 0.5 * np.arange(30))  # +0.5 per 1-minute bar
    per_bar = apply_model(b, Model("d1", per="bar"), 60)["close"]
    per_hour = apply_model(b, Model("d1", per="hour"), 60)["close"]
    assert np.allclose(per_bar, 0.5) and np.allclose(per_hour, 30.0)
    acc = apply_model(b, Model("d2", per="hour"), 60)["close"]
    assert np.allclose(acc, 0.0, atol=1e-9)


def test_log_basis_is_percent():
    # exp growth of 1% per bar -> d1 of ln(price)*100 = ln(1.01)*100 per bar
    b = _bars(100 * 1.01 ** np.arange(20))
    out = apply_model(b, Model("d1", basis="log", per="bar"), 60)["close"]
    assert np.allclose(out, np.log(1.01) * 100)


def test_gaps_split_segments_so_no_spike():
    day1 = _bars(100 + 0.1 * np.arange(20), start="2026-10-20 15:40")
    day2 = _bars(120 + 0.1 * np.arange(20), start="2026-10-21 09:30")  # jumps +20 overnight
    b = pd.concat([day1, day2])
    assert list(np.unique(segments(b.index, 60, True))) == [0, 1]
    d1 = apply_model(b, Model("d1", per="bar"), 60)["close"]
    assert np.allclose(d1, 0.1)  # the overnight jump never shows up as slope
    no_reset = apply_model(b, Model("d1", per="bar", reset_gaps=False), 60)["close"]
    assert no_reset.max() > 1  # without resets the jump is a spike


def test_edges_follow_the_data_instead_of_one_polynomial():
    rng = np.random.default_rng(11)
    y = np.cumsum(rng.normal(size=300))
    d2 = savgol_derivative(y, 101, 3, 2)
    # The newest 50 bars (half a window) each have their own window: not a straight line
    tail = d2[-50:]
    assert np.abs(np.diff(tail, 2)).max() > 1e-6
    # and the newest value is exactly the one computable at that bar (the trailing window)
    assert np.isclose(d2[-1], savgol_derivative_causal(y, 101, 3, 2)[-1])
    assert np.allclose(d2[-50:], savgol_derivative_causal(y, 101, 3, 2)[-50:])
    # The oldest bars mirror it: the window starting at the bar
    head = savgol_derivative(y[::-1], 101, 3, 2)[::-1]  # 2nd derivative is unchanged by reversal
    assert np.allclose(d2[:50], head[:50])


def test_edges_of_a_segment_barely_longer_than_the_window():
    # 105 bars, window 101: most edge bars have no full window ending (or starting) at them;
    # they use the nearest full window's fit, and everything stays exact for polynomials.
    x = np.arange(105, dtype=float)
    y = 2 + 3 * x - 0.5 * x**2 + 0.01 * x**3
    assert np.allclose(savgol_derivative(y, 101, 3, 1), 3 - x + 0.03 * x**2)
    assert np.allclose(savgol_derivative(y, 101, 3, 2), -1 + 0.06 * x)
    rng = np.random.default_rng(5)
    noisy = y + rng.normal(size=105)
    d1 = savgol_derivative(noisy, 101, 3, 1)
    assert np.isfinite(d1).all()
    assert np.isclose(d1[-1], savgol_derivative_causal(noisy, 101, 3, 1)[-1])  # the newest: its own window


def test_a_segment_shorter_than_the_window_has_no_values():
    # 30 bars then, after a gap, 8: a 9-bar window fits only in the first stretch. Fitted to the
    # 8 alone, the "slope" would be one polynomial's derivative whatever the window asked for.
    rng = np.random.default_rng(3)
    long = _bars(100 + np.cumsum(rng.normal(size=30)), start="2026-10-20 09:30")
    short = _bars(100 + np.cumsum(rng.normal(size=8)), start="2026-10-21 09:30")
    out = apply_model(pd.concat([long, short]), Model("d2", per="bar", window=9), 60)
    assert len(out) == 30 and out.index[-1] == long.index[-1]
    assert len(apply_model(short, Model("d1", per="bar", window=9), 60)) == 0
    assert len(apply_model(short, Model("integral", per="bar", window=9), 60)) == 8  # an area needs no window


def test_integral_trapezoid_and_gaps_add_nothing():
    b = _bars([10, 12, 14, 16])  # base = 0, 2, 4, 6 ; per bar trapezoids 1, 3, 5
    out = apply_model(b, Model("integral", per="bar"), 60)["close"].to_numpy()
    assert np.allclose(out, [0, 1, 4, 9])
    per_hour = apply_model(b, Model("integral", per="hour"), 60)["close"].to_numpy()
    assert np.allclose(per_hour, np.array([0, 1, 4, 9]) / 60)
    two = pd.concat([_bars([10, 12], start="2026-10-20 15:58"), _bars([30, 32], start="2026-10-21 09:30")])
    area = apply_model(two, Model("integral", per="bar"), 60)["close"].to_numpy()
    assert np.allclose(area, [0, 1, 1, 1 + (20 + 22) / 2])  # no area across the overnight gap


def test_model_output_is_ohlc_of_the_value_with_volume():
    b = _bars(np.arange(15, dtype=float))
    out = apply_model(b, Model("d1", per="bar"), 60)
    assert (out["open"] == out["close"]).all() and (out["high"] == out["low"]).all()
    assert (out["volume"] == 1).all() and len(out) == 15
    assert apply_model(b, Model("price"), 60) is b


@pytest.mark.parametrize("kw", [dict(kind="d3"), dict(basis="x"), dict(per="week"), dict(window=4),
                                dict(window=1), dict(kind="d2", order=1), dict(order=9, window=9)])
def test_invalid_models(kw):
    with pytest.raises(ValueError):
        Model(**kw).validate()


def test_unit_labels():
    assert Model("d1", basis="log", per="hour").unit_label() == "%/hour"
    assert Model("d2", per="minute").unit_label() == "price/minute²"
    assert Model("integral", per="day").unit_label() == "price·day"


def test_causal_derivative_never_looks_ahead():
    rng = np.random.default_rng(7)
    y = np.cumsum(rng.normal(size=200))
    full = savgol_derivative_causal(y, 9, 3, 1)
    for k in (5, 30, 120, 199):  # what was computable with only the first k samples is unchanged
        assert np.allclose(savgol_derivative_causal(y[:k], 9, 3, 1), full[:k], equal_nan=True)
    centred = savgol_derivative(y, 9, 3, 1)
    assert not np.allclose(savgol_derivative(y[:120], 9, 3, 1)[110:], centred[110:120])  # the viewing one does


def test_causal_derivative_is_exact_for_polynomials_once_it_has_enough_samples():
    x = np.arange(40, dtype=float)
    y = 2 + 3 * x - 0.5 * x**2 + 0.01 * x**3
    d1 = savgol_derivative_causal(y, 9, 3, 1)
    d2 = savgol_derivative_causal(y, 9, 3, 2)
    assert np.isnan(d1[:3]).all()  # fewer than order + 1 samples: nothing to fit
    assert np.allclose(d1[3:], (3 - x + 0.03 * x**2)[3:])
    assert np.allclose(d2[3:], (-1 + 0.06 * x)[3:])


def test_causal_model_restarts_at_gaps_and_scales_like_the_centred_one():
    day1 = _bars(100 + 0.1 * np.arange(20), start="2026-10-20 15:40")
    day2 = _bars(120 + 0.1 * np.arange(20), start="2026-10-21 09:30")
    b = pd.concat([day1, day2])
    out = apply_model(b, Model("d1", per="hour", causal=True), 60)["close"]
    assert len(out) == 40 - 2 * 3  # the first 3 bars of each segment have too few samples
    assert np.allclose(out, 6.0)  # 0.1 per minute = 6 per hour, no overnight spike


def test_the_fourier_model_keeps_the_slow_cycles_and_removes_the_fast_ones():
    import numpy as np
    import pandas as pd

    from massive_collate.calc.derivatives import Model, apply_model, fourier_lowpass

    t = np.arange(600, dtype=float)
    slow = 5 * np.sin(2 * np.pi * t / 200)  # a 200-bar cycle
    fast = 1 * np.sin(2 * np.pi * t / 8)  # an 8-bar wiggle
    y = 100 + 0.01 * t + slow + fast
    kept = fourier_lowpass(y, 41)
    inner = slice(50, 550)  # away from the ends
    assert np.max(np.abs(kept[inner] - (100 + 0.01 * t + slow)[inner])) < 0.35  # the wiggle is gone
    assert np.max(np.abs(fourier_lowpass(y, 4)[inner] - y[inner])) < 0.35  # a 4-bar cut keeps the 8-bar wiggle

    idx = pd.date_range("2026-10-20 09:30", periods=600, freq="1min", tz="America/New_York")
    bars = pd.DataFrame({"open": y, "high": y, "low": y, "close": y, "volume": 1.0, "turnover": y, "trades": 1}, index=idx)
    out = apply_model(bars, Model(kind="fourier", window=41, reset_gaps=False), 60)
    assert len(out) == 600 and abs(out["close"].iloc[300] - kept[300]) < 1e-9
    log = apply_model(bars, Model(kind="fourier", basis="log", window=41, reset_gaps=False), 60)
    assert abs(log["close"].iloc[300] - kept[300]) < 0.2  # in price on a log basis too
    assert Model(kind="fourier").unit_label() == "price"
    short = apply_model(bars.iloc[:30], Model(kind="fourier", window=41), 60)
    assert short.empty  # no whole 41-bar cycle in 30 bars
    try:
        Model(kind="fourier", causal=True).validate()
    except ValueError as e:
        assert "causal" in str(e)
    else:
        raise AssertionError("a Fourier fit has no causal form")
