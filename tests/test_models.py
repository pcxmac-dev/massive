import numpy as np
import pandas as pd
import pytest

from massive_collate.calc.derivatives import Model, apply_model, savgol_derivative, segments

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
