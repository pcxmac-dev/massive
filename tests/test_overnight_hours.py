"""Blue Ocean's feed keeps its own hours (freshness.FEED_HOURS): its nights, 20:00-04:00 ET. Judged
by the stock day, SPCX opened in the chart at 23:00 read up to date on every feed: the overnight
feed was asked only up to 20:00 (nothing there: it trades nights only), and a night's bars came the
next morning."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from massive_collate import collector, freshness
from massive_collate.config import load_settings
from massive_collate.coverage import has_open_time, ms, plan
from massive_collate.sessions import ET, MarketCalendar
from massive_collate.sources import feed_profiles

KEYS = {"MASSIVE_API_KEY": "k", "ALPACA_API_KEY_ID": "i", "ALPACA_API_SECRET_KEY": "s"}
TUE_2300 = datetime(2026, 10, 27, 23, 0, tzinfo=ET)


def _tier():
    return load_settings(env=KEYS, env_file=None).for_source("boats").tier


def test_the_overnight_calendar_is_blue_oceans_nights_alone():
    nights = MarketCalendar().sessions("overnight", date(2026, 10, 24), date(2026, 10, 28))  # Sat to Wed
    assert [(s.open, s.close) for s in nights] == [
        (datetime(2026, 10, 25, 20, 0, tzinfo=ET), datetime(2026, 10, 26, 4, 0, tzinfo=ET)),  # Sunday night
        (datetime(2026, 10, 26, 20, 0, tzinfo=ET), datetime(2026, 10, 27, 4, 0, tzinfo=ET)),
        (datetime(2026, 10, 27, 20, 0, tzinfo=ET), datetime(2026, 10, 28, 4, 0, tzinfo=ET))]
    assert freshness.hours("boats", "stock") == "overnight" and freshness.hours("alpaca", "stock") == "stock"
    tonight = (ms(datetime(2026, 10, 27, 20, 0, tzinfo=ET)), ms(datetime(2026, 10, 27, 22, 44, tzinfo=ET)))
    assert has_open_time("overnight", *tonight) and not has_open_time("stock", *tonight)  # asked, not passed over


def test_the_overnight_feed_has_new_data_through_the_night():
    tier = _tier()
    e = freshness.expected("overnight", tier, TUE_2300)
    assert e.latest == datetime(2026, 10, 27, 22, 43, tzinfo=ET)  # 16 minutes back: Alpaca Basic's delay
    assert freshness.expected("stock", tier, TUE_2300).latest == datetime(2026, 10, 27, 19, 59, tzinfo=ET)
    synced = TUE_2300 - timedelta(minutes=13)  # its last fetch, at 22:47
    assert collector._due("overnight", tier, synced, TUE_2300, 0.0)
    assert not collector._due("stock", tier, synced, TUE_2300, 0.0)  # how it read: nothing until 04:17


def test_an_opened_stock_needs_the_overnight_feed_at_night_and_its_window_reaches_the_night(store):
    settings = load_settings(env=KEYS, env_file=None)
    tier = _tier()
    store.watch("boats", ["SPCX"], "minute", exchange="XNAS")
    floor = collector.history_floor(tier, TUE_2300.date(), "stock")
    store.set_backfilled("boats", "SPCX", floor)
    store.set_synced("boats", "SPCX", datetime(2026, 10, 27, 22, 47, tzinfo=ET))
    assert collector.needs_fetch(store, "SPCX", {"boats": tier}, TUE_2300) == ["boats"]
    profiles = feed_profiles(settings)
    gaps = plan(store, profiles["boats"], profiles, "SPCX", "stock", "minute", floor, TUE_2300)
    assert gaps[-1][1] == ms(datetime(2026, 10, 27, 22, 44, tzinfo=ET))  # up to the newest published minute
    morning = datetime.combine(TUE_2300.date() + timedelta(days=1), time(5, 0), ET)  # Wednesday 05:00: night over
    store.set_synced("boats", "SPCX", datetime(2026, 10, 28, 4, 20, tzinfo=ET))
    assert collector.needs_fetch(store, "SPCX", {"boats": tier}, morning) == []  # nothing until 20:16
