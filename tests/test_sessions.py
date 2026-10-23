"""Trading calendars: exchange holidays, early closes, CME and FX holiday hours, any year."""
from datetime import date, datetime, timedelta

import pytest

from massive_collate.sessions import (CT, ET, MarketCalendar, Overrides, is_nyse_holiday, jobs_report_good_friday,
                                      nyse_early_close, sessions_from_events)

CAL = MarketCalendar()

# Published NYSE holiday calendars.
NYSE = {
    2025: ["2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
           "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
           "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
           "2027-09-06", "2027-11-25", "2027-12-24"],
}


@pytest.mark.parametrize("year", sorted(NYSE))
def test_nyse_holidays_match_the_published_calendar(year):
    got = [d for d in (date(year, 1, 1) + timedelta(days=i) for i in range(366))
           if d.year == year and d.weekday() < 5 and is_nyse_holiday(d)]
    assert [str(d) for d in got] == NYSE[year]


def test_saturday_new_year_is_not_observed_on_the_friday_before():
    assert not is_nyse_holiday(date(2021, 12, 31)) and CAL.is_trading_day(date(2021, 12, 31))


def test_rules_work_far_ahead():
    # Thanksgiving 2099 is the fourth Thursday of November; Christmas 2099 is a Friday.
    assert is_nyse_holiday(date(2099, 11, 26)) and is_nyse_holiday(date(2099, 12, 25))
    assert nyse_early_close(date(2099, 11, 27)) and nyse_early_close(date(2099, 12, 24))


def test_early_closes():
    assert nyse_early_close(date(2025, 7, 3)) and nyse_early_close(date(2025, 11, 28))
    assert nyse_early_close(date(2026, 11, 27)) and nyse_early_close(date(2026, 12, 24))
    assert not nyse_early_close(date(2026, 7, 2))  # July 4 2026 is a Saturday: no early close
    s = CAL.stock_session(date(2026, 11, 27))
    assert (s.open.hour, s.close.hour) == (4, 17)  # after-hours end four hours after the 13:00 close
    assert CAL.regular_hours(date(2026, 11, 27))[1].hour == 13


def test_index_session_includes_the_closing_print():
    s = CAL.sessions("index", date(2026, 10, 21), date(2026, 10, 21))[0]
    assert (s.open.hour, s.open.minute, s.close.hour, s.close.minute) == (9, 30, 16, 1)


def test_official_early_close_overrides_the_rules():
    d = date(2026, 10, 21)
    close = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
    cal = MarketCalendar(Overrides(us={d: ("early-close", datetime(2026, 10, 21, 9, 30, tzinfo=ET), close)}))
    assert cal.regular_hours(d)[1] == close
    closed = MarketCalendar(Overrides(us={d: ("closed", None, None)}))
    assert not closed.is_trading_day(d) and closed.sessions("stock", d, d) == []


def test_cme_holiday_halt_belongs_to_the_next_trade_date():
    # MLK Day 2026: open Sunday 18:00, halted at noon CT, no settlement: Tuesday's trade date.
    (s,) = CAL.future_sessions("NQ", date(2026, 1, 19))
    assert s.day == date(2026, 1, 20)
    assert s.open == datetime(2026, 1, 18, 18, 0, tzinfo=ET) and s.close == datetime(2026, 1, 19, 12, 0, tzinfo=CT)
    (cl,) = CAL.future_sessions("CL", date(2026, 1, 19))
    assert cl.close == datetime(2026, 1, 19, 13, 30, tzinfo=CT)  # energy halts later


def test_cme_good_friday():
    assert jobs_report_good_friday(date(2026, 4, 3)) and not jobs_report_good_friday(date(2025, 4, 18))
    (s,) = CAL.future_sessions("NQ", date(2026, 4, 3))  # equity: short session until 08:15 CT
    assert s.close == datetime(2026, 4, 3, 8, 15, tzinfo=CT) and s.day == date(2026, 4, 6)
    assert CAL.future_sessions("CL", date(2026, 4, 3)) == []
    assert CAL.future_sessions("NQ", date(2025, 4, 18)) == []


def test_cme_closed_on_christmas_and_weekends():
    assert CAL.future_sessions("ES", date(2026, 12, 25)) == []
    assert CAL.future_sessions("ES", date(2026, 10, 24)) == []
    (s,) = CAL.future_sessions("ES", date(2026, 10, 26))  # Monday: opens Sunday 18:00
    assert s.open == datetime(2026, 10, 25, 18, 0, tzinfo=ET)


def test_official_futures_schedule_overrides_the_rules():
    d = date(2026, 10, 21)
    events = [("pre_open", 0), ("open", 1_000), ("close", 2_000)]
    periods = sessions_from_events([(e, ms * 1000) for e, ms in events])
    cal = MarketCalendar(Overrides(futures={"NQ": {d: periods}}))
    (s,) = cal.future_sessions("NQ", d)
    assert (s.close - s.open).total_seconds() == 1000


def test_fx_holiday_eves_and_good_friday():
    eve = CAL.fx_session(date(2026, 12, 24))
    assert [(s.open.strftime("%d %H:%M"), s.close.strftime("%d %H:%M")) for s in eve] == [
        ("23 17:00", "24 13:06"), ("24 17:00", "24 17:01")]  # the feed's day-end marker print
    (xmas,) = CAL.fx_session(date(2026, 12, 25))
    assert xmas.open == datetime(2026, 12, 25, 16, 0, tzinfo=ET)
    assert CAL.fx_session(date(2027, 3, 26)) == []  # Good Friday without a jobs report
    assert CAL.fx_session(date(2026, 4, 3))  # with one: trades


def test_open_minutes_skip_weekends_and_holidays():
    start = datetime(2026, 11, 25, 19, 0, tzinfo=ET)
    mins = CAL.open_minutes("stock", start, start + timedelta(days=3))
    assert mins[0] == datetime(2026, 11, 25, 19, 0, tzinfo=ET)
    days = sorted({m.date() for m in mins})
    assert days == [date(2026, 11, 25), date(2026, 11, 27)]  # Thanksgiving skipped
    assert max(m for m in mins if m.date() == date(2026, 11, 27)).hour == 16  # 17:00 close on the early close
