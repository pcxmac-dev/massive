import numpy as np
import pandas as pd
import pytest

from massive_collate.gui.stats import details_subset, key_stats

ET = "America/New_York"


def _daily(closes, start="2025-12-24"):
    idx = pd.bdate_range(start, periods=len(closes), tz=ET)
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c - 1, "high": c + 2, "low": c - 2, "close": c, "volume": np.arange(1, len(c) + 1) * 10.0},
                        index=idx)


def test_key_stats_basics():
    d = _daily(list(range(100, 400)))  # 300 sessions, rising by 1
    s = key_stats(d)
    assert s["last"] == 399 and s["prevClose"] == 398 and s["change"] == 1
    assert s["changePct"] == pytest.approx(1 / 398 * 100)
    assert s["day"] == {"open": 398, "high": 401, "low": 397, "close": 399, "volume": 3000}
    assert s["range52w"]["high"] == 401 and s["range52w"]["low"] == 148 - 2 and s["range52w"]["sessions"] == 252
    assert s["rangeAll"]["low"] == 98
    assert s["avgVolume30"] == pytest.approx(np.mean(np.arange(271, 301) * 10))
    assert s["performance"]["1W"] == pytest.approx((399 / 394 - 1) * 100)
    assert s["performance"]["1Y"] == pytest.approx((399 / 147 - 1) * 100)
    assert s["performance"]["All"] == pytest.approx((399 / 99 - 1) * 100)
    assert s["volatility30"] > 0


def test_ytd_uses_last_close_of_previous_year():
    d = _daily([10, 11, 12, 13, 14], start="2025-12-30")  # Dec 30, Dec 31, Jan 1, Jan 2, Jan 5
    assert key_stats(d)["performance"]["YTD"] == pytest.approx((14 / 11 - 1) * 100)


def test_short_history_leaves_periods_empty():
    s = key_stats(_daily([5, 6, 7]))
    assert s["performance"]["1M"] is None and s["performance"]["1W"] is None
    assert key_stats(pd.DataFrame()) == {}


def test_details_subset_keeps_known_non_empty_fields():
    out = details_subset({"name": "X", "description": "", "market_cap": 1.5e12, "branding": {"logo_url": "u"}})
    assert out == {"name": "X", "market_cap": 1.5e12}
