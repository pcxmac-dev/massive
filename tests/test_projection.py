"""The time axis after the last bar: projected bars must be exactly where real bars will be."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from massive_collate.gui import projection as pj
from massive_collate.gui.bars import (COLUMNS, Series, calendar_key, lod_aggregate, regular_session, resample,
                                      wall_clock_bins)
from massive_collate.sessions import ET, MarketCalendar

CAL = MarketCalendar()
PRODUCT = {"future": "NQ", "stock": None, "index": None, "fx": None}


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({c: 1.0 for c in COLUMNS}, index=index)


def _series(asset: str, start: datetime, end: datetime, daily=None) -> Series:
    """A perfect feed: a bar in every open minute of the calendar."""
    minutes = CAL.open_minutes(asset, start, end, PRODUCT[asset])
    return Series("t", "X", "minute", _frame(minutes), daily, 2, 0, asset)


START, END = datetime(2025, 11, 3, tzinfo=ET), datetime(2026, 1, 31, tzinfo=ET)
CUTS = ["2025-11-25 15:00", "2025-12-23 12:00", "2025-12-31 16:59", "2026-01-15 16:30"]
SERIES = {a: _series(a, START, END) for a in ("stock", "index", "fx", "future")}


@pytest.mark.parametrize("asset", ["stock", "index", "fx", "future"])
@pytest.mark.parametrize("period,span", [("minute", 1), ("minute", 5), ("minute", 45), ("hour", 1), ("hour", 4),
                                         ("day", 1), ("week", 1)])
@pytest.mark.parametrize("session", ["regular", "extended"])
def test_projection_matches_a_perfect_feed_exactly(asset, period, span, session):
    s = SERIES[asset]
    session = s.session_for(session)
    bars = resample(s.frame, period, span, session, align_hours_to_open=asset in ("stock", "index"),
                    utc_days=asset == "fx")  # as the engine bins: forex days are UTC days
    for cut in CUTS:
        T = pd.Timestamp(cut, tz=ET)
        past, future = bars[bars.index <= T], bars[bars.index > T]
        future = future[future.index < pd.Timestamp(END) - pd.Timedelta(days=8)]  # whole periods only
        ms = pj.project(CAL, s, PRODUCT[asset], period, span, session, "candles", 1, len(past), past.index[-1],
                        0, len(future))
        got = pd.to_datetime(ms, unit="ms", utc=True).tz_convert(ET)
        assert got.equals(future.index), f"{asset} {period}{span} {session} cut {cut}"


@pytest.mark.parametrize("lod", [2, 7, 64])
def test_projection_continues_level_of_detail_groups(lod):
    s = SERIES["stock"]
    bars = resample(s.frame, "minute", 5, "regular")
    T = pd.Timestamp("2025-12-23 12:00", tz=ET)
    past, future = bars[bars.index <= T], bars[bars.index > T]
    whole = lod_aggregate(pd.concat([past, future]), lod)
    last_group_start = lod_aggregate(past, lod).index[-1]
    want = whole.index[whole.index > last_group_start][:20]
    ms = pj.project(CAL, s, None, "minute", 5, "regular", "candles", lod, len(past), past.index[-1], 0, 20)
    assert pd.to_datetime(ms, unit="ms", utc=True).tz_convert(ET).equals(want)


def test_projection_pages_and_runs_far_ahead():
    s = SERIES["stock"]
    bars = resample(s.frame, "minute", 1, "regular")
    last = bars.index[-1]
    first = pj.project(CAL, s, None, "minute", 1, "regular", "candles", 1, len(bars), last, 0, 1000)
    page = pj.project(CAL, s, None, "minute", 1, "regular", "candles", 1, len(bars), last, 500, 500)
    assert first[500:] == page
    far = pj.project(CAL, s, None, "minute", 1, "regular", "candles", 1, len(bars), last, 10_000_000, 1)
    t = pd.Timestamp(far[0], unit="ms", tz="UTC").tz_convert(ET)
    assert t.year > 2125 and CAL.is_trading_day(t.date()) and 9 <= t.hour < 16  # still a real session minute


def test_fast_session_counting_agrees_with_the_minute_by_minute_reference():
    for asset in ("stock", "index", "fx", "future"):
        s = SERIES[asset]
        for period, span in (("minute", 5), ("hour", 4)):
            for session in ("regular", "extended"):
                session = s.session_for(session)
                last = pd.Timestamp("2025-12-24 12:59", tz=ET)
                ref = pj.future_bar_starts_reference(CAL, s, PRODUCT[asset], period, span, session, last,
                                                     last.to_pydatetime() + timedelta(days=120))
                fast = pj.SessionBins(CAL, s, PRODUCT[asset], period, span, session, last)
                assert np.array_equal(fast.picks(np.arange(len(ref))), ref.as_unit("ms").asi8), (asset, period, session)


def test_price_based_chart_types_are_not_projected():
    s = SERIES["stock"]
    assert pj.project(CAL, s, None, "minute", 1, "regular", "renko", 1, 10, s.frame.index[-1], 0, 10) == []


def test_daily_stamp_conventions():
    utc = pd.DatetimeIndex(pd.date_range("2026-10-01", periods=20, freq="D", tz="UTC")).tz_convert(ET)
    assert pj.daily_stamps_utc_midnight(_frame(utc))  # forex: 00:00 UTC
    five = pd.DatetimeIndex(pd.date_range("2026-10-01 05:00", periods=20, freq="D", tz="UTC")).tz_convert(ET)
    assert not pj.daily_stamps_utc_midnight(_frame(five))  # indices: 05:00 UTC, i.e. the session date
    et = pd.date_range("2026-10-01", periods=20, freq="D", tz=ET)
    assert not pj.daily_stamps_utc_midnight(_frame(et))


def test_fx_daily_projection_uses_utc_days():
    daily = _frame(pd.DatetimeIndex(pd.date_range("2026-09-01", "2026-10-20", freq="D", tz="UTC")).tz_convert(ET))
    s = Series("t", "X", "minute", SERIES["fx"].frame, daily, 2, 0, "fx")
    last = calendar_key(daily.index[-1:], "day")[0]
    ms = pj.project(CAL, s, None, "day", 1, "extended", "candles", 1, 100, last, 0, 6)
    days = [pd.Timestamp(m, unit="ms", tz="UTC").tz_convert(ET).strftime("%a") for m in ms]
    assert "Fri" not in days and "Sat" in days  # Monday's UTC bar is keyed Sunday, Saturday's... none


def test_regular_session_follows_the_calendar():
    idx = pd.DatetimeIndex([
        datetime(2025, 11, 28, 12, 59, tzinfo=ET), datetime(2025, 11, 28, 13, 0, tzinfo=ET),  # early close
        datetime(2026, 9, 12, 10, 0, tzinfo=ET),  # a Saturday print
        datetime(2026, 9, 14, 15, 59, tzinfo=ET), datetime(2026, 9, 14, 16, 0, tzinfo=ET),
    ])
    kept = regular_session(_frame(idx)).index
    assert list(kept.strftime("%m-%d %H:%M")) == ["11-28 12:59", "09-14 15:59"]


def test_wall_clock_bins_keep_their_times_across_clock_changes():
    spring = pd.date_range("2026-03-06 09:00", "2026-03-10 12:00", freq="1min", tz=ET)
    four = wall_clock_bins(spring, "hour", 4, 570 * 60).unique()
    assert all(t.strftime("%H:%M") in ("01:30", "05:30", "09:30", "13:30", "17:30", "21:30") for t in four)
    fall = pd.date_range("2026-11-01 00:30", "2026-11-01 02:30", freq="1min", tz=ET)
    hours = wall_clock_bins(fall, "hour", 1).unique()
    assert len(hours) == 4  # 00:00, 01:00 EDT, 01:00 EST, 02:00: the repeated hour stays two bars


def test_engine_future_endpoint(tmp_path):
    from massive_collate.gui.server import ChartApp
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db", check_same_thread=False) as store:
        mins = CAL.open_minutes("stock", datetime(2026, 11, 23, tzinfo=ET), datetime(2026, 11, 26, tzinfo=ET))
        rows = [{"t": int(t.timestamp() * 1000), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for t in mins]
        store.upsert_bars("massive", "AAA", "minute", 1, rows)
        out = ChartApp(store).future({"source": "massive", "ticker": "AAA", "period": "hour", "span": "1",
                                      "session": "regular", "count": "9"})
    got = [pd.Timestamp(m, unit="ms", tz="UTC").tz_convert(ET).strftime("%a %d %H:%M") for m in out["timestamps"]]
    # Wednesday 25 ends at 15:30; Thanksgiving is skipped; Friday 27 closes at 13:00.
    assert got == ["Fri 27 09:30", "Fri 27 10:30", "Fri 27 11:30", "Fri 27 12:30",
                   "Mon 30 09:30", "Mon 30 10:30", "Mon 30 11:30", "Mon 30 12:30", "Mon 30 13:30"]
