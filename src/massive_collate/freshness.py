"""When should new data be available? Market calendars and each plan's publication delay.

`expected()` says, for an asset class on a plan at a moment, the newest bar time the source
should already have published and when the next new data is due. `assess()` compares that
with what is stored. Nothing here calls the API; `collector.scan` uses it to decide which
symbols are worth a (cheap) check.

Publication delays (observed on Massive, Oct 2026; adjust PLAN_DELAYS if the plan changes):
- free plan, stocks and indices: end of day. A session's minute bars appear overnight; they
  were present by 03:27 ET the next morning and absent at 21:23 ET the same evening.
- free plan, currencies: through the end of the previous UTC day, shortly after 00:00 UTC.
- futures (Basic): 8 hours delayed.
- paid stock plans: 15 minutes delayed (Starter, Developer) or real time (Advanced).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Tier
from .sessions import MarketCalendar

ET = ZoneInfo("America/New_York")

# Session-based publication: a session's data appears this long after midnight ET following it.
EOD_READY_AFTER_MIDNIGHT = timedelta(hours=4)
# The source may lag its stated delay a little, and thin symbols skip minutes without trades.
GRACE = timedelta(minutes=30)


@dataclass(frozen=True)
class Delay:
    kind: str  # "eod" (session published overnight) | "utc_day" (previous UTC day) | "delay"
    lag: timedelta = timedelta(0)

    def describe(self) -> str:
        if self.kind == "eod":
            return "end of day (published overnight)"
        if self.kind == "utc_day":
            return "previous UTC day (after 00:00 UTC)"
        minutes = int(self.lag.total_seconds() // 60)
        return "real time" if minutes == 0 else f"{minutes // 60} h delayed" if minutes % 60 == 0 else f"{minutes} min delayed"


PLAN_DELAYS: dict[str, dict[str, Delay]] = {
    "free": {"stock": Delay("eod"), "index": Delay("eod"), "fx": Delay("utc_day"),
             "future": Delay("delay", timedelta(hours=8))},
    "starter": {"stock": Delay("delay", timedelta(minutes=15)), "index": Delay("delay", timedelta(minutes=15)),
                "fx": Delay("delay", timedelta(minutes=15)), "future": Delay("delay", timedelta(hours=8))},
    "developer": {"stock": Delay("delay", timedelta(minutes=15)), "index": Delay("delay", timedelta(minutes=15)),
                  "fx": Delay("delay", timedelta(minutes=15)), "future": Delay("delay", timedelta(hours=8))},
    "advanced": {"stock": Delay("delay"), "index": Delay("delay"), "fx": Delay("delay"),
                 "future": Delay("delay", timedelta(hours=8))},
}


def plan_delay(tier: Tier, asset: str) -> Delay:
    return PLAN_DELAYS.get(tier.name, PLAN_DELAYS["free"]).get(asset, Delay("eod"))


# -- calendar (one source of truth: sessions.MarketCalendar) ---------------------------

CALENDAR = MarketCalendar()


def is_trading_day(d: date) -> bool:
    return CALENDAR.is_trading_day(d)


def session_hours(asset: str, d: date) -> tuple[datetime, datetime] | None:
    """(first bar start, end) of the US session for stocks (extended hours) or indices."""
    s = CALENDAR.sessions(asset, d, d)
    return (s[0].open, s[0].close) if s else None


def previous_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_session(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def _open(asset: str, t: datetime) -> bool:
    return CALENDAR.is_open(asset, t)


def last_open_minute(asset: str, before: datetime, limit_days: int = 10) -> datetime | None:
    """Start of the newest one-minute bar that ends at or before `before` while the market is open."""
    t = before.astimezone(ET)
    sessions = CALENDAR.sessions(asset, (t - timedelta(days=limit_days)).date(), t.date() + timedelta(days=1))
    for s in reversed(sessions):
        if s.open < t:
            end = min(s.close, t)
            return (end - timedelta(minutes=1)).replace(second=0, microsecond=0)
    return None


def next_open_minute(asset: str, after: datetime, limit_days: int = 10) -> datetime | None:
    t = after.astimezone(ET)
    for s in CALENDAR.sessions(asset, t.date() - timedelta(days=1), t.date() + timedelta(days=limit_days)):
        if s.close > t:
            return max(s.open, t).replace(second=0, microsecond=0)
    return None


# -- expectations ----------------------------------------------------------------------

@dataclass(frozen=True)
class Expectation:
    latest: datetime | None  # newest bar start the source should have published (ET)
    next_due: datetime | None  # when the next new data should appear
    rule: str  # the plan rule applied, for display


def _eod_ready(session: date) -> datetime:
    return datetime.combine(session + timedelta(days=1), time(0), ET) + EOD_READY_AFTER_MIDNIGHT


def expected(asset: str, tier: Tier, now: datetime) -> Expectation:
    now = now.astimezone(ET)
    delay = plan_delay(tier, asset)
    if delay.kind == "eod" and asset in ("stock", "index"):
        s = now.date() if is_trading_day(now.date()) else previous_session(now.date())
        while _eod_ready(s) > now:
            s = previous_session(s)
        latest = session_hours(asset, s)[1] - timedelta(minutes=1)
        return Expectation(latest, _eod_ready(next_session(s)), delay.describe())
    if delay.kind == "utc_day":
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        latest = last_open_minute(asset, midnight)
        nxt = midnight + timedelta(days=1)
        while next_open_minute(asset, nxt - timedelta(days=1)) is None or \
                next_open_minute(asset, nxt - timedelta(days=1)) >= nxt:  # a UTC day with no trading adds nothing
            nxt += timedelta(days=1)
        return Expectation(latest, nxt.astimezone(ET), delay.describe())
    cutoff = now - delay.lag
    latest = last_open_minute(asset, cutoff)
    if _open(asset, cutoff):
        nxt = now + timedelta(minutes=1)
    else:
        opens = next_open_minute(asset, cutoff)
        nxt = opens + delay.lag + timedelta(minutes=1) if opens else None
    return Expectation(latest, nxt, delay.describe())


def assess(asset: str, tier: Tier, last_bar: datetime | None, now: datetime) -> dict:
    """Compare the newest stored bar with what should exist.

    state: "empty" (nothing stored), "behind" (newer data should be available), "current".
    Session-based plans compare whole sessions, so a thinly traded stock whose last trade
    was at 18:40 is not reported behind for missing 19:59.
    """
    exp = expected(asset, tier, now)
    out = {"expectedThrough": exp.latest, "nextDue": exp.next_due, "rule": exp.rule, "behindBy": None}
    if last_bar is None:
        return out | {"state": "empty"}
    if exp.latest is None:
        return out | {"state": "current"}
    last_bar = last_bar.astimezone(ET)
    if plan_delay(tier, asset).kind == "eod" and asset in ("stock", "index"):
        behind = last_bar.date() < exp.latest.date()
    else:
        behind = last_bar < exp.latest - GRACE
    if behind:
        out["behindBy"] = exp.latest - last_bar
    return out | {"state": "behind" if behind else "current"}
