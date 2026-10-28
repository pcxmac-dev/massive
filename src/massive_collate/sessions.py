"""Trading calendars: when each market is open, for any date, past or future.

One calendar serves the chart's forward time axis, the collector's "should new data exist?"
checks and the tests. A trading *session* is one contiguous open period with a session date:

- US stocks: 04:00-20:00 ET (extended hours) on NYSE trading days; 17:00 on early-close days.
  The regular session (09:30-16:00, 13:00 on early-close days) is what the chart shows unless
  extended hours are on. Overnight, Blue Ocean ATS trades them 20:00-04:00 ET, Sunday to
  Thursday nights before a trading day: with all four sessions shown (the chart's "all"), a
  trading day's session runs from 20:00 the evening before. The collector's checks keep to
  04:00-20:00 (the consolidated tape); the overnight feed fills its own nights.
- US indices: 09:30-16:00 ET (13:00 on early-close days).
- Spot FX: each weekday's session runs from 17:00 ET the evening before to 17:00 ET
  (so the week is Sunday 17:00 to Friday 17:00).
- CME Globex futures: 18:00 ET the evening before to 17:00 ET on the session (settlement)
  date, Monday to Friday, with exchange-holiday halts that depend on the product group.

Exact rules generate any year. Official schedules from the data source override them where
known: Massive's upcoming market holidays (NYSE/Nasdaq closures and early closes, including
unscheduled ones the rules cannot know) and its CME futures schedules (per product and date).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# -- holiday rules -------------------------------------------------------------------


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon) of a month; n=-1 for the last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """Saturday holidays are observed on Friday, Sunday holidays on Monday."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> dict[date, str]:
    """NYSE full-day closures under the exchange's standing rules (Rule 7.2)."""
    days = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _easter(year) - timedelta(days=2): "Good Friday",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed(date(year, 12, 25)): "Christmas Day",
        _observed(date(year, 7, 4)): "Independence Day",
    }
    if year >= 2022:
        days[_observed(date(year, 6, 19))] = "Juneteenth"
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year's Day is not observed on the Friday before
        days[_observed(new_year)] = "New Year's Day"
    return days


# Unscheduled NYSE closures (national days of mourning, emergencies). Future ones come from the
# source's upcoming-holidays list; these keep past dates right.
NYSE_SPECIAL_CLOSURES = {
    date(2001, 9, 11): "September 11", date(2001, 9, 12): "September 11", date(2001, 9, 13): "September 11",
    date(2001, 9, 14): "September 11", date(2004, 6, 11): "Ronald Reagan day of mourning",
    date(2007, 1, 2): "Gerald Ford day of mourning", date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy", date(2018, 12, 5): "George H. W. Bush day of mourning",
    date(2025, 1, 9): "Jimmy Carter day of mourning",
}


def is_nyse_holiday(d: date) -> bool:
    return d in nyse_holidays(d.year) or d in NYSE_SPECIAL_CLOSURES


def jobs_report_good_friday(d: date) -> bool:
    """Good Friday on the first Friday of a month, when the US jobs report is released: CME
    equity futures and FX trade a short session (seen 2010, 2015, 2021, 2023, 2026)."""
    return nyse_holidays(d.year).get(d) == "Good Friday" and d.day <= 7


# Past early closes that broke the pattern (checked against exchange_calendars' XNYS, 2000-2027).
NYSE_EARLY_CLOSE_EXCEPTIONS = {date(2002, 7, 3): False, date(2002, 7, 5): True, date(2003, 12, 26): True}


def nyse_early_close(d: date) -> bool:
    """13:00 ET close: the day after Thanksgiving, Christmas Eve, and July 3 (when trading days)."""
    if d.weekday() >= 5 or is_nyse_holiday(d):
        return False
    if d in NYSE_EARLY_CLOSE_EXCEPTIONS:
        return NYSE_EARLY_CLOSE_EXCEPTIONS[d]
    return (d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1)
            or (d.month, d.day) == (12, 24)
            or ((d.month, d.day) == (7, 3) and date(d.year, 7, 4).weekday() < 5))


# -- CME Globex rules ------------------------------------------------------------------

EQUITY_FUTURES = {"ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K", "EMD", "NKD"}
ENERGY_FUTURES = {"CL", "NG", "RB", "HO", "BZ", "MCL", "QM", "QG"}
# Holiday halts in Central Time (CME publishes its schedule in CT): (equity, energy/other).
_CME_HOLIDAY_HALT = (time(12, 0), time(13, 30))
_CME_EARLY_CLOSE = (time(12, 15), time(13, 45))
_CME_FULL_CLOSURES = {"Good Friday", "Christmas Day", "New Year's Day"}


def product_group(product: str | None) -> int:
    """0 = equity index (and default), 1 = energy and other commodities."""
    return 1 if product and product.upper() in ENERGY_FUTURES else 0


# -- sessions ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Session:
    day: date  # trading / settlement date
    open: datetime  # aware
    close: datetime  # aware, exclusive


def _et(d: date, t: time) -> datetime:
    return datetime.combine(d, t, ET)


@dataclass
class Overrides:
    """Official schedules from the data source, preferred over the rules where present.

    `us`: date -> ("closed", None, None) or ("early-close", open_utc, close_utc) for NYSE/Nasdaq.
    `futures`: product -> session date -> [(open, close)] (aware datetimes) or [] when closed.
    """
    us: dict[date, tuple[str, datetime | None, datetime | None]] = field(default_factory=dict)
    futures: dict[str, dict[date, list[tuple[datetime, datetime]]]] = field(default_factory=dict)


class MarketCalendar:
    def __init__(self, overrides: Overrides | None = None):
        self.overrides = overrides or Overrides()

    # -- US equities -----------------------------------------------------------------
    def is_trading_day(self, d: date) -> bool:
        o = self.overrides.us.get(d)
        if o is not None:
            return o[0] != "closed"
        return d.weekday() < 5 and not is_nyse_holiday(d)

    def regular_hours(self, d: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(d):
            return None
        o = self.overrides.us.get(d)
        if o is not None and o[0] == "early-close" and o[1] and o[2]:
            return o[1].astimezone(ET), o[2].astimezone(ET)
        return _et(d, time(9, 30)), _et(d, time(13, 0) if nyse_early_close(d) else time(16, 0))

    def stock_session(self, d: date, extended: bool = True, overnight: bool = False) -> Session | None:
        reg = self.regular_hours(d)
        if reg is None:
            return None
        if not extended:
            return Session(d, *reg)
        # Pre-market from 04:00; after-hours run four hours past the close (20:00, or 17:00).
        # With `overnight`, from Blue Ocean's open the evening before.
        return Session(d, self.overnight_open(d) if overnight else _et(d, time(4, 0)), reg[1] + timedelta(hours=4))

    def overnight_open(self, d: date) -> datetime | None:
        """When Blue Ocean's overnight session before trading day `d` opens: 20:00 ET the evening
        before (Sunday to Thursday nights); None when `d` is not a trading day. It ends at 04:00
        on `d`, where pre-market begins."""
        return _et(d - timedelta(days=1), time(20, 0)) if self.is_trading_day(d) else None

    # -- futures ---------------------------------------------------------------------
    def future_sessions(self, product: str | None, d: date) -> list[Session]:
        official = self.overrides.futures.get((product or "").upper(), {}).get(d)
        if official is not None:
            return [Session(d, a, b) for a, b in official]
        if d.weekday() >= 5:
            return []
        group = product_group(product)
        holiday = nyse_holidays(d.year).get(d)
        start = _et(d - timedelta(days=1), time(18, 0))
        if holiday == "Good Friday" and group == 0 and jobs_report_good_friday(d):
            # Short session; with no settlement that day its trades count toward the next trade date.
            return [Session(self._next_cme_trade_date(product, d), start,
                            datetime.combine(d, time(8, 15), CT).astimezone(ET))]
        if holiday in _CME_FULL_CLOSURES:
            return []
        end = _et(d, time(17, 0))
        if holiday:
            # Other US holidays: open overnight, halted from midday, no settlement that day: the
            # trading belongs to the next trade date's session (and its official daily bar).
            end = datetime.combine(d, _CME_HOLIDAY_HALT[group], CT).astimezone(ET)
            return [Session(self._next_cme_trade_date(product, d), start, end)]
        elif nyse_early_close(d):
            end = datetime.combine(d, _CME_EARLY_CLOSE[group], CT).astimezone(ET)
        # After a holiday halt or full closure the next session opens as usual at 18:00 ET.
        return [Session(d, start, end)]

    def _next_cme_trade_date(self, product: str | None, d: date) -> date:
        """The next date with a normal (settling) session after a holiday."""
        n = d + timedelta(days=1)
        while n.weekday() >= 5 or nyse_holidays(n.year).get(n) is not None:
            n += timedelta(days=1)
        return n

    # -- fx --------------------------------------------------------------------------
    @staticmethod
    def fx_session(d: date) -> list[Session]:
        """Spot FX as the data source quotes it (observed on Massive, 2025-2026): Sunday 17:00
        to Friday 17:00 ET; on Christmas Eve and New Year's Eve quotes stop just after 13:00 and
        resume at 16:00 on the holiday; before a Good Friday without a jobs report they stop on
        the Thursday at 15:20 and resume on Sunday. On those early-stop days the feed still
        prints its day-end marker at 17:00."""
        if d.weekday() >= 5:
            return []
        start, end = _et(d - timedelta(days=1), time(17, 0)), _et(d, time(17, 0))
        if (d.month, d.day) in ((12, 25), (1, 1)):
            start = _et(d, time(16, 0))
        early = None
        if (d.month, d.day) in ((12, 24), (12, 31)):
            early = _et(d, time(13, 6))
        good_friday = nyse_holidays(d.year).get(d) == "Good Friday"
        if good_friday and not jobs_report_good_friday(d):
            return []
        tomorrow = d + timedelta(days=1)
        if nyse_holidays(tomorrow.year).get(tomorrow) == "Good Friday" and not jobs_report_good_friday(tomorrow):
            early = _et(d, time(15, 20))
        if early is None:
            return [Session(d, start, end)]
        return [Session(d, start, early), Session(d, end, end + timedelta(minutes=1))]

    # -- crypto ------------------------------------------------------------------------
    @staticmethod
    def crypto_session(d: date) -> list[Session]:
        """Crypto trades around the clock, every day of the year. The source's day bars are UTC
        days (stamped 00:00 UTC), so a session is one UTC day."""
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        return [Session(d, start.astimezone(ET), (start + timedelta(days=1)).astimezone(ET))]

    # -- any asset ---------------------------------------------------------------------
    def sessions(self, asset: str, start: date, end: date, product: str | None = None,
                 extended: bool = True, overnight: bool = False) -> list[Session]:
        """Sessions with session dates in [start, end], in order (US stocks: with `overnight`,
        each from Blue Ocean's open the evening before). `asset` "overnight": Blue Ocean's nights
        alone, 20:00 the evening before to 04:00 (its feed's own hours: freshness.FEED_HOURS)."""
        out: list[Session] = []
        d = start
        while d <= end:
            if asset == "future":
                out.extend(self.future_sessions(product, d))
            elif asset == "fx":
                out.extend(self.fx_session(d))
            elif asset == "crypto":
                out.extend(self.crypto_session(d))
            elif asset == "overnight":
                if (opens := self.overnight_open(d)) is not None:
                    out.append(Session(d, opens, _et(d, time(4, 0))))
            else:
                s = self.stock_session(d, extended=extended, overnight=overnight and extended) if asset == "stock" \
                    else self._index_session(d)
                if s:
                    out.append(s)
            d += timedelta(days=1)
        return out

    def _index_session(self, d: date) -> Session | None:
        reg = self.regular_hours(d)
        # The official closing value is published as a bar stamped at the close (16:00).
        return Session(d, reg[0], reg[1] + timedelta(minutes=1)) if reg else None

    def is_open(self, asset: str, t: datetime, product: str | None = None) -> bool:
        t = t.astimezone(ET)
        for s in self.sessions(asset, t.date() - timedelta(days=1), t.date() + timedelta(days=1), product):
            if s.open <= t < s.close:
                return True
        return False

    def open_minutes(self, asset: str, start: datetime, end: datetime, product: str | None = None,
                     step_seconds: int = 60, extended: bool = True, overnight: bool = False) -> pd.DatetimeIndex:
        """Start of every `step_seconds` interval in [start, end) during which the market is open,
        as US/Eastern timestamps (what a feed with a bar for every open interval would hold)."""
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        step = step_seconds * 1_000_000_000
        chunks = []
        for s in self.sessions(asset, (start - timedelta(days=2)).astimezone(ET).date(),
                               (end + timedelta(days=2)).astimezone(ET).date(), product, extended=extended,
                               overnight=overnight):
            a, b = max(s.open, start), min(s.close, end)
            if a >= b:
                continue
            a_ns = -(-int(a.timestamp() * 1e9) // step) * step  # first whole step at or after a
            b_ns = int(b.timestamp() * 1e9)
            if a_ns < b_ns:
                chunks.append(np.arange(a_ns, b_ns, step, dtype=np.int64))
        values = np.unique(np.concatenate(chunks)) if chunks else np.array([], dtype=np.int64)
        return pd.DatetimeIndex(values.astype("datetime64[ns]")).tz_localize("UTC").tz_convert(ET)


# -- official schedules from the store ----------------------------------------------------

def overrides_from_store(store, source: str = "massive") -> Overrides:
    """Build overrides from the market_holidays and futures_schedule tables (see storage)."""
    o = Overrides()
    for day, status, open_ms, close_ms in store.market_holidays(source):
        o.us[day] = (status, _ms(open_ms), _ms(close_ms))
    for product, day, events in store.futures_schedules(source):
        o.futures.setdefault(product, {})[day] = sessions_from_events(events)
    return o


def _ms(v: int | None) -> datetime | None:
    return datetime.fromtimestamp(v / 1000, timezone.utc) if v is not None else None


def sessions_from_events(events: list[tuple[str, int]]) -> list[tuple[datetime, datetime]]:
    """Open periods from a session date's schedule events [(event, ms)]: trading runs from each
    'open' to the next event that is not an open (close, halt, pause, pre_open...)."""
    out, start = [], None
    for event, ms in sorted(events, key=lambda e: e[1]):
        t = datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(ET)
        if event == "open":
            start = start or t
        elif start is not None:
            if t > start:
                out.append((start, t))
            start = None
    return out
